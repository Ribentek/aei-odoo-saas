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
import os
import re

import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)

PORTAL_URL = os.getenv("SAAS_PORTAL_URL", "http://portal.aeisoftware.svc.cluster.local:8000")
PORTAL_KEY = os.getenv("SAAS_PORTAL_KEY", "")

# Stage XML IDs from subscription_oca
_STAGE_IN_PROGRESS = "subscription_oca.subscription_stage_in_progress"
_STAGE_CLOSED = "subscription_oca.subscription_stage_closed"


class SaleSubscription(models.Model):
    _inherit = ["sale.subscription", "portal.mixin"]
    _name = "sale.subscription"

    # ── Relational fields ───────────────────────────────────────
    saas_instance_ids = fields.One2many(
        "saas.instance",
        "subscription_id",
        string="SaaS Instances",
    )

    # ── Computed fields ─────────────────────────────────────────
    saas_instance_count = fields.Integer(
        string="SaaS Instances",
        compute="_compute_saas_instance_count",
    )
    has_active_instance = fields.Boolean(
        compute="_compute_saas_instance_count",
    )

    # ── Per-user billing computed fields ──────────────────────────
    current_user_count = fields.Integer(
        string="Current Users",
        compute="_compute_user_billing",
        help="Sum of active users across all instances linked to this subscription.",
    )
    extra_users = fields.Integer(
        string="Extra Users",
        compute="_compute_user_billing",
        help="Number of users beyond the included amount.",
    )
    extra_users_amount = fields.Float(
        string="Extra Users Amount",
        compute="_compute_user_billing",
        digits="Product Price",
        help="Monthly charge for extra users (extra_users × price_per_extra_user).",
    )

    @api.depends("saas_instance_ids", "saas_instance_ids.state")
    def _compute_saas_instance_count(self):
        for rec in self:
            active = rec.saas_instance_ids.filtered(
                lambda i: i.state not in ("deleted",)
            )
            rec.saas_instance_count = len(active)
            rec.has_active_instance = bool(active)

    @api.depends(
        "saas_instance_ids.user_count",
        "saas_instance_ids.state",
        "template_id.included_users",
        "template_id.price_per_extra_user",
    )
    def _compute_user_billing(self):
        for rec in self:
            active_instances = rec.saas_instance_ids.filtered(
                lambda i: i.state not in ("deleted",)
            )
            total_users = sum(active_instances.mapped("user_count"))
            included = rec.template_id.included_users if rec.template_id else 0
            extra = max(0, total_users - included)
            price = rec.template_id.price_per_extra_user if rec.template_id else 0.0

            rec.current_user_count = total_users
            rec.extra_users = extra
            rec.extra_users_amount = extra * price

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

        # Determine saas product to copy configuration
        saas_product = False
        if self.sale_order_id:
            saas_category = self.env.ref("odoo_k8s_saas.product_category_odoo_saas", raise_if_not_found=False)
            for line in self.sale_order_id.order_line:
                if not line.product_id: continue
                in_categ = saas_category and self._is_saas_category(line.product_id.categ_id, saas_category)
                in_name = "saas" in (line.product_id.name or "").lower()
                if in_categ or in_name:
                    saas_product = line.product_id
                    break

        instance = self.env["saas.instance"].create({
            "name": f"{self.partner_id.name} — Re-provision ({self.display_name})",
            "tenant_id": tenant_id,
            "plan": plan,
            "storage_gi": storage_map.get(plan, 10),
            "partner_id": self.partner_id.id,
            "sale_order_id": self.sale_order_id.id if self.sale_order_id else False,
            "subscription_id": self.id,
            "odoo_version": saas_product.odoo_version if saas_product else "18.0",
            "custom_image": saas_product.custom_image if saas_product else False,
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
    # ── Stage-change and Template-change hooks ────────────────────────────────
    def write(self, vals):
        """Detect stage_id and template_id changes and trigger SaaS actions."""
        old_stages = {rec.id: rec.stage_id.id for rec in self}
        old_templates = {rec.id: rec.template_id.id for rec in self}
        res = super().write(vals)

        # Handle template_id change (Upgrades)
        if "template_id" in vals:
            for rec in self:
                if old_templates.get(rec.id) == rec.template_id.id:
                    continue
                
                instances = self.env["saas.instance"].search([
                    ("subscription_id", "=", rec.id),
                    ("state", "not in", ["deleted"]),
                ])
                if not instances:
                    continue
                
                plan = "starter"
                if rec.template_id:
                    tmpl_name = (rec.template_id.name or "").lower()
                    if "enterprise" in tmpl_name:
                        plan = "enterprise"
                    elif "pro" in tmpl_name:
                        plan = "pro"
                
                storage_map = {"starter": 10, "pro": 50, "enterprise": 100}
                new_storage = storage_map.get(plan, 10)
                
                for inst in instances:
                    if inst.plan != plan or inst.storage_gi != new_storage:
                        logger.info("Upgrade/Downgrade: Subscription %s changed from template_id %s to %s. Updating instance %s.", rec.display_name, old_templates.get(rec.id), rec.template_id.id, inst.tenant_id)
                        inst.write({
                            "plan": plan,
                            "storage_gi": new_storage,
                        })
                        try:
                            # Re-provision applies new limits via k8s_utils
                            inst.action_provision()
                        except Exception:
                            logger.exception("Failed to apply upgraded resource limits for %s", inst.tenant_id)

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
                # Guard: only provision if the template is marked as a SaaS plan
                if (not instances and rec.sale_order_id
                        and rec.template_id and getattr(rec.template_id, 'is_saas_plan', False)):
                    # Collect ALL SaaS order lines (one instance per line)
                    saas_category = self.env.ref(
                        "odoo_k8s_saas.product_category_odoo_saas",
                        raise_if_not_found=False,
                    )
                    saas_lines = []
                    for line in rec.sale_order_id.order_line:
                        if not line.product_id:
                            continue
                        in_categ = (saas_category
                                    and rec._is_saas_category(
                                        line.product_id.categ_id, saas_category))
                        in_name = "saas" in (line.product_id.name or "").lower()
                        if in_categ or in_name:
                            saas_lines.append(line)

                    for sol in saas_lines:
                        saas_product = sol.product_id
                        sub_code = (rec.name or "").lower().replace("/", "-")
                        tenant_id = rec._generate_saas_tenant_id(
                            rec.partner_id, sub_code)
                        # Append version suffix when there are multiple SaaS lines
                        if len(saas_lines) > 1 and saas_product.odoo_version:
                            tenant_id = f"{tenant_id}-{saas_product.odoo_version.replace('.', '')}"

                        # ── Idempotency guard 1: by sale_order_line_id ────────
                        existing = self.env["saas.instance"].search(
                            [("sale_order_line_id", "=", sol.id)], limit=1,
                        )
                        if existing:
                            logger.warning(
                                "write() subscription %s: order line %s already "
                                "has saas.instance (id=%s) — reusing.",
                                rec.name, sol.id, existing.id,
                            )
                            if not existing.subscription_id:
                                existing.subscription_id = rec.id
                            instances |= existing
                            continue

                        # ── Idempotency guard 2: by tenant_id ─────────────────
                        existing_by_tid = self.env["saas.instance"].search(
                            [("tenant_id", "=", tenant_id)], limit=1,
                        )
                        if existing_by_tid:
                            logger.warning(
                                "write() subscription %s: tenant_id '%s' "
                                "already exists (id=%s) — reusing.",
                                rec.name, tenant_id, existing_by_tid.id,
                            )
                            if not existing_by_tid.subscription_id:
                                existing_by_tid.subscription_id = rec.id
                            instances |= existing_by_tid
                            continue

                        # ── Create instance ───────────────────────────────────
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
                            "sale_order_line_id": sol.id,
                            "subscription_id": rec.id,
                            "odoo_version": saas_product.odoo_version if saas_product else "18.0",
                            "custom_image": saas_product.custom_image if saas_product else False,
                        })
                        logger.info(
                            "Created saas.instance %s (version=%s) from "
                            "subscription %s, order line %s",
                            inst.tenant_id, inst.odoo_version,
                            rec.name, sol.id,
                        )
                        instances |= inst

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

            # → Closed: suspend all non-deleted instances safely instead of deleting
            elif stage_closed and new_stage_id == stage_closed.id:
                to_delete = instances.filtered(
                    lambda i: i.state in ("draft", "provisioning", "ready", "suspended")
                )
                if to_delete:
                    logger.info(
                        "Subscription %s → Closed: marking %d instance(s) FOR SUSPENSION to prevent data loss",
                        rec.display_name, len(to_delete),
                    )
                    to_delete.action_stop()

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
                ("state", "not in", ["deleted", "pending_delete"]),
            ])
            if instances:
                logger.warning(
                    "_cron_sync_closed: %d active instance(s) found on "
                    "closed subscription %s — marking for deletion.",
                    len(instances), sub.display_name,
                )
                instances.action_request_delete()

    @api.model
    def _cron_sync_user_count(self):
        """Cron: sync active-user counts from the portal API.

        Calls GET /api/v1/instances/{tenant_id} for each active instance
        and updates saas.instance.user_count.
        """
        instances = self.env["saas.instance"].search([
            ("state", "in", ["ready", "provisioning"]),
        ])
        logger.info("_cron_sync_user_count: syncing %d instances", len(instances))
        for inst in instances:
            try:
                resp = requests.get(
                    f"{PORTAL_URL}/api/v1/instances/{inst.tenant_id}",
                    headers={"X-API-Key": PORTAL_KEY},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                user_count = data.get("user_count", 0)
                if user_count != inst.user_count:
                    inst.user_count = user_count
                    logger.info(
                        "_cron_sync_user_count: %s → %d users",
                        inst.tenant_id, user_count,
                    )
            except Exception:
                logger.exception(
                    "_cron_sync_user_count: failed for %s", inst.tenant_id
                )

    @api.model
    def _cron_update_extra_user_line(self):
        """Daily cron: update subscription line qty for extra users.

        For each active SaaS subscription:
        - If extra_users > 0: create or update an "Extra User" line
          with qty = extra_users and price = template.price_per_extra_user
        - If extra_users <= 0: remove any existing "Extra User" line

        This runs daily, ensuring the subscription line reflects the current
        user count before OCA's invoicing cron generates the monthly invoice.
        """
        stage_in_progress = self.env.ref(_STAGE_IN_PROGRESS, raise_if_not_found=False)
        if not stage_in_progress:
            logger.warning(
                "_cron_update_extra_user_line: stage '%s' not found — skipping.",
                _STAGE_IN_PROGRESS,
            )
            return

        active_subs = self.search([
            ("stage_id", "=", stage_in_progress.id),
            ("template_id.is_saas_plan", "=", True),
        ])

        extra_user_product = self.env.ref(
            "odoo_k8s_saas_subscription.product_extra_user",
            raise_if_not_found=False,
        )
        if not extra_user_product:
            logger.error(
                "_cron_update_extra_user_line: missing product "
                "'product_extra_user' — cannot bill extra users."
            )
            return

        logger.info(
            "_cron_update_extra_user_line: processing %d active subscriptions",
            len(active_subs),
        )

        for sub in active_subs:
            extra = sub.extra_users  # computed field
            existing_line = sub.sale_subscription_line_ids.filtered(
                lambda l: l.product_id == extra_user_product
            )

            if extra <= 0:
                # No extra users — remove the billing line if present
                if existing_line:
                    existing_line.unlink()
                    logger.info(
                        "_cron_update_extra_user_line: removed extra-user line "
                        "from subscription %s (no extra users)",
                        sub.display_name,
                    )
                continue

            price = sub.template_id.price_per_extra_user

            if existing_line:
                # Update qty/price only if changed
                if existing_line.product_uom_qty != extra or existing_line.price_unit != price:
                    existing_line.write({
                        "product_uom_qty": extra,
                        "price_unit": price,
                        "name": f"Extra Users ({extra} × {price} Bs./user/month)",
                    })
                    logger.info(
                        "_cron_update_extra_user_line: updated %s → %d extra users "
                        "× %.2f Bs.",
                        sub.display_name, extra, price,
                    )
            else:
                # Create new extra-user line
                self.env["sale.subscription.line"].create({
                    "subscription_id": sub.id,
                    "product_id": extra_user_product.id,
                    "name": f"Extra Users ({extra} × {price} Bs./user/month)",
                    "product_uom_qty": extra,
                    "price_unit": price,
                })
                logger.info(
                    "_cron_update_extra_user_line: created extra-user line for "
                    "%s → %d extra users × %.2f Bs.",
                    sub.display_name, extra, price,
                )

    @api.model
    def cron_subscription_management(self):
        """Override to prevent TypeError if recurring_next_date is False on dirty data"""
        today = fields.Date.today()
        # Fallback filter just in case
        for subscription in self.search([], order="recurring_next_date asc"):
            try:
                subscription = subscription.with_company(subscription.company_id)
                if subscription.in_progress:
                    if (
                        subscription.recurring_next_date
                        and subscription.recurring_next_date <= today
                        and subscription.sale_subscription_line_ids
                    ):
                        try:
                            subscription.generate_invoice()
                        except Exception:
                            logger.exception("Error on subscription invoice generate (Tenant: %s)", subscription.display_name)
                    if (
                        not subscription.recurring_rule_boundary
                        and subscription.date 
                        and subscription.date <= today
                    ):
                        subscription.close_subscription()
                elif (
                    subscription.date_start 
                    and subscription.date_start <= today 
                    and subscription.stage_id.type == "pre"
                ):
                    subscription.action_start_subscription()
                    try:
                        subscription.generate_invoice()
                    except Exception:
                        logger.exception("Error on subscription invoice generate (Tenant: %s)", subscription.display_name)
            except Exception:
                logger.exception("Fatal error processing subscription %s in cron", subscription.id)

