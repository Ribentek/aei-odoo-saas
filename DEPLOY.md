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
| Imagen portal | `ghcr.io/ribentek/aei-odoo-saas/portal:latest` |
| Repo en initContainer | `https://github.com/Ribentek/aei-odoo-saas.git` (branch `main`, `--depth=1`) |
| Addons copiados | `payment_qr_mercantil`, `odoo_k8s_saas`, `odoo_k8s_saas_subscription` (del repo principal) + `subscription_oca` (local en `external_addons/`) |
[cert-manager]: https://cert-manager.io/

---

## Creación de Custom Images (SaaS Tenants)

Si un cliente requiere Odoo con módulos pre-instalados (baked-in), la imagen de Docker debe cumplir estas dos reglas principales para no colisionar con la infraestructura de inicialización nativa del SaaS:

1. **Ruta de Addons Aislada:** Los addons custom deben copiarse obligatoriamente dentro de `/opt/custom-addons` durante el paso `RUN` del Dockerfile. No ubicar los archivos en `/mnt/extra-addons` ni otras rutas por omisión de Odoo, ya que Kubernetes monta de manera forzosa un volumen `emptyDir` allí para su `initContainer`, lo que sobreescribiría silenciosamente todas las capas instaladas.
2. **Entrypoint Wrapper Dinámico:** Es imperativo utilizar un script de entrada (ej: `entrypoint-custom.sh`) que extienda el entrypoint original de Odoo sumando la inyección local `--addons-path="...,/opt/custom-addons"`. *Importante:* Desde Odoo 19 en adelante, cualquier ruta de un `odoo.conf` inyectada de manera global que resulte no existir en un contenedor, detonará una excepción fatal `FileNotFoundError`; por esto mismo, la inyección del path custom debe realizarse siempre desde el argumento CLI dentro de la propia imagen custom.

---

## Flujo de despliegue estándar

```bash
# 1. Commit y push del código
git add <archivos>
git commit -m "tipo(módulo): descripción"
git push origin main

# 2. Restart — el initContainer clona el repo actualizado automáticamente
kubectl rollout restart deployment/odoo-admin -n odoo-admin

# 3. Esperar a que el pod esté Running
kubectl rollout status deployment/odoo-admin -n odoo-admin
```

> **No hay CI/CD automático para odoo-admin.** El restart debe hacerse manualmente después del push.
> Ningún módulo se auto-actualiza — el container Odoo inicia sin flag `-u`.

---

## Cuando hay cambios de esquema BD (campos nuevos en modelos)

> ⚠️ Obligatorio tras agregar o renombrar `fields.*` en cualquier modelo Odoo.

```bash
# 1. Obtener nombre del pod (tras el rollout restart)
POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')

# 2. Actualizar el módulo afectado
kubectl exec -n odoo-admin $POD -- \
  odoo -u payment_qr_mercantil -d admin --stop-after-init

# 3. Para actualizar TODOS los módulos del repo:
kubectl exec -n odoo-admin $POD -- \
  odoo -u payment_qr_mercantil,odoo_k8s_saas,odoo_k8s_saas_subscription,subscription_oca \
  -d admin --stop-after-init

# 4. Restart limpio tras el update
kubectl rollout restart deployment/odoo-admin -n odoo-admin
kubectl rollout status deployment/odoo-admin -n odoo-admin
```

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
# Odoo admin
POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n odoo-admin $POD -f --tail=100

# Portal FastAPI
kubectl logs -n aeisoftware deployment/portal -f --tail=100

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
kubectl get pods -n aeisoftware

# Describir pod (ver errores de initContainer)
kubectl describe pod -n odoo-admin <pod-name>

# Verificar secrets aplicados
kubectl get secrets -n odoo-admin
kubectl get secrets -n aeisoftware

# PVCs
kubectl get pvc -n odoo-admin

# IngressRoutes
kubectl get ingress -n odoo-admin
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

## Notas importantes

- El initContainer `copy-addon` clona `main` con `--depth=1` en **cada restart** del pod.
  Siempre hacer `push` **antes** de `rollout restart`.
- **Ningún módulo se auto-actualiza.** El container Odoo inicia sin flag `-u`.
  Correr el comando `odoo -u <módulo> --stop-after-init` manualmente tras cambios de esquema.
- La BD `postgres` es la instancia admin. Las BDs de clientes SaaS son dinámicas (creadas por el portal).
- El campo `odoo.conf` se renderiza en runtime vía `sed` (placeholders `REPLACE_*`) — **no hay secretos en git**.
- En modo `state=test` (Prueba) el proveedor QR Mercantil **no llama al banco** y usa QRs demo SVG.
