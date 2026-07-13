from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    # Defaults True so existing and admin-created users are never gated.
    # Only the open-signup controller flips this to False for new accounts.
    email_verified = fields.Boolean(
        string="Email Verified",
        default=True,
        copy=False,
    )
