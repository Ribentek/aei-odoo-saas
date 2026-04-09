#!/usr/bin/env python3
"""Create pgbouncer.user_lookup() in template1 + all existing databases."""
import psycopg2, os

host = os.environ.get("POSTGRES_HOST", "postgres.aeisoftware.svc.cluster.local")
pwd  = os.environ.get("POSTGRES_ADMIN_PASSWORD", "VMzDSrRBOunSx2U0yy2Pzsr8PS5BOQ")
port = 5000

FUNC_SQL = """
CREATE SCHEMA IF NOT EXISTS pgbouncer;
CREATE OR REPLACE FUNCTION pgbouncer.user_lookup(
    p_username TEXT, OUT uname TEXT, OUT phash TEXT
) RETURNS record AS
$$
BEGIN
    SELECT usename, passwd INTO uname, phash
    FROM pg_catalog.pg_shadow
    WHERE usename = p_username;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
"""

c = psycopg2.connect(host=host, port=port, dbname="postgres", user="odoo", password=pwd)
c.autocommit = True
cur = c.cursor()
cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false AND datname != 'postgres' ORDER BY datname")
dbs = [r[0] for r in cur.fetchall()]
c.close()

all_dbs = ["template1"] + dbs
print(f"Patching {len(all_dbs)} databases: {all_dbs}")

for db in all_dbs:
    try:
        conn = psycopg2.connect(host=host, port=port, dbname=db, user="odoo", password=pwd)
        conn.autocommit = True
        conn.cursor().execute(FUNC_SQL)
        conn.close()
        print(f"  OK: {db}")
    except Exception as e:
        print(f"  FAIL {db}: {e}")
