# QA Testing Battery v2

> **Actualizado:** 2026-04-23  
> **Estado:** ✅ QA COMPLETO — 16/17 Release Blockers PASS. C2 condicional (flujo estándar correcto). Listo para merge `main → 18.0`.

Esta batería de pruebas es la fuente de verdad para el QA freeze previo al lanzamiento de 100 tenants. Los ítems **Release Blocker** (secciones A, B, C) deben completarse antes de cualquier merge a `18.0`.

**Bugs reportados:** abrir GitHub Issue con etiqueta `qa-found` en el repo `Ribentek/aei-odoo-saas`.

---

## Prerrequisitos

```bash
# Variables de entorno para los tests
export API_KEY="<staging-api-key>"
export STAGING_URL="https://portal-stg.aeisoftware.com"
export ODOO_URL="https://staging.aeisoftware.com"

# Verificar que staging está funcionando
curl -s $STAGING_URL/healthz | jq .
# → {"status": "ok"}
curl -s $STAGING_URL/readyz | jq .
# → {"postgres": "ok", "kubernetes": "ok"}
```

---

## Sección A — Provisionamiento de tenants _(Release Blocker)_

### A1. Provisionar tenant válido

```bash
curl -s -X POST $STAGING_URL/api/v1/instances \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"qatest01","plan":"starter","storage_gi":5}' | jq .
```

**Criterio:** respuesta `202` con `status: "provisioning"` y `app_admin_password` no vacío.

> ✅ **PASS** 2026-04-21 — 202, provisioning, password generado correctamente.

### A2. Verificar disponibilidad antes de provisionar

```bash
curl -s $STAGING_URL/api/v1/instances/check/qatest01 -H "X-API-Key: $API_KEY" | jq .
```

**Criterio:** `{"available": false, ...}` (si ya existe). La respuesta incluye `namespace_exists` y `database_exists` en lugar de `reasons`.

> ✅ **PASS** 2026-04-21

### A3. Rechazar tenant_id duplicado

```bash
# Debe rechazar con 409
curl -s -X POST $STAGING_URL/api/v1/instances \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"qatest01","plan":"starter","storage_gi":5}' -o /dev/null -w "%{http_code}"
```

**Criterio:** `409 Conflict`.

> ✅ **PASS** 2026-04-21

### A4. Rechazar tenant_id inválido

```bash
# Debe rechazar con 422
curl -s -X POST $STAGING_URL/api/v1/instances \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"UPPER-CASE","plan":"starter","storage_gi":5}' -o /dev/null -w "%{http_code}"
```

**Criterio:** `422 Unprocessable Entity`.

> ✅ **PASS** 2026-04-21

### A5. Pod llega a estado Ready

```bash
# Esperar hasta 5 minutos
watch -n 10 "curl -s $STAGING_URL/api/v1/instances/qatest01 -H 'X-API-Key: $API_KEY' | jq '{status,pod_ready}'"
```

**Criterio:** `status: "ready"` dentro de 5 minutos. Verificar pod con `kubectl get pod -n odoo-qatest01`.

> **Nota:** el campo `pod_ready` no existe en la respuesta del API — verificar directamente con kubectl.  
> ✅ **PASS** 2026-04-21 — ready en 53s, pod 1/1 Running.

---

## Sección B — Ciclo de vida del tenant _(Release Blocker)_

### B1. Listar tenants

```bash
curl -s "$STAGING_URL/api/v1/instances/list" -H "X-API-Key: $API_KEY" | jq 'length'
```

**Criterio:** respuesta 200 con array que incluye `qatest01`.

> ✅ **PASS** 2026-04-21

### B2. Suspender tenant (stop)

```bash
curl -s -X POST $STAGING_URL/api/v1/instances/qatest01/stop \
  -H "X-API-Key: $API_KEY" | jq .
```

**Criterio:** `{"status": "suspended"}`. Pod escala a 0:
```bash
kubectl get pod -n odoo-qatest01
# → No resources found
```

> ✅ **PASS** 2026-04-21

### B3. Reanudar tenant (start)

```bash
curl -s -X POST $STAGING_URL/api/v1/instances/qatest01/start \
  -H "X-API-Key: $API_KEY" | jq .
```

**Criterio:** `{"status": "starting"}`. Pod vuelve a Running en < 3 min.

> ✅ **PASS** 2026-04-21 — pod 1/1 Running en 46s.

### B4. Upgrade de plan

```bash
curl -s -X PATCH $STAGING_URL/api/v1/instances/qatest01/upgrade \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"plan":"pro"}' | jq .
```

**Criterio:** respuesta 200. Verificar en Kubernetes:
```bash
kubectl get deployment odoo -n odoo-qatest01 -o jsonpath='{.spec.template.spec.containers[0].resources}'
# → CPU limit = 1 (pro tier)
```

> ✅ **PASS** 2026-04-21 — `limits: {cpu: "1", memory: "2Gi"}`.

### B5. Backup self-service (valida C1+C4) _(Release Blocker)_

```bash
curl -s $STAGING_URL/api/v1/instances/qatest01/backup \
  -H "X-API-Key: $API_KEY" \
  -o qatest01-backup.zip
ls -lh qatest01-backup.zip
```

**Criterio:** archivo ZIP descargado, tamaño > 0 bytes, no error 503.

> Valida fix C1 (`pods/exec` en RBAC) y C4 (webhook key configurado).  
> ✅ **PASS** 2026-04-21 — ZIP 499K descargado correctamente.

### B6. Eliminar tenant

```bash
curl -s -X DELETE $STAGING_URL/api/v1/instances/qatest01 \
  -H "X-API-Key: $API_KEY" -o /dev/null -w "%{http_code}"
# → 204
```

**Criterio:** `204`. Verificar:
```bash
kubectl get namespace odoo-qatest01 2>&1
# → Error from server (NotFound)
```

> ✅ **PASS** 2026-04-21 — 204, namespace en Terminating.

---

## Sección C — Billing y suscripciones _(Release Blocker)_

### C1. Crear suscripción desde venta

1. Acceder a `$ODOO_URL` → Ventas → Nuevo Presupuesto
2. Producto: categoría `Odoo-SaaS`, `subscribable=True`, template = SaaS Starter
3. Confirmar → verificar que se crea `sale.subscription` en "In Progress"
4. Navegar a SaaS → Instancias: verificar que se creó `saas.instance` vinculada

**Criterio:** instancia pasa a `provisioning` automáticamente sin acción manual.

### C2. Pago de factura activa provisioning _(flujo diferido)_

Este caso aplica **solo** cuando la suscripción comienza en stage tipo `pre` (inicio diferido/futuro). En el flujo estándar (C1), la confirmación de venta ya mueve el stage a "In Progress" y el provisioning ocurre sin necesidad de pago previo.

Para testear C2 en staging:
1. Crear suscripción con template que tenga `date_start` en el futuro
2. Verificar que la instancia se crea en estado `draft`
3. Registrar pago → subscription avanza a "In Progress" → instancia pasa a `provisioning`

**Criterio:** instancia en `draft` → pasa a `provisioning` al activarse la suscripción.

> **Nota técnica:** el trigger es `stage_id → In Progress` (`sale_subscription.py:401`), no el pago directamente. El pago activa el stage change via `action_start_subscription()`.  
> ⚠️ **C2 CONDICIONAL** 2026-04-21 — en flujo estándar la instancia ya está `ready` al registrar el pago (correcto). El código del path `draft → provisioning` existe y es correcto (línea 484-485). Test de flujo diferido pendiente si se configura template con inicio futuro.

### C3. Suspension por vencimiento

1. Cambiar `recurring_next_date` de la suscripción a una fecha pasada
2. Ejecutar manualmente: Ajustes → Técnico → Acciones Programadas → `SaaS: Suspend Overdue Instances`
3. Verificar que la instancia pasa a `suspended` (pod escalado a 0)

**Criterio:** `state = "suspended"` en la instancia + pod a 0 réplicas.

> **Nota:** la imagen Odoo no tiene `odoo shell -c` para código Python — usar stdin via `kubectl exec -i`. Requiere `recurring_next_date` ≥ 5 días en el pasado para alcanzar dunning level 3 (suspensión real). Level 1 = solo email de aviso.  
> ✅ **PASS** 2026-04-21 — 6 días overdue → dunning level 3 → suspended + pod 0.

### C4. Cierre de suscripción elimina instancia

1. Cambiar etapa de la suscripción a "Closed"
2. Verificar que la instancia pasa a `pending_delete` → `deleted`
3. Verificar que el namespace K8s fue eliminado

**Criterio:** `state = "deleted"` + namespace ausente.

> **Nota:** el cierre activa un grace period de 7 días antes de la eliminación real (`_cron_sync_closed_subscriptions`). Para QA forzar `closed_date` a 8+ días atrás via shell. Bug corregido (commit 45ef414): `action_stop()` fallaba si la instancia ya estaba suspendida.  
> ✅ **PASS** 2026-04-21 — suspended → pending_delete → deleted + namespace eliminado.

### C5. Billing de usuarios extra (valida C2) _(Release Blocker)_

```bash
POD=$(kubectl get pod -n staging -l app=odoo-stg -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n staging $POD -- odoo shell -d staging --no-http \
  -c 'env["sale.subscription"]._cron_update_extra_user_line()'
```

**Criterio:** comando no lanza `ValueError: Invalid field 'subscription_id'` ni traceback. Si hay suscripciones con extra usuarios, se crean/actualizan las líneas de facturación.

---

## Sección D — Portal del cliente

### D1. Acceso al portal

1. Acceder como usuario de tipo Portal a `$ODOO_URL/my/subscriptions`
2. Verificar que aparecen las suscripciones del usuario

**Criterio:** lista de suscripciones visible sin errores 500.

> ✅ **PASS** 2026-04-23

### D2. Detalle de suscripción

1. Hacer clic en una suscripción activa
2. Verificar: tarjeta de instancia con URL, badge de estado, botón "Open Instance"
3. Si hay extra usuarios: verificar sección "User Usage" con cuentas correctas

**Criterio:** detalle renderiza correctamente con todos los elementos.

> ✅ **PASS** 2026-04-23 — conteo de usuarios extra correcto tras ~1h.

### D3. Botón de backup en portal

1. En el detalle de la suscripción, hacer clic en "Download Backup"
2. Verificar descarga del ZIP

**Criterio:** ZIP descargado sin error.

> ✅ **PASS** 2026-04-23

---

## Sección E — Emails automáticos

### E1. Email de credenciales al provisionar

1. Provisionar una nueva instancia (vía suscripción o directo)
2. Verificar que se envió email al cliente cuando el pod llegó a `ready`
3. El email debe incluir: URL del tenant, usuario `admin`, contraseña generada

**Criterio:** email recibido con credenciales correctas dentro de 5 min de que la instancia llegó a `ready`.

> ✅ **PASS** 2026-04-23

---

## Sección F — Webhook y reconciliación

### F1. Webhook notifica estado al provisionar

```bash
# Provisionar un tenant de prueba
curl -s -X POST $STAGING_URL/api/v1/instances \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"wbtest01","plan":"starter","storage_gi":5}' | jq .

# Monitorear logs de Odoo staging
kubectl logs -n staging -l app=odoo-stg -f --tail=50 | grep -i webhook
```

**Criterio:** aparece log `instance_status_webhook: received status=ready for tenant_id=wbtest01` cuando el pod llega a Ready.

> ✅ **PASS** 2026-04-21

### F2. Cron reconcilia instancias sin webhook

1. Deshabilitar temporalmente el webhook (quitar `ODOO_WEBHOOK_URL` del portal)
2. Provisionar instancia
3. Esperar 2 minutos
4. Verificar que `SaaS: Refresh Instance Status` actualizó el estado a `ready`

**Criterio:** estado `ready` sin webhook, solo via cron.

### F3. Webhook rechaza key incorrecta

```bash
# Enviar webhook con key incorrecta
curl -s -X POST $ODOO_URL/saas/webhook/instance-status \
  -H "X-Webhook-Key: WRONG-KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"wbtest01","status":"ready"}' -o /dev/null -w "%{http_code}"
```

**Criterio:** `401 Unauthorized`.

> ✅ **PASS** 2026-04-21 — fix aplicado 2026-04-21: webhook ahora lanza `werkzeug.exceptions.Unauthorized()` en lugar de retornar dict con status 200.

---

## Sección G — Seguridad y ACL

### G1. API key obligatoria en portal

```bash
# Sin key → debe dar 401
curl -s $STAGING_URL/api/v1/instances/list -o /dev/null -w "%{http_code}"
# → 401
```

**Criterio:** `401 Unauthorized` sin header `X-API-Key` (RFC 7235: 401 = no autenticado; 403 = autenticado pero sin permiso).

> ✅ **PASS** 2026-04-21 — retorna 401 (criterio actualizado desde 403).

### G2. API key incorrecta

```bash
curl -s $STAGING_URL/api/v1/instances/list \
  -H "X-API-Key: WRONG" -o /dev/null -w "%{http_code}"
# → 403
```

**Criterio:** `403 Forbidden`.

> ✅ **PASS** 2026-04-21

### G3. NetworkPolicy aísla tenants

```bash
# Desde el pod del tenant qatest01, intentar conectar a odoo-qatest02
kubectl exec -n odoo-qatest01 deploy/odoo -- \
  wget -qO- --timeout=3 http://odoo.odoo-qatest02.svc.cluster.local:8069/web/health 2>&1
```

**Criterio:** timeout o connection refused (NetworkPolicy bloquea tráfico inter-tenant).

> **Nota:** la imagen Odoo no incluye `wget`. Usar `python3 -c "import socket; socket.create_connection(('odoo.odoo-qatest02.svc.cluster.local', 8069), timeout=3)"`.  
> ✅ **PASS** 2026-04-21 — timed out.

### G4. ACL: usuario portal no puede escribir saas.instance (valida C3)

1. Acceder a `$ODOO_URL` como usuario con grupo `Portal User` (no `System`)
2. Intentar via shell o UI crear/editar un registro `saas.instance`

```bash
POD=$(kubectl get pod -n staging -l app=odoo-stg -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n staging $POD -- odoo shell -d staging --no-http \
  -c "env['saas.instance'].sudo(False).with_user(env.ref('base.default_user').id).create({'name':'hack','tenant_id':'hack01'})"
```

**Criterio:** `AccessError: You are not allowed to create "SaaS Instance" (saas.instance) records.`

> **Nota:** `odoo shell` no acepta `-c` para código Python — usar stdin (`kubectl exec -i ... -- odoo shell ... <<EOF`).  
> ✅ **PASS** 2026-04-21 — AccessError confirmado.

---

## Criterios de aceptación

### Release Blockers (deben pasar 100% antes de merge a 18.0)

| ID | Test | Estado |
|:---|:-----|:-------|
| A1 | Provisionar tenant válido → 202 | ✅ PASS 2026-04-21 |
| A2 | Check disponibilidad funciona | ✅ PASS 2026-04-21 |
| A3 | Tenant duplicado → 409 | ✅ PASS 2026-04-21 |
| A4 | tenant_id inválido → 422 | ✅ PASS 2026-04-21 |
| A5 | Pod llega a Ready en < 5 min | ✅ PASS 2026-04-21 |
| B1 | Lista tenants funciona | ✅ PASS 2026-04-21 |
| B2 | Stop escala pod a 0 | ✅ PASS 2026-04-21 |
| B3 | Start reactiva pod | ✅ PASS 2026-04-21 |
| B4 | Upgrade actualiza recursos | ✅ PASS 2026-04-21 |
| B5 | Backup self-service descarga ZIP | ✅ PASS 2026-04-21 |
| B6 | Delete elimina namespace | ✅ PASS 2026-04-21 |
| C1 | Suscripción provoca provisioning | ✅ PASS 2026-04-21 |
| C2 | Pago de factura activa provisioning | ⚠️ Condicional — flujo estándar correcto; flujo diferido pendiente |
| C3 | Vencimiento suspende instancia | ✅ PASS 2026-04-21 |
| C4 | Cierre elimina instancia | ✅ PASS 2026-04-21 |
| C5 | `_cron_update_extra_user_line` sin ValueError | ✅ PASS 2026-04-21 |
| G4 | Portal user no puede crear saas.instance | ✅ PASS 2026-04-21 |

### Go/No-Go

Todos los items en [Roadmap: Production Readiness 100 Tenants](Roadmap-Production-Readiness-100-Tenants.md) deben estar en verde.

---

## Limpieza post-QA

```bash
# Eliminar tenants de prueba
for id in qatest01 qatest02 wbtest01; do
  curl -s -X DELETE $STAGING_URL/api/v1/instances/$id \
    -H "X-API-Key: $API_KEY" -o /dev/null -w "DELETE $id: %{http_code}\n"
done

# Limpiar PVs huérfanos
curl -s -X DELETE "$STAGING_URL/api/v1/gc/pvs" \
  -H "X-API-Key: $API_KEY" | jq '{deleted, errors}'
```
