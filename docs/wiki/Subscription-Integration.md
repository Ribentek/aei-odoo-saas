# Subscription Integration — Recurring SaaS Billing

The `odoo_k8s_saas_subscription` bridge module connects **subscription_oca** (OCA) with the `odoo_k8s_saas` addon. When installed, SaaS products generate recurring monthly subscriptions, and subscription lifecycle events drive instance provisioning and suspension.

## End-to-End Flow

```
Salesperson creates Sale Order
  │  (product with subscribable=True + subscription template)
  │
  ▼
Confirm SO → subscription_oca creates sale.subscription
  │  Stage: "In Progress", recurring_next_date set
  │
  ▼
Bridge module creates saas.instance linked to subscription
  │  instance.subscription_id = subscription
  │
  ▼
Subscription cron generates recurring invoice
  │  (every month per recurring_next_date)
  │
  ▼
Customer pays invoice → payment_state → "paid"
  │
  ▼
Existing _compute_payment_state() trigger fires
  │  → _saas_check_and_provision() → action_provision()
  │  (belt-and-suspenders: subscription stage hook also provisions)
  │
  ▼
Tenant Odoo pod starts → cron checks status → "ready"
  │  → action_send_credentials_email() → customer notified
  │
  ▼
Customer accesses https://<tenant_id>.aeisoftware.com
```

### Suspension Flow (Non-Payment)

```
Subscription overdue (recurring_next_date < today)
  │
  ▼
Daily cron: _cron_suspend_overdue()
  │  Finds ready instances on overdue subscriptions
  │
  ▼
instance.action_stop() → POST /api/v1/instances/{id}/stop
  │  State: ready → suspended (scales K8s deployment to 0)
  │
  ▼
Customer pays overdue invoice
  │
  ▼
_saas_check_and_provision() detects suspended instance
  │  → instance.action_resume() → scales back to 1
  │  State: suspended → ready
```

### Closure Flow

```
Subscription stage → "Closed"  (non-payment, cancellation, etc.)
  │
  ▼
Bridge module write() override detects stage change
  │
  ▼
instance.action_delete() → DELETE /api/v1/instances/{id}
  │  (handles draft, provisioning, ready, AND suspended instances)
  │
  ▼
Tenant namespace + resources removed
```

### Re-provision Flow

If a `saas.instance` is manually deleted (or fails) while the subscription is still **In Progress**, admins can re-create the instance directly from the subscription form:

```
Subscription form → "Re-provision Instance" button
  │  (visible only when stage = In Progress AND no active instance)
  │
  ▼
Bridge creates new saas.instance linked to subscription
  │  tenant_id derived from subscription + partner
  │  sale_order_id copied from subscription's origin SO
  │
  ▼
action_provision() → Portal API → tenant pod starts
```

The subscription form also shows a **stat button** with the count of linked SaaS instances.

## Architecture

### Why a Bridge Module?

The bridge module (`odoo_k8s_saas_subscription`) keeps the base `odoo_k8s_saas` addon independent of `subscription_oca`. If `subscription_oca` is not installed, the base addon still works for one-time provisioning via the payment trigger.

| Module | Purpose | Depends |
|:---|:---|:---|
| `odoo_k8s_saas` | Base SaaS management, one-time provisioning | `base`, `web`, `mail`, `sale`, `account` |
| `subscription_oca` | OCA subscription lifecycle (Vendorized in `external_addons/`) | `sale` |
| `odoo_k8s_saas_subscription` | **Bridge** — connects the two | `odoo_k8s_saas`, `subscription_oca`, `portal`, `website_sale` |

The bridge module uses `auto_install: True` — it installs automatically when both dependencies are present.

## Lifecycle Hooks

The bridge module overrides `sale.subscription.write()` to detect `stage_id` changes:

| Stage Transition | SaaS Action | Filter |
|:---|:---|:---|
| → **In Progress** | `action_provision()` | Only instances in `draft` or `error` state |
| → **Closed** | `action_delete()` | Instances in `draft`, `provisioning`, `ready`, or `suspended` state |

```python
# Simplified — see sale_subscription.py for full code
def write(self, vals):
    old_stages = {rec.id: rec.stage_id.id for rec in self}
    res = super().write(vals)
    if "stage_id" not in vals:
        return res
    for rec in self:
        # Detect transition, find linked instances, trigger action
```

Errors are caught and logged per-instance — a failed provision/delete does not block other subscriptions.

### Idempotency Guards

Instance creation includes two idempotency guards to prevent duplicates:

1. **By `sale_order_line_id`** — If an instance already exists for the same order line, it is reused
2. **By `tenant_id`** — If an instance already exists with the same tenant_id, it is reused

Both guards link the subscription if not already linked.

## Per-User Billing

The bridge module tracks active users across tenant instances and computes extra-user charges.

### Subscription Template Fields

| Field | Type | Description |
|:---|:---|:---|
| `is_saas_plan` | `Boolean` | Marks this template as a SaaS plan (required for auto-provisioning) |
| `included_users` | `Integer` | Users included in the plan before extra charges |
| `price_per_extra_user` | `Float` | Monthly charge per extra user |

### Default Template Configuration

| Template | included_users | price_per_extra_user |
|:---|:---|:---|
| SaaS Starter (Monthly) | 3 | 5.00 Bs. |
| SaaS Pro (Monthly) | 10 | 3.00 Bs. |
| SaaS Enterprise (Monthly) | 50 | 2.00 Bs. |

### Fields on `sale.subscription`

| Field | Type | Description |
|:---|:---|:---|
| `saas_instance_ids` | `One2many` | Inverse of `saas.instance.subscription_id` — all linked instances |
| `current_user_count` | `Integer` (computed) | Sum of `user_count` across non-deleted instances |
| `extra_users` | `Integer` (computed) | `max(0, current_user_count - included_users)` |
| `extra_users_amount` | `Float` (computed) | `extra_users × price_per_extra_user` |

> [!IMPORTANT]
> The computed fields use `@api.depends("saas_instance_ids.user_count", "saas_instance_ids.state", ...)` so that Odoo's ORM automatically recomputes them when the cron updates `saas.instance.user_count`. Using `search()` instead of the One2many field would cause Odoo to cache stale values.

### Extra User Product

A service product `Extra User (Monthly)` (`data/products.xml`, XML ID: `product_extra_user`) is created with:
- `sale_ok = False` — not sold via e-commerce
- `list_price = 0.0` — price is set dynamically from the template's `price_per_extra_user`
- Category: `Odoo-SaaS`

This product is added as a subscription line by the daily cron `_cron_update_extra_user_line`.

### Auto-Billing Flow

```
_cron_sync_user_count() runs hourly
  │  Calls GET /api/v1/instances/{tenant_id} on portal API
  │  Writes saas.instance.user_count
  │  → @api.depends triggers recompute of subscription computed fields
  │
  ▼
_cron_update_extra_user_line() runs daily
  │  For each active SaaS subscription:
  │  1. Reads extra_users (auto-recomputed from latest user_count)
  │  2. If extra_users > 0 → creates/updates "Extra User" line
  │     with qty = extra_users, price = price_per_extra_user
  │  3. If extra_users ≤ 0 → removes existing line
  │
  ▼
OCA invoicing cron (monthly, recurring_next_date)
  │  Generates invoice including the "Extra User" line
  │
  ▼
Invoice sent to customer
```

## Subscription Templates

Three pre-configured templates in `data/subscription_templates.xml`:

| Template | XML ID | Interval | Rule Type |
|:---|:---|:---|:---|
| SaaS Starter (Monthly) | `subscription_template_saas_starter` | 1 | months |
| SaaS Pro (Monthly) | `subscription_template_saas_pro` | 1 | months |
| SaaS Enterprise (Monthly) | `subscription_template_saas_enterprise` | 1 | months |

### Configuring a SaaS Product for Subscriptions

1. **Sales → Products → New**
2. Set category to `Odoo-SaaS`
3. Check **Subscribable** (`subscribable = True`)
4. Set **Subscription Template** to one of the SaaS templates above
5. Set price and invoicing policy

## Model Extension: `saas.instance`

The bridge adds these fields to `saas.instance`:

| Field | Type | Notes |
|:---|:---|:---|
| `subscription_id` | `Many2one` | `sale.subscription` — the recurring subscription managing billing (inverse: `saas_instance_ids`) |
| `sale_order_line_id` | `Many2one` | `sale.order.line` — specific order line that triggered creation |
| `subscription_stage` | `Char` | Related field showing `subscription_id.stage_id.name` (readonly) |
| `user_count` | `Integer` | Current active users, synced from portal API (tracked). Writing this field triggers recompute of `current_user_count` on the parent subscription via `@api.depends` |
| `max_users` | `Integer` | Max users from template `included_users` (readonly) |

All fields are visible in the instance form view when the bridge module is installed.

## Cron Jobs

The bridge module defines **4 cron jobs** in `data/ir_cron.xml`:

| Cron | Interval | Description |
|:---|:---|:---|
| `SaaS: Suspend Overdue Instances` | Daily | Suspends (`action_stop`) ready instances whose subscription is past-due |
| `SaaS: Sync Closed Subscription Instances` | Hourly | Safety net — deletes any still-active instance on a Closed subscription |
| `SaaS: Sync User Count from Portal` | Hourly | Calls GET for each active instance and updates `user_count` |
| `SaaS: Update Extra User Billing Line` | Daily | Creates/updates/removes "Extra User" subscription lines based on current user counts |

## Customer Portal

The bridge module includes a **customer-facing portal** at `/my/subscriptions` with:

### Subscription List (`/my/subscriptions`)
- Shows all subscriptions with stage badges, next invoice date, and recurring totals
- Searchable via the portal searchbar

### Subscription Detail (`/my/subscriptions/<id>`)
- **Customer and agent info** with avatar images
- **Subscription details**: reference code, plan, start date, next invoice, end date
- **SaaS instance card** showing:
  - Instance URL (clickable when ready)
  - Status badge (Ready ✓ / Provisioning… / Suspended / Error)
  - Plan name
  - "Open Instance" button
- **User Usage section** (when template has `included_users`):
  - Current Users, Included in Plan, Extra Users, Extra Charges
- **Estimated Next Invoice** (when extra users are present):
  - Shows: recurring_total + extra_users_amount = estimated total
  - Only visible when `extra_users_amount > 0`
- **Line items table** with quantities, unit prices, discounts, subtotals
- **Totals section** with recurring total, taxes, and grand total
- **Communication/chatter** thread for messages

The portal controller is in `controllers/` and uses standard Odoo portal patterns with `portal.mixin`.

## File Structure

```
odoo_k8s_saas_subscription/
├── __init__.py
├── __manifest__.py
├── controllers/                          ← Portal controllers for /my/subscriptions
│   └── portal.py
├── data/
│   ├── subscription_templates.xml        ← Starter/Pro/Enterprise templates
│   ├── ir_cron.xml                       ← 4 cron jobs (suspend, cleanup, user sync, extra-user billing)
│   ├── products.xml                      ← Extra User (Monthly) product for billing
│   └── website_checkout_config.xml       ← Website checkout configuration
├── models/
│   ├── __init__.py
│   ├── saas_instance.py                  ← subscription_id, sale_order_line_id, user_count, max_users
│   └── sale_subscription.py              ← Lifecycle hooks, portal mixin, per-user billing, crons
├── security/
│   ├── ir.model.access.csv
│   └── ir_rules.xml                      ← Record-level security rules
├── static/
│   └── src/img/                          ← Portal icons
└── views/
    ├── saas_instance_views.xml           ← Shows subscription on instance form
    ├── sale_subscription_views.xml       ← Stat button + Re-provision button
    ├── subscription_template_views.xml   ← Template config (is_saas_plan, included_users, etc.)
    └── subscription_portal_templates.xml ← Customer-facing portal templates (424 lines)
```

## Interaction with Payment Trigger

The existing one-time payment trigger in `saas_sale.py` still works alongside subscriptions:

1. SO confirmed → `subscription_oca` creates `sale.subscription`
2. Bridge module detects "In Progress" stage → provisions if instance is in `draft`
3. Subscription cron generates monthly invoices
4. When invoice is paid → `_compute_payment_state()` fires → `_saas_check_and_provision()` provisions if not already done
5. Both triggers are **idempotent** — duplicate prevention ensures no double-provisioning
6. If a suspended instance exists for the SO, the payment trigger calls `action_resume()` instead of creating a new instance

> The subscription stage hook and the payment trigger serve as **belt-and-suspenders** — either one alone would work, both together cover edge cases.

## Implementation History

The bridge module was built iteratively in 8 phases:

| Fase | Descripción | Área |
|:-----|:------------|:-----|
| Fase 1 | Core lifecycle hooks: `write()` override for In Progress → provision, Closed → delete | `sale_subscription.py` |
| Fase 2 | OCA subscription templates (Starter/Pro/Enterprise) + `is_saas_plan` template field | `data/subscription_templates.xml` |
| Fase 3 | Per-user billing: `extra_users` computed fields + `_cron_update_extra_user_line` | `sale_subscription.py` |
| Fase 4 | Idempotency guards (by `sale_order_line_id` and `tenant_id`) | `sale_subscription.py` |
| Fase 5 | Customer portal `/my/subscriptions` with instance status card + billing details | `controllers/portal.py` |
| Fase 6 | Re-provision button on subscription form + overdue suspension cron | `sale_subscription.py` |
| Fase 7 | User count sync cron (`_cron_sync_user_count`) + `user_count` field on `saas.instance` | `models/saas_instance.py` |
| Fase 8 | Self-service backup download via portal proxy (`/my/subscriptions/<id>/backup`) | `controllers/portal.py` |

The bridge module is loaded alongside the base addon by the init container in `06-odoo-admin.yaml`:

```bash
# Clone main SaaS repo (base addon + bridge module + payment module + OCA)
git clone --depth=1 -b main \
  https://github.com/AEI-Software/aei-odoo-saas.git /tmp/repo
cp -r /tmp/repo/odoo_k8s_saas /mnt/extra-addons/
cp -r /tmp/repo/odoo_k8s_saas_subscription /mnt/extra-addons/
cp -r /tmp/repo/payment_qr_mercantil /mnt/extra-addons/
cp -r /tmp/repo/external_addons/subscription_oca /mnt/extra-addons/

# Clone MUK modules (v19.0)
git clone --depth=1 -b 19.0 https://github.com/muk-it/muk_base.git /tmp/muk-base
for d in /tmp/muk-base/muk_*/; do
  cp -r "$d" /mnt/extra-addons/
done
```

After pod restart:
1. Log in to **https://admin.aeisoftware.com** (developer mode)
2. Install `subscription_oca` first
3. `odoo_k8s_saas_subscription` should auto-install (if not, install manually)

## Testing the Flow

1. Create a product: category `Odoo-SaaS`, `subscribable=True`, template = SaaS Starter
2. Create a Sale Order with that product → Confirm
3. Verify: `sale.subscription` created (Subscriptions → All Subscriptions)
4. Verify: `saas.instance` linked to the subscription (stat button shows count)
5. Wait for subscription cron (or trigger via Settings → Technical → Scheduled Actions → `SaaS: ...`)
6. Register payment on the generated invoice
7. Check: instance transitioned to `provisioning` → `ready`
8. To test suspension: close the subscription → instance should be deleted
9. To test overdue suspension: advance `recurring_next_date` to the past → wait for cron → instance suspended
10. To test re-provision: delete the instance, reopen subscription → click **Re-provision Instance** button

## Log Messages

```
Subscription SUB/2026/001 → In Progress: provisioning instance acme-corp-001
Subscription SUB/2026/001 → Closed: deleting instance acme-corp-001 (state=ready)
_cron_suspend_overdue: checking 2 overdue subscriptions
Suspending instance acme-corp-001 (subscription SUB/2026/001 overdue since 2026-03-01)
_cron_sync_user_count: acme-corp-001 → 5 users
_cron_update_extra_user_line: processing 3 active subscriptions
_cron_update_extra_user_line: created extra-user line for SUB/2026/001 → 2 extra users × 5.00 Bs.
```

Grep for subscription-related messages:

```bash
kubectl -n odoo-admin logs deployment/odoo-admin -f | grep -i "subscription"
```
