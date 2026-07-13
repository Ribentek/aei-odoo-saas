# Odoo SaaS MVP

Multi-node Kubernetes SaaS provisioning for Odoo 18, running on **K3s + Ceph RBD + PostgreSQL HA + Cloudflare tunnels**.

> 📖 Full documentation: [**docs/wiki/Home.md**](docs/wiki/Home.md)

---

## Architecture

```
Internet → Cloudflare Tunnel → Traefik (K3s ingress)
                                    ├── admin.aeisoftware.com  → odoo-admin pod (namespace: odoo-admin)
                                    ├── portal.aeisoftware.com → portal FastAPI  (namespace: aeisoftware)
                                    └── <tenant>.aeisoftware.com → per-tenant Odoo pod (namespace: odoo-<tenant>)
                                              ↓
                                    PostgreSQL HA (HAProxy:5000 — primary directo)
```

> **Nota:** PgBouncer fue eliminado de la arquitectura. Todo el tráfico (workers, admin, DDL)
> va directamente a través de HAProxy en el puerto **5000**. Esto permite LISTEN/NOTIFY nativo
> (longpolling sin `bus_alt_connection`) y evita problemas de compatibilidad con transacciones.

**Init container flow (every pod restart):**
1. `copy-addon` (alpine/git) — clona el repo `main` con `--depth=1` y copia los addons a `/mnt/extra-addons` (incluye `subscription_oca` desde un fork OCA externo)
2. `render-config` (alpine) — usa `sed` para reemplazar placeholders `REPLACE_*` con valores de secretos en `odoo.conf`
3. `odoo:18` inicia leyendo `/etc/odoo/odoo.conf` y `/mnt/extra-addons`

---

## Addons incluidos

| Módulo | Descripción |
|---|---|
| `payment_qr_mercantil` | Pago por QR — Banco Mercantil Santa Cruz (mc4.com.bo) |
| `odoo_k8s_saas` | UI admin de instancias SaaS (kanban, estados, acciones K8s) |
| `odoo_k8s_saas_subscription` | Bridge de suscripciones OCA ↔ SaaS instances |
| `subscription_oca` | Contratos de suscripción recurrentes — clonado desde [Ribentek/odoo18-oca-contract](https://github.com/Ribentek/odoo18-oca-contract) (OCA fork 18.0) |

---

## Security Features

| Feature | Implementation |
|:--------|:-------------|
| SQL Injection Protection | DDL queries use `psycopg2.sql.Identifier()` — no f-strings |
| NetworkPolicy | Default-deny + whitelist for `odoo-admin`; per-tenant isolation |
| PodDisruptionBudgets | `minAvailable: 1` for portal and odoo-admin |
| Liveness Probes | All services: portal (`/healthz`), odoo-admin (`/web/health`), tenants |
| Tenant ID Validation | `@api.constrains` regex + min 2 chars + SQL `UNIQUE` |
| Secrets Management | K8s Secrets + ConfigMap (passwords embedded at provision time) |
| Image Pinning | `cloudflared:2026.3.0`, no `:latest` |
| Data Protection on Close | Subscription "Closed" → `action_stop()` suspends pods, preserves PVC+DB |

---

## Branch Strategy & Namespace Topology

| Branch | Ambiente | Namespace K8s | Pod / Deploy | Dominio |
|:-------|:---------|:-------------|:------------|:--------|
| `main` | **Staging** | `staging` | `odoo-stg` | `staging.aeisoftware.com` |
| `18.0` | **Producción** | `odoo-admin` | `odoo-admin` | `admin.aeisoftware.com` / `www.aeisoftware.com` |

> **Regla:** Todos los cambios van a `main` primero, se prueban en Staging, y luego se promueven a `18.0`.

> [!CAUTION]
> **Para reiniciar Staging** usar siempre:
> ```bash
> kubectl rollout restart deployment/odoo-stg -n staging
> ```
> **Para reiniciar Producción** (solo en ventanas de mantenimiento):
> ```bash
> kubectl rollout restart deployment/odoo-admin -n odoo-admin
> ```
> ⚠️ `odoo-admin` es **PRODUCCIÓN**. `odoo-stg` es **STAGING**.

---

## Day 0 — Instalación desde cero

> **Prerequisites:**
> - Ubuntu 22.04 / Debian 12 VM con `root` o `sudo`
> - Dominio DNS apuntando al servidor (o Cloudflare tunnel token)
> - GHCR token con acceso read a `ghcr.io/Ribentek/aei-odoo-saas/portal:latest`
> - Credenciales MC4 del Banco Mercantil (si usas `payment_qr_mercantil` en producción)

---

### Paso 1 — Clonar el repositorio

```bash
git clone https://github.com/Ribentek/aei-odoo-saas.git
cd aei-odoo-saas
```

---

### Paso 2 — Crear el archivo de secretos

```bash
cp .secrets.env.example .secrets.env
nano .secrets.env          # completar TODOS los valores — nunca hacer commit de este archivo
```

**Variables requeridas en `.secrets.env`:**

| Variable | Requerida | Descripción | Ejemplo |
|---|---|---|---|
| `DB_PASSWORD` | ✅ sí | Contraseña del usuario `odoo` en PostgreSQL | `S3cre7DB!` |
| `ADMIN_PASSWD` | ✅ sí | Master password de Odoo (para gestión de bases de datos) | `MasterP4ss!` |
| `API_KEY` | ✅ sí | Clave secreta del portal FastAPI (Bearer token) | `uuid4-largo` |
| `CLOUDFLARE_TUNNEL_TOKEN` | ⚡ opcional | Token del tunnel en Cloudflare Zero Trust Dashboard | `eyJ...` |

> **`.secrets.env` está en `.gitignore` — nunca se commitea.**
> El script `apply-manifests.sh` lo inyecta como Kubernetes Secrets al momento del despliegue.

---

### Paso 3 — Instalar K3s (sin Traefik integrado)

```bash
bash infra/install-k3s.sh
```

Instala K3s con `--disable=traefik` y espera a que el nodo quede `Ready`.

---

### Paso 4 — Instalar Traefik via Helm

```bash
bash infra/install-traefik.sh
```

Instala Traefik con Helm en el namespace `kube-system` como controlador de ingress.

---

### Paso 5 — Aplicar todos los manifests

```bash
bash infra/apply-manifests.sh
```

El script:
1. Lee `.secrets.env` y valida que no haya placeholders `change_me`
2. Crea namespaces `aeisoftware` y `odoo-admin` si no existen
3. Crea el PVC `odoo-admin-data` (20Gi) si no existe
4. Crea Kubernetes Secrets a partir de las variables env (nunca desde archivos git)
5. Aplica los manifests `k8s/0*.yaml` en orden (saltando `01-secrets.yaml`)
6. Espera a que PostgreSQL esté `Ready`

> **Dry-run:** `bash infra/apply-manifests.sh --dry-run` muestra qué se aplicaría sin tocar el cluster.

---

### Paso 6 — Configurar el proveedor de pago QR Mercantil

Una vez que el pod `odoo-admin` esté `Running`:

1. Ir a **Contabilidad → Configuración → Diarios de pago** (o **Contabilidad → Configuración → Proveedores de pago**)
2. Buscar **"QR Mercantil"** y abrirlo
3. En la pestaña **Credenciales** completar:

| Campo Odoo | Header MC4 API | Descripción |
|---|---|---|
| **API Key (Login)** | `apikey` | Clave para el endpoint de autenticación (`/autenticacion/v1/generarToken`) |
| **API Key Servicio** | `apikeyServicio` | Clave para los endpoints de QR (`/api/v1/generaQr`, `/api/v1/estadoTransaccion`) |
| **Usuario API** | `username` en body | Usuario para obtener el JWT |
| **Contraseña API** | `password` en body | Contraseña para obtener el JWT |
| **URL Base API** | — | Default: `https://sip.mc4.com.bo:8443` |
| **Webhook URL** | `callback` en generaQr | Ej: `https://admin.aeisoftware.com/payment/qr_mercantil/webhook` |

4. En la pestaña **Configuración**:
   - **Estado** → `Producción` (para llamadas reales al banco) o `Prueba` (modo demo, sin llamadas reales)

> **Modo Prueba (`state=test`):** genera QRs SVG ficticios, nunca llama al banco.
> El botón "Simular Pago" en el checkout confirma la transacción directamente.
> Ideal para flujos de testing de SaaS provisioning sin credenciales reales.

---

### Paso 7 — Instalar / actualizar módulos Odoo

Solo la **primera vez** (o cuando hay cambios de esquema en modelos):

```bash
POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')

# Instalar todos los módulos del repo
kubectl exec -n odoo-admin $POD -- \
  odoo -u payment_qr_mercantil,odoo_k8s_saas,odoo_k8s_saas_subscription,subscription_oca \
  -d admin --stop-after-init --no-http

# Luego restart limpio
kubectl rollout restart deployment/odoo-admin -n odoo-admin
kubectl rollout status deployment/odoo-admin -n odoo-admin
```

> **Nota:** ningún módulo se auto-actualiza en restart. El container Odoo inicia sin flag `-u`.
> Para actualizar módulos tras cambios de esquema, ejecutar el comando `odoo -u <módulo>` manualmente.
> Ver la sección "Cuando hay cambios de esquema" en [DEPLOY.md](DEPLOY.md) para el procedimiento detallado.

---

## Flujo de despliegue estándar (Day N)

```bash
# 1. Commit y push del código
git add <archivos>
git commit -m "tipo(módulo): descripción"
git push origin main

# 2. Rollout restart — el initContainer clona el repo actualizado
kubectl rollout restart deployment/odoo-admin -n odoo-admin

# 3. Esperar Ready
kubectl rollout status deployment/odoo-admin -n odoo-admin

# 4. Verificar logs
POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n odoo-admin $POD -f
```

> No hay CI/CD automático para odoo-admin. El portal **sí** tiene CI via GitHub Actions ([`ci.yaml`](.github/workflows/ci.yaml)) que hace build + push a GHCR. El deploy del portal es manual después del push.

---

## Provisionamiento de un Tenant (Day 1)

```bash
# Crear instancia
curl -X POST https://portal.aeisoftware.com/api/v1/instances \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "demo", "plan": "starter", "storage_gi": 10}'

# Verificar estado (poll hasta "ready")
curl -H "X-API-Key: $API_KEY" https://portal.aeisoftware.com/api/v1/instances/demo
```

La instancia queda disponible en `https://demo.aeisoftware.com`.

Each tenant gets:
- Dedicated K8s namespace (`odoo-<tenant_id>`)
- Dedicated PostgreSQL database + role
- PVC with `local-path` (dev) or `ceph-rbd` (prod) StorageClass
- NetworkPolicy (tenant isolation)
- Liveness probe (`/web/health`)
- Ingress via Traefik → Cloudflare tunnel
- **Plan-specific resources** (workers, CPU, RAM) — see table below

---

## Plan de Recursos por Tier

Cada plan SaaS provisiona instancias con recursos de cómputo diferenciados:

| Plan | Workers Odoo | CPU Request | CPU Limit | RAM Request | RAM Limit | Almacenamiento |
|:-----|:------------:|:-----------:|:---------:|:-----------:|:---------:|:--------------:|
| **Starter** | 2 | 100m | 500m | 512Mi | 1Gi | 10 GB |
| **Pro** | 4 | 250m | 1 core | 1Gi | 2Gi | 50 GB |
| **Enterprise** | 8 | 500m | 2 cores | 2Gi | 4Gi | 100 GB |

Definidos en `portal/k8s_utils/manifests.py` → `PLAN_RESOURCES`.

### Upgrade de Plan (sin pérdida de datos)

```bash
# Cambiar un tenant existente de starter a pro (en vivo, sin borrar datos)
curl -X PATCH https://portal.aeisoftware.com/api/v1/instances/demo/upgrade \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"plan": "pro", "storage_gi": 50}'
```

El endpoint:
1. Actualiza el `ConfigMap` odoo.conf (`workers`, `max_cron_threads`)
2. Parchea el `Deployment` con nuevos limits de CPU/RAM
3. Reinicia el pod para que tome los cambios (rolling restart)

> **Regla operativa:** Los upgrades de plan se hacen **editando la plantilla (template) en la suscripción OCA**,
> no creando una nueva suscripción. Crear una nueva suscripción generará una instancia nueva en blanco.

---

## Ciclo de vida de Suscripciones y Instancias

| Evento | Acción en Odoo | Resultado en K8s |
|:-------|:--------------|:----------------|
| Confirmar venta + Activar suscripción | Stage → *In Progress* | Pod aprovisionado, DB creada |
| Cambiar plantilla (Upgrade/Downgrade) | Template → Pro/Enterprise | ConfigMap actualizado, Deployment re-aplicado |
| Suscripción vence (cron) | `_cron_suspend_overdue` | Pod escalado a 0 réplicas (datos preservados) |
| Cerrar suscripción | Stage → *Closed* | Pod suspendido (`action_stop()`) — **datos preservados** |
| Borrado manual por Sysadmin | `action_request_delete()` desde `saas.instance` | Namespace + DB eliminados definitivamente |

> ⚠️ **IMPORTANTE:** Cerrar una suscripción **suspende** la instancia, no la borra.
> El borrado definitivo debe ser ejecutado manualmente por el administrador del sistema
> desde la vista **SaaS → Instancias** usando el botón **"Eliminar instancia"**.

## Estructura del repositorio

```
aei-odoo-saas/
├── k8s/                              # Kubernetes manifests
│   ├── 00-namespace.yaml             # Namespaces (aeisoftware, odoo-admin)
│   ├── 00-network-policy.yaml        # Base network policies
│   ├── 01-secrets.yaml               # Placeholder — secretos se aplican vía .secrets.env
│   ├── 01-traefik.yaml               # Traefik CRDs / IngressRoutes
│   ├── 02-postgres.yaml              # PostgreSQL service
│   ├── 02-postgres-external.yaml     # PostgreSQL HA external endpoints
│   ├── 02-postgres-config.yaml       # PostgreSQL config
│   ├── 02-cloudflare-tunnel.yaml     # Cloudflare tunnel (pinned v2026.3.0)
│   ├── 03-cloudflared.yaml           # cloudflared DaemonSet (pinned v2026.3.0)
│   ├── 03-traefik-middleware.yaml    # Traefik middlewares
│   ├── 04-rbac.yaml                  # ServiceAccount + ClusterRole + Binding (aeisoftware + staging)
│   ├── 05-portal.yaml                # Portal FastAPI (4 workers, liveness probe)
│   ├── 05b-pdb.yaml                  # PodDisruptionBudgets (portal + odoo-admin)
│   ├── 06-odoo-admin.yaml            # Odoo admin (Deployment + PVC + ConfigMap + Service + Ingress)
│   ├── 06b-odoo-admin-netpol.yaml    # NetworkPolicy for odoo-admin (default-deny + whitelist)
│   ├── 07-staging.yaml               # Complete staging environment
│   ├── 07-cloudflare-tunnel.yaml     # Cloudflare tunnel alternativo
│   ├── 08-backup-cronjob.yaml        # Backup CronJob
│   ├── dev/                          # Dev environment manifests
│   └── prod/
│       └── 06-odoo-admin-cloud.yaml  # Production odoo-admin (liveness probe)
├── portal/                           # FastAPI portal API
│   ├── main.py
│   ├── routers/instances.py          # Lifecycle API: POST create, PATCH upgrade, DELETE, stop/start
│   ├── k8s_utils/
│   │   ├── manifests.py              # PLAN_RESOURCES + manifest generators per tenant
│   │   └── client.py                 # K8s SDK wrapper (apply, patch, scale, restart)
│   ├── Dockerfile                    # uvicorn --workers 4
│   └── requirements.txt
├── payment_qr_mercantil/             # Odoo addon — pago por QR Banco Mercantil
│   ├── models/
│   │   ├── payment_provider.py       # Credenciales, token cache, llamadas MC4 API
│   │   └── payment_transaction.py    # Renderizado QR, webhook handler, estado TX
│   ├── controllers/
│   │   └── main.py                   # /payment/qr_mercantil/webhook, /simulate
│   ├── static/src/js/
│   │   └── qr_mercantil_form.js      # Frontend: polling, simulación, doble-click guard
│   ├── views/
│   │   └── payment_provider_views.xml # Formulario de configuración (tabs nativos Odoo 18)
│   └── data/payment_method.xml       # Registro del método de pago
├── odoo_k8s_saas/                    # Odoo addon — UI admin SaaS instances
│   ├── models/saas_instance.py       # saas.instance: action_provision, action_upgrade, action_stop/resume
│   ├── views/saas_instance_views.xml # Kanban, form, list, menú, acciones
│   ├── data/ir_cron.xml              # Cron: refresh estado cada 2 min
│   └── security/ir.model.access.csv
├── odoo_k8s_saas_subscription/       # Odoo addon — bridge de suscripciones OCA ↔ K8s
│   ├── models/sale_subscription.py   # Hooks: stage_id + template_id; cron override (try/except guard)
│   ├── views/                        # Kanban extendido, menús de suscripción, portal
│   ├── data/ir_cron.xml              # Cron: suspender instancias vencidas diariamente
│   └── security/ir.model.access.csv
├── scripts/
│   ├── reset_transactional_data.sql  # SQL para limpiar datos transaccionales
│   └── backup-odoo-admin.sh         # Full backup (DB + filestore)
├── infra/
│   ├── install-k3s.sh               # Instala K3s sin Traefik integrado
│   ├── install-traefik.sh           # Instala Traefik via Helm
│   ├── apply-manifests.sh           # Aplica todos los manifests (lee .secrets.env)
│   └── create-cf-route.sh           # Helper para rutas Cloudflare
├── k8s/dev/
│   └── 00-dev-secrets.yaml          # Secretos para entorno local de desarrollo
├── dev-setup.sh                      # Bootstrap automático K3s local (WSL / Linux)
├── .secrets.env.example              # Plantilla — copiar a .secrets.env y completar
├── .gitignore                        # .secrets.env excluido
└── .github/workflows/
    └── ci.yaml                       # CI: build + push portal:latest a GHCR en push a main
```

> **Nota:** `subscription_oca` no está en este repositorio. El init container lo clona de
> [Ribentek/odoo18-oca-contract](https://github.com/Ribentek/odoo18-oca-contract) (branch `18.0`).

---

## Cloudflare Tunnel

Regla wildcard configurada en Zero Trust Dashboard:
```
*.aeisoftware.com → http://traefik.kube-system.svc.cluster.local:80
```

No se requieren cambios DNS por tenant.  
El `IngressRoute` de Traefik por tenant enruta por header `Host:`.

---

## GitHub Actions CI (portal solamente)

El workflow [`.github/workflows/ci.yaml`](.github/workflows/ci.yaml) usa permisos `packages: write` y `GITHUB_TOKEN` (automático, no requiere configuración manual).

En cada push a `main`:
1. Build imagen Docker del portal con Docker Buildx + layer cache
2. Push a `ghcr.io/ribentek/aei-odoo-saas/portal:latest` y `:$SHA`
3. **Deploy manual:** SSH al servidor y ejecutar `kubectl -n aeisoftware rollout restart deployment/portal`

> El deploy del portal no es automático. Tras el push a GHCR, el operador debe reiniciar el deployment manualmente.

---

## Módulos de terceros

> **Nota:** El módulo `subscription_oca` ha sido incorporado al repositorio en `external_addons/` para simplificar el despliegue. Anteriormente se clonaba de un repositorio externo.

---

## Diagnóstico rápido

```bash
# Ver todos los pods en namespaces relevantes
kubectl get pods -n aeisoftware
kubectl get pods -n odoo-admin
kubectl get pods -n staging

# Logs del pod Odoo admin
POD=$(kubectl get pod -n odoo-admin -l app=odoo -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n odoo-admin $POD -f --tail=100

# Logs del portal
kubectl logs -n aeisoftware deployment/portal -f --tail=100

# NetworkPolicies
kubectl get netpol -A

# PDBs
kubectl get pdb -A

# Liveness probes status
kubectl get deploy -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PROBE:.spec.template.spec.containers[0].livenessProbe.httpGet.path'
```

### Reset completo (Eliminar SaaS + Tenant DBs + PVCs)

```bash
# Borra las bases de datos en Postgres
## Nota: el cluster PG corre en 3 VMs externas (no en K8s). Para eliminar DBs/roles de tenants manualmente, ver [[PostgreSQL Cluster Operations]] en la wiki.

# Borra la instancia en K8s (deployments, servicios, PVCs y secretos)
python3 infra/delete-instance.py demo-company
```

---

## Multi-Version y Custom Images

La plataforma soporta la creación de instancias con versiones específicas de Odoo (17.0, 18.0, 19.0) y el uso de **imágenes de Docker personalizadas** por cliente (ej: `ghcr.io/ribentek/aei-odoo-saas/custom-odoo-images:19.0`).
- La configuración de versión se define en el **Producto SaaS** (Pestaña "SaaS Configuration").
- Cuando se vende una suscripción con una imagen personalizada, Kubernetes forzará un `imagePullPolicy: Always` para asegurar que el tenant siempre utilice la última versión de su imagen custom sin depender del caché del nodo.
- Los módulos integrados en la imagen personalizada deben ubicarse en `/opt/custom-addons` para evitar ser sobrescritos por los volúmenes efímeros de Kubernetes.

---

## Batería de Pruebas Sugeridas (QA)

### Pruebas desde la perspectiva del Administrador (Admin Odoo-SaaS-MVP)
1. **Creación de Producto:** Crear un nuevo producto SaaS, ir a la pestaña "SaaS Configuration", asignar `Odoo Version = Custom Image`, e introducir el tag de una imagen alojada en Github en el campo `Custom Odoo Image`.
2. **Venta Automática:** Crear un presupuesto para un cliente con el producto SaaS y confirmarlo. Verificar que el estado de la venta avance y la suscripción pase a "In Progress".
3. **Provisionamiento:** Navegar a "SaaS -> Instancias" en el backend administrativo. Verificar que el ORM haya creado la instancia automáticamente heredando la versión y ruta de imagen del producto hijo.
4. **Verificación de Logs:** Acceder a la vista formulario de la instancia SaaS y confirmar a través de los botones nativos "Odoo Logs" e "Init Logs" que la instancia booteó sin errores y descargó las credenciales de la API FastAPI.
5. **Gestión de Ciclo de Vida:** Ejecutar la acción "Suspend" para escalar el pod a 0 (reducción de consumo) y "Resume" para escalar a 1. Comprobar en Kubernetes que los pods bajen o suban efectivamente.

### Pruebas desde la perspectiva del Cliente Final (Tenant SaaS)
1. **Recepción de Credenciales:** Tras confirmarse el pago u orden, verificar la recepción del correo electrónico automatizado enviado por el Cron Job, conteniendo la URL única, usuario (`admin`) y la contraseña generada aleatoriamente.
2. **Acceso al Sistema:** Navegar a la URL autogenerada para el Tenant (vía Traefik ingress) e ingresar con las credenciales exactas del email.
3. **Verificación de Módulos Base:** Confirmar que el Wizard de inicialización cargó el Odoo básico en blanco sin romper dependencias de Python.
4. **Verificación de Addons Custom (Si aplica):** Comprobar que los addons inyectados a nivel de la imagen Docker en `/opt/custom-addons` aparecen correctamente en la lista de Aplicaciones listas para instalar sin arrojar exclusiones de `FileNotFoundError` en logs.
5. **Persistencia Volumétrica:** Crear un registro transaccional cualquiera (ej. un Cliente). Solicitar al Admin simular una Suspensión temporal y posterior Reactivación de la instancia. Retornar al sistema y validar que la base de datos Postgres y el volumen Ceph RBD conservaron la integridad del registro intacta.

---

## Teardown

### Solo los workloads Odoo/portal (mantiene K3s)

```bash
kubectl delete namespace odoo-admin aeisoftware --ignore-not-found
kubectl get ns -o name | grep '^namespace/odoo-' | xargs -r kubectl delete
```

### Teardown completo (elimina K3s y todos los datos)

```bash
/usr/local/bin/k3s-uninstall.sh
```

> ⚠️ **Destructivo.** Todos los PVCs, bases de datos y estado del cluster se pierden permanentemente.

---

## Admin Odoo

Acceso en `https://admin.aeisoftware.com`

Los addons proveen:
- **App SaaS** en el menú principal (kanban de instancias)
- Estados: `draft → provisioning → ready → suspended → pending_delete → deleted`
- Validación de `tenant_id`: mínimo 2 chars, regex `[a-z0-9\-]`, SQL `UNIQUE`
- Edición On-The-Fly de `odoo.conf` y Repositorios Extra (vía K8s ConfigMap)
- Extracción de **Logs** del pod directamente desde la UI de Odoo
- Botones Suspender / Reanudar con scale-down/up en K8s a 0 o 1 réplicas
- Hook automático: Upgrade de plantilla de suscripción → actualiza workers/CPU/RAM en K8s
- Cron jobs: sync de estado e inquilinos cada 2 min, suspensión de instancias vencidas diariamente
- Per-tenant NetworkPolicy: aislamiento automático de cada namespace
- Cron de facturación OCA con `try/except` por subscripción — un registro corrupto no detiene el proceso
