# Odoo SaaS Addon — `odoo_k8s_saas`

The `odoo_k8s_saas` addon runs inside the **admin Odoo instance** (`odoo-admin` namespace). It provides a UI for operators to create, monitor, and delete tenant instances by calling the SaaS portal API.

## Module Details

| Field | Value |
|:---|:---|
| Technical Name | `odoo_k8s_saas` |
| Version | `18.0.1.0.0` |
| Category | Technical |
| License | LGPL-3 |
| Depends | `base`, `web`, `sale`, `account` |
| Author | AEI Software |

## Installation

The addon is loaded into the admin Odoo pod via an `initContainer` that clones the repository at startup:

```bash
git clone --depth=1 https://github.com/jpvargassoruco/odoo-saas-mvp.git /tmp/repo
cp -r /tmp/repo/odoo_k8s_saas /mnt/extra-addons/
```

The volume `odoo-addons` is `emptyDir` — the addon is always re-cloned from the `main` branch on pod restart.

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
| `state` | `Selection` | `draft / provisioning / ready / error / deleted` |
| `plan` | `Selection` | `starter / pro / enterprise` |
| `storage_gi` | `Integer` | PVC size in GB (default 10) |
| `error_msg` | `Text` | Error detail, readonly |
| `partner_id` | `Many2one` | `res.partner` — the customer |
| `sale_order_id` | `Many2one` | `sale.order` — originating sale order (set by auto-provision) |
| `subscription_id` | `Many2one` | `sale.subscription` — recurring subscription (added by bridge module) |
| `subscription_stage` | `Char` | Related field: `subscription_id.stage_id.name` (readonly, added by bridge module) |

### State Machine

```
draft ──────────────────► provisioning ──► ready
  ▲                             │
  │  (re-provision)         error ──────────┘
  └────────────────────────── ◄─┘

ready ──► deleted
```

State transitions are tracked via Odoo chatter (`tracking=True`).

### Actions

#### `action_provision()`

Called by the "Provision" button in the form view. Only available from `draft` or `error` states.

```python
POST /api/v1/instances
Headers: X-API-Key: <SAAS_PORTAL_KEY>
Body: {"tenant_id": ..., "plan": ..., "storage_gi": ...}
```

On success: sets `state = "provisioning"`, stores returned `url` and `namespace`.  
On failure: sets `state = "error"` with `error_msg`.

#### `action_check_status()`

Called by cron job every 2 minutes on all `provisioning` instances.

```python
GET /api/v1/instances/{tenant_id}
Headers: X-API-Key: <SAAS_PORTAL_KEY>
```

- `status == "ready"` → `state = "ready"`
- `404` → `state = "deleted"`

Can also be triggered manually from the Kanban/list view.

#### `action_delete()`

Called by the "Delete" button. Calls portal DELETE, then sets `state = "deleted"`.

```python
DELETE /api/v1/instances/{tenant_id}
Headers: X-API-Key: <SAAS_PORTAL_KEY>
```

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
│   ├── mail_template.xml        ← provisioning email template
│   └── product_category.xml     ← "Odoo-SaaS" category + ir.sequence
├── models/
│   ├── __init__.py
│   ├── saas_instance.py         ← SaasInstance model + actions
│   └── saas_sale.py             ← AccountMove override (sales trigger)
├── security/
│   └── ir.model.access.csv      ← access control rules
└── views/
    └── saas_instance_views.xml  ← form + kanban + list views
```

## Sales Integration

See **[[Sales Integration]]** for the full quote-to-provision pipeline. The addon extends `account.move` to auto-provision instances when invoices are paid for products in the `Odoo-SaaS` category.

## Bridge Module: `odoo_k8s_saas_subscription`

A separate bridge module connects `subscription_oca` to the SaaS provisioning pipeline for recurring billing. It:

- Adds `subscription_id` and `subscription_stage` fields to `saas.instance`
- Hooks into `sale.subscription.write()` to trigger provisioning/deletion on stage changes
- Ships three subscription templates (Starter/Pro/Enterprise Monthly)
- Uses `auto_install: True` — installs automatically when both `odoo_k8s_saas` and `subscription_oca` are present

```
odoo_k8s_saas_subscription/
├── __manifest__.py          (depends: odoo_k8s_saas, subscription_oca)
├── data/subscription_templates.xml
├── models/
│   ├── saas_instance.py     (subscription_id field)
│   └── sale_subscription.py (lifecycle hooks)
├── security/ir.model.access.csv
└── views/saas_instance_views.xml
```

See **[[Subscription Integration]]** for the full details.

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
