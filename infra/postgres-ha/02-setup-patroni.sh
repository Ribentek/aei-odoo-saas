#!/bin/bash
# =============================================================================
# 02-setup-patroni.sh — Configura Patroni + PostgreSQL en un nodo
#
# Variables de entorno requeridas:
#   NODE_NAME             — pg-node1, pg-node2, pg-node3
#   NODE_IP               — 192.168.0.x (IP interna)
#   DB_PASSWORD           — Contraseña del usuario 'odoo'
#   REPLICATOR_PASSWORD   — Contraseña del usuario 'replicator'
#   PG_SUPERUSER_PASSWORD — Contraseña del superuser 'postgres'
#
# El primer nodo en arrancar se convierte en Leader.
# Los siguientes se unen como Replicas automáticamente.
# =============================================================================
set -euo pipefail

echo "══════════════════════════════════════════════════"
echo "  02-setup-patroni.sh — Configurando Patroni"
echo "  Nodo: ${NODE_NAME} (${NODE_IP})"
echo "══════════════════════════════════════════════════"

# ─── Validar variables ──────────────────────────────────────────────────────
: "${NODE_NAME:?ERROR: NODE_NAME no definido}"
: "${NODE_IP:?ERROR: NODE_IP no definido}"
: "${DB_PASSWORD:?ERROR: DB_PASSWORD no definido}"
: "${REPLICATOR_PASSWORD:?ERROR: REPLICATOR_PASSWORD no definido}"
: "${PG_SUPERUSER_PASSWORD:?ERROR: PG_SUPERUSER_PASSWORD no definido}"

# ─── IPs del clúster ────────────────────────────────────────────────────────
NODE1_IP="192.168.0.127"
NODE2_IP="192.168.0.186"
NODE3_IP="192.168.0.226"

# ─── Detener PostgreSQL standalone si está corriendo ─────────────────────────
echo "→ Deteniendo PostgreSQL standalone..."
systemctl stop postgresql 2>/dev/null || true
systemctl disable postgresql 2>/dev/null || true

# Limpiar datos del clúster por defecto si existe
if [ -d /var/lib/postgresql/16/main ]; then
  echo "→ Eliminando clúster PostgreSQL por defecto (main)..."
  pg_dropcluster --stop 16 main 2>/dev/null || true
fi

# ─── Asegurar directorio de datos de Patroni ────────────────────────────────
echo "→ Preparando directorio de datos..."
mkdir -p /var/lib/postgresql/16/patroni
chown -R postgres:postgres /var/lib/postgresql/16/patroni
chmod 700 /var/lib/postgresql/16/patroni

# ─── Generar patroni.yml ────────────────────────────────────────────────────
echo "→ Generando /etc/patroni/patroni.yml..."

cat > /etc/patroni/patroni.yml <<EOF
# ─────────────────────────────────────────────────────────────────────────────
# Patroni Configuration — ${NODE_NAME}
# Cluster: odoo-saas-ha
# Tuned for: 8GB RAM / 4 vCPU / Ceph HDD+SSD cache / 1000 Odoo tenants
# ─────────────────────────────────────────────────────────────────────────────

scope: odoo-saas-ha
namespace: /db/
name: ${NODE_NAME}

restapi:
  listen: 0.0.0.0:8008
  connect_address: ${NODE_IP}:8008

etcd3:
  hosts:
    - ${NODE1_IP}:2379
    - ${NODE2_IP}:2379
    - ${NODE3_IP}:2379

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576  # 1MB — evita promover réplicas muy atrasadas
    master_start_timeout: 300
    synchronous_mode: false           # Async replication (mejor performance)

    postgresql:
      use_pg_rewind: true
      use_slots: true
      parameters:

        # ─── Conexiones ───────────────────────────────────────────────────
        max_connections: 800
        superuser_reserved_connections: 5

        # ─── Memoria (8GB RAM) ────────────────────────────────────────────
        shared_buffers: '2GB'
        effective_cache_size: '6GB'
        work_mem: '2MB'
        maintenance_work_mem: '512MB'
        huge_pages: 'off'
        temp_buffers: '8MB'

        # ─── WAL ─────────────────────────────────────────────────────────
        wal_level: replica
        wal_buffers: '32MB'
        min_wal_size: '1GB'
        max_wal_size: '4GB'
        checkpoint_completion_target: 0.9
        checkpoint_timeout: '10min'
        archive_mode: 'on'
        archive_command: 'pgbackrest --stanza=odoo-saas archive-push %p'
        archive_timeout: '60s'

        # ─── Replicación ─────────────────────────────────────────────────
        max_wal_senders: 10
        max_replication_slots: 10
        hot_standby: 'on'
        hot_standby_feedback: 'on'
        wal_keep_size: '1GB'

        # ─── Query Planner (Ceph HDD + SSD cache) ────────────────────────
        random_page_cost: 1.5
        effective_io_concurrency: 100
        seq_page_cost: 1.0

        # ─── Autovacuum (CRÍTICO para Odoo) ──────────────────────────────
        autovacuum: 'on'
        autovacuum_vacuum_scale_factor: 0.02
        autovacuum_analyze_scale_factor: 0.01
        autovacuum_max_workers: 3
        autovacuum_vacuum_cost_delay: '5ms'
        autovacuum_vacuum_cost_limit: 1000
        autovacuum_naptime: '30s'
        autovacuum_freeze_max_age: 200000000

        # ─── Logging ─────────────────────────────────────────────────────
        log_min_duration_statement: 1000        # Queries > 1s
        log_checkpoints: 'on'
        log_lock_waits: 'on'
        log_temp_files: 0                       # Log ALL temp files
        log_autovacuum_min_duration: 0          # Log ALL autovacuum
        log_line_prefix: '%t [%p]: user=%u,db=%d,app=%a,client=%h '
        log_statement: none
        logging_collector: 'on'
        log_directory: 'log'
        log_filename: 'postgresql-%a.log'
        log_rotation_age: '1d'
        log_rotation_size: '100MB'
        log_truncate_on_rotation: 'on'

        # ─── Estadísticas ────────────────────────────────────────────────
        default_statistics_target: 200
        track_activities: 'on'
        track_counts: 'on'
        track_io_timing: 'on'
        track_functions: 'all'

        # ─── Locale y Timezone ───────────────────────────────────────────
        timezone: 'America/La_Paz'
        log_timezone: 'America/La_Paz'
        datestyle: 'iso, mdy'
        lc_messages: 'en_US.UTF-8'
        lc_monetary: 'en_US.UTF-8'
        lc_numeric: 'en_US.UTF-8'
        lc_time: 'en_US.UTF-8'
        default_text_search_config: 'pg_catalog.english'

  initdb:
    - encoding: UTF8
    - data-checksums
    - locale: en_US.UTF-8

  pg_hba:
    - local   all             all                          peer
    - host    all             all         127.0.0.1/32     scram-sha-256
    - host    all             all         ::1/128          scram-sha-256
    - host    all             all         192.168.0.0/24   scram-sha-256
    - host    replication     replicator  192.168.0.0/24   scram-sha-256
    # Acceso desde K3s (Fase 2) — rango amplio, se ajustará después
    - host    all             all         0.0.0.0/0        scram-sha-256

  users:
    odoo:
      password: '${DB_PASSWORD}'
      options:
        - createrole
        - createdb
    replicator:
      password: '${REPLICATOR_PASSWORD}'
      options:
        - replication

postgresql:
  listen: 0.0.0.0:5432
  connect_address: ${NODE_IP}:5432
  data_dir: /var/lib/postgresql/16/patroni
  bin_dir: /usr/lib/postgresql/16/bin
  pgpass: /var/lib/postgresql/.pgpass
  authentication:
    superuser:
      username: postgres
      password: '${PG_SUPERUSER_PASSWORD}'
    replication:
      username: replicator
      password: '${REPLICATOR_PASSWORD}'
  parameters:
    unix_socket_directories: '/var/run/postgresql'

  create_replica_methods:
    - basebackup
  basebackup:
    checkpoint: fast
    max-rate: '100M'

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: false
  nosync: false
EOF

chown postgres:postgres /etc/patroni/patroni.yml
chmod 600 /etc/patroni/patroni.yml

# ─── Callback script para cambios de rol ─────────────────────────────────────
echo "→ Creando callback script..."
cat > /etc/patroni/callbacks/on_role_change.sh <<'CALLBACK'
#!/bin/bash
# Patroni callback: se ejecuta cuando cambia el rol del nodo
# $1 = action (on_start, on_stop, on_role_change, on_restart)
# $2 = role (master, replica, ...)
# $3 = cluster name

ACTION="$1"
ROLE="$2"
CLUSTER="$3"

logger -t patroni-callback "Action: $ACTION, Role: $ROLE, Cluster: $CLUSTER"

case "$ROLE" in
  master|primary)
    logger -t patroni-callback "Este nodo es ahora PRIMARY"
    # Reconfigurar PgBouncer para apuntar al local PostgreSQL
    # PgBouncer ya apunta a localhost, así que no hay cambio necesario
    ;;
  replica)
    logger -t patroni-callback "Este nodo es ahora REPLICA"
    ;;
esac
CALLBACK

chmod +x /etc/patroni/callbacks/on_role_change.sh
chown postgres:postgres /etc/patroni/callbacks/on_role_change.sh

# ─── Crear servicio systemd de Patroni ───────────────────────────────────────
echo "→ Creando servicio systemd para Patroni..."

cat > /etc/systemd/system/patroni.service <<'UNIT'
[Unit]
Description=Patroni PostgreSQL HA Cluster Manager
Documentation=https://patroni.readthedocs.io/
After=network-online.target etcd.service
Wants=network-online.target
Requires=etcd.service

[Service]
Type=simple
User=postgres
Group=postgres
ExecStart=/usr/local/bin/patroni /etc/patroni/patroni.yml
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
TimeoutSec=30
Restart=always
RestartSec=5s
LimitNOFILE=65536
LimitNPROC=65536

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=patroni

[Install]
WantedBy=multi-user.target
UNIT

# ─── Iniciar Patroni ────────────────────────────────────────────────────────
echo "→ Iniciando Patroni..."
systemctl daemon-reload
systemctl enable patroni
systemctl start patroni

# Esperar a que Patroni arranque y PostgreSQL esté listo
echo "→ Esperando a que Patroni arranque PostgreSQL..."
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8008/health &>/dev/null; then
    echo "  Patroni healthy!"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "  ⚠️  Patroni no respondió en 120s. Revisa: journalctl -u patroni -n 50"
    exit 1
  fi
  echo "  Intento $i/60... esperando 2s"
  sleep 2
done

# ─── Verificar estado ───────────────────────────────────────────────────────
echo ""
echo "→ Estado del nodo:"
curl -s http://127.0.0.1:8008/ | python3 -m json.tool 2>/dev/null || true

echo ""
echo "→ Estado del clúster:"
patronictl -c /etc/patroni/patroni.yml list 2>/dev/null || \
  echo "  (Esperando a que todos los nodos se unan...)"

echo ""
echo "══════════════════════════════════════════════════"
echo "  ✅ Patroni configurado y activo en ${NODE_NAME}"
echo "══════════════════════════════════════════════════"
