# Odoo SaaS MVP

Single-server Kubernetes SaaS provisioning for Odoo 18, running on **K3s + Cloudflare tunnels**.

---

## Architecture

```
Internet → Cloudflare Tunnel → Traefik (K3s ingress)
                                    ├── admin.aeisoftware.com  → odoo-admin pod (namespace: odoo-admin)
                                    ├── portal.aeisoftware.com → portal FastAPI  (namespace: aeisoftware)
                                    └── <tenant>.aeisoftware.com → per-tenant Odoo pod (namespace: odoo-<tenant>)
                                              ↓
                                    Shared PostgreSQL StatefulSet (namespace: aeisoftware)
```

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
| `subscription_oca` | Contratos de suscripción recurrentes — clonado desde [jpvargassoruco/odoo18-oca-contract](https://github.com/jpvargassoruco/odoo18-oca-contract) (OCA fork 18.0) |

---

## Day 0 — Instalación desde cero

> **Prerequisites:**
> - Ubuntu 22.04 / Debian 12 VM con `root` o `sudo`
> - Dominio DNS apuntando al servidor (o Cloudflare tunnel token)
> - GHCR token con acceso read a `ghcr.io/jpvargassoruco/odoo-saas-mvp/portal:latest`
> - Credenciales MC4 del Banco Mercantil (si usas `payment_qr_mercantil` en producción)

---

### Paso 1 — Clonar el repositorio

```bash
git clone https://github.com/jpvargassoruco/odoo-saas-mvp.git
cd odoo-saas-mvp
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
curl -X POST http://portal.aeisoftware.com/api/v1/instances \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "demo", "plan": "starter", "storage_gi": 10}'

# Verificar estado (poll hasta "running")
curl -H "X-API-Key: $API_KEY" http://portal.aeisoftware.com/api/v1/instances/demo
```

La instancia queda disponible en `https://demo.aeisoftware.com`.

---

## Estructura del repositorio

```
odoo-saas-mvp/
├── k8s/                              # Kubernetes manifests
│   ├── 00-namespace.yaml             # Namespaces (aeisoftware, odoo-admin)
│   ├── 01-secrets.yaml               # Placeholder — secretos se aplican vía .secrets.env
│   ├── 01-traefik.yaml               # Traefik CRDs / IngressRoutes
│   ├── 02-postgres.yaml              # PostgreSQL StatefulSet compartido
│   ├── 02-cloudflare-tunnel.yaml     # Cloudflare tunnel deployment
│   ├── 03-cloudflared.yaml           # cloudflared DaemonSet
│   ├── 03-traefik-middleware.yaml    # Traefik middlewares
│   ├── 04-rbac.yaml                  # ServiceAccount + ClusterRole para portal
│   ├── 05-portal.yaml                # Portal FastAPI (Deployment + Service + Ingress)
│   ├── 06-odoo-admin.yaml            # Odoo admin (Deployment + PVC + ConfigMap + Service + Ingress)
│   └── 07-cloudflare-tunnel.yaml     # Cloudflare tunnel alternativo
├── portal/                           # FastAPI portal API
│   ├── main.py
│   ├── routers/instances.py          # POST/GET/DELETE /api/v1/instances
│   ├── k8s_utils/
│   │   ├── manifests.py              # Generador de manifests por tenant
│   │   └── client.py                 # Wrapper kubernetes SDK
│   ├── Dockerfile
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
│   ├── models/saas_instance.py       # Modelo saas.instance (estados, cron, K8s sync)
│   ├── views/saas_instance_views.xml # Kanban, form, list, menú, acciones
│   ├── data/ir_cron.xml              # Cron: refresh estado cada 2 min
│   └── security/ir.model.access.csv
├── odoo_k8s_saas_subscription/       # Odoo addon — bridge de suscripciones
│   ├── models/saas_instance.py       # Extiende saas.instance con plan/subscription
│   ├── views/                        # Kanban extendido, menús de suscripción, portal
│   ├── data/ir_cron.xml              # Cron: suspender instancias vencidas diariamente
│   └── security/ir.model.access.csv
├── infra/
│   ├── install-k3s.sh               # Instala K3s sin Traefik integrado
│   ├── install-traefik.sh           # Instala Traefik via Helm
│   ├── apply-manifests.sh           # Aplica todos los manifests (lee .secrets.env)
│   └── create-cf-route.sh           # Helper para rutas Cloudflare
├── scripts/
│   └── reset_transactional_data.sql # SQL para limpiar datos transaccionales
├── k8s/dev/
│   └── 00-dev-secrets.yaml          # Secretos para entorno local de desarrollo
├── dev-setup.sh                      # Bootstrap automático K3s local (WSL / Linux)
├── .secrets.env.example              # Plantilla — copiar a .secrets.env y completar
├── .gitignore                        # .secrets.env excluido
└── .github/workflows/
    └── ci.yaml                       # CI: build + push portal:latest a GHCR en push a main
```

> **Nota:** `subscription_oca` no está en este repositorio. El init container lo clona de
> [jpvargassoruco/odoo18-oca-contract](https://github.com/jpvargassoruco/odoo18-oca-contract) (branch `18.0`).

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
2. Push a `ghcr.io/jpvargassoruco/odoo-saas-mvp/portal:latest` y `:$SHA`
3. **Deploy manual:** SSH al servidor y ejecutar `kubectl -n aeisoftware rollout restart deployment/portal`

> El deploy del portal no es automático. Tras el push a GHCR, el operador debe reiniciar el deployment manualmente.

---

## Diagnóstico rápido

```bash
# Ver todos los pods en namespaces relevantes
kubectl get pods -n aeisoftware
kubectl get pods -n odoo-admin

# Logs del pod Odoo admin
POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n odoo-admin $POD -f --tail=100

# Logs del portal
kubectl logs -n aeisoftware deployment/portal -f --tail=100

# Estado del PostgreSQL
kubectl exec -n aeisoftware postgres-0 -- psql -U odoo -d postgres -c "\l"

# Reiniciar odoo-admin
kubectl rollout restart deployment/odoo-admin -n odoo-admin
kubectl rollout status deployment/odoo-admin -n odoo-admin
```

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
- Estados: `draft → provisioning → running → suspended → terminated`
- Edición On-The-Fly de `odoo.conf` y Repositorios Extra (vía K8s ConfigMap)
- Extracción de **Logs** del pod directamente desde la UI de Odoo
- Botones Suspender / Reanudar con scale-down/up en K8s a 0 o 1 réplicas
- Pago QR nativo integrado (flujo SO → Factura → QR Mercantil → SaaS provisioning)
- Cron jobs: sync de estado e inquilinos cada 2 min, suspensión de instancias vencidas diariamente
