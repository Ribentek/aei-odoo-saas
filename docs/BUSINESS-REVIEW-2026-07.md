# Revisión de negocio y técnica — AEI Odoo SaaS

**Fecha:** 2026-07-08 · **Estado:** pre-producción · **Alcance:** modelo de negocio, e-commerce/suscripciones, soporte, control de usuarios, seguridad del tenant, competencia LatAm, costos, limpieza de código.

> Decisiones ya tomadas al cierre de este reporte: SIAT incluida en planes ✓ · horas de soporte **por instancia** (no por usuario) ✓ · fix del bug `user_count = -1` aplicado ✓ · limpieza de repo ejecutada ✓.

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

1. **Derecho base por instancia** (implementado): campo `support_hours_included` en `sale.subscription.template` — Starter 2h, Pro 5h, Enterprise 10h mensuales, pooled por instancia, expiran cada mes.
2. **Paquetes adicionales** como producto suscribible ("Paquete Soporte 5h/mes", template con `is_saas_plan=False` — el guard de provisioning ya los ignora). ⚠️ `create_subscription` en `sale_order.py` interpreta qty>1 como meses prepagados: excluir productos de soporte de esa lógica.
3. **Preferir línea adicional sobre suscripción separada** (una sola factura/mes): botón "Agregar soporte" en el portal que agrega una `sale.subscription.line`, mismo patrón que `_cron_update_extra_user_line`.
4. **Tarjeta en portal:** "Horas incluidas / consumidas / disponibles" (llega con la integración helpdesk, ver §3).

## 3. Control de horas de soporte

**Recomendación: OCA `helpdesk_mgmt` + `helpdesk_mgmt_timesheet`** (rama 18.0 verificada) en la **instancia admin**:

- Ticket vinculado a partner + Many2one a `sale.subscription`.
- Técnicos registran tiempo en el ticket (timesheet estándar).
- Consumo mensual = suma de horas de timesheets en tickets del mes vs `support_hours_included` + paquetes.
- Exceso → línea "Horas adicionales" en la suscripción (mismo patrón del cron de usuarios extra) u oferta de paquete en portal.
- Portal de tickets incluido (`/my/tickets`) — canaliza el soporte y reduce carga operativa.
- **Política:** horas no consumidas expiran cada mes (estándar de industria). Documentar en ToS.

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
| Soporte sin acceso a la instancia | **Gap:** crear usuario `soporte@aeisoftware.com` admin en `odoo-init`, credencial en Secret K8s, divulgado en ToS → Roadmap |
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

**Checklist pre-producción:** status page, ToS/SLA (horas, upgrades, retención post-cancelación — grace period 7 días ya existe), smoke test post-provisioning, email transaccional (SPF/DKIM), datos bolivianos precargados (plan de cuentas, IVA 13%, IT 3%).

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

⚠️ Los `price_per_extra_user` actuales (5/3/2) son placeholders — ajustar antes de producción.

## 8. Wiki

La wiki vive en `https://github.com/Ribentek/aei-odoo-saas/wiki` (23 páginas: HLD, LLD, runbooks, API reference, QA battery, roadmaps). Último commit 2026-05-15 — incluye la migración a Ribentek y `--no-http`, pero **le faltan los cambios de junio** del módulo de pagos (`ssl_verify`, URL dev MC4, validación de códigos del banco). El submodule local roto (apuntaba al repo viejo `jpvargassoruco/odoo-saas-mvp.wiki`) fue eliminado en la limpieza.

## 9. Limpieza ejecutada (2026-07-08)

- ✂️ Submodule roto `odoo-saas-mvp.wiki` + `.gitmodules`
- ✂️ Scripts obsoletos de raíz: `restart_staging.sh`, `rollout_status.sh`, `update_modules.sh` (usaba workaround `--http-port=8088` pre-`--no-http`), `apply_changes.sh`, `deploy_rollback.sh`, `deploy_local.sh` — todo documentado en DEPLOY.md
- ✂️ `skills-lock.json` (artefacto de tooling sin carpeta compañera), `.antigravitycli/` (+ gitignore), `*:Zone.Identifier`
- 📦 `SIP DEV RB.postman_collection.json` → movido a `.secrets/` (colección del API bancario, fuera de git)
- ✅ **Conservado:** `setup_cloudflare_wildcard_tunnel.py` — vital: crea wildcard tunnels vía API (imposible desde la web de Cloudflare)
- 📝 CLAUDE.md: eliminada referencia muerta a `tmp-oca/`, agregado enlace a la wiki

---

## Próximos pasos (roadmap)

1. ~~**Fix bug `user_count = -1`**~~ ✅ hecho (2026-07-08)
2. ~~**Política de soporte: horas por instancia**~~ ✅ decidido e implementado en templates (2/5/10h)
3. **Helpdesk OCA + productos de soporte + tarjeta de horas en portal** (~2-3 días)
4. **Módulo `saas_client`** (límite de usuarios) + usuario `soporte@` en odoo-init
5. **Custom domains por cliente** — ver diseño abajo
6. **Precios reales** en templates + ToS/SLA
7. Histórico diario de user_count + facturación por máximo del período
8. Actualizar wiki (página Payment-QR-Mercantil con ssl_verify) y publicar este reporte

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
