import logging

from . import controllers

_logger = logging.getLogger(__name__)

# Cookies that must always carry Secure + HttpOnly + SameSite=Lax, regardless of
# whether Odoo detects the request as HTTPS (proxy_mode / X-Forwarded-Proto can be
# lost when traffic arrives via the in-cluster cloudflared tunnel). Narrowly scoped
# so unrelated cookies keep their intended flags.
_HARDENED_COOKIES = {"session_id", "frontend_lang"}


def _patch_secure_cookies():
    from odoo.http import Response

    if getattr(Response, "_saas_secure_cookies_patched", False):
        return

    _orig_set_cookie = Response.set_cookie

    def set_cookie(self, key, value="", *args, **kwargs):
        if key in _HARDENED_COOKIES:
            kwargs["secure"] = True
            kwargs["httponly"] = True
            kwargs.setdefault("samesite", "Lax")
        return _orig_set_cookie(self, key, value, *args, **kwargs)

    Response.set_cookie = set_cookie
    Response._saas_secure_cookies_patched = True
    _logger.info("saas_security_hardening: session cookies hardened (Secure/HttpOnly/SameSite=Lax)")


_patch_secure_cookies()
