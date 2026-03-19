"""
models/sale_subscription.py

Subscription lifecycle hooks for SaaS provisioning/suspension,
portal.mixin for customer-facing /my/subscriptions portal,
and re-provision action when an instance is manually deleted.

Stage transitions:
  → In Progress : provision the linked saas.instance (if not already)
  → Closed      : delete/suspend the linked saas.instance
"""
import logging
import re

from odoo import models, fields, api, _
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)

# Stage XML IDs from subscription_oca
_STAGE_IN_PROGRESS = "subscription_oca.subscription_stage_in_progress"
_STAGE_CLOSED = "subscription_oca.subscription_stage_closed"


class SaleSubscription(models.Model):
    _inherit = ["sale.subscription", "portal.mixin"]
    _name = "sale.subscription"

    # ── Computed fields ─────────────────────────────────────────
    saas_instance_count = fields.Integer(
        string="SaaS Instances",
        compute="_compute_saas_instance_count",
    )
    has_active_instance = fields.Boolean(
        compute="_compute_saas_instance_count",
    )

    @api.depends_context("uid")
    def _compute_saas_instance_count(self):
        Instance = self.env["saas.instance"]
        for rec in self:
            instances = Instance.search([
                ("subscription_id", "=", rec.id),
                ("state", "not in", ["deleted"]),
            ])
            rec.saas_instance_count = len(instances)
            rec.has_active_instance = bool(instances)

    # ── Portal mixin ────────────────────────────────────────────
    def _compute_access_url(self):
        super()._compute_access_url()
        for rec in self:
            rec.access_url = f"/my/subscriptions/{rec.id}"

    # ── Actions ─────────────────────────────────────────────────
    def action_view_saas_instances(self):
        """Open a list of linked SaaS instances."""
        self.ensure_one()
        instances = self.env["saas.instance"].search([
            ("subscription_id", "=", self.id),
        ])
        action = {
            "type": "ir.actions.act_window",
            "name": _("SaaS Instances"),
            "res_model": "saas.instance",
            "view_mode": "list,form",
            "domain": [("id", "in", instances.ids)],
            "context": {"default_subscription_id": self.id},
        }
        if len(instances) == 1:
            action["view_mode"] = "form"
            action["res_id"] = instances.id
        return action

    def action_reprovision_instance(self):
        """Re-create and provision a saas.instance when the old one was deleted."""
        self.ensure_one()

        # Guard: subscription must be "In Progress"
        stage_in_progress = self.env.ref(_STAGE_IN_PROGRESS, raise_if_not_found=False)
        if stage_in_progress and self.stage_id.id != stage_in_progress.id:
            raise UserError(
                _("You can only re-provision an instance for subscriptions "
                  "that are in the 'In Progress' stage.")
            )

        # Guard: must not already have an active instance
        existing = self.env["saas.instance"].search([
            ("subscription_id", "=", self.id),
            ("state", "not in", ["deleted"]),
        ], limit=1)
        if existing:
            raise UserError(
                _("This subscription already has an active instance: %s.\n"
                  "Delete it first if you want to re-provision.")
                % existing.tenant_id
            )

        # Generate a new tenant ID
        sub_code = (self.name or "").lower().replace("/", "-")
        tenant_id = self._generate_saas_tenant_id(self.partner_id, sub_code)

        # Determine plan from subscription template name
        plan = "starter"
        if self.template_id:
            tmpl_name = (self.template_id.name or "").lower()
            if "enterprise" in tmpl_name:
                plan = "enterprise"
            elif "pro" in tmpl_name:
                plan = "pro"

        storage_map = {"starter": 10, "pro": 50, "enterprise": 100}

        instance = self.env["saas.instance"].create({
            "name": f"{self.partner_id.name} — Re-provision ({self.display_name})",
            "tenant_id": tenant_id,
            "plan": plan,
            "storage_gi": storage_map.get(plan, 10),
            "partner_id": self.partner_id.id,
            "sale_order_id": self.sale_order_id.id if self.sale_order_id else False,
            "subscription_id": self.id,
        })

        logger.info(
            "Re-provisioned instance %s for subscription %s",
            instance.tenant_id, self.display_name,
        )

        # Auto-provision it
        try:
            instance.action_provision()
        except Exception:
            logger.exception(
                "Auto-provision failed for re-provisioned instance %s",
                instance.tenant_id,
            )

        # Return the instance form view
        return {
            "type": "ir.actions.act_window",
            "name": _("Re-provisioned Instance"),
            "res_model": "saas.instance",
            "view_mode": "form",
            "res_id": instance.id,
        }

    # ── Stage-change hooks ──────────────────────────────────────
    def write(self, vals):
        """Detect stage_id changes and trigger SaaS actions."""
        old_stages = {rec.id: rec.stage_id.id for rec in self}
        res = super().write(vals)

        if "stage_id" not in vals:
            return res

        new_stage_id = int(vals["stage_id"])  # RPC may deliver as str; cast for safe comparison

        # Resolve known stage IDs
        stage_in_progress = self.env.ref(_STAGE_IN_PROGRESS, raise_if_not_found=False)
        stage_closed = self.env.ref(_STAGE_CLOSED, raise_if_not_found=False)

        if not stage_closed:
            logger.warning(
                "Stage XML ID '%s' not found — instance cleanup on Closed is DISABLED. "
                "Ensure subscription_oca is installed.",
                _STAGE_CLOSED,
            )

        for rec in self:
            old_stage_id = old_stages.get(rec.id)
            if old_stage_id == new_stage_id:
                continue  # no change

            # Find linked SaaS instances
            instances = self.env["saas.instance"].search([
                ("subscription_id", "=", rec.id),
                ("state", "not in", ["deleted"]),
            ])

            # → In Progress: create and provision
            if stage_in_progress and new_stage_id == stage_in_progress.id:
                if not instances and rec.sale_order_id:
                    # Check if there is a SaaS product in the SO
                    saas_category = self.env.ref("odoo_k8s_saas.product_category_odoo_saas", raise_if_not_found=False)
                    has_saas = False
                    for line in rec.sale_order_id.order_line:
                        if not line.product_id:
                            continue
                        in_categ = saas_category and rec._is_saas_category(line.product_id.categ_id, saas_category)
                        in_name = "saas" in (line.product_id.name or "").lower()
                        if in_categ or in_name:
                            has_saas = True
                            break
                    
                    if has_saas:
                        # Create the instance
                        sub_code = (rec.name or "").lower().replace("/", "-")
                        tenant_id = rec._generate_saas_tenant_id(rec.partner_id, sub_code)

                        plan = "starter"
                        if rec.template_id:
                            tmpl_name = (rec.template_id.name or "").lower()
                            if "enterprise" in tmpl_name:
                                plan = "enterprise"
                            elif "pro" in tmpl_name:
                                plan = "pro"

                        storage_map = {"starter": 10, "pro": 50, "enterprise": 100}

                        inst = self.env["saas.instance"].create({
                            "name": f"{rec.partner_id.name} — {rec.display_name}",
                            "tenant_id": tenant_id,
                            "plan": plan,
                            "storage_gi": storage_map.get(plan, 10),
                            "partner_id": rec.partner_id.id,
                            "sale_order_id": rec.sale_order_id.id,
                            "subscription_id": rec.id,
                        })
                        logger.info("Created saas.instance %s from subscription %s", inst.tenant_id, rec.name)
                        instances = inst

                # Provision any draft/error instances
                for inst in instances.filtered(lambda i: i.state in ("draft", "error")):
                    logger.info(
                        "Subscription %s → In Progress: provisioning instance %s",
                        rec.display_name, inst.tenant_id,
                    )
                    try:
                        inst.action_provision()
                    except Exception:
                        logger.exception(
                            "Failed to provision %s from subscription %s",
                            inst.tenant_id, rec.display_name,
                        )

            # → Closed: delete all non-deleted instances (incl. suspended)
            elif stage_closed and new_stage_id == stage_closed.id:
                for inst in instances.filtered(
                    lambda i: i.state in ("draft", "provisioning", "ready", "suspended")
                ):
                    logger.info(
                        "Subscription %s → Closed: deleting instance %s (state=%s)",
                        rec.display_name, inst.tenant_id, inst.state,
                    )
                    try:
                        inst.action_delete()
                    except Exception:
                        logger.exception(
                            "Failed to delete %s from subscription %s",
                            inst.tenant_id, rec.display_name,
                        )

        return res

    def _generate_saas_tenant_id(self, partner, suffix=""):
        """Generate a URL-safe tenant_id from partner name + suffix."""
        slug = re.sub(r"[^a-z0-9]+", "-", (partner.name or "tenant").lower()).strip("-")
        slug = slug[:25].rstrip("-")
        suffix = re.sub(r"[^a-z0-9]+", "-", suffix).strip("-")
        if not suffix:
            suffix = self.env["ir.sequence"].next_by_code("saas.tenant.id") or "001"
        return f"{slug}-{suffix}"

    def _is_saas_category(self, categ, saas_categ):
        """Return True if categ is saas_categ or a child of it."""
        while categ:
            if categ.id == saas_categ.id:
                return True
            categ = categ.parent_id
        return False

    @api.model
    def _cron_suspend_overdue(self):
        """Cron: suspend instances whose subscription is past-due.

        Finds subscriptions that are:
        - Not in a closed stage
        - Have exceeded their next invoice date (overdue payment)
        - Have an active (ready) linked instance

        Calls action_stop() on each overdue instance to scale it to 0.
        """
        stage_closed = self.env.ref(_STAGE_CLOSED, raise_if_not_found=False)
        today = fields.Date.today()

        domain = [
            ("recurring_next_date", "<", today),
        ]
        if stage_closed:
            domain.append(("stage_id", "!=", stage_closed.id))

        overdue_subs = self.search(domain)
        logger.info("_cron_suspend_overdue: checking %d overdue subscriptions", len(overdue_subs))

        for sub in overdue_subs:
            instances = self.env["saas.instance"].search([
                ("subscription_id", "=", sub.id),
                ("state", "=", "ready"),
            ])
            for inst in instances:
                logger.info(
                    "Suspending instance %s (subscription %s overdue since %s)",
                    inst.tenant_id, sub.display_name, sub.recurring_next_date,
                )
                try:
                    inst.action_stop()
                except Exception:
                    logger.exception(
                        "Failed to suspend overdue instance %s", inst.tenant_id
                    )

    @api.model
    def _cron_sync_closed_subscriptions(self):
        """Safety net cron: delete any instance still active on a Closed subscription.

        Runs hourly. Catches cases where the write() stage-change hook was bypassed
        (e.g., portal API was down, bulk SQL update, or exception during transition).
        """
        stage_closed = self.env.ref(_STAGE_CLOSED, raise_if_not_found=False)
        if not stage_closed:
            logger.warning(
                "_cron_sync_closed_subscriptions: stage '%s' not found — skipping.",
                _STAGE_CLOSED,
            )
            return

        closed_subs = self.search([("stage_id", "=", stage_closed.id)])
        logger.info(
            "_cron_sync_closed_subscriptions: checking %d closed subscriptions",
            len(closed_subs),
        )

        for sub in closed_subs:
            instances = self.env["saas.instance"].search([
                ("subscription_id", "=", sub.id),
                ("state", "not in", ["deleted"]),
            ])
            for inst in instances:
                logger.warning(
                    "_cron_sync_closed: active instance %s (state=%s) found on "
                    "closed subscription %s — deleting now.",
                    inst.tenant_id, inst.state, sub.display_name,
                )
                try:
                    inst.action_delete()
                except Exception:
                    logger.exception(
                        "_cron_sync_closed: failed to delete instance %s",
                        inst.tenant_id,
                    )

