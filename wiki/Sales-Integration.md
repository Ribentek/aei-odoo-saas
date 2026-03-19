# Sales Integration — Quote-to-Provision Pipeline

The `odoo_k8s_saas` addon includes automated sales integration: when a customer pays for an Odoo-SaaS product, the system **automatically provisions a tenant instance** — no operator action needed.

## End-to-End Flow

```
Salesperson creates Sale Order
  │  (product in "Odoo-SaaS" category)
  │
  ▼
Confirm SO → Create Invoice → Confirm Invoice
  │
  ▼
Customer pays invoice (bank, manual, or online)
  │
  ▼
Odoo reconciles payment → payment_state transitions to "paid"
  │
  ▼
_compute_payment_state() override detects the transition
  │
  ▼
_saas_check_and_provision()
  │  1. Gets linked sale orders from invoice lines
  │  2. Finds products in "Odoo-SaaS" category
  │  3. Checks for duplicate instances (skips if exists)
  │  4. Generates tenant_id from partner name + sequence
  │  5. Creates saas.instance record
  │  6. Calls action_provision() → POST /api/v1/instances
  │  7. Sends provisioned email notification (best-effort)
  │
  ▼
Tenant Odoo pod starts in odoo-<tenant_id> namespace
  │
  ▼
Cron (every 2 min) checks status → "ready"
  │
  ▼
Customer accesses https://<tenant_id>.aeisoftware.com
```

## Trigger Model

### Primary Trigger — `_compute_payment_state()`

In Odoo 18, `payment_state` on `account.move` is a **computed stored field**. When a payment is reconciled, the ORM calls `_compute_payment_state()` internally and persists the result via an internal `_write()` — this **bypasses** the public `write()` method entirely.

The override:

```python
def _compute_payment_state(self):
    old_states = {m.id: m.payment_state for m in self if m.id}
    super()._compute_payment_state()
    for move in self:
        new_state = move.payment_state
        old_state = old_states.get(move.id)
        if (new_state in ("paid", "in_payment")
            and old_state not in ("paid", "in_payment")
            and move.move_type == "out_invoice"):
            move._saas_check_and_provision()
```

1. **Snapshot** old states before the `super()` call
2. Let Odoo **recompute** normally
3. **Detect** transitions to `paid` or `in_payment`
4. **Trigger** provisioning only for customer invoices (`out_invoice`)

### Secondary Trigger — `write()` (fallback)

A `write()` override catches edge cases where `payment_state` is set directly via the API or a manual write:

```python
def write(self, vals):
    res = super().write(vals)
    if vals.get("payment_state") in ("paid", "in_payment"):
        for move in self.filtered(lambda m: m.move_type == "out_invoice" ...):
            move._saas_check_and_provision()
    return res
```

> **Why two triggers?** The `_compute_payment_state()` handles the standard payment reconciliation path (99% of cases). The `write()` handles manual/API corrections. Together they cover all payment state transitions.

## Product Category Setup

The trigger checks if a sale order line's product belongs to the `Odoo-SaaS` product category (or a child category).

### Resolution Order

1. **XML ID lookup** — `self.env.ref("odoo_k8s_saas.product_category_odoo_saas")`
2. **Name fallback** — `search([("name", "ilike", "odoo%saas")])`

### Data File

Defined in `odoo_k8s_saas/data/product_category.xml`:

```xml
<record id="product_category_odoo_saas" model="product.category">
    <field name="name">Odoo-SaaS</field>
</record>
```

### Creating a SaaS Product

1. **Settings → Technical → Product Categories** — verify `Odoo-SaaS` exists
2. **Sales → Products → New**
3. Set category to `Odoo-SaaS`
4. Set a price and configure invoicing policy

## Tenant ID Generation

```python
def _generate_tenant_id(self, partner):
    slug = re.sub(r"[^a-z0-9]+", "-", (partner.name or "tenant").lower()).strip("-")
    slug = slug[:30].rstrip("-")
    seq = self.env["ir.sequence"].next_by_code("saas.tenant.id") or "001"
    return f"{slug}-{seq}"
```

Examples:

| Partner Name | Generated Tenant ID |
|:---|:---|
| Acme Corp | `acme-corp-001` |
| María García S.A. | `mar-a-garc-a-s-a-002` |
| 日本テスト | `tenant-003` |

The sequence `saas.tenant.id` is defined in `product_category.xml` and auto-increments.

## Duplicate Prevention

Before creating an instance, the system checks:

```python
existing = Instance.search([
    ("sale_order_id", "=", order.id),
    ("state", "not in", ["deleted"]),
], limit=1)
```

If a non-deleted instance already exists for the same sale order, it is skipped. This makes the trigger **idempotent** — re-running on the same invoice won't create duplicates.

## Email Notification

On successful provisioning, the system sends an email to the customer using the mail template `odoo_k8s_saas.mail_template_instance_provisioned`.

Defined in `odoo_k8s_saas/data/mail_template.xml`:

- **Subject:** `Your Odoo Instance is Ready — {{ object.name }}`
- **Body:** Includes tenant URL, instance name, and next steps
- **Failure mode:** Best-effort — email errors are logged but don't prevent provisioning

## Model Extension: `saas.instance`

The sales integration adds one field to `saas.instance`:

| Field | Type | Notes |
|:---|:---|:---|
| `sale_order_id` | `Many2one` | `sale.order` — the originating sale order |

This field is set by `_saas_check_and_provision()` and links the instance back to its sale order.

## File Structure (sales-related)

```
odoo_k8s_saas/
├── models/
│   └── saas_sale.py          ← AccountMove override + provisioning logic
├── data/
│   ├── product_category.xml  ← "Odoo-SaaS" category + ir.sequence
│   └── mail_template.xml     ← provisioning email template
```

## Log Messages

The integration produces structured log messages for debugging:

```
SaaS trigger (compute): payment_state False → paid for INV/2026/00001
SaaS check: invoice INV/2026/00001 (type=out_invoice)
SaaS check: linked SOs = ['S00010']
SaaS check: using category 'Odoo-SaaS' (id=6)
Auto-created saas.instance acme-corp-001 for SO S00010
```

## Testing the Flow

1. Create a product with category `Odoo-SaaS`
2. Create a Sale Order → Confirm → Create Invoice → Confirm Invoice
3. Register Payment on the invoice
4. Check Odoo logs for `SaaS trigger (compute):` messages
5. Go to **SaaS Instances** menu — new instance should appear
6. Wait 2–5 minutes for cron to transition to `ready`

## Recurring Billing

> **For subscription-based SaaS plans** with recurring monthly invoicing, see **[[Subscription Integration]]**. The one-time payment trigger documented on this page still works alongside subscriptions as a belt-and-suspenders approach — both triggers are idempotent.

## Known Odoo 18 Gotcha

> **Critical:** In Odoo 18, `payment_state` is a computed stored field — `write()` is NOT called during normal payment reconciliation. If you override only `write()`, your trigger will never fire for standard payment flows. You must override `_compute_payment_state()`.

This is documented in `saas_sale.py` header comments and is the root cause of the most common "trigger doesn't fire" issue.
