# Roadmap: Audit v3 — P0 (One Man SaaS Gaps)

> **Navegación:** [← Home](Home) | [Roadmap Hardening →](Roadmap-Hardening)

Roadmap P0 derivado del contraste del proyecto contra "One Man SaaS Architecture". Documento para ejecución en próximas iteraciones — **NO ejecutado aún**.

**Fecha de generación:** 2026-04-17
**Estado:** 🔲 Pendiente de ejecución

---

## Contexto

Decisiones confirmadas durante la exploración:

- Compliance Bolivia SIN está **fuera de scope**
- Hito 9 resuelto localmente
- Alertas sólo por **email** (sin Slack)
- Backup offsite a **AWS S3** (cuenta existente)

**Hallazgo clave:** `resourcequota_manifest()` y `limitrange_manifest()` ya están implementados en `portal/k8s_utils/manifests.py:475–587` y se aplican a tenants nuevos vía `all_manifests()`. Los tenants existentes son de prueba y serán eliminados — no se necesita script retroactivo.

---

## Nota: dos deployments de cloudflared

| Archivo | Namespace | Réplicas | Imagen | Secret |
|---------|-----------|----------|--------|--------|
| `k8s/03-cloudflared.yaml` | `aeisoftware` | 1 | `2026.3.0` ✓ | `cloudflare-secret` |
| `k8s/07-cloudflare-tunnel.yaml` | `cloudflare` | 2 (HA) | `:latest` ← fix | `cloudflared-token` |

El 07 es el deployment HA activo en producción. El 03 es el setup original — si ya no recibe tráfico, puede eliminarse en P1. El fix `:latest` aplica solo al 07.

---

## Dependency diagram

```
P0 #1 (pin cloudflared 07)  ────────────────────────────────────► commit
P0 #2 (rotate password)     ────────────────────────────────────► commit
P0 #3 (Sentry portal)       ────────────────────────────────────► commit
P0 #4 (cron monitor)        ────────────────────────────────────► commit
P0 #5 (Trivy CI)            ────────────────────────────────────► commit
P0 #6 (smoke test)          ─── requires: P0 #5 (CI yaml abierto)► commit
P0 #7 (AWS S3 offsite)      ────────────────────────────────────► commit
```

---

## P0 Items

### 1. Pin cloudflared image en namespace `cloudflare` (5 min)

**File:** `k8s/07-cloudflare-tunnel.yaml:42`

```yaml
# Antes
image: cloudflare/cloudflared:latest
# Después
image: cloudflare/cloudflared:2026.3.0
```

**Verify:** `kubectl describe pod -l app=cloudflared -n cloudflare | grep Image:` → `2026.3.0`.

---

### 2. Rotar password hardcodeado (1–2h)

**File:** `scripts/fix_pgbouncer_auth.py:6`

El password `VMzDSrRBOunSx2U0yy2Pzsr8PS5BOQ` está como fallback default en git history. Está en el rol `odoo` usado por el portal para provisioning.

**Steps:**
1. `grep -rn "VMzDSrRBOunSx2U0yy2Pzsr8PS5BOQ" .` — auditar todas las apariciones.
2. Generar nuevo: `openssl rand -base64 24`.
3. En pg-node2 primary: `ALTER USER odoo WITH PASSWORD '<nuevo>';` — replica automáticamente.
4. `kubectl patch secret postgres-app-secret -n aeisoftware -p '{"data":{"password":"<base64>"}}'`.
5. Revisar `k8s/06-odoo-admin.yaml` por refs adicionales al secret.
6. `kubectl rollout restart deployment/portal -n aeisoftware && deployment/odoo-admin -n odoo-admin`.
7. **Editar `scripts/fix_pgbouncer_auth.py:6`** — eliminar el fallback hardcodeado:
   ```python
   pwd = os.environ["POSTGRES_ADMIN_PASSWORD"]  # fail fast; sin default
   ```

**Verify:** `psql -h pg-node2 -U odoo -W` con password viejo → auth failure.

---

### 3. Sentry integration — portal FastAPI (2–3h)

**Files:**
- `portal/requirements.txt` — agregar `sentry-sdk[fastapi]>=2.0.0`
- `portal/main.py` — init Sentry después de `import os`, antes de FastAPI app
- `.secrets.env.example` — agregar `SENTRY_DSN_PORTAL` y `SENTRY_DSN_ODOO`

**`portal/main.py` — adición después de `import os`:**
```python
import sentry_sdk
_sentry_dsn = os.getenv("SENTRY_DSN_PORTAL", "")
if _sentry_dsn:
    sentry_sdk.init(dsn=_sentry_dsn, traces_sample_rate=0.0, send_default_pii=False)
```

**Setup externo:**
1. Crear cuenta Sentry free → proyectos: `portal-fastapi`, `odoo-admin`.
2. Agregar `SENTRY_DSN_PORTAL` al K8s Secret del deployment portal.
3. Configurar alerta Sentry: notificar solo en primera ocurrencia por issue, email a `jpvargas@aeisoftware.com`.

**Verify:** `curl http://portal/api/v1/instances/nonexistent-xyz` → evento en Sentry UI + email en <1 min.

---

### 4. Alerta de CronJobs estancados (2–4h)

**File to create:** `k8s/10-monitoring-rules.yaml`

No existe ningún archivo de observabilidad en k8s/ — crear nuevo. El ServiceMonitor en `k8s/05c-portal-servicemonitor.yaml` confirma que kube-prometheus-stack ya está instalado.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: cronjob-staleness
  namespace: monitoring
  labels:
    release: kube-prom
spec:
  groups:
    - name: cronjobs
      rules:
        - alert: CronJobNotRunning
          expr: |
            time() - kube_cronjob_status_last_schedule_time{namespace=~"odoo-.*|backup"} > 86400
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "CronJob {{ $labels.namespace }}/{{ $labels.cronjob }} sin correr >24h"
```

También configurar **UptimeRobot** (free tier) con HTTP monitor sobre `https://admin.aeisoftware.com/` cada 5 min — cubre el caso de cluster-wide down donde Prometheus no puede alertar.

**Verify:** suspender un CronJob del namespace `backup` → alerta llega en <25h.

---

### 5. Trivy image scan en CI (1–2h)

**File:** `.github/workflows/ci.yaml`

Agregar nuevo job después de `build-portal`:
```yaml
  trivy-scan:
    name: Trivy vulnerability scan
    needs: build-portal
    runs-on: ubuntu-latest
    steps:
      - name: Trivy scan
        uses: aquasecurity/trivy-action@0.28.0   # versión pinned, no @master
        with:
          image-ref: ghcr.io/${{ github.repository_owner }}/aei-odoo-saas/portal:${{ github.sha }}
          severity: CRITICAL,HIGH
          exit-code: 0       # warn-only; cambiar a 1 después de establecer baseline
          format: table
```

**Verify:** push commit → step `Trivy vulnerability scan` visible en Actions log con reporte.

---

### 6. Smoke test e2e del happy path (1 día)

**New file:** `tests/e2e_smoke.py`

```python
import os, time
import pytest, httpx

PORTAL = os.environ["PORTAL_URL"]
API_KEY = os.environ["SAAS_PORTAL_KEY"]
TENANT = f"smoke-{int(time.time())}"
HEADERS = {"X-API-Key": API_KEY}

def test_create_wait_delete():
    r = httpx.post(f"{PORTAL}/api/v1/instances",
                   json={"tenant_id": TENANT, "plan": "starter", "storage_gi": 10},
                   headers=HEADERS, timeout=30)
    assert r.status_code == 200, r.text
    try:
        for _ in range(60):  # 5 min max
            s = httpx.get(f"{PORTAL}/api/v1/instances/{TENANT}", headers=HEADERS, timeout=10).json()
            if s.get("status") == "ready":
                return
            time.sleep(5)
        pytest.fail(f"Tenant nunca llegó a ready; último status: {s}")
    finally:
        httpx.delete(f"{PORTAL}/api/v1/instances/{TENANT}", headers=HEADERS, timeout=30)
```

**CI:** agregar job `smoke-test` en `.github/workflows/ci.yaml` que corre solo en push a `main`/`18.0`, usando secrets de staging `PORTAL_URL` + `SAAS_PORTAL_KEY`. Gate: bloquea merge a `18.0` si falla.

**Verify:** correr localmente contra staging → tenant llega a `ready`, cleanup borra namespace.

---

### 7. Backup offsite a AWS S3 (medio día)

**New file:** `k8s/backup/30b-cronjob-sync-offsite.yaml`

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: pg-sync-offsite
  namespace: backup
spec:
  schedule: "0 6 * * *"   # diario a las 06:00, después del pgdump (30-cronjob-pgdump.yaml)
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: sync
              image: amazon/aws-cli:2.22.0   # pinned
              envFrom:
                - secretRef:
                    name: aws-offsite-creds
              command:
                - /bin/sh
                - -c
                - |
                  aws s3 sync s3://pg-backups/ s3://$AWS_BUCKET_OFFSITE/ \
                    --region $AWS_REGION --no-progress
              resources:
                requests: {cpu: 50m, memory: 64Mi}
                limits:   {cpu: 200m, memory: 128Mi}
```

**También crear** K8s Secret `aws-offsite-creds` en namespace `backup` con claves: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_BUCKET_OFFSITE`, `AWS_REGION`.

**Setup en AWS:**
1. Crear bucket S3 `aei-pg-backups-offsite` en la cuenta existente.
2. Crear IAM user con política restrictiva: solo `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` sobre ese bucket.
3. Lifecycle rule en el bucket: expirar objetos después de 90 días.

**Verify:** `aws s3 ls s3://aei-pg-backups-offsite/` → archivos del último dump presentes.

---

## Files modificados por P0

| # | Archivos |
|---|---------|
| 1 | `k8s/07-cloudflare-tunnel.yaml` (línea 42) |
| 2 | `scripts/fix_pgbouncer_auth.py` (línea 6); SSH pg-node2; kubectl patch secret; rollout restart |
| 3 | `portal/requirements.txt`, `portal/main.py`, `.secrets.env.example` |
| 4 | `k8s/10-monitoring-rules.yaml` (nuevo) |
| 5 | `.github/workflows/ci.yaml` |
| 6 | `tests/e2e_smoke.py` (nuevo), `.github/workflows/ci.yaml` |
| 7 | `k8s/backup/30b-cronjob-sync-offsite.yaml` (nuevo); kubectl create secret `aws-offsite-creds` |

---

## Checklist de verificación end-to-end

1. `kubectl describe pod -l app=cloudflared -n cloudflare | grep Image:` → `2026.3.0`
2. `psql -h pg-node2 -U odoo -W` con password viejo → auth failure
3. Error en portal → evento en Sentry UI + email en <1 min
4. `kubectl get prometheusrule -n monitoring cronjob-staleness` → existe
5. Push PR → step Trivy visible en Actions log
6. Push a `main` → smoke test corre y pasa en CI
7. `aws s3 ls s3://aei-pg-backups-offsite/` → archivos de hoy presentes

---

## Commits propuestos

1. `fix(k8s): pin cloudflared to 2026.3.0 in cloudflare namespace`
2. `security(pg): rotate portal DB password, remove hardcoded fallback`
3. `feat(observability): Sentry SDK integration for portal`
4. `feat(monitoring): PrometheusRule for stale CronJobs`
5. `ci: add Trivy image scan (warn-only, pinned action version)`
6. `test(e2e): smoke test for tenant provisioning happy path`
7. `feat(backup): AWS S3 offsite sync CronJob for pg-backups`

---

## P1 — siguientes 1-2 semanas (referencia)

| Item | Esfuerzo |
|------|----------|
| Evaluar si `k8s/03-cloudflared.yaml` (namespace aeisoftware) puede eliminarse | 30 min |
| Traefik HA (2 réplicas + anti-affinity) | medio día |
| PDB para tenants (hito 6) | 2h |
| Restore drill automático mensual | 1 día |
| RUNBOOK.md (5 incidentes comunes) | medio día |
| Fix drift IaC: `local-path` → `ceph-rbd` en odoo-admin | medio día |
| `resources.requests/limits` en init containers | 2h |
| HPA portal + odoo-admin | 2-3h |
| Sealed Secrets | medio día |
| SMTP DKIM/SPF/DMARC | 2-3h |
