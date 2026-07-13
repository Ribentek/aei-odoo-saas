# Auditoría de Producción

> **Navegación:** [← Production Cloud Environment](Production-Cloud-Environment) | [Roadmap →](Roadmap-Hardening)

Auditoría de seguridad y estabilidad del cluster de producción. Basada en el análisis de [@Pothoko](https://github.com/Pothoko/aei-odoo-saas/wiki) y verificada en producción.

**Última actualización:** 2026-04-24

---

## Resumen

| Severidad | Total | Resueltos | Pendientes |
|:----------|:-----:|:---------:|:----------:|
| 🔴 Crítico | 3 | 3 | 0 |
| 🟠 Alto | 7 | 6 | 1 |
| 🟡 Medio | 8 | 8 | 0 |

---

## 🔴 Hallazgos Críticos (Resueltos)

### ✅ PgBouncer no autentica roles de tenants — SUPERSEDED
**Causa raíz (3 problemas combinados):**
1. `auth_type = md5` incompatible con PG16 `password_encryption = scram-sha-256`
2. `* = host=127.0.0.1 port=5432` → PgBouncer en réplicas conectaba a PostgreSQL read-only
3. `pgbouncer.user_lookup()` no existía en las BDs de tenants (solo en `postgres`)

**Fix original:** SCRAM auth + HAProxy backend + user_lookup() en template1.

**Resolución definitiva (2026-04-11):** PgBouncer eliminado de la arquitectura. Todo el tráfico va directo via HAProxy:5000 → Primary. `max_connections` escalado a 800. PgBouncer deshabilitado (`systemctl disable pgbouncer`) en los 3 nodos.

### ✅ DDL operations via PgBouncer transaction mode — SUPERSEDED
`CREATE ROLE` / `CREATE DATABASE` fallaban en `pool_mode=transaction`.

**Resolución definitiva (2026-04-11):** Sin PgBouncer, no existe restricción de pool mode. `_pg_conn()` y `_pg_admin_conn()` usan ambos port 5000 (HAProxy directo). LISTEN/NOTIFY funciona nativamente → longpolling habilitado sin `bus_alt_connection`.

### ✅ CI/CD pipeline roto
El job `deploy-portal` fallaba por `KUBECONFIG` stale.

**Fix:** Eliminado el deploy automático. Build-only CI, deploy manual via `kubectl`.

---

## 🟠 Hallazgos Altos

### ✅ #3 — Contraseñas en ConfigMaps (texto plano)
ConfigMaps ahora usan `REPLACE_*` placeholders. Valores inyectados via init-container `render-config` con `secretKeyRef`. *Resuelto.*

### ⚠️ #4 — ClusterRole excesivo del portal
El portal tiene `ClusterRole` con permisos amplios. Parcialmente mitigado: se verificó que solo tiene los recursos necesarios (namespaces, deployments, services, configmaps, secrets, pvc, networkpolicies, pdb). Downgrade a `Role` namespaced no es viable por design multi-namespace.

**Mitigación:** RBAC auditado y verificado en `k8s/04-rbac.yaml`. Solo ServiceAccounts de `aeisoftware` y `staging` tienen binding.

### ✅ #5 — apply_manifest solo CREATE, nunca UPDATE
Se agregaron handlers para `NetworkPolicy` (replace en 409) y `PodDisruptionBudget`. *Resuelto.*

### ✅ #12 — SQL Injection en helpers de PostgreSQL
6 f-strings DDL reemplazados con `psycopg2.sql.Identifier()`. *Resuelto.*

### ✅ #15 — pgBackRest backups no funcionales
**Descubierto 2026-04-10:** pgBackRest estaba configurado pero no funcionaba — 564 intentos fallidos, 0 WAL archivados.

**Causa raíz:** stunnel (TLS proxy) no había sido instalado. pgBackRest intentaba HTTPS directo a RadosGW (HTTP-only) → `TLS error: wrong version number`.

**Fix aplicado en los 3 nodos PG:**
1. Instalado stunnel4 con cert self-signed
2. Configurado proxy: `HTTPS://127.0.0.1:18480 → HTTP://10.40.1.240:7480`
3. Actualizado `pgbackrest.conf`: endpoint de `10.40.1.240:7480` → `127.0.0.1:18480`
4. Creada stanza + ejecutado primer full backup (273.6MB → 34.6MB comprimido)
5. WAL archiving activo: disco bajó de 16GB a 2.9GB

### ✅ #16 — Sin monitoring centralizado
**Descubierto 2026-04-10:** `install-monitoring.sh` nunca fue ejecutado. Sin Prometheus, Grafana ni log aggregation.

**Fix aplicado:**
- Instalado kube-prometheus-stack: Prometheus (20Gi) + Grafana (5Gi) + AlertManager (2Gi)
- Instalado Loki + Promtail para log aggregation (10Gi)
- Configurados 32 scrape targets (todos UP: K3s, PG exporters, Patroni, node-exporters)
- Corregidos `postgres_exporter` custom queries (conflictos de métricas duplicadas)
- Creado Ingress para Grafana (`grafana.aeisoftware.com`)

---

## 🟡 Hallazgos Medios

| # | Hallazgo | Estado | Fix |
|:--|:---------|:-------|:----|
| #2 | Sin TLS entre Cloudflare y cluster | ✅ Resuelto | `websecure` entrypoint, Cloudflare SSL = Full (Strict) |
| #6 | Git clone sin autenticación | ✅ Resuelto | GitHub PAT via `GIT_TOKEN` secret + env var en init-container `clone-addons` (`aed4c51`, `3b4ad2d`) |
| #8 | Sin liveness probes (pods zombie) | ✅ Resuelto | `livenessProbe` en portal, admin, staging, tenants |
| #9 | cloudflared:latest no reproducible | ✅ Resuelto | Pineado a `2026.3.0` |
| #10 | Sin PodDisruptionBudget | ✅ Resuelto | PDB `minAvailable: 1` para portal y odoo-admin |
| #11 | Portal con 1 worker Uvicorn | ✅ Resuelto | `--workers 4` |
| #14 | odoo-admin sin NetworkPolicy | ✅ Resuelto | `06b-odoo-admin-netpol.yaml` default-deny + whitelist |
| #17 | `list_db = True` en admin | ✅ Resuelto | Confirmado `list_db = False` en `k8s/06-odoo-admin.yaml` línea 49 y `k8s/07-staging.yaml` línea 68 |
| #18 | healthz no verifica PostgreSQL | ✅ Resuelto | `/readyz` verifica PG (`connect_timeout=2s`) + K8s API → 503 si alguno falla. `/healthz` queda como liveness-only. `readinessProbe` apunta a `/readyz` en `05-portal.yaml` y `07-staging.yaml` (`03381b1`) |

---

## Bugs de Código

| Bug | Severidad | Archivo | Descripción | Estado |
|:----|:----------|:--------|:------------|:-------|
| BUG-01 | 🔴 | `saas_sale.py` | `subscription_id` referenciado pero no definido | ✅ Resuelto |
| BUG-02 | 🟠 | `sale_subscription_line.py` | Falta `continue` después de `name = False` | ✅ Resuelto |
| BUG-03 | 🟠 | `k8s_utils/client.py` | `lru_cache` no recarga credenciales K8s | ✅ Resuelto |
| BUG-04 | 🟠 | `k8s_utils/manifests.py` | NetworkPolicy usa puertos incorrectos | ✅ Resuelto |
| BUG-05 | 🟡 | `k8s_utils/client.py` | `apply_manifest` no soporta `NetworkPolicy` kind | ✅ Resuelto |

### Bugs Descubiertos en QA (2026-04-21/23)

| Bug | Severidad | Archivo | Descripción | Commit | Estado |
|:----|:----------|:--------|:------------|:-------|:-------|
| BUG-06 | 🟠 | `payment_qr_mercantil/controllers/main.py` | Ruta webhook declarada como `type='json'` → devolvía HTTP 200 en errores de auth y forzaba JSON en respuestas que debían ser HTTP puro | `78d5e3b` | ✅ Resuelto |
| BUG-07 | 🟠 | `odoo_k8s_saas_subscription/models/sale_subscription.py` | `action_stop()` llamado sobre instancias ya en estado `suspended` al cerrar suscripción → error de transición inválida | `45ef414` | ✅ Resuelto |
| BUG-08 | 🟠 | `k8s/` manifests + `portal/routers/instances.py` | `PodDisruptionBudget minAvailable: 1` creado para instancias de tenants → impedía `scale=0` en stop y re-creación en start | `bdada0d` | ✅ Resuelto |
| BUG-09 | — | OCA `sale_subscription` / `generate_invoice()` | `recurring_next_date` muestra `date_start+2m` después de la primera factura — **comportamiento correcto de OCA**: `recurring_next_date` siempre apunta a la *próxima* factura pendiente. El avance observado en QA fue por llamada manual a `generate_invoice()` durante las pruebas C2, no por el cron. Flujo normal del cron es correcto. | — | ✅ No es bug |

---

## Bugs Descubiertos en Ejecución (2026-04-10)

| Bug | Causa | Fix | Commit |
|:----|:------|:----|:-------|
| Tenants no inician (PVC pending) | `STORAGE_CLASS=local-path` en staging, cluster solo tiene `ceph-rbd` | Cambiar env var en `07-staging.yaml` | `0dab3a2` |
| Portal 500 en provision (403 netpol) | `ClusterRole` sin permiso `networkpolicies` | Agregar resource al role | `b2a05c7` |
| Portal 500 en provision (403 namespace) | `ClusterRoleBinding` solo vinculaba SA `aeisoftware` | Agregar subject `staging:saas-portal` | `023bf31` |
| KeyError `pending_delete` en close | `state` field con `tracking=True` pero BD no conocía nuevo valor | Module upgrade `-u odoo_k8s_saas` | manual |
| pgBackRest 564 failures | stunnel no instalado; pgBackRest intentaba HTTPS a RadosGW HTTP | Instalar stunnel, reconfig endpoint | manual |
| postgres_exporter HTTP 500 | Custom queries duplicaban métricas built-in; `odoo_replication_lag` sin `master: true` | Reescribir `queries.yaml` con prefijo `odoo_` y `master: true` | manual |

---

## Mejoras Propuestas

| # | Archivo | Descripción | Estado |
|:--|:--------|:------------|:-------|
| MEJORA-01 | `routers/instances.py` | `GET /api/v1/instances/list` — listar todos los tenants | ✅ |
| MEJORA-02 | `k8s_utils/manifests.py` | `APP_ADMIN_PASSWORD` via env, no via shell | ✅ |
| MEJORA-03 | `saas_instance.py` | Mínimo 2 chars en slug del tenant_id | ✅ |

---

> **Fuente original:** [Pothoko Wiki — Auditoría de Producción](https://github.com/Pothoko/aei-odoo-saas/wiki/Auditoria-Produccion)
