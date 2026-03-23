"""
models/saas_sale.py

Hooks into the invoice payment flow:
  When a customer invoice is fully paid and its source sale order
  contains products in the "Odoo-SaaS" category, automatically
  create and provision a saas.instance for each such line.

IMPORTANT (Odoo 18):
  ``payment_state`` is a **computed stored field**.  When a payment is
  reconciled the ORM calls ``_compute_payment_state()`` and persists the
  new value through an internal ``_write()``, which *bypasses* the public
  ``write()`` method.  Therefore, the primary trigger must override
  ``_compute_payment_state()``, NOT ``write()``.
"""
import logging
import re

from odoo import models, fields, api

logger = logging.getLogger(__name__)

SAAS_CATEGORY_XMLID = "odoo_k8s_saas.product_category_odoo_saas"

# States that mean "invoice is paid"
_PAID_STATES = frozenset(("paid", "in_payment"))


class AccountMove(models.Model):
    """Extend account.move to trigger SaaS provisioning on full payment."""

    _inherit = "account.move"

    # ── primary trigger: computed-field override ───────────────────────────
    def _compute_payment_state(self):
        """Override the ORM compute to detect paid→provisioning transitions."""
        # Snapshot current states BEFORE recomputation
        old_states = {m.id: m.payment_state for m in self if m.id}

        super()._compute_payment_state()

        # After recomputation, check for newly-paid customer invoices
        for move in self:
            new_state = move.payment_state
            old_state = old_states.get(move.id)
            if (
                new_state in _PAID_STATES
                and old_state not in _PAID_STATES
                and move.move_type == "out_invoice"
            ):
                logger.info(
                    "SaaS trigger (compute): payment_state %s → %s for %s",
                    old_state, new_state, move.name,
                )
                try:
                    move._saas_check_and_provision()
                except Exception:
                    logger.exception(
                        "SaaS auto-provision failed for invoice %s", move.name
                    )

    # ── secondary trigger: manual / API writes ────────────────────────────
    def write(self, vals):
        """Fallback: detect payment_state set explicitly via write()."""
        res = super().write(vals)
        if vals.get("payment_state") in _PAID_STATES:
            logger.info(
                "SaaS trigger (write): payment_state → %s for %s invoice(s)",
                vals["payment_state"], len(self),
            )
            for move in self.filtered(
                lambda m: m.move_type == "out_invoice"
                and m.payment_state in _PAID_STATES
            ):
                try:
                    move._saas_check_and_provision()
                except Exception:
                    logger.exception(
                        "SaaS auto-provision failed for invoice %s", move.name
                    )
        return res

    def _saas_check_and_provision(self):
        """Find SaaS lines from the linked sale orders and provision."""
        self.ensure_one()
        logger.info("SaaS check: invoice %s (type=%s)", self.name, self.move_type)

        # Get the sale orders linked to this invoice
        sale_orders = self.line_ids.sale_line_ids.order_id
        if not sale_orders:
            logger.info("SaaS check: no linked sale orders — skipping.")
            return
        logger.info("SaaS check: linked SOs = %s", sale_orders.mapped("name"))

        saas_category = self._get_saas_category()
        if not saas_category:
            logger.warning("SaaS check: no SaaS product category found — skipping.")
            return
        logger.info("SaaS check: using category '%s' (id=%s)", saas_category.name, saas_category.id)

        Instance = self.env["saas.instance"]

        for order in sale_orders:
            for line in order.order_line:
                product = line.product_id
                if not product or not product.categ_id:
                    continue
                # Check if product category is "Odoo-SaaS" or a child of it
                if not self._is_saas_category(product.categ_id, saas_category):
                    continue

                # ── Find existing (non-deleted) instance for this SO ──────────
                existing = Instance.search([
                    ("sale_order_id", "=", order.id),
                    ("state", "not in", ["deleted"]),
                ], limit=1)

                # ── Resume a suspended instance (overdue-payment resumed) ──────
                if existing and existing.state == "suspended":
                    logger.info(
                        "Instance %s is suspended — resuming for SO %s.",
                        existing.tenant_id, order.name,
                    )
                    try:
                        existing.action_resume()
                    except Exception:
                        logger.exception(
                            "Failed to resume suspended instance %s", existing.tenant_id
                        )
                    self._link_subscription(order, existing)
                    continue

                if existing:
                    logger.info(
                        "Instance already exists for SO %s: %s — ensuring linked.",
                        order.name, existing.tenant_id,
                    )
                    self._link_subscription(order, existing)
                    continue

                # Build tenant_id from partner name
                tenant_id = self._generate_tenant_id(order.partner_id)

                # ── Find subscription FIRST so we can set subscription_id at
                # create-time.  This is CRITICAL: if subscription_id is set
                # as part of the create() call it is flushed to the DB before
                # _link_subscription() calls subscription.write(stage_id=…).
                # Without this, SaleSubscription.write() searches for linked
                # instances and finds none (ORM buffer not yet flushed) →
                # creates a 2nd duplicate namespace → infinite loop.
                Subscription = self.env["sale.subscription"]
                subscription_for_order = Subscription.search([
                    ("sale_order_id", "=", order.id),
                ], limit=1)

                instance = Instance.create({
                    "name": f"{order.partner_id.name} — {order.name}",
                    "tenant_id": tenant_id,
                    "plan": "starter",
                    "storage_gi": 10,
                    "partner_id": order.partner_id.id,
                    "sale_order_id": order.id,
                    # Set subscription_id at create-time so the ORM flushes it
                    # to the DB before the stage-advance write() fires the
                    # SaleSubscription.write() hook below.
                    "subscription_id": subscription_for_order.id if subscription_for_order else False,
                })
                logger.info(
                    "Auto-created saas.instance %s for SO %s (sub=%s)",
                    instance.tenant_id, order.name,
                    subscription_for_order.name if subscription_for_order else "none",
                )

                # Flush instance to DB NOW so that SaleSubscription.write()
                # can find it via search(subscription_id=…) without creating
                # a duplicate.  This is the key fix for the infinite-loop bug.
                self.env.flush_all()

                # ── Link subscription_id + advance stage ──────────────────────
                self._link_subscription(order, instance)

                # Provision (calls portal API)
                try:
                    instance.action_provision()
                except Exception:
                    logger.exception(
                        "Failed to auto-provision %s", instance.tenant_id
                    )

                # Send email (best-effort — don't rollback instance on failure)
                try:
                    template = self.env.ref(
                        "odoo_k8s_saas.mail_template_instance_provisioned",
                        raise_if_not_found=False,
                    )
                    if template:
                        template.send_mail(instance.id, force_send=True)
                except Exception:
                    logger.exception(
                        "Failed to send provisioning email for %s",
                        instance.tenant_id,
                    )

    def _link_subscription(self, order, instance):
        """Link instance → subscription and auto-advance stage to In Progress."""
        Subscription = self.env["sale.subscription"]
        subscription = Subscription.search([
            ("sale_order_id", "=", order.id),
        ], limit=1)
        if not subscription:
            return

        # Set subscription_id on the instance (fixes close-doesn't-stop bug).
        # NOTE: ideally this is already set at create() time (see caller above),
        # but we keep this as a safety net for callers that don't pre-set it.
        if not instance.subscription_id:
            instance.subscription_id = subscription.id
            # Flush immediately so the next write() hook sees the link.
            instance.env.flush_all()
            logger.info(
                "Linked instance %s → subscription %s (late-link)",
                instance.tenant_id, subscription.name,
            )

        # Auto-advance subscription to "In Progress" if not already there
        stage_ip = self.env.ref(
            "subscription_oca.subscription_stage_in_progress",
            raise_if_not_found=False,
        )
        if stage_ip and subscription.stage_id.id != stage_ip.id:
            logger.info(
                "Advancing subscription %s → In Progress", subscription.name,
            )
            subscription.sudo().write({"stage_id": stage_ip.id})


    @api.model
    def _get_saas_category(self):
        """Resolve the SaaS product category — XML ID first, then name search."""
        cat = self.env.ref(SAAS_CATEGORY_XMLID, raise_if_not_found=False)
        if cat:
            return cat
        # Fallback: search by name pattern (handles manual creation)
        cat = self.env["product.category"].search(
            [("name", "ilike", "odoo%saas")], limit=1
        )
        if cat:
            logger.info(
                "SaaS category found by name fallback: '%s' (id=%s)", cat.name, cat.id
            )
        return cat

    @api.model
    def _is_saas_category(self, categ, saas_categ):
        """Return True if categ is saas_categ or a child of it."""
        while categ:
            if categ.id == saas_categ.id:
                return True
            categ = categ.parent_id
        return False

    @api.model
    def _generate_tenant_id(self, partner):
        """Generate a URL-safe tenant_id from partner name + sequence."""
        slug = re.sub(r"[^a-z0-9]+", "-", (partner.name or "tenant").lower()).strip("-")
        # Truncate to 30 chars max
        slug = slug[:30].rstrip("-")
        # Add a short sequence to avoid collisions
        seq = self.env["ir.sequence"].next_by_code("saas.tenant.id") or "001"
        return f"{slug}-{seq}"
