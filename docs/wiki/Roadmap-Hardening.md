# Roadmap: Hardening & Bug Fixes

> **Navegación:** [← Auditoría](Auditoria-Produccion) | [Home →](Home)

Plan de implementación para resolver los hallazgos de la auditoría y bugs de código, organizado en 4 fases con prioridad decreciente.

**Última actualización:** 2026-04-11 — PgBouncer eliminado. Fases 0–4 ejecutadas en `main` (staging). Producción (`18.0`) intacta hasta merge.

---

## Fase 0 — Crítico ✅ (Completado 2026-04-09)

| Item | Cambio | Archivo |
|:-----|:-------|:-------|
| ✅ ~~PgBouncer SCRAM auth~~ | **SUPERSEDED** — PgBouncer eliminado (2026-04-11) | ~~`03-setup-pgbouncer.sh`~~ |
| ✅ ~~PgBouncer → HAProxy:5000~~ | **SUPERSEDED** — Todo tráfico va directo a HAProxy:5000 | ~~`03-setup-pgbouncer.sh`~~ |
| ✅ ~~user_lookup() en template1~~ | **SUPERSEDED** — Sin PgBouncer no se necesita auth_query | ~~`03-setup-pgbouncer.sh`~~ |
| ✅ ~~DDL via port 5000~~ | **SUPERSEDED** — Todo usa port 5000 sin bypass | `instances.py` |
| ✅ STORAGE_CLASS=ceph-rbd | Storage class correcto para producción | `05-portal.yaml` |
| ✅ PG16 GRANT fix | `GRANT role TO odoo` antes de `CREATE DATABASE` | `instances.py` |
| ✅ CI/CD deploy removed | Build-only, deploy manual | `ci.yaml` |

---

## Fase 1 — Seguridad ✅ (Completado 2026-04-10)

### 1.1 Contraseñas fuera de ConfigMaps ✅
ConfigMaps usan `REPLACE_*` placeholders, values inyectados via init-container `render-config` con `secretKeyRef`.

### 1.2 SQL Injection Hardening ✅ `d4c5faa`
6 f-strings DDL reemplazados con `psycopg2.sql.Identifier()`.

### 1.3 BUG-01 — Campo `subscription_id` ✅
Resuelto. El campo existe y funciona para auto-provisioning desde ventas.

### 1.4 RBAC Restringido ⚠️ Parcial
ClusterRole es amplio pero necesario para provisioning multi-namespace. Se agregó `networkpolicies`, `poddisruptionbudgets`. Binding incluye SAs de `aeisoftware` + `staging`.

---

## Fase 2 — Estabilidad ✅ (Completado 2026-04-10)

### 2.1 Liveness Probes ✅ `d4c5faa`
Probes en portal, odoo-admin, staging, y template de tenants.

### 2.2 Portal Multi-worker ✅ `d4c5faa`
`--workers 1` → `--workers 4`

### 2.3 apply_manifest con UPDATE ✅
Handlers para `NetworkPolicy` (replace en 409) y `PodDisruptionBudget`.

### 2.4 BUG-03 — lru_cache en K8s client ✅ `d4c5faa`
Module-level singletons con `_config_loaded` guard.

### 2.5 BUG-02 — Missing `continue` ✅

### 2.6 `/readyz` con DB + K8s Check ✅ (2026-04-16)
`GET /readyz` implementado en `portal/main.py`: verifica conexión a PostgreSQL (`SELECT 1`) y a la Kubernetes API, retorna `503` si alguna falla. Usado como readiness probe en `05-portal.yaml` y `07-staging.yaml`.

### 2.7 PodDisruptionBudgets ✅ `d4c5faa`
`minAvailable: 1` para portal y odoo-admin.

---

## Fase 3 — Hardening ✅ (Completado 2026-04-10)

### 3.1 TLS/HTTPS ✅
Cloudflare SSL = Full (Strict).

### 3.2 NetworkPolicy odoo-admin ✅ `d4c5faa`
`06b-odoo-admin-netpol.yaml`: default-deny + allow Traefik, Portal, egress PG + DNS.

### 3.3 Odoo Admin Hardening ✅
`list_db = False` verificado en ConfigMap.

### 3.4 Cloudflared Pinned ✅ `d4c5faa`
`:latest` → `:2026.3.0`

### 3.5 Git Clone con Auth ✅ (2026-04-16)
`GIT_TOKEN` inyectado via `secretKeyRef` con `optional: true` en todos los init containers (`06-odoo-admin.yaml`, `07-staging.yaml`, tenant manifests). Auth activo para repos privados sin bloquear repos públicos.

### 3.6 BUG-04 & BUG-05 — NetworkPolicy ✅
`apply_manifest()` maneja `NetworkPolicy` y `PodDisruptionBudget`.

---

## Fase 4 — Mejoras & Operaciones (Completado 2026-04-10)

### 4.1 MEJORA-01 — GET /api/v1/instances/list ✅ (2026-04-16)
Endpoint `GET /api/v1/instances/list` implementado en `portal/routers/instances.py:129`. Lista todos los namespaces `odoo-*` activos con status y user_count opcional.

### 4.2 MEJORA-02 — APP_ADMIN_PASSWORD via env ✅

### 4.3 MEJORA-03 — Slug mínimo 2 chars ✅ `d4c5faa`

### 4.4 ~~bus_alt_connection para Longpolling~~ ✅ Resuelto (2026-04-11)
~~OCA module para compatibilidad con PgBouncer transaction mode.~~

**Resolución:** PgBouncer eliminado de la arquitectura. LISTEN/NOTIFY funciona nativamente con HAProxy:5000. Longpolling (chat, Discuss, notificaciones) habilitado sin necesidad de OCA module.

### 4.5 pgBackRest WAL Archiving ✅ (2026-04-10)
**Descubierto:** 564 intentos fallidos, 0 WAL archivados. stunnel no instalado.

**Fix:**
- Instalado stunnel4 en 3 nodos PG (10.40.2.182, .174, .193)
- Proxy: `HTTPS://127.0.0.1:18480 → HTTP://10.40.1.240:7480`
- Stanza creada + primer full backup: 273.6MB → 34.6MB (zstd+AES-256)
- WAL archiving activo, cron configurado (full dom 2AM, diff lun-sáb 2AM)

### 4.6 Monitoring Stack ✅ (2026-04-10)
**Descubierto:** `install-monitoring.sh` nunca ejecutado. Sin observabilidad.

**Fix:**
- kube-prometheus-stack: Prometheus (20Gi) + Grafana (5Gi) + AlertManager (2Gi)
- Loki + Promtail: log aggregation (10Gi)
- 32 scrape targets, todos UP (K3s, PG exporters, Patroni, node-exporters)
- postgres_exporter queries corregidos en 3 nodos PG
- Ingress Grafana: `grafana.aeisoftware.com`

---

## Bugs descubiertos durante ejecución (2026-04-10)

| Bug | Causa | Fix | Commit |
|:----|:------|:----|:-------|
| Tenants no inician (PVC pending) | `STORAGE_CLASS=local-path` en staging | Cambiar env var en `07-staging.yaml` | `0dab3a2` |
| Portal 500 (403 netpol) | `ClusterRole` sin permiso `networkpolicies` | Agregar resource al role | `b2a05c7` |
| Portal 500 (403 namespace) | `ClusterRoleBinding` solo SA `aeisoftware` | Agregar SA `staging` | `023bf31` |
| KeyError `pending_delete` | BD no conocía nuevo state value | Module upgrade | manual |
| pgBackRest 564 failures | stunnel no instalado, HTTPS→HTTP mismatch | Instalar stunnel, reconfig | manual |
| postgres_exporter 500 | Queries custom duplican métricas built-in | Reescribir con prefijo `odoo_` | manual |

---

## Fase 5 — Remediación Pentest 2026-07 🚧 (en `security/pentest-remediation-2026-07`)

Respuesta a dos pentests black-box (informes en `strix/`). Detalle completo, matriz de findings y
verificación en **[Security-Remediation-2026-07](Security-Remediation-2026-07)**.

Resumen de código implementado (staging primero):
- Traefik `security-headers` (HSTS/X-Frame-Options/CSP/Referrer-Policy) + `trustedIPs` Cloudflare.
- `infra/apply-cf-security-rules.sh`: bloqueo edge de `/web/database/*` `/xmlrpc/*` `/jsonrpc`
  `/website/info` + rate-limit de auth.
- Addon `saas_security_hardening`: flags de cookie (Secure/HttpOnly/SameSite) + reset sin enumeración.
- Addon `auth_signup_verify`: verificación de email en signup abierto (Turnstile en el edge CF).
- HMAC en el webhook de `payment_qr_mercantil` (hallazgo extra, no reportado).

Pendiente: aplicar/validar en staging, verificar CVEs Odoo + CSRF `/contactus`, decidir sobre
enumeración de partner images, promover a producción.

---

## Resumen de estado

| Fase | Total Items | Completados | Pendientes |
|:-----|:-----------|:-----------|:-----------|
| Fase 0 — Crítico | 7 | 7 ✅ | 0 |
| Fase 1 — Seguridad | 4 | 4 ✅ | 0 |
| Fase 2 — Estabilidad | 7 | 7 ✅ | 0 |
| Fase 3 — Hardening | 6 | 6 ✅ | 0 |
| Fase 4 — Mejoras & Ops | 6 | 6 ✅ | 0 |
| Fase 5 — Remediación Pentest 2026-07 | 8 | 6 código 🚧 | deploy + verif |
| **Total** | **38** | **36** | **2** |

---

## Verificación

```bash
# pgBackRest
ssh ubuntu@10.40.2.174 'sudo -u postgres pgbackrest --stanza=odoo-saas info'

# Monitoring
kubectl -n monitoring get pods
kubectl -n monitoring exec prometheus-kube-prom-kube-prometheus-prometheus-0 \
  -c prometheus -- wget -qO- http://localhost:9090/api/v1/targets | \
  python3 -c "import sys,json;d=json.load(sys.stdin);print(f'{sum(1 for t in d[\"data\"][\"activeTargets\"] if t[\"health\"]==\"up\")} targets UP')"

# Grafana
kubectl -n monitoring port-forward svc/kube-prom-grafana 3000:80
# → http://localhost:3000  admin/AeiMonitor2026
```
