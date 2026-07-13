# Runbook: Backup y Restore

> **Actualizado:** 2026-04-14  
> **Relacionado:** [Roadmap: Production Readiness 100 Tenants](Roadmap-Production-Readiness-100-Tenants.md) · [Operational Runbook](Operational-Runbook.md)

---

## Arquitectura de backups

```
capa 1  pgBackRest (físico/PITR)
        PostgreSQL 16 (archive_command) → stunnel → RadosGW
        bucket: pg-backups, path: /odoo-saas-ha
        Full Domingos 02:00 AM, Diff Lun-Sáb 02:00 AM (BOT)
        Retención: full=4, diff=14, archive=4 full

capa 2  pg_dump lógico por base (CronJob K8s pg-logical-dump)
        bucket: pg-backups, path: /pgdump/<db>/YYYY-MM-DD.dump
        Diario 03:30 AM, fuente: réplica HAProxy :5001
        Retención: 7 daily, 4 weekly, 3 monthly

capa 3  Filestore Odoo (CronJob K8s filestore-dump)
        bucket: pg-backups, path: /filestore/<db>/YYYY-MM-DD.tgz
        Diario 04:00 AM, vía kubectl exec | tar | aws s3 cp
        Retención: 7 daily, 4 weekly
```

---

## 1. Estado y verificación de backups

### pgBackRest (capa 1)

```bash
# Desde cualquier nodo PG
ssh ubuntu@10.40.2.182   # pg-node1 (replica)
ssh ubuntu@10.40.2.174   # pg-node2 (leader actual)
ssh ubuntu@10.40.2.193   # pg-node3 (replica)

# Estado de la stanza
sudo -u postgres pgbackrest --stanza=odoo-saas info

# Verificar salud del stanza + archiving
sudo -u postgres pgbackrest --stanza=odoo-saas check

# Estado del WAL archiving en el líder
sudo -u postgres psql -c "SELECT archived_count, failed_count, last_archived_time, last_failed_time FROM pg_stat_archiver;"

# Ver log de cron de backups
sudo tail -50 /var/log/pgbackrest/cron.log
```

### pg_dump + filestore (capas 2 y 3)

```bash
# Ver últimas ejecuciones de CronJobs
kubectl -n backup-system get cronjob
kubectl -n backup-system get jobs --sort-by=.status.startTime | tail -10

# Ver logs del último job pg_dump
kubectl -n backup-system logs -l app=pg-logical-dump --tail=50

# Ver logs del último job filestore
kubectl -n backup-system logs -l app=filestore-dump --tail=50

# Listar dumps en S3 (desde pg-node2)
ssh ubuntu@10.40.2.174 "
  sudo -u postgres aws --endpoint-url http://10.40.1.240:7480 --no-verify-ssl \
    s3 ls s3://pg-backups/pgdump/ --recursive | tail -20
  sudo -u postgres aws --endpoint-url http://10.40.1.240:7480 --no-verify-ssl \
    s3 ls s3://pg-backups/filestore/ --recursive | tail -20
"
```

---

## 2. Backup self-service vía Portal API (Capa 4)

El endpoint `GET /api/v1/instances/{tenant_id}/backup` genera y descarga un ZIP completo (DB + filestore) de un tenant sin parar el pod. Usa `kubectl exec` para ejecutar `dump_db` dentro del pod del tenant, evitando la restricción `list_db=False` de Odoo 18.

**Cuándo usar:** backup puntual a petición de un cliente o antes de una migración.

```bash
# Descargar backup de un tenant
curl -s https://portal.aeisoftware.com/api/v1/instances/<tenant_id>/backup \
  -H "X-API-Key: $API_KEY" \
  -o "backup-<tenant_id>-$(date +%Y%m%d).zip"

# Staging
curl -s https://portal-stg.aeisoftware.com/api/v1/instances/<tenant_id>/backup \
  -H "X-API-Key: $STAGING_API_KEY" \
  -o "backup-<tenant_id>-$(date +%Y%m%d).zip"
```

**Prerrequisitos:**
- El ClusterRole `saas-portal-role` debe incluir `pods/exec` (`k8s/04-rbac.yaml` — corregido 2026-04-16)
- El pod del tenant debe estar `Running`

**Errores comunes:**

| Código | Causa | Solución |
|:-------|:------|:---------|
| `403 Forbidden` | API key inválida | Verificar `X-API-Key` |
| `404` | Tenant no encontrado | Verificar `tenant_id` |
| `503` | Pod no está Running | Verificar `kubectl get pod -n odoo-<tenant_id>` |
| `stream error` | `pods/exec` RBAC faltante | Aplicar `kubectl apply -f k8s/04-rbac.yaml` |

---

## 4. Restaurar UN tenant desde pg_dump (RTO ~5 min)

**Cuándo usar:** restaurar datos de un tenant específico sin afectar el cluster. RPO: hasta 24h antes.

```bash
# 1. Identificar el dump más reciente del tenant
DB="odoo_<tenant_id>"
DATE="2026-04-14"   # usar la fecha del dump deseado
DUMP_KEY="pgdump/${DB}/${DATE}.dump"

# 2. Descargar el dump (desde un host con acceso a RadosGW o desde un pod)
kubectl -n backup-system run restore-shell --rm -it --restart=Never \
  --image=postgres:16-alpine \
  --env="PGPASSWORD=<DB_PASSWORD>" \
  --env="AWS_ACCESS_KEY_ID=<S3_ACCESS_KEY>" \
  --env="AWS_SECRET_ACCESS_KEY=<S3_SECRET_KEY>" \
  -- sh

# Dentro del pod:
apk add --no-cache aws-cli
aws --endpoint-url http://10.40.1.240:7480 --no-verify-ssl \
    s3 cp s3://pg-backups/${DUMP_KEY} /tmp/restore.dump

# 3. Restaurar en una DB temporal (verificar antes de pisar la real)
psql -h postgres.aeisoftware.svc.cluster.local -p 5000 -U postgres \
  -c "CREATE DATABASE ${DB}_restore OWNER \"odoo-<tenant_id>\";"

pg_restore -h postgres.aeisoftware.svc.cluster.local -p 5000 -U postgres \
  -d "${DB}_restore" -Fc --no-owner --role="odoo-<tenant_id>" /tmp/restore.dump

# 4. Validar datos clave (ejemplo: número de usuarios)
psql -h postgres.aeisoftware.svc.cluster.local -p 5000 -U postgres \
  -d "${DB}_restore" -c "SELECT COUNT(*) FROM res_users WHERE active;"

# 5. Si OK: pisar la base real (detener el pod Odoo del tenant primero)
kubectl -n odoo-<tenant_id> scale deployment odoo --replicas=0
psql -h ... -p 5000 -U postgres \
  -c "DROP DATABASE ${DB};" \
  -c "ALTER DATABASE ${DB}_restore RENAME TO ${DB};"
kubectl -n odoo-<tenant_id> scale deployment odoo --replicas=1
```

---

## 5. Restaurar filestore de un tenant

**Cuándo usar:** recovery de archivos adjuntos (ir.attachment) perdidos o corruptos.

```bash
DATE="2026-04-14"
TENANT_ID="<tenant_id>"
DB="odoo_${TENANT_ID}"

# 1. Detener el pod Odoo del tenant
kubectl -n odoo-${TENANT_ID} scale deployment odoo --replicas=0

# 2. Descargar y restaurar el filestore en el pod Odoo
# (el pod está detenido pero el PVC sigue montable en un pod temporal)
kubectl -n odoo-${TENANT_ID} run restore-fs --rm -it --restart=Never \
  --image=alpine/k8s:1.34.1 \
  --overrides='{"spec":{"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"odoo-data"}}],"containers":[{"name":"restore-fs","image":"alpine/k8s:1.34.1","command":["sh"],"stdin":true,"tty":true,"volumeMounts":[{"name":"data","mountPath":"/odoo-data"}],"env":[{"name":"AWS_ACCESS_KEY_ID","value":"<KEY>"},{"name":"AWS_SECRET_ACCESS_KEY","value":"<SECRET>"}]}]}}' \
  -- sh

# Dentro del pod temporal:
aws --endpoint-url http://10.40.1.240:7480 --no-verify-ssl \
    s3 cp s3://pg-backups/filestore/${DB}/${DATE}.tgz - \
    | tar xzf - -C /odoo-data/.local/share/Odoo/filestore/

# 3. Reiniciar Odoo
kubectl -n odoo-${TENANT_ID} scale deployment odoo --replicas=1
```

---

## 6. PITR full-cluster con pgBackRest (RTO ~15 min)

**Cuándo usar:** corrupción masiva de datos, rollback de migración fallida, recuperación ante desastre. Afecta **todos** los tenants del cluster.

> ⚠️ Requiere parar Patroni en todos los nodos. Planificar ventana de mantenimiento.

```bash
# 1. Identificar el timestamp objetivo
TARGET="2026-04-14 02:00:00+00"  # usar timestamp en UTC

# 2. Parar Patroni en los 3 nodos PG (ejecutar en cada uno)
for HOST in 10.40.2.182 10.40.2.174 10.40.2.193; do
  ssh ubuntu@$HOST "sudo systemctl stop patroni"
done

# 3. Limpiar datadir del futuro primary (pg-node1 en este ejemplo)
ssh ubuntu@10.40.2.182 "sudo rm -rf /var/lib/postgresql/16/patroni"

# 4. Restaurar desde backup en el futuro primary
ssh ubuntu@10.40.2.182 "
  sudo -u postgres pgbackrest \
    --stanza=odoo-saas \
    --delta \
    --type=time \
    \"--target=${TARGET}\" \
    --target-action=promote \
    restore
"

# 5. Iniciar Patroni solo en el primary primero
ssh ubuntu@10.40.2.182 "sudo systemctl start patroni"
# Esperar a que llegue a estado 'running'
ssh ubuntu@10.40.2.182 "sudo patronictl -c /etc/patroni/patroni.yml list"

# 6. Iniciar Patroni en los replicas (clonan automáticamente del primary)
for HOST in 10.40.2.174 10.40.2.193; do
  ssh ubuntu@$HOST "sudo systemctl start patroni"
done

# 7. Verificar cluster
ssh ubuntu@10.40.2.182 "sudo patronictl -c /etc/patroni/patroni.yml list"
```

**Criterio de aceptación:** cluster en estado `Leader+2 Replica streaming`, lag 0.

---

## 7. Disparar backup manual

```bash
# pg_dump manual inmediato
kubectl -n backup-system create job --from=cronjob/pg-logical-dump \
  "manual-pg-$(date +%s)"

# filestore manual inmediato
kubectl -n backup-system create job --from=cronjob/filestore-dump \
  "manual-fs-$(date +%s)"

# Seguir logs en tiempo real
kubectl -n backup-system logs -f \
  "$(kubectl -n backup-system get pod -l app=pg-logical-dump \
     --sort-by=.metadata.creationTimestamp -o name | tail -1)"
```

---

## 8. Troubleshooting

| Síntoma | Diagnóstico | Solución |
|---|---|---|
| `pg_stat_archiver.failed_count` sube | `sudo systemctl status stunnel-s3proxy` | Reiniciar stunnel: `sudo systemctl restart stunnel-s3proxy` |
| CronJob pg_dump en `Error` | `kubectl -n backup-system logs job/<name>` | Verificar conectividad PG y creds S3 |
| `s3 cp` timeout | `curl http://10.40.1.240:7480` desde un pod | Verificar RadosGW: `sudo ceph status` en nodos Ceph |
| filestore-dump no encuentra pod | Pod del tenant en estado diferente de Running | Verificar `kubectl get pod -n odoo-<id>` |
| Dump de 0 bytes | `pg_dump` exitoso pero DB vacía | Normal para tenants recién creados |

---

## 9. Registro de drills de restore

Los resultados de cada ejecución del drill se documentan en [Restore Drill Hito 3](Restore-Drill-Hito-3.md).

**Frecuencia recomendada:** mensual.  
**Próximo drill:** 2026-04-20.
