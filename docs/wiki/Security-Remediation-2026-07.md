# Security Remediation — Pentest 2026-07

> **Navegación:** [← Roadmap Hardening](Roadmap-Hardening) | [Auditoría](Auditoria-Produccion) | [Home →](Home)

Remediación de los hallazgos de dos pentests black-box (2026-07-11/12, informes en `strix/`)
contra `staging.aeisoftware.com` / `www.aeisoftware.com` (portal Odoo 18 SaaS). Rollout
**staging primero** (`main`), luego producción (`18.0`) con aprobación.

**Branch:** `security/pentest-remediation-2026-07`

---

## Reencuadre de severidad (tras revisar el código)

- **VULN-0001 "DB Manager (9.8)" está sobrevalorado.** `list_db = False` ya estaba en todos los
  configs; el PoC del propio informe muestra POST → "Access Denied". El mensaje "disabled by
  administrator" es el comportamiento real de `list_db=False`. Riesgo real ≈ Medium. Se añade solo
  defensa en profundidad (bloqueo en el edge).
- **VULN-0006/0008/0011 (cookies) = una sola causa raíz.** Odoo marca `Secure`+`SameSite=Lax` cuando
  detecta HTTPS (`proxy_mode` + `X-Forwarded-Proto`). El header no llegaba efectivamente a Odoo. Se
  corrige con `trustedIPs` en Traefik **y** con enforcement determinista a nivel Odoo (addon), que
  funciona sin importar la ruta de red (túnel cloudflared in-cluster).
- **Hallazgo EXTRA (no en el informe):** el webhook `/payment/qr_mercantil/webhook` era `auth='public'`,
  `csrf=False`, **sin verificación de firma** → confirmación de pagos sin autenticar. Corregido con HMAC.

---

## Matriz de remediación

| VULN | Título | Sev | Capa del fix | Estado |
|:-----|:-------|:----|:-------------|:-------|
| 0001 | DB Manager sin auth | Crit→Med | `list_db=False` (ya) + bloqueo edge/Traefik | ✅ código |
| 0006 | Cookie sin Secure | Crit | Traefik `trustedIPs` + addon cookie enforcement | ✅ código |
| 0007 | Sin rate-limit en auth | High | Cloudflare rate-limit rules + Traefik backstop | ✅ código |
| 0002 | Signup abierto sin verificación | Med | Addon `auth_signup_verify` + Turnstile (CF edge) | ✅ código |
| 0010 | Faltan security headers | Med | Traefik `security-headers` middleware | ✅ código |
| 0012 | Disclosure K8s vía XMLRPC | Med | Bloqueo edge `/xmlrpc` `/jsonrpc` | ✅ código |
| 0011 | Cookie sin SameSite | Med | Igual que 0006 | ✅ código |
| 0003 | Disclosure `/website/info` | Med | Bloqueo edge `/website/info` | ✅ código |
| 0004 | Enumeración vía partner image | Med | Documentado — trade-off, riesgo residual | ⏳ decisión |
| 0005 | Enumeración en reset password | Med | Addon `saas_security_hardening` (respuesta genérica) | ✅ código |
| 0008 | `frontend_lang` sin HttpOnly/Secure | Med | Addon cookie enforcement | ✅ código |
| 0009 | Sin métodos de pago | Info | Config, no vuln | — |
| — | Webhook de pago sin firma | Extra | HMAC en `payment_qr_mercantil` | ✅ código |
| Rec9 | CVEs Odoo antiguos | Med | Verificar build 18.0-20260630 + Trivy en CI | ⏳ verificar |
| Rec7 | CSRF en `/contactus` | Med | Verificar token CSRF (stock website) | ⏳ verificar |

---

## Cambios por artefacto

**Traefik (`k8s/`)**
- `03-traefik-middleware.yaml`: nuevos middlewares `security-headers` (response: HSTS, X-Frame-Options,
  Referrer-Policy, X-Content-Type-Options, CSP `frame-ancestors 'self'`) en `kube-system` y
  `aeisoftware`; `block-sensitive-paths` (plugin, opcional — ver nota).
- `01-traefik.yaml`: `forwardedHeaders.trustedIPs` con rangos Cloudflare (staging).
- Ingresses (`05-portal`, `06-odoo-admin`, `07-staging`, `prod/06-odoo-admin-cloud`): anexan
  `security-headers`; portal API además `portal-ratelimit`.
- `portal/k8s_utils/manifests.py`: ingress per-tenant anexa `SECURITY_HEADERS_MIDDLEWARE`;
  env añadido en `05-portal.yaml`.

**Cloudflare (`infra/apply-cf-security-rules.sh`, nuevo)**
- Rulesets API (idempotente, PUT). No hay Terraform; este script es la fuente de verdad.
- Bloqueo (403): `/web/database/*`, `/xmlrpc/*`, `/jsonrpc`, `/website/info`.
- Rate-limit: `/web/login`, `/web/signup`, `/web/reset_password` (20 req / 60 s / IP, block 600 s).
- Scope por `CF_PROTECTED_HOSTS` (default: solo staging). Añadir `www`/`admin` tras validar.
- Turnstile en `/web/signup` se configura en el dashboard de Cloudflare (edge), no requiere código.

**Odoo addons**
- `saas_security_hardening` (auto_install): fuerza `Secure/HttpOnly/SameSite=Lax` en cookies
  `session_id`/`frontend_lang` (monkeypatch acotado de `Response.set_cookie`); reset password con
  respuesta genérica.
- `auth_signup_verify` (install manual): signup abierto crea la cuenta **desactivada** hasta confirmar
  email; Odoo bloquea logins inactivos de forma nativa (no se parchea el auth). Signups por invitación
  (token de admin) omiten la verificación.
- `payment_qr_mercantil/controllers/main.py`: webhook valida HMAC-SHA256 del body contra
  `payment_qr_mercantil.webhook_secret` (ir.config_parameter) o `QR_WEBHOOK_SECRET` (env); rechaza 401.
  Fail-closed: sin secreto configurado, el webhook se rechaza y el pago se confirma vía el polling
  autenticado de `/status`.

> **Nota `block-sensitive-paths`:** el bloqueo primario es Cloudflare. El middleware Traefik requiere
> un plugin de path-block (`denyrequest`/`blockpath`); si no está instalado, no se referencia desde los
> ingresses y se depende del edge. Instalar el plugin es opcional (cubre acceso directo al clúster).

---

## Secretos / configuración requerida antes de habilitar

| Clave | Dónde | Para |
|:------|:------|:-----|
| `QR_WEBHOOK_SECRET` (o `ir.config_parameter payment_qr_mercantil.webhook_secret`) | Secret Odoo / env | HMAC webhook pago |
| `CF_API_TOKEN`, `CF_ZONE_ID` | env al correr el script CF | Reglas WAF/rate-limit |
| Turnstile site/secret keys | Dashboard Cloudflare | CAPTCHA signup (edge) |

---

## Verificación (staging)

```bash
SSH="ssh -i .secrets/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158"

# Headers
curl -sI https://staging.aeisoftware.com/web/login | grep -iE 'strict-transport|x-frame|content-security|referrer-policy'
# Cookies
curl -sI https://staging.aeisoftware.com/web/login | grep -i set-cookie   # session_id → Secure; HttpOnly; SameSite=Lax
# Paths bloqueados (tras aplicar reglas CF)
for p in /web/database/manager /xmlrpc/2/common /website/info; do
  echo -n "$p -> "; curl -s -o /dev/null -w '%{http_code}\n' https://staging.aeisoftware.com$p; done   # 403
# Rate-limit
for i in $(seq 1 30); do curl -s -o /dev/null -w '%{http_code}\n' https://staging.aeisoftware.com/web/login; done   # 429 tras ~20
# Webhook sin firma
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://staging.aeisoftware.com/payment/qr_mercantil/webhook -H 'Content-Type: application/json' -d '{}'   # 401
```

## Rollout

1. `main` → aplicar manifests + addons a staging (`kubectl rollout restart`, `-u <addon> --no-http`).
2. Correr `infra/apply-cf-security-rules.sh` con `CF_PROTECTED_HOSTS="staging.aeisoftware.com"`.
3. Retest completo (arriba).
4. Con aprobación: merge a `18.0`, ampliar `CF_PROTECTED_HOSTS` a `www`/`admin`, retest en prod.
