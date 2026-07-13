{
    "name": "Signup Email Verification",
    "version": "18.0.1.0.0",
    "summary": "Require email confirmation before an open-signup account can log in",
    "description": """
Hardens Odoo's open self-service signup (VULN-0002).

Flow for open signups (no invitation token):
1. The account is created but deactivated (active=False) and marked
   email_verified=False.
2. A confirmation link is emailed to the address used at signup.
3. Clicking the link activates the account; only then can the user log in
   (Odoo natively blocks inactive logins, so no auth internals are patched).

Invitation-based signups (admin-issued token) are trusted and skip verification.
Bot protection is handled at the Cloudflare edge via Turnstile on /web/signup.
""",
    "author": "AEI Software",
    "website": "https://aeisoftware.com",
    "category": "Authentication",
    "license": "LGPL-3",
    "depends": ["auth_signup", "website", "mail"],
    "data": [
        "views/email_verify_templates.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
