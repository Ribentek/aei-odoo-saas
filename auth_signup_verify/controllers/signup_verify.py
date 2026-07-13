import logging
import secrets

from odoo import _, http, SUPERUSER_ID
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

        # do_signup authenticated the session AS the new user. Deactivating a user
        # while logged in as them raises "You cannot deactivate the user you're
        # currently logged in as" — and .sudo() does NOT change the uid in modern
        # Odoo (it only sets su=True). Use with_user(SUPERUSER_ID) so env.uid is the
        # superuser, which the guard (self._uid in self._ids) allows.
        user = request.env.user.with_user(SUPERUSER_ID)
        verify_token = secrets.token_urlsafe(24)
        try:
            user.write({
                "active": False,
                "email_verified": False,
                "email_verify_token": verify_token,
            })
            self._send_verification_email(user, verify_token)
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

        # Inactive users are excluded by default, so active_test=False is required
        # to find the account we just deactivated. Superuser env for the write.
        user = (
            request.env["res.users"]
            .with_user(SUPERUSER_ID)
            .with_context(active_test=False)
            .search([("email_verify_token", "=", token)], limit=1)
        )
        if not user:
            return request.render("auth_signup_verify.email_verify_result", {"ok": False})

        # Activate and consume the token (single use).
        user.write({"active": True, "email_verified": True, "email_verify_token": False})
        _logger.info("auth_signup_verify: email confirmed for user id=%s", user.id)
        return request.render("auth_signup_verify.email_verify_result", {"ok": True})
