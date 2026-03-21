"""
models/sale_subscription_template.py

Extends sale.subscription.template with per-user pricing fields.
Each plan defines the number of included users and the price
for each additional user.
"""
from odoo import fields, models


class SaleSubscriptionTemplate(models.Model):
    _inherit = "sale.subscription.template"

    included_users = fields.Integer(
        string="Included Users",
        default=1,
        help="Number of users included in the base price of this plan.",
    )
    price_per_extra_user = fields.Float(
        string="Price per Extra User",
        digits="Product Price",
        default=0.0,
        help="Monthly price charged for each user beyond the included amount.",
    )
