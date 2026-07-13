import logging

from . import controllers

_logger = logging.getLogger(__name__)

# Cookies that must always carry Secure + HttpOnly + SameSite=Lax, regardless of
# whether Odoo detects the request as HTTPS (proxy_mode / X-Forwarded-Proto can be
# lost when traffic arrives via the in-cluster cloudflared tunnel). Narrowly scoped
# so unrelated cookies keep their intended flags.
_HARDENED_COOKIES = {"session_id", "frontend_lang"}


def _harden(cls):
    """Wrap `cls.set_cookie` so hardened cookies always get Secure/HttpOnly/SameSite."""
    if cls is None or getattr(cls, "_saas_secure_cookies_patched", False):
        return False

    _orig_set_cookie = getattr(cls, "set_cookie", None)
    if not callable(_orig_set_cookie):
        return False

    def set_cookie(self, key, value="", *args, **kwargs):
        if key in _HARDENED_COOKIES:
            kwargs["secure"] = True
            kwargs["httponly"] = True
            kwargs.setdefault("samesite", "Lax")
        return _orig_set_cookie(self, key, value, *args, **kwargs)

    cls.set_cookie = set_cookie
    cls._saas_secure_cookies_patched = True
    return True


def _patch_secure_cookies():
    import odoo.http as http

    # Odoo 18 sets the session cookie via request.future_response.set_cookie(...)
    # with only httponly=True (odoo/http.py). Patch FutureResponse (primary path)
    # and Response (fallback for other set_cookie call sites).
    patched = []
    for name in ("FutureResponse", "Response"):
        cls = getattr(http, name, None)
        if _harden(cls):
            patched.append(name)
    if patched:
        _logger.info(
            "saas_security_hardening: session cookies hardened (Secure/HttpOnly/SameSite=Lax) on %s",
            ", ".join(patched),
        )


_patch_secure_cookies()
