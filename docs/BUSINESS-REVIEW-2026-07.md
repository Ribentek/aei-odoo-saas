# Revisión de negocio y técnica — AEI Odoo SaaS

**Fecha:** 2026-07-08 · **Última actualización:** 2026-07-13 · **Estado:** pre-producción · **Alcance:** modelo de negocio, e-commerce/suscripciones, soporte, control de usuarios, seguridad del tenant, competencia LatAm, costos, limpieza de código.

> Decisiones ya tomadas al cierre de este reporte: SIAT incluida en planes ✓ · horas de soporte **por instancia** (no por usuario) ✓ · fix del bug `user_count = -1` aplicado ✓ · limpieza de repo ejecutada ✓.
>
> **Actualización 2026-07-13** — avances desde el reporte original: módulo de horas de soporte (`odoo_k8s_saas_support`) implementado ✓ · usuario de soporte por instancia ✓ · remediación de pentest 2026-07 (código completo, staging) ✓ · verificación de email en signup ✓ · sitio web de staging con contenido real, SEO y og:image ✓ · wiki migrada a `docs/wiki/` ✓ · T&Cs por defecto en planes ✓ · tenants nuevos arrancan en `es_BO` ✓. Detalle en §§ 3, 5, 8, 10, 11 y roadmap.

---

## 1. Modelo de negocio: instancias Odoo 17/18/19 en SaaS

La plataforma actual (producto e-commerce con `odoo_version` → suscripción OCA → provisioning automático vía portal FastAPI → namespace aislado con NetworkPolicy, ResourceQuota, PDB y backups) supera técnicamente a la mayoría de la oferta regional, que hace implementaciones manuales.

**Recomendaciones:**

1. **Vender "Odoo gestionado", no versiones.** Cada versión extra triplica la matriz de soporte (SIAT, addons y repos deben mantenerse ×3). MVP: **una versión por defecto (18)**, 19 como "early adopter", 17 solo para migraciones entrantes. Odoo soporta las últimas 3 versiones: al salir Odoo 20 (~oct 2026) la 17 queda sin parches de seguridad.
2. **Definir la ruta de upgrade ANTES de vender.** Odoo Community no tiene servicio oficial de migración (el upgrade service es Enterprise-only); la ruta es [OpenUpgrade de OCA](https://github.com/OCA/OpenUpgrade). Decidir: upgrade mayor como servicio pago (facturable con horas de soporte) o incluido 1×/año en Pro+. Documentarlo en el contrato desde el día 1.
3. **Empaquetar por vertical** ("Comercio": POS + inventario + SIAT; "Servicios": CRM + facturación + proyectos). Mismo backend, distinto `install_modules` por producto — el campo ya existe, no requiere código.

## 2. E-commerce y suscripciones

**Funciona bien:** prepago multi-mes en carrito, provisioning idempotente (guards por `sale_order_line_id` y `tenant_id`), dunning 1/3/5 días con suspensión, self-service en portal (cancel/upgrade/backup), facturación automática de usuarios extra.

**Diseño para soporte y paquetes:**

1. ✅ **Derecho base por instancia**: campo `support_hours_included` en `sale.subscription.template` — Starter 2h, Pro 5h, Enterprise 10h mensuales, pooled por instancia, expiran cada mes.
2. ✅ **Paquetes adicionales** implementados en `odoo_k8s_saas_support`: productos con `support_pack_hours`; cada línea de suscripción con ese producto suma `qty × horas` al derecho mensual.
3. **Preferir línea adicional sobre suscripción separada** (una sola factura/mes): botón "Agregar soporte" en el portal que agrega una `sale.subscription.line`, mismo patrón que `_cron_update_extra_user_line`.
4. ✅ **Tarjeta en portal** "Horas incluidas / consumidas / disponibles" (`views/portal_templates.xml` del módulo de soporte).

## 3. Control de horas de soporte — ✅ IMPLEMENTADO (2026-07-08)

Módulo **`odoo_k8s_saas_support`** (depende de `odoo_k8s_saas_subscription` + OCA `helpdesk_mgmt`, vendorizado en `external_addons/`):

- Ticket de helpdesk vinculado a `sale.subscription`; consumo mensual vs `support_hours_included` + paquetes.
- Paquetes de horas como productos (`data/products.xml`) — el guard de provisioning los ignora (`is_saas_plan=False`).
- Vistas en suscripción y **tarjeta de horas en el portal** (`views/portal_templates.xml`).
- Portal de tickets (`/my/tickets`) vía helpdesk_mgmt — canaliza el soporte y reduce carga operativa.
- **Política:** horas no consumidas expiran cada mes (estándar de industria). Documentar en ToS (los templates de plan ya tienen T&Cs por defecto desde 2026-07-08).

## 4. Control de cantidad de usuarios — auditoría

**Mecanismo actual:** el portal consulta la BD del tenant directamente (`res_users` con `share=false, active=true`) — no falsificable desde la UI del cliente. Cron sincroniza a `saas.instance.user_count`; cron diario ajusta la línea de facturación.

**Hallazgos:**

1. ~~**Bug:** `_get_user_count` devuelve `-1` en error de conectividad y la suma lo restaba del total facturable.~~ **CORREGIDO 2026-07-08**: la suma filtra valores negativos y el cron conserva el último conteo válido.
2. **Sin enforcement:** módulo `saas_client` en cada tenant (vía `install_modules`) que bloquee crear usuarios sobre el límite con mensaje "contacta a tu proveedor". → Roadmap.
3. **Facturación por foto puntual:** guardar conteo diario en tabla histórica y facturar el **máximo del período**. → Roadmap.
4. **Orden de crones:** `_cron_update_extra_user_line` debe correr antes que el cron de facturación OCA. Verificar horas en `ir_cron.xml`.

## 5. Cliente como Admin de Odoo — riesgo

**Estructuralmente contenido:** el cliente NO puede instalar código arbitrario (Apps solo muestra módulos del `addons_path`: CE oficial + repos definidos por AEI). Pod no-root UID 101, NetworkPolicy egress limitado a PG y 443, blast radius = su propio namespace/BD. `list_db=False` y master password aleatoria ✓.

| Riesgo operativo | Mitigación |
|---|---|
| "Me rompí la base" | Backups diarios ✓ + runbook de restore rápido; restore cobra horas de soporte |
| Módulos pesados degradan su instancia | Contenido por limits + ResourceQuota ✓ |
| Soporte sin acceso a la instancia | ✅ **RESUELTO (2026-07-13):** usuario de soporte por instancia creado en el provisioning, con contraseña única por tenant registrada en el chatter de `saas.instance` |
| `ir.actions.server` con Python | `safe_eval` + contención del contenedor. Aceptable para MVP |

**No quitar el admin al cliente** — es el diferenciador vs Odoo Online. Guardrails: usuario de soporte + `saas_client` + ToS.

## 6. Competencia LatAm y posicionamiento

| Competidor | Oferta | Precio | Ventaja AEI |
|---|---|---|---|
| Odoo Online | SaaS oficial cerrado | $8.95/usuario/mes (BO) | Sin terceros, sin SIAT nativo, datos fuera, soporte global |
| Odoo.sh | PaaS oficial | ~$593/mes (10 usuarios) | Prohibitivo para pyme |
| CUCU Bolivia | Odoo SaaS + SIAT s/ K8s | Desde Bs 648/mes + Bs 4,500 inicial | Onboarding AEI self-service sin setup fee |
| Sintic / Onixia / Retail Solutions | Módulos SIAT + implementación | Proyecto | Semanas vs 5 minutos |

**Posicionamiento:** *"Tu Odoo funcionando hoy: facturación SIAT incluida, pagos QR, datos y soporte en Bolivia."* Competir por instancia todo-incluido, no por usuario contra Odoo SA.

**Checklist pre-producción** (estado 2026-07-13):

| Ítem | Estado |
|---|---|
| ToS/SLA | 🟡 Parcial — T&Cs por defecto en templates de plan (2026-07-08); falta SLA formal |
| Email transaccional | ✅ Emails de cliente en español, links absolutos, branding AEI; fix del email de credenciales desde webhook anónimo (2026-07-09/10) |
| Datos bolivianos precargados | 🟡 Parcial — tenants nuevos arrancan en `es_BO` (2026-07-13); falta plan de cuentas, IVA 13%, IT 3% |
| Signup verificado | ✅ `auth_signup_verify`: cuenta desactivada hasta confirmar email (2026-07-12) |
| Contenido web + SEO | ✅ En staging — ver §10 |
| Status page | ⏳ Pendiente |
| Smoke test post-provisioning | ⏳ Pendiente |

## 7. Costos a precios de mercado IaaS

La infraestructura física es propia (colocation con intercambio de servicios) — el modelo usa **precios de mercado** para que el negocio sobreviva una migración. Infraestructura virtual: **9 VMs** (3 control 4vCPU/8GB + 3 workers 8vCPU/16GB + 3 PostgreSQL 4vCPU/8GB).

| Escenario | Mensual |
|---|---|
| **Mid-market LatAm (Vultr/DO São Paulo)** — planificar con este | **≈ $630–700** |
| Económico (Hetzner, latencia ~150ms) | ≈ $280–350 |
| AWS São Paulo | ≈ $1,500–1,900 |

**Capacidad:** 3 workers ≈ 42GB asignables → ~40 tenants Starter (0.8–1GB c/u). Worker extra (+$96/mes) → +15 tenants.

| Ocupación | $/tenant/mes | $/usuario/mes (3 u/tenant) |
|---|---|---|
| 10 tenants | $70 | ~$23 |
| 40 tenants | $17.5 | ~$5.8 |
| 100 tenants (+2 workers) | $8.9 | ~$3.0 |

**El costo dominante es soporte, no infra:** a Bs 125/h interno, el tope por instancia (2/5/10h) es lo que hace el modelo operable con poco personal.

**Precios sugeridos** (competitivos vs CUCU, cubren costos desde ~15 tenants):

| Plan | Precio | Incluye |
|---|---|---|
| Starter | Bs 349/mes | 3 usuarios, 10GB, SIAT, 2h soporte |
| Pro | Bs 649/mes | 10 usuarios, 20GB, SIAT, 5h soporte |
| Enterprise | Bs 1,290/mes | 25 usuarios, 50GB, SIAT, 10h soporte |
| Usuario extra | Bs 45–70/mes | — |
| Paquete soporte 5h | Bs 450/mes | — |

⚠️ Los `price_per_extra_user` actuales (5/3/2) siguen siendo placeholders — ajustar antes de producción. Los precios Bs 349/649/1.290 ya están publicados en la página `/pricing` del sitio de staging (§10); falta confirmación formal y carga en los templates.

## 8. Wiki — ✅ MIGRADA A `docs/wiki/` (2026-07-10)

La wiki de GitHub era ilegible desde el navegador (repos privados requieren plan pago para ver la wiki), así que **todo el contenido se migró al repo en `docs/wiki/`** (punto de entrada: `docs/wiki/Home.md`). 24+ páginas: HLD, LLD, runbooks, API reference, QA battery, roadmaps, operación de PostgreSQL, secrets, y la nueva página `Security-Remediation-2026-07.md`. Documentar ahí de aquí en adelante, no en la wiki de GitHub. Los reportes de análisis de negocio (como este) viven en `docs/` directamente.

## 9. Limpieza ejecutada (2026-07-08)

- ✂️ Submodule roto `odoo-saas-mvp.wiki` + `.gitmodules`
- ✂️ Scripts obsoletos de raíz: `restart_staging.sh`, `rollout_status.sh`, `update_modules.sh` (usaba workaround `--http-port=8088` pre-`--no-http`), `apply_changes.sh`, `deploy_rollback.sh`, `deploy_local.sh` — todo documentado en DEPLOY.md
- ✂️ `skills-lock.json` (artefacto de tooling sin carpeta compañera), `.antigravitycli/` (+ gitignore), `*:Zone.Identifier`
- 📦 `SIP DEV RB.postman_collection.json` → movido a `.secrets/` (colección del API bancario, fuera de git)
- ✅ **Conservado:** `setup_cloudflare_wildcard_tunnel.py` — vital: crea wildcard tunnels vía API (imposible desde la web de Cloudflare)
- 📝 CLAUDE.md: eliminada referencia muerta a `tmp-oca/`, agregado enlace a la wiki

## 10. Sitio web y marketing (2026-07-11 → 13)

El sitio de staging (`staging.aeisoftware.com`) pasó de plantilla demo a contenido comercial real:

- **Páginas con contenido definitivo:** home, `/about-us`, `/our-services`, `/pricing` (Bs 349/649/1.290, todo incluido), `/contactus` (WhatsApp +591 73670803), `/privacy` (política alineada a "tus datos se alojan en Bolivia y te pertenecen").
- **SEO configurado en las 6 páginas:** `meta title`, `description` y `keywords` por página, escritos por idioma instalado (`es_BO`) — los campos meta son traducibles y escribir sin contexto de idioma deja valores viejos servidos.
- **og:image por página** con imágenes extraídas del *Manual de Identidad de Marca* (`docs/brand/*.docx`): logo (home, privacy), equipo (about-us), aprovisionamiento automático (services), tríptico de planes (pricing), soporte horario boliviano (contactus). Attachments públicos en la BD de staging — **recrear al promover a producción**.
- **Identidad de marca:** manual + logos + imágenes en `docs/brand/`; favicon y app icons aplicados (2026-07-08).
- Nota: Odoo genera la URL absoluta de og:image con el dominio configurado del website (`www.aeisoftware.com`), incluso en staging.

**Pendiente de insumos del negocio:** NIT de la empresa, links de redes sociales, confirmación formal de precios, nombres reales del equipo para `/about-us`.

## 11. Seguridad — remediación pentest 2026-07 (2026-07-11 → 12)

Dos pentests black-box contra staging/www generaron 12 hallazgos + 1 extra encontrado al revisar código. Detalle completo en `docs/wiki/Security-Remediation-2026-07.md`; resumen ejecutivo:

- **El hallazgo "crítico" de DB Manager estaba sobrevalorado:** `list_db=False` ya estaba activo; riesgo real Medium. Se añadió bloqueo en el edge como defensa en profundidad.
- **Cookies sin Secure/SameSite = una sola causa raíz** (header `X-Forwarded-Proto` no llegaba a Odoo): corregido con `trustedIPs` en Traefik + addon `saas_security_hardening` (auto_install) que fuerza los flags sin importar la ruta de red.
- **Hallazgo EXTRA (no estaba en los informes):** el webhook de pago QR era público y sin verificación — confirmación de pagos sin autenticar. Corregido con token per-transacción en la URL de callback + cron de polling cada 2 min como respaldo (MC4 no firma sus callbacks).
- **Signup verificado:** addon `auth_signup_verify` — la cuenta nace desactivada hasta confirmar email. Requirió **imagen Odoo custom** (`docker/odoo/Dockerfile`, `odoo:18` + `email_validator`) — primera desviación del modelo "imagen oficial + init containers"; la imagen es delgada y rara vez se reconstruye.
- **Edge:** middlewares Traefik de security headers + script idempotente `infra/apply-cf-security-rules.sh` (bloqueo de `/web/database/*`, `/xmlrpc`, `/website/info`; rate-limit en login/signup/reset), compatible con plan Free de Cloudflare, dry-run por defecto.

**Estado:** código completo y mergeado a `main` (staging). **Pendiente:** rollout a producción (`18.0`) con aprobación, ampliar `CF_PROTECTED_HOSTS` a `www`/`admin`, claves Turnstile en el dashboard de Cloudflare, decisión sobre VULN-0004 (enumeración vía imagen de partner — trade-off documentado).

---

## Próximos pasos (roadmap)

1. ~~**Fix bug `user_count = -1`**~~ ✅ hecho (2026-07-08)
2. ~~**Política de soporte: horas por instancia**~~ ✅ decidido e implementado en templates (2/5/10h)
3. ~~**Helpdesk OCA + productos de soporte + tarjeta de horas en portal**~~ ✅ módulo `odoo_k8s_saas_support` (2026-07-08)
4. **Módulo `saas_client`** (límite de usuarios) — ~~usuario `soporte@`~~ ✅ usuario de soporte por instancia hecho (2026-07-13)
5. **Custom domains por cliente** — ver diseño abajo
6. **Precios reales** en templates (los publicados en `/pricing` de staging necesitan confirmación) + SLA formal
7. Histórico diario de user_count + facturación por máximo del período
8. ~~Actualizar wiki~~ ✅ superado: wiki completa migrada a `docs/wiki/` (2026-07-10)
9. **Separación física de ambientes staging / producción** — ver diseño abajo. 🟡 Parcial: ya existe ambiente staging lógico en el clúster compartido (namespace `staging`, deployment `odoo-stg`, BD `staging`, `portal-stg`, dominio `staging.aeisoftware.com`, branch `main`); la separación **física** (clúster propio) sigue pendiente
10. **Rollout de seguridad a producción** — merge a `18.0`, reglas CF en `www`/`admin`, Turnstile, retest (§11)
11. **Promover contenido web de staging a producción** — incluye recrear attachments de og:image en la BD de producción (§10)

### Diseño: Separación de ambientes staging vs producción (paso 9)

> **Estado 2026-07-13:** implementada la separación **lógica** (namespace `staging` con Odoo `odoo-stg`, BD `staging`, `portal-stg` y dominio `staging.aeisoftware.com`, desplegado desde branch `main`; producción en `odoo-admin` desde branch `18.0`). La separación **física** en clúster propio descrita abajo sigue pendiente — la regla operativa del final se mantiene vigente.

**Motivación (incidente 2026-07-08):** hoy staging y producción comparten el mismo clúster K3s y el mismo PostgreSQL HA. Los tenants de ambos entornos viven mezclados en namespaces `odoo-*` indistinguibles, y una operación de limpieza "de staging" alcanzó instancias referenciadas por la BD de producción. El aislamiento por namespace no es suficiente para operaciones destructivas.

**Diseño objetivo:**

| Recurso | Staging (separado) | Producción (actual) |
|---|---|---|
| Clúster K3s | 1 VM single-node (K3s todo-en-uno, 8vCPU/16GB) | 3 control + 3 workers actuales |
| PostgreSQL | 1 VM (sin HA — es staging, 4vCPU/8GB) o Postgres in-cluster | Clúster HA Patroni actual (3 VMs) |
| Dominio | `*.stg.aeisoftware.com` (nuevo wildcard tunnel CF) | `*.aeisoftware.com` |
| Portal | portal-stg en el clúster staging | portal en clúster prod |
| Secrets/API keys | Propios, nunca compartidos con prod | Propios |
| Tenants de prueba | Solo aquí | Solo clientes reales |

**Beneficios:** blast radius cero hacia prod, pruebas destructivas libres (borrar TODO staging sin miedo), upgrade de K3s/Postgres ensayable en staging antes de prod, y el clúster prod recupera los recursos que hoy consume staging.

**Costo:** +2 VMs (~$110–145/mes a precios de mercado; ~gratis en el hardware propio actual). Esfuerzo: ~1 día usando los scripts existentes de `infra/` (install-k3s, wildcard tunnel vía `setup_cloudflare_wildcard_tunnel.py`, apply-manifests con `.secrets.env` propio).

**Regla operativa desde hoy (aún en clúster compartido):** ninguna operación destructiva sobre tenants sin clasificar antes cada recurso contra AMBAS BDs (`staging` y `admin`), y nunca tocar recursos de producción sin permiso explícito.

### Diseño: Custom domains por cliente (paso 5)

Hoy solo hay subdominios `*.aeisoftware.com` vía wildcard tunnel de Cloudflare. Para dominios propios (`www.cliente.com.bo`) la opción que encaja con la arquitectura existente es **Cloudflare for SaaS (Custom Hostnames)**:

1. **Activar Cloudflare for SaaS** en la zona `aeisoftware.com` — 100 custom hostnames gratis, luego ~$0.10/hostname/mes. Cloudflare emite y renueva el certificado TLS del dominio del cliente automáticamente (sin cert-manager, sin exponer Traefik).
2. **Fallback origin:** crear `saas-fallback.aeisoftware.com` apuntando al tunnel existente (una entrada más en la config del tunnel → Traefik).
3. **Flujo por tenant** — nuevo endpoint del portal `PATCH /api/v1/instances/{tenant}/domain`:
   a. Crea el Custom Hostname vía API de Cloudflare (mismo patrón que `setup_cloudflare_wildcard_tunnel.py`), con validación TXT/HTTP.
   b. Parchea el Ingress del tenant agregando la regla `host: www.cliente.com.bo` (mismo Service).
   c. Setea `web.base.url` + `web.base.url.freeze` en la BD del tenant.
4. **Instrucciones al cliente:** CNAME `www` → `saas-fallback.aeisoftware.com` + registro TXT de validación. El `dbfilter` por `db_name` fijo ya soporta multi-host sin cambios.
5. **UI:** campo "Dominio propio" en `/my/subscriptions/<id>` con estado de validación (polling al portal).
6. **Monetización:** add-on Bs 30–50/mes en Pro, incluido en Enterprise.

Alternativa descartada: exponer Traefik públicamente + cert-manager HTTP-01 — rompe el modelo "todo detrás del tunnel" y agrega partes móviles.

---

**Fuentes:** [Odoo Pricing](https://www.odoo.com/pricing) · [OEC.sh pricing por país](https://oec.sh/odoo-pricing) · [CUCU Bolivia](https://cucu.bo/precios) · [Sintic SIAT](https://sinticbolivia.net/productos/facturacion-electronica-siat-odoo/) · [Onixia SIAT](https://www.onixia.com.bo/blog/odoo-erp-5/module-de-facturacion-4) · [Partners Odoo Bolivia](https://www.odoo.com/partners/country/bolivia-28) · [Comparativa VPS](https://getdeploying.com/reference/compute-prices) · [Vultr](https://www.vultr.com/pricing/) · [DigitalOcean](https://www.digitalocean.com/pricing/droplets) · [Hetzner](https://www.hetzner.com/cloud/regular-performance) · [Cloudflare for SaaS](https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/)
