# Odoo SaaS Addon — `odoo_k8s_saas`

The `odoo_k8s_saas` addon runs inside the **admin Odoo instance** (`odoo-admin` namespace). It provides a UI for operators to create, monitor, and delete tenant instances by calling the SaaS portal API.

## Module Details

| Field | Value |
|:---|:---|
| Technical Name | `odoo_k8s_saas` |
| Version | `18.0.3.0.0` |
| Category | Technical |
| License | LGPL-3 |
| Depends | `base`, `web`, `mail`, `sale`, `account` |
| Author | AEI Software |

## Installation

The addon is loaded into the admin Odoo pod via an `initContainer` that clones the repository at startup and copies the integrated OCA modules:

```bash
# Clone main SaaS repo
git clone --depth=1 https://github.com/Ribentek/aei-odoo-saas.git /tmp/repo
cp -r /tmp/repo/odoo_k8s_saas /mnt/extra-addons/
cp -r /tmp/repo/odoo_k8s_saas_subscription /mnt/extra-addons/
cp -r /tmp/repo/external_addons/subscription_oca /mnt/extra-addons/
```

The volume `odoo-addons` is `emptyDir` — the addons are re-cloned on pod restart.

To install in the Odoo app:
1. Log in to **https://admin.aeisoftware.com**
2. Activate developer mode (`/web?debug=1`)
3. **Settings → Technical → Modules → Installed Modules** → search "K8s SaaS" → Install

## Model: `saas.instance`

File: `odoo_k8s_saas/models/saas_instance.py`

### Fields

| Field | Type | Notes |
|:---|:---|:---|
| `name` | `Char` | Human-readable display name |
| `tenant_id` | `Char` | URL slug, used as K8s namespace suffix and subdomain |
| `url` | `Char` | Full tenant URL (returned by portal, readonly) |
| `namespace` | `Char` | K8s namespace name (returned by portal, readonly) |
| `state` | `Selection` | `draft / provisioning / ready / suspended / pending_delete / error / deleted` |
| `odoo_version` | `Selection` | `17.0 / 18.0 / 19.0 / custom` (Default: 18.0) |
| `custom_image` | `Char` | Custom Docker Image string if version is custom |
| `plan` | `Selection` | `starter / pro / enterprise` |
| `storage_gi` | `Integer` | PVC size in GB (default 10) |
| `error_msg` | `Text` | Error detail, readonly |
| `partner_id` | `Many2one` | `res.partner` — the customer |
| `sale_order_id` | `Many2one` | `sale.order` — originating sale order (set by auto-provision) |
| `admin_password` | `Char` | Randomly generated app admin password (returned by portal on provision) |
| `odoo_conf` | `Text` | Rendered K8s ConfigMap mapped to `/etc/odoo/odoo.conf` inside the instance |
| `addons_repos_json` | `Text` | JSON structure defining `[{"url": "...", "branch": "..."}]` for dynamic additive repositories |
| `pod_logs` | `Text` | Unwrapped raw logs fetched from the tenant's Odoo pod |

**Added by bridge module** (`odoo_k8s_saas_subscription`, when installed):

| Field | Type | Notes |
|:---|:---|:---|
| `subscription_id` | `Many2one` | `sale.subscription` — recurring subscription |
| `sale_order_line_id` | `Many2one` | `sale.order.line` — specific order line that triggered creation |
| `subscription_stage` | `Char` | Related field: `subscription_id.stage_id.name` (readonly) |
| `user_count` | `Integer` | Current active user count queried from the tenant database (readonly) |
| `max_users` | `Integer` | Allowed users, synced from the subscription template (readonly) |

> **Inheritance from subscription template:** When an instance is provisioned via subscription, `plan` and `storage_gi` are copied from the subscription template's configured values (`template_id.plan` / `template_id.storage_gi`). This ensures Starter/Pro/Enterprise resource limits are automatically applied without manual configuration per instance.

### State Machine

```
draft ───────────────────► provisioning ──► ready ──► suspended
  ▲                             │              │          │
  │  (re-provision)         error ──────────┘  │          │
  └────────────────────────── ◄──┘              │          │
                                                │          │
ready ──► pending_delete ──► deleted            │          │
suspended ──► pending_delete ──► deleted        │          │
suspended ──► ready ◄──────────────────────────────────────┘
                              (action_resume)
```

`pending_delete` is an intermediate state set before calling the portal DELETE endpoint. It prevents concurrent deletion attempts and allows the admin to see which instances are in the process of being torn down.

State transitions are tracked via Odoo chatter (`tracking=True`).

### Actions

#### `action_check_availability()`

Called by the "Check Availability" button. Verifies the tenant_id namespace and database don't already exist.

```python
GET /api/v1/instances/check/{tenant_id}
Headers: X-API-Key: <SAAS_PORTAL_KEY>
```

Returns a success notification if available; raises `UserError` with reasons if not.

#### `action_provision()`

Called by the "Provision" button in the form view. Only available from `draft` or `error` states.

Includes an **idempotency guard**: checks if the K8s namespace already exists before calling the portal. If it already exists, syncs state from the portal instead of re-creating.

```python
POST /api/v1/instances
Headers: X-API-Key: <SAAS_PORTAL_KEY>
Body: {"tenant_id": ..., "plan": ..., "storage_gi": ..., "odoo_version": ..., "custom_image": ..., "addons_repos": [...]}
```

On success: sets `state = "provisioning"`, stores returned `url`, `namespace`, and `admin_password`.
On failure: sets `state = "error"` with `error_msg`.

#### `action_check_status()`

Called by cron job every 2 minutes on all `provisioning` instances.

```python
GET /api/v1/instances/{tenant_id}
Headers: X-API-Key: <SAAS_PORTAL_KEY>
```

- `status == "ready"` → `state = "ready"`, sends credentials email
- `404` → `state = "deleted"`

Can also be triggered manually from the Kanban/list view.

#### `action_delete()`

Called by the "Delete" button. Calls portal DELETE, then sets `state = "deleted"`.

```python
DELETE /api/v1/instances/{tenant_id}
Headers: X-API-Key: <SAAS_PORTAL_KEY>
```

#### `action_stop()`

Suspends a running instance by scaling the tenant's Kubernetes Deployment replicas to `0`. Only available from `ready` state.

```python
POST /api/v1/instances/{tenant_id}/stop
```

Sets `state = "suspended"`.

#### `action_resume()`

Resumes a suspended instance by scaling the deployment replicas back to `1`. Only available from `suspended` state.

```python
POST /api/v1/instances/{tenant_id}/start
```

Sets `state = "ready"`.

#### `action_open_url()`

Opens the instance URL in a new browser tab (action button).

#### `action_send_credentials_email()`

Sends the provisioning credentials email to the customer when an instance transitions to `ready`. Uses the template `odoo_k8s_saas.email_template_saas_credentials`.

#### `action_fetch_config()`, `action_save_config()`

Synchronizes the K8s ConfigMap holding `odoo.conf`. Fetches the current config (GET) or overwrites it (PUT) and triggers a forced restart of the tenant pod.

#### `action_patch_addons()`

Writes the parsed JSON from `addons_repos_json` to the Portal via PATCH. Overwrites the ConfigMap and triggers a pod restart for the `copy-addon` container to fetch the new git repositories.

#### `action_fetch_logs()`

Fetches up to 200 lines of K8s container logs using the `.../logs` API endpoint.

## Environment Variables (Odoo pod)

| Variable | Default | Description |
|:---|:---|:---|
| `SAAS_PORTAL_URL` | `http://portal.aeisoftware.svc.cluster.local:8000` | Portal API URL |
| `SAAS_PORTAL_KEY` | `""` | Must match `API_KEY` in portal Secret |

Set these in `k8s/06-odoo-admin.yaml` or via:

```bash
kubectl -n odoo-admin set env deployment/odoo-admin \
  SAAS_PORTAL_URL=http://portal.aeisoftware.svc.cluster.local:8000 \
  SAAS_PORTAL_KEY=<api-key>
```

## Cron Job

Defined in `odoo_k8s_saas/data/ir_cron.xml`:

| Setting | Value |
|:---|:---|
| Name | `SaaS: Refresh Instance Status` |
| Model | `saas.instance` |
| Code | `model.search([('state', '=', 'provisioning')]).action_check_status()` |
| Interval | every 2 minutes |
| Calls | `-1` (infinite) |

## File Structure

```
odoo_k8s_saas/
├── __init__.py
├── __manifest__.py
├── data/
│   ├── ir_cron.xml              ← scheduled status refresh
│   ├── mail_template_data.xml   ← provisioning email + credentials template
│   └── product_category.xml     ← "Odoo-SaaS" category + ir.sequence
├── models/
│   ├── __init__.py
│   ├── saas_instance.py         ← SaasInstance model + actions
│   └── saas_sale.py             ← AccountMove override (sales trigger)
├── security/
│   └── ir.model.access.csv      ← access control rules
└── views/
    ├── product_views.xml        ← product category + SaaS product views
    └── saas_instance_views.xml  ← form + kanban + list views
```

## Sales Integration

See **[Sales Integration](Sales-Integration.md)** for the full quote-to-provision pipeline. The addon extends `account.move` to auto-provision instances when invoices are paid for products in the `Odoo-SaaS` category.

## Bridge Module: `odoo_k8s_saas_subscription`

A separate bridge module connects `subscription_oca` to the SaaS provisioning pipeline for recurring billing. It:

- Adds `subscription_id`, `sale_order_line_id`, `subscription_stage`, `user_count`, and `max_users` fields to `saas.instance`
- Hooks into `sale.subscription.write()` to trigger provisioning/deletion on stage changes
- Ships three subscription templates (Starter/Pro/Enterprise Monthly) with per-user billing
- Includes a **customer portal** at `/my/subscriptions` with instance status and billing details
- Includes **cron jobs** for overdue suspension, closed-subscription cleanup, and user-count sync
- Uses `auto_install: True` — installs automatically when both `odoo_k8s_saas` and `subscription_oca` are present

```
odoo_k8s_saas_subscription/
├── __manifest__.py          (depends: odoo_k8s_saas, subscription_oca, portal, website_sale)
├── controllers/             ← portal controllers for /my/subscriptions
├── data/
│   ├── subscription_templates.xml  ← Starter/Pro/Enterprise templates
│   ├── ir_cron.xml                 ← 3 scheduled actions
│   └── website_checkout_config.xml ← checkout configuration
├── models/
│   ├── saas_instance.py     (subscription_id, sale_order_line_id, user_count, max_users)
│   └── sale_subscription.py (lifecycle hooks, portal mixin, per-user billing, crons)
├── security/
│   ├── ir.model.access.csv
│   └── ir_rules.xml         ← record rules
├── static/                  ← portal icons and assets
└── views/
    ├── saas_instance_views.xml           ← Shows subscription on instance form
    ├── sale_subscription_views.xml       ← Stat button + Re-provision button
    ├── subscription_template_views.xml   ← Template config (included_users, etc.)
    └── subscription_portal_templates.xml ← Customer-facing portal pages
```

See **[Subscription Integration](Subscription-Integration.md)** for the full details.

## Adding Tenant Plans

Edit the `plan` selection in `saas_instance.py`:

```python
plan = fields.Selection(
    [
        ("starter", "Starter"),
        ("pro", "Pro"),
        ("enterprise", "Enterprise"),
        ("unlimited", "Unlimited"),   # ← add here
    ],
    ...
)
```

The portal uses `plan` only for recording purposes — storage size controls the actual PVC size.
