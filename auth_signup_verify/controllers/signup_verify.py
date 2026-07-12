import logging

from odoo import _, http
from odoo.http import request
from odoo.addons.auth_signup.controllers.main import AuthSignupHome

_logger = logging.getLogger(__name__)


class AuthSignupVerify(AuthSignupHome):

    # ── Open signup: create deactivated + send confirmation email ────────────
    @http.route()
    def web_auth_signup(self, *args, **kw):
        qcontext = self.get_auth_signup_qcontext()

        # Defer to stock behaviour for: GET (render form), invitation-token
        # signups (admin-initiated, already trusted), and incomplete posts.
        if (
            request.httprequest.method != "POST"
            or qcontext.get("token")
            or not qcontext.get("login")
            or not qcontext.get("password")
        ):
            return super().web_auth_signup(*args, **kw)

        try:
            # do_signup creates the user and authenticates the session.
            self.do_signup(qcontext)
        except Exception as exc:  # noqa: BLE001 — surface as a form error like stock
            _logger.info("auth_signup_verify: signup failed (%s)", exc)
            qcontext["error"] = str(exc)
            return request.render("auth_signup.signup", qcontext)

        user = request.env.user.sudo()
        try:
            token = user.partner_id.signup_prepare()  # fresh signup token
            user.write({"active": False, "email_verified": False})
            self._send_verification_email(user, token)
        finally:
            # Drop the session created by do_signup — no access until verified.
            request.session.logout(keep_db=True)

        return request.render(
            "auth_signup_verify.email_verify_pending",
            {"email": qcontext.get("login")},
        )

    def _send_verification_email(self, user, token):
        base_url = user.get_base_url().rstrip("/")
        verify_url = f"{base_url}/web/email/verify?token={token}"
        body = _(
            "<p>Welcome to AEI Software.</p>"
            "<p>Please confirm your email address to activate your account:</p>"
            "<p><a href=\"%s\">Confirm my email</a></p>"
            "<p>If you did not request this, you can ignore this message.</p>"
        ) % verify_url
        request.env["mail.mail"].sudo().create(
            {
                "subject": _("Confirm your email — AEI Software"),
                "email_to": user.email or user.login,
                "body_html": body,
                "auto_delete": True,
            }
        ).send()

    # ── Confirmation link target ─────────────────────────────────────────────
    @http.route(
        "/web/email/verify",
        type="http",
        auth="public",
        website=True,
        sitemap=False,
        methods=["GET"],
    )
    def email_verify(self, token=None, **kw):
        if not token:
            return request.render("auth_signup_verify.email_verify_result", {"ok": False})

        partner = (
            request.env["res.partner"]
            .sudo()
            .search([("signup_token", "=", token)], limit=1)
        )
        user = (
            request.env["res.users"].sudo().search([("partner_id", "=", partner.id)], limit=1)
            if partner
            else request.env["res.users"]
        )
        if not user:
            return request.render("auth_signup_verify.email_verify_result", {"ok": False})

        user.write({"active": True, "email_verified": True})
        partner.sudo().signup_end()  # consume the token
        _logger.info("auth_signup_verify: email confirmed for user id=%s", user.id)
        return request.render("auth_signup_verify.email_verify_result", {"ok": True})
