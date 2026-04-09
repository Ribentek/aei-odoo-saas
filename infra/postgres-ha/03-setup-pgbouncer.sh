#!/bin/bash
# =============================================================================
# 03-setup-pgbouncer.sh — Configura PgBouncer en un nodo
# =============================================================================
set -euo pipefail

echo "══════════════════════════════════════════════════"
echo "  03-setup-pgbouncer.sh — Configurando PgBouncer"
echo "══════════════════════════════════════════════════"

: "${DB_PASSWORD:?ERROR: DB_PASSWORD no definido}"
: "${PG_SUPERUSER_PASSWORD:?ERROR: PG_SUPERUSER_PASSWORD no definido}"

# ─── Esperar a que PostgreSQL esté listo ─────────────────────────────────────
echo "→ Esperando a que PostgreSQL esté listo..."
for i in $(seq 1 30); do
  if pg_isready -h 127.0.0.1 -p 5432 -U postgres &>/dev/null; then
    echo "  PostgreSQL listo!"
    break
  fi
  echo "  Intento $i/30..."
  sleep 2
done

# ─── Solo en el primary: crear usuarios y schema de auth ─────────────────────
IS_PRIMARY=$(psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -tAc \
  "SELECT NOT pg_is_in_recovery();" 2>/dev/null || echo "f")

if [ "$IS_PRIMARY" = "t" ]; then
  echo "→ Configurando usuarios y schema en PostgreSQL primary..."

  # FIX: Crear usuarios si Patroni bootstrap no los creó
  psql -h 127.0.0.1 -p 5432 -U postgres -d postgres <<SQL
-- Crear usuario odoo si no existe
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'odoo') THEN
    CREATE ROLE odoo WITH LOGIN PASSWORD '${DB_PASSWORD}' CREATEROLE CREATEDB;
    RAISE NOTICE 'Usuario odoo creado';
  ELSE
    ALTER ROLE odoo WITH PASSWORD '${DB_PASSWORD}';
    RAISE NOTICE 'Contraseña de odoo actualizada';
  END IF;
END;
\$\$;

-- Crear usuario replicator si no existe
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'replicator') THEN
    CREATE ROLE replicator WITH LOGIN REPLICATION PASSWORD '${REPLICATOR_PASSWORD:-replicator}';
    RAISE NOTICE 'Usuario replicator creado';
  END IF;
END;
\$\$;

-- Schema y función de autenticación para PgBouncer
CREATE SCHEMA IF NOT EXISTS pgbouncer;
DROP FUNCTION IF EXISTS pgbouncer.user_lookup(text);
CREATE OR REPLACE FUNCTION pgbouncer.user_lookup(in i_username text,
  out uname text, out phash text)
RETURNS record AS \$\$
BEGIN
  SELECT usename, passwd FROM pg_catalog.pg_shadow
  WHERE usename = i_username INTO uname, phash;
  RETURN;
END;
\$\$ LANGUAGE plpgsql SECURITY DEFINER;

GRANT USAGE ON SCHEMA pgbouncer TO odoo;
GRANT EXECUTE ON FUNCTION pgbouncer.user_lookup(text) TO odoo;
SQL
  echo "  Usuarios y schema configurados"
else
  echo "  Replica — usuarios se replicarán desde primary"
fi

# ─── Generar userlist.txt con hash MD5 ──────────────────────────────────────
# FIX: auth_type=md5 funciona con plain text en userlist.txt en PgBouncer 1.25
# scram-sha-256 en userlist.txt requiere el hash SCRAM completo, no plain text
echo "→ Creando userlist.txt..."

cat > /etc/pgbouncer/userlist.txt <<EOF
"odoo" "${DB_PASSWORD}"
"postgres" "${PG_SUPERUSER_PASSWORD}"
EOF

chmod 640 /etc/pgbouncer/userlist.txt
chown postgres:postgres /etc/pgbouncer/userlist.txt

# ─── Configurar PgBouncer ───────────────────────────────────────────────────
echo "→ Generando configuración de PgBouncer..."

cat > /etc/pgbouncer/pgbouncer.ini <<'PGBOUNCER'
;; PgBouncer Configuration — Odoo SaaS HA Cluster
;; Mode: Transaction pooling

[databases]
;; Wildcard: cualquier DB conecta al PostgreSQL local
* = host=127.0.0.1 port=5432

[pgbouncer]
;; ─── Conexión ──────────────────────────────────────────────────
listen_addr = 0.0.0.0
listen_port = 6432
;; FIX: no usar unix_socket en el mismo dir que PG para evitar conflictos
unix_socket_dir = /var/run/pgbouncer

;; ─── Autenticación ─────────────────────────────────────────────
;; auth_type md5 — compatible con plain text en userlist.txt
;; auth_user + auth_query permiten autenticar cualquier rol de PG
;; de forma dinámica sin modificar userlist.txt.
;; pgbouncer.user_lookup() es SECURITY DEFINER (corre como postgres)
;; para poder leer pg_shadow sin privilegios de superusuario.
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
auth_user = odoo
auth_query = SELECT uname, phash FROM pgbouncer.user_lookup($1)

;; ─── Pool Mode ─────────────────────────────────────────────────
pool_mode = transaction

;; ─── Pool Sizes ────────────────────────────────────────────────
default_pool_size = 30
min_pool_size = 5
reserve_pool_size = 5
reserve_pool_timeout = 3

;; ─── Límites ───────────────────────────────────────────────────
max_client_conn = 3000
max_db_connections = 150
max_user_connections = 0

;; ─── Server behaviour ──────────────────────────────────────────
server_reset_query = DISCARD ALL
server_check_query = SELECT 1
server_check_delay = 15
server_lifetime = 3600
server_idle_timeout = 600
server_connect_timeout = 5
server_login_retry = 3
query_timeout = 120
query_wait_timeout = 60

;; ─── Client behaviour ──────────────────────────────────────────
client_idle_timeout = 0
client_login_timeout = 30

;; ─── Misc ──────────────────────────────────────────────────────
application_name_add_host = 1
ignore_startup_parameters = extra_float_digits,search_path

;; ─── Logging ───────────────────────────────────────────────────
log_connections = 0
log_disconnections = 0
log_pooler_errors = 1
stats_period = 60

;; ─── Admin ─────────────────────────────────────────────────────
admin_users = postgres
stats_users = odoo,postgres
PGBOUNCER

chown postgres:postgres /etc/pgbouncer/pgbouncer.ini
chmod 640 /etc/pgbouncer/pgbouncer.ini

# ─── Crear directorio de socket para PgBouncer ───────────────────────────────
# FIX: directorio dedicado para PgBouncer, separado de PostgreSQL
mkdir -p /var/run/pgbouncer
chown postgres:postgres /var/run/pgbouncer

# ─── Servicio systemd — override mínimo ──────────────────────────────────────
# FIX: mantener el servicio pgbouncer de PGDG pero solo añadir dependencia
# El servicio PGDG ya corre como postgres correctamente
echo "→ Configurando servicio systemd de PgBouncer..."

mkdir -p /etc/systemd/system/pgbouncer.service.d

cat > /etc/systemd/system/pgbouncer.service.d/override.conf <<'OVERRIDE'
[Unit]
After=patroni.service
Wants=patroni.service

[Service]
User=postgres
Group=postgres
ExecStart=
ExecStart=/usr/sbin/pgbouncer /etc/pgbouncer/pgbouncer.ini
RuntimeDirectory=pgbouncer
RuntimeDirectoryMode=0755
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536
OVERRIDE

# ─── Iniciar PgBouncer ──────────────────────────────────────────────────────
echo "→ Iniciando PgBouncer..."
systemctl daemon-reload
systemctl enable pgbouncer
systemctl restart pgbouncer || {
  echo "  ⚠️  PgBouncer falló. Diagnóstico:"
  journalctl -u pgbouncer -n 20 --no-pager
  echo ""
  echo "  Intentando continuar igualmente..."
}

sleep 3

# ─── Verificar ──────────────────────────────────────────────────────────────
echo "→ Verificando PgBouncer..."
if pg_isready -h 127.0.0.1 -p 6432 2>/dev/null; then
  echo "  ✅ PgBouncer respondiendo en :6432"
else
  echo "  ⚠️  PgBouncer no responde en :6432"
  journalctl -u pgbouncer -n 10 --no-pager
fi

echo ""
echo "══════════════════════════════════════════════════"
echo "  ✅ PgBouncer configurado"
echo "  Puerto: 6432 (transaction pooling)"
echo "  Auth:   md5 + auth_query via pgbouncer.user_lookup()"
echo "  auth_user: odoo (roles dinámicos sin modificar userlist.txt)"
echo "  Max clientes: 3000 → 150 conexiones a PG"
echo "══════════════════════════════════════════════════"
