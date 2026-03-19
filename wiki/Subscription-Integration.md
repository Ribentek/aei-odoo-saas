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
  │
  ▼
Customer accesses https://<tenant_id>.aeisoftware.com
```

### Suspension Flow

```
Subscription stage → "Closed"  (non-payment, cancellation, etc.)
  │
  ▼
Bridge module write() override detects stage change
  │
  ▼
instance.action_delete() → DELETE /api/v1/instances/{id}
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
| `odoo_k8s_saas` | Base SaaS management, one-time provisioning | `base`, `web`, `sale`, `account` |
| `subscription_oca` | OCA subscription lifecycle + recurring invoicing | `sale` |
| `odoo_k8s_saas_subscription` | **Bridge** — connects the two | `odoo_k8s_saas`, `subscription_oca` |

The bridge module uses `auto_install: True` — it installs automatically when both dependencies are present.

## Lifecycle Hooks

The bridge module overrides `sale.subscription.write()` to detect `stage_id` changes:

| Stage Transition | SaaS Action | Filter |
|:---|:---|:---|
| → **In Progress** | `action_provision()` | Only instances in `draft` or `error` state |
| → **Closed** | `action_delete()` | Only instances in `draft`, `provisioning`, or `ready` state |

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

The bridge adds two fields to `saas.instance`:

| Field | Type | Notes |
|:---|:---|:---|
| `subscription_id` | `Many2one` | `sale.subscription` — the recurring subscription managing billing |
| `subscription_stage` | `Char` | Related field showing `subscription_id.stage_id.name` (readonly) |

Both fields are visible in the instance form view when the bridge module is installed.

## File Structure

```
odoo_k8s_saas_subscription/
├── __init__.py
├── __manifest__.py
├── data/
│   └── subscription_templates.xml        ← Starter/Pro/Enterprise templates
├── models/
│   ├── __init__.py
│   ├── saas_instance.py                  ← subscription_id + subscription_stage
│   └── sale_subscription.py              ← Lifecycle hooks + re-provision action
├── security/
│   └── ir.model.access.csv
└── views/
    ├── saas_instance_views.xml           ← Shows subscription on instance form
    └── sale_subscription_views.xml       ← Stat button + Re-provision button
```

## Interaction with Payment Trigger

The existing one-time payment trigger in `saas_sale.py` still works alongside subscriptions:

1. SO confirmed → `subscription_oca` creates `sale.subscription`
2. Bridge module detects "In Progress" stage → provisions if instance is in `draft`
3. Subscription cron generates monthly invoices
4. When invoice is paid → `_compute_payment_state()` fires → `_saas_check_and_provision()` provisions if not already done
5. Both triggers are **idempotent** — duplicate prevention ensures no double-provisioning

> The subscription stage hook and the payment trigger serve as **belt-and-suspenders** — either one alone would work, both together cover edge cases.

## Installation

The bridge module is loaded alongside the base addon by the init container in `06-odoo-admin.yaml`:

```bash
# Clone main SaaS repo (base addon + bridge module)
git clone --depth=1 -b feature/subscription-integration \
  https://github.com/jpvargassoruco/odoo-saas-mvp.git /tmp/repo
cp -r /tmp/repo/odoo_k8s_saas /mnt/extra-addons/
cp -r /tmp/repo/odoo_k8s_saas_subscription /mnt/extra-addons/

# Clone subscription_oca from OCA fork
git clone --depth=1 -b 18.0 \
  https://github.com/jpvargassoruco/odoo18-oca-contract.git /tmp/oca-contract
cp -r /tmp/oca-contract/subscription_oca /mnt/extra-addons/
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
9. To test re-provision: delete the instance, reopen subscription → click **Re-provision Instance** button

## Log Messages

```
Subscription SUB/2026/001 → In Progress: provisioning instance acme-corp-001
Subscription SUB/2026/001 → Closed: deleting instance acme-corp-001
```

Grep for subscription-related messages:

```bash
kubectl -n odoo-admin logs deployment/odoo-admin -f | grep -i "subscription"
```
