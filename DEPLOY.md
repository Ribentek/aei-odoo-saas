# DEPLOY — odoo-saas-mvp

## Entorno de producción

| Elemento | Valor |
|---|---|
| Namespace Odoo admin | `odoo-admin` |
| Deployment | `odoo-admin` |
| Label selector | `app=odoo-admin` |
| Base de datos Odoo admin | `postgres` (filtro: `^admin$`) |
| Namespace portal / postgres | `aeisoftware` |
| Deployment portal | `portal` |
| Imagen portal | `ghcr.io/aei-software/aei-odoo-saas/portal:latest` |
| Repo en initContainer | `https://github.com/AEI-Software/aei-odoo-saas.git` (branch `18.0`, `--depth=1`) |
| Addons copiados | `payment_qr_mercantil`, `odoo_k8s_saas`, `odoo_k8s_saas_subscription` (del repo principal) + `subscription_oca` (local en `external_addons/`) |
[cert-manager]: https://cert-manager.io/

---

## Entorno de staging

| Elemento | Valor |
|---|---|
| Namespace Odoo staging | `staging` |
| Deployment Odoo | `odoo-stg` |
| Label selector | `app=odoo-stg` |
| Base de datos Odoo staging | `staging` |
| Namespace / deployment portal | `staging` / `portal-stg` |
| Dominio | staging.aeisoftware.com |
| Repo en initContainer | `https://github.com/AEI-Software/aei-odoo-saas.git` (branch `main`, `--depth=1`) |

> **Nunca mezclar comandos:** namespace `staging` usa branch `main`; namespace `odoo-admin` usa branch `18.0`.

---

## Creación de Custom Images (SaaS Tenants)

Si un cliente requiere Odoo con módulos pre-instalados (baked-in), la imagen de Docker debe cumplir estas dos reglas principales para no colisionar con la infraestructura de inicialización nativa del SaaS:

1. **Ruta de Addons Aislada:** Los addons custom deben copiarse obligatoriamente dentro de `/opt/custom-addons` durante el paso `RUN` del Dockerfile. No ubicar los archivos en `/mnt/extra-addons` ni otras rutas por omisión de Odoo, ya que Kubernetes monta de manera forzosa un volumen `emptyDir` allí para su `initContainer`, lo que sobreescribiría silenciosamente todas las capas instaladas.
2. **Entrypoint Wrapper Dinámico:** Es imperativo utilizar un script de entrada (ej: `entrypoint-custom.sh`) que extienda el entrypoint original de Odoo sumando la inyección local `--addons-path="...,/opt/custom-addons"`. *Importante:* Desde Odoo 19 en adelante, cualquier ruta de un `odoo.conf` inyectada de manera global que resulte no existir en un contenedor, detonará una excepción fatal `FileNotFoundError`; por esto mismo, la inyección del path custom debe realizarse siempre desde el argumento CLI dentro de la propia imagen custom.

> **Incidente (2026-07-08, corregido 2026-07-09):** El commit `4096902` agregó `/opt/odoo/symlinked_addons` al `addons_path` **global** de `configmap_manifest()` (`portal/k8s_utils/manifests.py`), violando la regla #2 de arriba con una ruta que además no coincide con la convención documentada (`/opt/custom-addons`) y que ninguna imagen ni initContainer crea. Como esa ruta no existe en la imagen estándar `odoo:18`, cada request de archivo estático (`get_static_file`/`statics` en `odoo/http.py`, que hace `os.listdir()` sobre *todas* las entradas de `addons_path`) fallaba con `FileNotFoundError` → 500, rompiendo íconos y assets estáticos en **todos** los tenants, no solo los que usan imagen custom. Ya existía precedente idéntico en el commit `5cd8cce` ("remove fatal /opt/addons global path"), que había corregido el mismo error con `/opt/custom-addons`. Se eliminó la entrada; la única forma soportada de exponer addons pre-instalados sigue siendo el patrón de las reglas #1 y #2 (imagen custom + CLI arg, nunca en `odoo.conf` global).

---

## Flujo de despliegue estándar — Producción (branch `18.0`)

```bash
# 1. Commit y push del código (a 18.0, tras validar en staging)
git add <archivos>
git commit -m "tipo(módulo): descripción"
git push origin 18.0

# 2. Restart — el initContainer clona el repo actualizado automáticamente
kubectl rollout restart deployment/odoo-admin -n odoo-admin

# 3. Esperar a que el pod esté Running
kubectl rollout status deployment/odoo-admin -n odoo-admin
```

> **No hay CI/CD automático para odoo-admin.** El restart debe hacerse manualmente después del push.
> Ningún módulo se auto-actualiza — el container Odoo inicia sin flag `-u`.

---

## Flujo de despliegue — Staging (branch `main`)

```bash
# 1. Commit y push del código a main
git add <archivos>
git commit -m "tipo(módulo): descripción"
git push origin main

# 2. Restart — el initContainer clona main automáticamente
kubectl rollout restart deployment/odoo-stg -n staging

# 3. Esperar a que el pod esté Running
kubectl rollout status deployment/odoo-stg -n staging
```

> Todo cambio va primero a `main`/staging, se valida ahí, y luego se mergea a `18.0` para producción.
> Igual que en producción: ningún módulo se auto-actualiza, el container inicia sin flag `-u`.

---

## Cuando hay cambios de esquema BD (campos nuevos en modelos)

> ⚠️ Obligatorio tras agregar o renombrar `fields.*` en cualquier modelo Odoo.

### Producción (namespace `odoo-admin`, DB `admin`)

```bash
# 1. Obtener nombre del pod (tras el rollout restart)
POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')

# 2. Actualizar el módulo afectado
kubectl exec -n odoo-admin $POD -- \
  odoo -u payment_qr_mercantil -d admin --stop-after-init --no-http

# 3. Para actualizar TODOS los módulos del repo:
kubectl exec -n odoo-admin $POD -- \
  odoo -u payment_qr_mercantil,odoo_k8s_saas,odoo_k8s_saas_subscription,subscription_oca \
  -d admin --stop-after-init --no-http

# 4. Restart limpio tras el update
kubectl rollout restart deployment/odoo-admin -n odoo-admin
kubectl rollout status deployment/odoo-admin -n odoo-admin
```

### Staging (namespace `staging`, DB `staging`)

```bash
# 1. Obtener nombre del pod (tras el rollout restart)
POD=$(kubectl get pod -n staging -l app=odoo-stg -o jsonpath='{.items[0].metadata.name}')

# 2. Actualizar el módulo afectado
kubectl exec -n staging $POD -- \
  odoo -u payment_qr_mercantil -d staging --stop-after-init --no-http

# 3. Para actualizar TODOS los módulos del repo:
kubectl exec -n staging $POD -- \
  odoo -u payment_qr_mercantil,odoo_k8s_saas,odoo_k8s_saas_subscription,subscription_oca \
  -d staging --stop-after-init --no-http

# 4. Restart limpio tras el update
kubectl rollout restart deployment/odoo-stg -n staging
kubectl rollout status deployment/odoo-stg -n staging
```

> El flag `--no-http` es obligatorio en ambos entornos: sin él falla con `[Errno 98] Address already in use`
> porque el proceso principal ya ocupa el puerto 8069.

---

## Portal FastAPI

El portal **sí** tiene CI automático via GitHub Actions ([`ci.yaml`](../.github/workflows/ci.yaml)).
En cada push a `main`: build + push de la imagen a GHCR. El deploy del portal es **manual** tras el push.

```bash
# Si necesitas forzar un restart manual del portal
kubectl rollout restart deployment/portal -n aeisoftware
kubectl rollout status deployment/portal -n aeisoftware
```

---

## Verificar logs en tiempo real

```bash
# Odoo admin (producción)
POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n odoo-admin $POD -f --tail=100

# Odoo staging
kubectl logs -n staging -l app=odoo-stg -f --tail=100

# Portal FastAPI (producción)
kubectl logs -n aeisoftware deployment/portal -f --tail=100

# Portal FastAPI (staging)
kubectl logs -n staging deployment/portal-stg -f --tail=100

# PostgreSQL
kubectl logs -n aeisoftware statefulset/postgres -f --tail=50
```

---

## Módulos del repo

| Módulo | Update en restart | Descripción |
|---|---|---|
| `payment_qr_mercantil` | Manual | Pago por QR — Banco Mercantil Santa Cruz (mc4.com.bo) |
| `odoo_k8s_saas` | Manual | UI admin de instancias SaaS sobre K8s |
| `odoo_k8s_saas_subscription` | Manual | Bridge suscripciones OCA ↔ SaaS instances |
| `subscription_oca` | Manual | Contratos recurrentes (local en `external_addons/`) |

---

## Diagnóstico rápido

```bash
# Estado general de pods
kubectl get pods -n odoo-admin
kubectl get pods -n staging
kubectl get pods -n aeisoftware

# Describir pod (ver errores de initContainer)
kubectl describe pod -n odoo-admin <pod-name>
kubectl describe pod -n staging <pod-name>

# Verificar secrets aplicados
kubectl get secrets -n odoo-admin
kubectl get secrets -n staging
kubectl get secrets -n aeisoftware

# PVCs
kubectl get pvc -n odoo-admin
kubectl get pvc -n staging

# IngressRoutes
kubectl get ingress -n odoo-admin
kubectl get ingress -n staging
kubectl get ingress -n aeisoftware
```

---

## Backup y Restauración

### Arquitectura del backup

| CronJob | Horario (La_Paz) | Retención | Destino S3 |
|---------|-----------------|-----------|------------|
| `pg-logical-dump` | 03:30 diario | 7d daily · 4w weekly · 3mo monthly | `s3://pg-backups/pgdump/<db>/<fecha>.dump` |
| `filestore-dump` | 04:00 diario | 7d daily · 4w weekly | `s3://pg-backups/filestore/<db>/<fecha>.tgz` |
| `backup-prune` | 05:00 domingos | — | elimina objetos S3 fuera de retención |

> **Limitación conocida:** tenants suspendidos (pod escalado a 0) no generan backup de filestore.
> La BD sí se respalda desde PostgreSQL HA (via HAProxy :5001). El filestore se recupera del último backup antes de la suspensión.

### Verificar estado de backups

```bash
# Ver últimos 3 jobs de pg_dump
kubectl get jobs -n backup-system -l app=pg-logical-dump --sort-by=.metadata.creationTimestamp

# Ver logs del último job pg_dump
kubectl logs -n backup-system -l app=pg-logical-dump --tail=50

# Ver logs del último job filestore
kubectl logs -n backup-system -l app=filestore-dump --tail=50

# Listar dumps disponibles en S3 (desde un pod con aws-cli o kubectl run)
kubectl run -it --rm awscli --image=amazon/aws-cli --restart=Never -- \
  --endpoint-url http://10.40.1.240:7480 \
  s3 ls s3://pg-backups/ --recursive
```

### Restaurar una base de datos (pg_dump)

```bash
# 1. Identificar el dump a restaurar
#    Formato: pgdump/<db>/<YYYY-MM-DD>.dump
DB=odoo_acme                  # o 'admin'
FECHA=2026-04-14
S3_KEY="pgdump/${DB}/${FECHA}.dump"

# 2. Descargar el dump desde S3 a un pod temporal
kubectl run -it --rm pg-restore \
  --image=postgres:16-alpine \
  --env="PGPASSWORD=<superuser-password>" \
  --env="AWS_ACCESS_KEY_ID=<key>" \
  --env="AWS_SECRET_ACCESS_KEY=<secret>" \
  --restart=Never -- /bin/sh

# Dentro del pod temporal:
apk add --no-cache aws-cli
aws --endpoint-url http://10.40.1.240:7480 --no-verify-ssl \
    s3 cp "s3://pg-backups/${S3_KEY}" /tmp/restore.dump

# 3. Restaurar en una BD nueva (nunca sobre la BD en uso sin detener Odoo primero)
createdb -h postgres.aeisoftware.svc.cluster.local -p 5000 \
         -U postgres "${DB}_restore"
pg_restore -h postgres.aeisoftware.svc.cluster.local -p 5000 \
           -U postgres -d "${DB}_restore" \
           --no-owner --role=odoo \
           /tmp/restore.dump

# 4. Verificar la BD restaurada, luego renombrar si todo está OK:
#    Detener el pod Odoo del tenant antes de renombrar.
psql -h postgres.aeisoftware.svc.cluster.local -p 5000 -U postgres \
  -c "ALTER DATABASE ${DB} RENAME TO ${DB}_bak; \
      ALTER DATABASE ${DB}_restore RENAME TO ${DB};"

# 5. Reiniciar el pod del tenant
kubectl rollout restart deployment/odoo -n odoo-<tenant_id>
```

### Restaurar filestore de un tenant

```bash
TENANT=acme
DB="odoo_${TENANT}"
NS="odoo-${TENANT}"
FECHA=2026-04-14

# 1. Escalar a 0 el pod del tenant (para desmontar el filestore)
kubectl scale deployment/odoo -n "$NS" --replicas=0

# 2. Bajar el tgz desde S3 usando un pod temporal con la misma PVC
kubectl run -it --rm filestore-restore \
  --image=amazon/aws-cli \
  --overrides='{
    "spec": {
      "volumes": [{"name":"fs","persistentVolumeClaim":{"claimName":"odoo-'"$TENANT"'-data"}}],
      "containers": [{"name":"filestore-restore","image":"amazon/aws-cli",
        "command":["sh"],
        "volumeMounts":[{"name":"fs","mountPath":"/filestore"}]}]
    }
  }' --restart=Never -- /bin/sh

# Dentro del pod de restauración:
aws --endpoint-url http://10.40.1.240:7480 --no-verify-ssl \
    s3 cp "s3://pg-backups/filestore/${DB}/${FECHA}.tgz" /tmp/restore.tgz
# Limpiar filestore actual y extraer el backup
rm -rf /filestore/.local
tar xzf /tmp/restore.tgz -C /filestore/.local/share/Odoo/filestore/

# 3. Restaurar el pod del tenant
kubectl scale deployment/odoo -n "$NS" --replicas=1
kubectl rollout status deployment/odoo -n "$NS"
```

### Restaurar el admin Odoo

```bash
FECHA=2026-04-14

# 1. Escalar a 0
kubectl scale deployment/odoo-admin -n odoo-admin --replicas=0

# 2. Restaurar BD admin (mismos pasos que arriba, DB='admin')

# 3. Restaurar filestore admin con la misma técnica de pod temporal
#    PVC: odoo-admin-data — mountPath /filestore

# 4. Reiniciar
kubectl scale deployment/odoo-admin -n odoo-admin --replicas=1
```

### Forzar un backup inmediato (manual)

```bash
# Lanzar job de pg_dump ahora mismo
kubectl create job -n backup-system --from=cronjob/pg-logical-dump pg-dump-manual-$(date +%s)

# Lanzar job de filestore ahora mismo
kubectl create job -n backup-system --from=cronjob/filestore-dump filestore-manual-$(date +%s)

# Seguir los logs en tiempo real
kubectl logs -n backup-system -l app=pg-logical-dump -f
```

---

## Caché de assets frontend (ir.attachment) y Cloudflare

> **Incidente (2026-07-09):** error `OwlError: Missing template: "portal.Chatter"` al abrir tickets de
> soporte en el portal de cliente (staging y latente en producción). La causa fue una cadena de **tres cachés**:
>
> 1. **Odoo cachea los bundles compilados en `ir.attachment`** (URLs `/web/assets/...`). Sobreviven a los
>    redeploys, y como el deployment usa el tag flotante `odoo:18` (cada nodo tiene cacheado un digest
>    distinto), la BD acumula bundles compilados por builds de Odoo de épocas diferentes. Si
>    `web.assets_frontend_lazy` y `portal.assets_chatter` registran el mismo template (ej. `mail.Thread`)
>    con contenido distinto, `registerTemplate` lanza `Template already exists` y **todas** las
>    registraciones del bundle chatter mueren juntas (van en un solo `odoo.define`) → "Missing template".
> 2. **El hash de la URL del asset NO cambia** aunque el contenido recompilado cambie, y se sirve con
>    `Cache-Control: max-age=31536000, immutable` → los navegadores retienen copias rotas.
> 3. **Cloudflare** (proxy de `aeisoftware.com`) cachea `/web/assets/*` en el edge → purgar el servidor
>    no basta; las peticiones ni siquiera llegan a Odoo (`cf-cache-status: HIT`).

### Fix estructural (2026-07-09): auto-flush en cada arranque, en vez de fijar la imagen

Se evaluó fijar `odoo:18` por digest (`odoo:18@sha256:...`), pero eso renuncia a los parches de
seguridad que Odoo sigue publicando para la serie 18 durante ~1 año de soporte, y en la práctica el
bump manual tiende a olvidarse. Además el estado *previo* (tag flotante + `imagePullPolicy` por
defecto `IfNotPresent`) tampoco daba parches automáticos: cada nodo K3s descarga la imagen una vez y
la retiene para siempre, así que los nodos divergían en silencio (se confirmó con evidencia real: el
mismo día del incidente, un pod de `odoo-stg` se reprogramó a otro nodo por una falla no relacionada —
ver "Notas importantes" — y ese nodo tenía un digest distinto de `odoo:18`, produciendo bundles con
`mail.Thread` inconsistente sin que nadie tocara la imagen a propósito).

En su lugar se implementó (commit posterior a `e62e9e7`):

1. **`imagePullPolicy: Always`** en el contenedor `odoo` de `k8s/06-odoo-admin.yaml`,
   `k8s/07-staging.yaml` y en `portal/k8s_utils/manifests.py` (tenants ya lo tenían) — cada restart
   re-sincroniza con el `odoo:18` vigente en Docker Hub, autocorrigiendo el drift entre nodos y
   manteniendo los parches de seguridad al día.
2. **Init container `flush-asset-cache`** (nuevo, corre en cada arranque del pod, no solo en
   incidentes) que borra `ir_attachment` con `url LIKE '/web/assets/%'` vía `psql` directo — usa la
   misma imagen `odoo:18` del pod, así que nunca desincroniza con el build real que va a arrancar.
   Es un no-op seguro en el primer boot (tabla vacía o inexistente, con `|| echo ...` de resguardo).
   Los assets se recompilan solos en el siguiente request.

Con esto el bug de plantillas Owl inconsistentes queda neutralizado sin importar *cuándo* ni *por qué*
cambie la imagen (bump de Docker Hub, reschedule por falla de nodo, etc.), sin sacrificar parches.

### Procedimiento manual de saneamiento (incidentes puntuales / verificación)

```bash
# 1. Purgar bundles cacheados — STAGING (para producción: -n odoo-admin, app=odoo-admin, -d admin)
POD=$(kubectl get pod -n staging -l app=odoo-stg --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
kubectl exec -i -n staging $POD -- odoo shell -d staging --no-http --stop-after-init <<'EOF'
env['ir.attachment'].search([('url', '=like', '/web/assets/%')]).unlink()
env.cr.commit()
EOF

# 2. Purgar caché de Cloudflare: dashboard → zona aeisoftware.com → Caching → Purge Cache
#    (imprescindible — el paso 1 no invalida el edge)

# 3. Verificar consistencia entre bundles (el conflicto típico es mail.Thread):
curl -s 'https://staging.aeisoftware.com/web/bundle/portal.assets_chatter?lang=es_BO'
```

- Regla Cloudflare recomendada: **bypass de caché para `staging.aeisoftware.com`** (un entorno de pruebas
  no debe cachearse en CDN).

---

## Email de credenciales roto desde el webhook anónimo (2026-07-10)

> **Incidente:** el email "¡Tu sistema Odoo está listo!" no llegaba al cliente al aprovisionar una
> instancia nueva vía compra real (`SUB00219`), aunque el mismo envío funcionaba al probarlo manualmente
> desde `odoo shell`. Causa: `controllers/webhook.py` (`POST /saas/webhook/instance-status`, `auth='none'`,
> `env = request.env(su=True)`) no tiene sesión de usuario — `env.uid` es `None`. Eso rompía **dos cosas
> distintas** en cadena, y la segunda enmascaró a la primera durante el triage inicial:
>
> 1. `email_from` del template usaba `object.env.company`, que depende de `env.user.company_id` — con
>    `env.uid=None`, `env.company` es un recordset VACÍO, así que el remitente rendía en blanco →
>    `mail_from_missing`. Fix: `object.env.ref('base.main_company')` (lookup directo por XML-ID,
>    independiente de `env.user`) + un fallback final hardcodeado como red de seguridad.
> 2. Con (1) corregido, el email en realidad **sí se enviaba** — pero
>    `action_send_credentials_email()` crasheaba en la línea siguiente (`self.message_post(...)`)
>    con `ValueError: Expected singleton: res.users()`, porque `mail.thread.message_post()` llama
>    `env.user._is_public()`, y `env.user` también es un recordset vacío bajo `env.uid=None`. Esa
>    excepción la atrapaba el `except Exception` del webhook y logueaba **"credentials email failed"
>    incluso cuando el correo ya se había enviado con éxito** — una falsa alarma que ocultó el bug (1)
>    en la primera revisión. Fix: fijar `SUPERUSER_ID` con `self.with_user(SUPERUSER_ID)` cuando
>    `self.env.uid` es falsy, para que `message_post` siempre tenga un usuario real (singleton).
>
> **Lección:** verificar un fix de email reenviándolo manualmente desde `odoo shell` como Administrator
> NO reproduce el contexto real del webhook (`env.uid=None`). Para validar de verdad, reproducir el
> contexto exacto: `env(user=None, su=True)` en shell, o disparar el webhook real via
> `kubectl run ... curl -X POST http://<svc>:8069/saas/webhook/instance-status ...` con la
> `SAAS_WEBHOOK_KEY` del secret `portal-secret`.

---

## "Sync Addons to Instance" no mostraba módulos nuevos (2026-07-10)

> **Incidente:** al agregar un repo en la pestaña "Addon Repos" de un `saas.instance` y hacer clic en
> "Sync Addons to Instance", el `clone-addons` init container clonaba y simlinkeaba los módulos
> correctamente en `/mnt/extra-addons`, pero no aparecían en Apps del tenant.
>
> **Causa:** `configmap_manifest()` (`portal/k8s_utils/manifests.py`) solo agregaba `/mnt/extra-addons`
> al `addons_path` **en el momento del aprovisionamiento inicial**, y solo si el tenant ya tenía
> `addons_repos` configurados en ESE momento (lógica de commit `3a11f2e`, 2026-04-16 — anterior y no
> relacionada al fix de `symlinked_addons` de esta misma semana). El botón "Sync Addons to Instance"
> llama a `PATCH /{tenant_id}/config`, que solo actualiza la clave `addons.json` del ConfigMap y
> reinicia el pod — nunca volvía a renderizar `odoo.conf`. Cualquier tenant creado sin repos desde el
> inicio quedaba permanentemente incapaz de usar la función, aunque el clonado funcionara perfecto.
>
> **Fix (tres partes):**
> 1. `configmap_manifest()`: `/mnt/extra-addons` ahora se incluye **siempre** e incondicionalmente en
>    `addons_path`. Ya no hace falta la condición — `clone-addons` garantiza un directorio válido
>    incluso sin repos reales (crea un módulo `_placeholder` con `installable: False`).
> 2. `PATCH /{tenant_id}/config` (`routers/instances.py`) ahora es auto-reparador: cuando se actualiza
>    `addons_repos`, lee el `odoo.conf` actual y le inserta `/mnt/extra-addons` si falta
>    (`_ensure_extra_addons_path()`, idempotente). Repara tenants legacy la próxima vez que usan el
>    botón, sin migración aparte.
> 3. El init container `odoo-init` (tenants) ahora corre `ir.module.module.update_list()` en **cada**
>    arranque del pod — refresca la lista de Apps automáticamente tras un sync, sin que nadie tenga que
>    entrar en modo desarrollador y hacer clic en "Update Apps List" a mano. **No instala nada** — un
>    repo puede traer múltiples módulos/apps y cuál instalar es una decisión deliberada del staff, no
>    automática.
>
> **Bug adicional encontrado al verificar (3):** `odoo shell` no hace commit automático. La primera
> versión del paso 3 llamaba `update_list()` sin `env.cr.commit()` — corría sin error (confirmado en el
> log `ALLOW access to module.update_list`), pero los módulos nuevos desaparecían de `ir_module_module`
> al salir el proceso (rollback silencioso). El bloque de bootstrap de esquema, un poco más arriba en el
> mismo init container, ya hacía `env.cr.commit()` explícito — el paso nuevo no lo copió. Corregido.
>
> **Límite conocido — tenants creados antes de este fix:** el objeto `Deployment` de K8s de un tenant
> ya aprovisionado queda congelado con el script del init container `odoo-init` vigente al momento de su
> creación (o de su último re-apply completo). Un restart por sí solo (lo único que hace hoy
> `PATCH /config`) **no** regenera esa definición desde el código actual — solo re-renderiza el
> ConfigMap. Verificado en vivo en `administrator-sub00219`: tras el fix, el `addons_path`
> se auto-reparó correctamente (parte 2), pero el paso `update_list()` de la parte 3 no llegó a ese pod
> porque su Deployment es de antes del fix. Pendiente de decisión: agregar un mecanismo de re-apply
> completo del Deployment (vía portal API, nunca `kubectl` directo) para que tenants legacy también
> hereden cambios futuros del init container, no solo del ConfigMap.

---

## Reparación de tenants — siempre vía portal API

Los arreglos directos con `kubectl` sobre recursos de un tenant (ej. `kubectl set image deployment/odoo -n odoo-<tenant> ...`)
dejan el `saas.instance` desincronizado: una instancia en estado `error` **no vuelve sola a `ready`**
aunque el pod quede sano. Las reparaciones deben hacerse a través del portal API o de las acciones del
módulo SaaS para que el estado se actualice. (Incidente SUB00218, 2026-07-09.)

**Configuración de productos SaaS:** `odoo_version = 'custom'` exige tener `custom_image` configurada
(ej. `ghcr.io/aei-software/custom-odoo-images:18.0`). Si queda vacía, el portal genera la imagen
inexistente `odoo:custom` → `Init:ImagePullBackOff` y la instancia queda en `error`. (Incidente SUB00218:
producto "Odoo SaaS Enterprise (Mensual)". Pendiente: validación en código que rechace la venta con
mensaje claro.)

---

## Notas importantes

- El initContainer `copy-addon` clona el branch del pod (`main` en staging, `18.0` en producción)
  con `--depth=1` en **cada restart** del pod. Siempre hacer `push` **antes** de `rollout restart`.
  Nunca mezclar comandos de `odoo-admin` con los de `staging` ni viceversa.
- **Ningún módulo se auto-actualiza.** El container Odoo inicia sin flag `-u`.
  Correr el comando `odoo -u <módulo> --stop-after-init --no-http` manualmente tras cambios de esquema.
  El flag `--no-http` es obligatorio: sin él falla con `[Errno 98] Address already in use` porque el proceso principal ya ocupa el puerto 8069.
- La BD `postgres` es la instancia admin. Las BDs de clientes SaaS son dinámicas (creadas por el portal).
- El campo `odoo.conf` se renderiza en runtime vía `sed` (placeholders `REPLACE_*`) — **no hay secretos en git**.
- En modo `state=test` (Prueba) el proveedor QR Mercantil **no llama al banco** y usa QRs demo SVG.
