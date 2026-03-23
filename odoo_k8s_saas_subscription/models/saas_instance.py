"""
models/saas_instance.py

Extends saas.instance with a link to sale.subscription
and per-user tracking fields.
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

