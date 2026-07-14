# PostgreSQL HA Cluster — Guía Operativa

> **AVISO (2026-04-11):** Las secciones de **PgBouncer** (puerto `:5002`, `:6432`, `SHOW POOLS`, `pgbouncer.ini`) en este documento son **HISTÓRICAS**. PgBouncer fue eliminado del path de tráfico en commit `100d435`. Sigue instalado en las VMs pero deshabilitado (`systemctl disable pgbouncer`). Todo el tráfico va por HAProxy `:5000` (RW) y `:5001` (RO). Para la referencia operativa vigente, ver [`docs/wiki/PostgreSQL-Cluster-Operations.md`](../../docs/wiki/PostgreSQL-Cluster-Operations.md).

Para instalar este stack en un cluster nuevo:
```bash
git clone https://github.com/AEI-Software/aei-odoo-saas.git
cd aei-odoo-saas/infra/postgres-ha
```

Clúster PostgreSQL 16 con alta disponibilidad para Odoo SaaS, corriendo en 3 VMs dedicadas.

## Arquitectura

| Componente | Puerto | Función |
|---|---|---|
| PostgreSQL 16 | 5432 | Motor de base de datos |
| Patroni | 8008 | Orquestador HA, failover automático |
| etcd | 2379/2380 | Consenso distribuido (DCS) |
| PgBouncer | 6432 | Connection pooling (transaction mode) |
| HAProxy | 5000/5001/5002/7000 | Routing inteligente a primary/replicas |
| pgBackRest | — | Backups a RadosGW (S3) + PITR |

## Nodos

| Nodo | IP Interna | IP SSH | Specs |
|---|---|---|---|
| pg-node1 | 192.168.0.127 | 10.40.2.182 | 4 vCPU, 8GB RAM, 200GB Ceph |
| pg-node2 | 192.168.0.186 | 10.40.2.174 | 4 vCPU, 8GB RAM, 200GB Ceph |
| pg-node3 | 192.168.0.226 | 10.40.2.193 | 4 vCPU, 8GB RAM, 200GB Ceph |

## Endpoints de conexión

| Endpoint | Puerto | Uso |
|---|---|---|
| `192.168.0.{127,186,226}:5000` | RW directo | Longpolling (LISTEN/NOTIFY), DDL, admin |
| `192.168.0.{127,186,226}:5001` | RO directo | Reportes pesados, analytics |
| `192.168.0.{127,186,226}:5002` | RW pooled | HTTP workers de Odoo (tráfico principal) |
| `192.168.0.{127,186,226}:7000` | Stats | Dashboard HAProxy (admin/password) |

> **Nota**: HAProxy corre en los 3 nodos. Puedes conectar a cualquiera de ellos;
> HAProxy siempre redirige al primary correcto usando health checks contra Patroni.

---

## Despliegue inicial

```bash
# 1. Copiar y editar variables de entorno
cp .env.example .env
nano .env   # Completar credenciales de RadosGW

# 2. Ejecutar despliegue completo
chmod +x deploy-all.sh
./deploy-all.sh

# El script:
# - Genera contraseñas automáticamente (.secrets.generated)
# - Instala paquetes en las 3 VMs
# - Configura etcd → Patroni → PgBouncer → HAProxy → pgBackRest
# - Ejecuta validación final
```

---

## Operaciones del día a día

### Ver estado del clúster

```bash
# Desde cualquier nodo:
ssh -i ~/.ssh/id_rsa ubuntu@10.40.2.182

sudo patronictl -c /etc/patroni/patroni.yml list
```

### Switchover controlado (cambiar el primary)

```bash
# Desde cualquier nodo:
sudo patronictl -c /etc/patroni/patroni.yml switchover

# Te pedirá confirmar el nuevo leader. Seleccionar y confirmar.
# El switchover es transparente — las conexiones se reconectan vía HAProxy.
```

### Reiniciar PostgreSQL en un nodo

```bash
# NUNCA usar systemctl restart postgresql directamente.
# Siempre usar patronictl:
sudo patronictl -c /etc/patroni/patroni.yml restart odoo-saas-ha pg-node1

# Para reiniciar todos los nodos secuencialmente:
sudo patronictl -c /etc/patroni/patroni.yml restart odoo-saas-ha
```

### Aplicar cambios de configuración PostgreSQL

```bash
# 1. Editar la configuración DCS (se aplica a todos los nodos):
sudo patronictl -c /etc/patroni/patroni.yml edit-config

# 2. Algunos cambios requieren restart:
sudo patronictl -c /etc/patroni/patroni.yml restart odoo-saas-ha --pending
```

---

## Backups y restauración

### Ver información de backups

```bash
sudo -u postgres pgbackrest --stanza=odoo-saas info
```

### Ejecutar backup manual

```bash
# Full backup:
sudo -u postgres pgbackrest --stanza=odoo-saas --type=full backup

# Differential backup:
sudo -u postgres pgbackrest --stanza=odoo-saas --type=diff backup
```

### Restaurar un backup (PITR)

> ⚠️ **DESTRUCTIVO** — Esto reemplaza todos los datos del clúster.

```bash
# 1. Detener Patroni en TODOS los nodos
ssh ubuntu@10.40.2.182 "sudo systemctl stop patroni"
ssh ubuntu@10.40.2.174 "sudo systemctl stop patroni"
ssh ubuntu@10.40.2.193 "sudo systemctl stop patroni"

# 2. En el nodo que será el nuevo primary:
sudo -u postgres pgbackrest --stanza=odoo-saas \
  --type=time --target="2026-04-05 12:00:00-04:00" \
  --delta --target-action=promote \
  restore

# 3. Reiniciar Patroni (primero en el nodo restaurado)
sudo systemctl start patroni
# Esperar a que arranque, luego los otros nodos:
ssh ubuntu@10.40.2.174 "sudo systemctl start patroni"
ssh ubuntu@10.40.2.193 "sudo systemctl start patroni"
```

---

## Monitoreo

### Endpoints de métricas (Prometheus)

```
# node_exporter (métricas de sistema):
192.168.0.127:9100/metrics
192.168.0.186:9100/metrics
192.168.0.226:9100/metrics

# postgres_exporter (métricas de PostgreSQL):
192.168.0.127:9187/metrics
192.168.0.186:9187/metrics
192.168.0.226:9187/metrics

# Patroni API:
192.168.0.127:8008/
192.168.0.186:8008/
192.168.0.226:8008/
```

### HAProxy Stats Dashboard

```
http://192.168.0.127:7000/
Auth: admin / (ver .secrets.generated)
```

### Comandos de diagnóstico rápido

```bash
# Estado de todos los servicios
for svc in etcd patroni pgbouncer haproxy node_exporter postgres_exporter; do
  echo "=== $svc ===" && systemctl status $svc --no-pager | head -5
done

# Logs de Patroni
journalctl -u patroni -f --no-pager

# Logs de PostgreSQL
sudo -u postgres tail -f /var/lib/postgresql/16/patroni/log/postgresql-*.log

# Pools de PgBouncer
PGPASSWORD=<password> psql -h 127.0.0.1 -p 6432 -U postgres pgbouncer -c "SHOW POOLS;"

# Lag de replicación
PGPASSWORD=<password> psql -h 127.0.0.1 -p 5432 -U postgres -c \
  "SELECT client_addr, state, sent_lsn, replay_lsn, 
   replay_lag FROM pg_stat_replication;"
```

---

## Troubleshooting

### "No se eligió leader"

```bash
# Verificar que etcd funciona:
etcdctl endpoint health --endpoints=http://192.168.0.127:2379,http://192.168.0.186:2379,http://192.168.0.226:2379

# Si etcd falla, reiniciar en todos los nodos:
sudo systemctl restart etcd
```

### "PgBouncer no conecta"

```bash
# Verificar que PostgreSQL acepta conexiones:
pg_isready -h 127.0.0.1 -p 5432

# Verificar userlist.txt:
cat /etc/pgbouncer/userlist.txt

# Logs de PgBouncer:
journalctl -u pgbouncer -n 50
```

### "HAProxy muestra todos los backends DOWN"

```bash
# Verificar Patroni API:
curl http://192.168.0.127:8008/
curl http://192.168.0.186:8008/
curl http://192.168.0.226:8008/

# Verificar /primary y /replica endpoints:
curl http://192.168.0.127:8008/primary
curl http://192.168.0.127:8008/replica
```

### Disco lleno

```bash
# Ver uso de disco:
df -h

# Ver tamaño de databases:
sudo -u postgres psql -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database ORDER BY pg_database_size(datname) DESC;"

# Limpiar WAL antiguos (solo si pgBackRest ya archivó):
sudo -u postgres psql -c "SELECT pg_switch_wal();"
```

---

## Archivos importantes

| Archivo | Descripción |
|---|---|
| `/etc/patroni/patroni.yml` | Configuración de Patroni + PG |
| `/etc/pgbouncer/pgbouncer.ini` | Configuración de PgBouncer |
| `/etc/haproxy/haproxy.cfg` | Configuración de HAProxy |
| `/etc/pgbackrest/pgbackrest.conf` | Configuración de pgBackRest |
| `/etc/etcd.conf.yml` | Configuración de etcd |
| `/var/lib/postgresql/16/patroni/` | Datos de PostgreSQL |
| `/var/lib/etcd/` | Datos de etcd |
| `/var/log/pgbackrest/` | Logs de pgBackRest |

---

## Schedule de backups

| Tipo | Cuándo | Retención |
|---|---|---|
| Full | Domingos 2:00 AM | 4 semanas |
| Differential | Lun-Sáb 2:00 AM | 14 días |
| WAL | Continuo | 4 full backups |
