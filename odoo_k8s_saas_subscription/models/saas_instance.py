"""
models/saas_instance.py

Extends saas.instance with a link to sale.subscription,
per-user tracking fields, dunning state, and grace-period tracking.
"""
from odoo import models, fields


class SaasInstance(models.Model):
    _inherit = "saas.instance"

    subscription_id = fields.Many2one(
        "sale.subscription",
        string="Subscription",
        ondelete="set null",
        tracking=True,
        help="Recurring subscription that manages billing for this instance.",
    )
    sale_order_line_id = fields.Many2one(
        "sale.order.line",
        string="Sale Order Line",
        ondelete="set null",
        help="Specific order line that triggered this instance's creation.",
    )
    subscription_stage = fields.Char(
        string="Subscription Stage",
        related="subscription_id.stage_id.name",
        readonly=True,
    )
    user_count = fields.Integer(
        string="Active Users",
        default=0,
        tracking=True,
        help="Current number of active users (synced from the portal API).",
    )
    max_users = fields.Integer(
        string="Max Included Users",
        related="subscription_id.template_id.included_users",
        readonly=True,
        help="Maximum users included in the plan before extra charges apply.",
    )

    # ── Dunning fields ───────────────────────────────────────────
    dunning_level = fields.Integer(
        string="Dunning Level",
        default=0,
        tracking=True,
        help="Payment dunning escalation level.\n"
             "0 = current (no overdue)\n"
             "1 = first warning sent (+1 day overdue)\n"
             "2 = final warning sent (+3 days overdue)\n"
             "3 = suspended (+5 days overdue)",
    )
    dunning_last_sent = fields.Date(
        string="Last Dunning Email",
        help="Date when the last dunning notification was sent.",
    )

    # ── Grace-period tracking ────────────────────────────────────
    closed_date = fields.Datetime(
        string="Closed Date",
        tracking=True,
        help="Timestamp when the linked subscription was closed. "
             "Used to enforce a grace period before permanent deletion.",
    )

