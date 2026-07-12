import logging

from odoo import _, http
from odoo.http import request
from odoo.addons.auth_signup.controllers.main import AuthSignupHome

_logger = logging.getLogger(__name__)

class SaasAuthSignupHome(AuthSignupHome):

    @http.route()
    def web_auth_reset_password(self, *args, **kw):
        """Normalize the reset-password response to prevent user enumeration.

        For GET (rendering the form) and non-login POSTs, defer to the stock
        controller. For a login-bearing POST, always send the reset silently
        and render the same generic confirmation regardless of outcome.
        """
        qcontext = self.get_auth_signup_qcontext()

        if request.httprequest.method == "POST" and qcontext.get("login"):
            try:
                request.env["res.users"].sudo().reset_password(qcontext.get("login"))
            except Exception as exc:  # noqa: BLE001 — intentionally swallow to avoid disclosure
                _logger.info(
                    "saas_security_hardening: reset_password detail suppressed (%s)", exc
                )
            # Uniform message so existing accounts are indistinguishable from
            # non-existing ones (VULN-0005, CWE-204).
            qcontext["message"] = _(
                "If an account matches that email address, a password reset link has been sent."
            )
            return request.render("auth_signup.reset_password", qcontext)

        return super().web_auth_reset_password(*args, **kw)
