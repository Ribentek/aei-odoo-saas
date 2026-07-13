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
    # Unguessable token embedded in the confirmation link. Self-contained — does
    # not rely on Odoo's signup_token (res.partner has no such field in 18.0).
    email_verify_token = fields.Char(
        string="Email Verify Token",
        copy=False,
    )
