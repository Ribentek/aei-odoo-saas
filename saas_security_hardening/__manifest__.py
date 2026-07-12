{
    "name": "SaaS Security Hardening",
    "version": "18.0.1.0.0",
    "summary": "Cross-cutting security hardening for the AEI Odoo SaaS portal",
    "description": """
Remediation of black-box pentest findings (2026-07):

* Forces Secure + HttpOnly + SameSite=Lax on session_id / frontend_lang cookies
  (VULN-0006 / VULN-0008 / VULN-0011), independent of the reverse-proxy path.
* Removes user-enumeration from the password-reset flow — the response is the
  same whether or not an account exists (VULN-0005).

Path-level exposure (DB manager, /xmlrpc, /website/info — VULN-0001/0003/0012)
is blocked at the edge; see infra/apply-cf-security-rules.sh and
k8s/03-traefik-middleware.yaml.
""",
    "author": "AEI Software",
    "website": "https://aeisoftware.com",
    "category": "Tools",
    "license": "LGPL-3",
    "depends": ["base", "web", "auth_signup"],
    "installable": True,
    "application": False,
    "auto_install": True,
}
