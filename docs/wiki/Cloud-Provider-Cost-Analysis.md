# Análisis de costo y auto-scaling por proveedor — Vultr, AWS, Azure, GCP

> Fecha: 2026-07-17. Precios aproximados de lista (on-demand, región US/estándar), verificar antes de
> contratar. Base: arquitectura mínima validada en el testbed cruzoil (branch `feat/cloud-portability`):
> K8s gestionado + 2 workers 2vCPU/4GB + 1 VM PostgreSQL (Patroni single) + object storage S3 +
> ingress por Cloudflare Tunnel (sin LoadBalancer ni IP pública).

## Resumen ejecutivo

| | Vultr | GCP (GKE) | Azure (AKS) | AWS (EKS) |
|---|---|---|---|---|
| Control plane K8s | **$0** | $0 (1 clúster zonal con crédito) / $73 | **$0** (Free tier) / $73 (Standard) | **$73** siempre |
| 2 workers 2vCPU/4GB | ~$40–48 | ~$50 (e2-medium×2 ≈ $25 c/u) | ~$60 (B2s ≈ $30 c/u) | ~$60 (t3.medium ≈ $30 c/u) |
| VM PostgreSQL 2vCPU/4GB | ~$20–24 | ~$25 | ~$30 | ~$30 |
| Storage bloque (StorageClass) | $1/10GB | $0.10/GB (pd-balanced) | ~$0.08/GB (Standard SSD) | $0.08/GB (gp3) |
| Object storage (backups) | ~$6–18 | GCS ~$0.02/GB | Blob ~$0.02/GB | S3 ~$0.023/GB |
| Egress (tráfico salida) | **Incluido por TB** | ~$0.12/GB | ~$0.09/GB | ~$0.09/GB |
| **Total arranque estimado** | **~$70–90/mes** | **~$85–160/mes** | **~$95–170/mes** | **~$170–200/mes** |
| Auto-scaling de workers | Autoscaler VKE nativo | Autoscaler + NAP (el mejor) | Cluster Autoscaler nativo | Karpenter / Cluster Autoscaler |
| Compatibilidad S3 backups | Nativa | GCS modo interop (HMAC) | ⚠️ Blob NO es S3 | Nativa |

**Recomendación:** Vultr para arrancar barato (ya hay cuenta). GCP segunda opción por autoscaling
superior y control plane zonal gratis. AWS solo si hay requisito de cliente/compliance — el control
plane de $73 + NAT + egress lo hacen ~2× Vultr. Azure intermedio, con fricción extra en backups.

Referencia de costo actual: COTAS ≈ $700/mes (modelo IaaS del análisis de negocio 2026-07).

---

## Vultr (VKE)

- **Control plane:** gratis (HA opcional $10/mes).
- **Workers:** vc2-2c-4gb ≈ $20–24/mes c/u. Node pool con `min_nodes`/`max_nodes` → autoscaler integrado.
- **StorageClass:** `vultr-block-storage` (CSI incluido en VKE) — ni Ceph ni Longhorn.
- **Backups:** Vultr Object Storage (S3 nativo) — mismo flujo validado con MinIO, solo cambia endpoint/keys.
- **Auto-scaling:** el portal asigna resource requests por plan (`PLAN_RESOURCES`); tenant nuevo sin
  espacio → pod `Pending` → VKE agrega worker (~2–3 min); escala hacia abajo al liberar.
- **`vultr.env`:** `STORAGE_BACKEND=provider-csi`, `STORAGE_CLASS=vultr-block-storage`,
  `PG_TOPOLOGY=single`, `S3_USE_STUNNEL=false`, sin `K3S_NODES` (VKE reemplaza `infra/k3s-ha/`).

## GCP (GKE Standard)

- **Control plane:** $0.10/h ≈ $73/mes, PERO el crédito mensual de GKE cubre **1 clúster zonal** → $0
  efectivo para un solo clúster (zonal = sin SLA multi-zona del control plane; los workers siguen siendo tuyos).
- **Workers:** e2-medium (2vCPU/4GB) ≈ $25/mes; e2-standard-2 (2vCPU/8GB) ≈ $49/mes. Descuento por uso
  sostenido automático (~20–30%). Spot VMs hasta -70% (viable para tenants Starter tolerantes a reinicio).
- **StorageClass:** `standard-rwo` (pd-balanced, CSI incluido).
- **Backups:** GCS en **modo interoperabilidad S3** (claves HMAC) — pgBackRest y cronjobs funcionan sin
  cambios (`repo1-s3-*` / `BACKUP_S3_ENDPOINT=https://storage.googleapis.com`).
- **Auto-scaling:** el más maduro — Cluster Autoscaler por node pool + **Node Auto-Provisioning** (crea
  node pools nuevos con el tamaño óptimo según los requests). Mismo disparador: requests del portal.
- **Ojo:** egress ~$0.12/GB; con Cloudflare delante el HTML/assets sale del origen igual (el cache de CF
  amortigua estáticos, no el tráfico dinámico de Odoo).

## Azure (AKS)

- **Control plane:** Free tier $0 (hasta ~1000 nodos, sin SLA financiero); Standard $73/mes con SLA.
- **Workers:** B2s (2vCPU/4GB, burstable) ≈ $30/mes; D2as_v5 (2vCPU/8GB) ≈ $70/mes. Reservas 1 año -30/40%.
- **StorageClass:** `managed-csi` (Standard SSD) incluido.
- **Backups — fricción:** Azure Blob **no habla S3**. Tres salidas:
  1. pgBackRest soporta Blob nativo (`repo1-type=azure`) → cambio menor en `05-setup-pgbackrest.sh`
     (parametrizar `repo1-type`);
  2. los cronjobs del clúster usan aws-cli/S3 → correr un **MinIO Gateway/instancia** delante de Blob, o
  3. MinIO standalone en una VM (como el testbed) — $15/mes extra.
  La opción 1+2 combinadas son ~2–3 h de trabajo; presupuestar.
- **Auto-scaling:** Cluster Autoscaler integrado (`az aks nodepool update --enable-cluster-autoscaler
  --min-count 2 --max-count N`). Mismo mecanismo de requests.

## AWS (EKS)

- **Control plane:** $0.10/h ≈ **$73/mes fijo**, sin tier gratis.
- **Workers:** t3.medium (2vCPU/4GB) ≈ $30/mes; t3.large ≈ $60/mes. Spot -60/70%. Savings Plans -30/40%.
- **StorageClass:** `gp3` (EBS CSI addon) — $0.08/GB.
- **Backups:** S3 nativo, cero fricción (el flujo validado funciona tal cual).
- **Costos ocultos típicos:** NAT Gateway ~$32/mes + $0.045/GB (evitable poniendo los nodos en subnet
  pública con security groups estrictos — aceptable porque el ingress es solo por Cloudflare Tunnel y no
  se exponen puertos), egress ~$0.09/GB, EKS addons.
- **Auto-scaling:** **Karpenter** (recomendado, provisiona la instancia exacta por pod pendiente en ~1 min)
  o Cluster Autoscaler clásico con node groups. Mismo disparador de requests.
- Managed DB (RDS PG) desde ~$60/mes si se quisiera eliminar la VM Patroni — no necesario para arrancar.

---

## Cómo se activa el auto-scaling con nuestra plataforma (igual en los 4)

1. El portal ya declara requests/limits por plan en `portal/k8s_utils/manifests.py` (`PLAN_RESOURCES`):
   Starter 100m/512Mi, Pro 250m/1Gi, Enterprise 500m/2Gi.
2. Node pool con autoscaler: `min` = 2 (piso de costo), `max` = techo de gasto que definas.
3. Tenant nuevo que no cabe → pod `Pending` → el autoscaler del proveedor crea el worker → el pod agenda.
   Al eliminar/suspender tenants, el autoscaler consolida y elimina workers ociosos (10–30 min).
4. El "umbral predefinido" se gobierna con los requests por plan — no requiere métricas externas ni
   código nuestro. (Escalado por CPU real de un tenant individual = HPA, capa aparte y opcional.)

## Qué cambia en el repo por proveedor

Con el refactor de portabilidad, cada proveedor es **un archivo de inventario** (`infra/environments/
<proveedor>.env`) + los pasos de creación del clúster gestionado (CLI/terraform, fuera de `infra/k3s-ha/`
que solo aplica a K3s auto-gestionado):

| Variable | vultr.env | gcp.env | azure.env | aws.env |
|----------|-----------|---------|-----------|---------|
| `STORAGE_CLASS` | `vultr-block-storage` | `standard-rwo` | `managed-csi` | `gp3` |
| `BACKUP_S3_ENDPOINT` | Vultr Object Storage | `https://storage.googleapis.com` | (Blob/MinIO — ver fricción) | `https://s3.<region>.amazonaws.com` |
| `PG_TOPOLOGY` | `single` (→ `ha` con ingresos) | ídem | ídem | ídem |
| `PG_NETWORK_CIDR` | VPC Vultr | VPC GCP | VNet | VPC |
| `MANIFEST_EXCLUDE` | `06-odoo-admin*` según entorno | ídem | ídem | ídem |
| cloudflared | in-cluster (`07-cloudflare-tunnel.yaml`) | ídem | ídem | ídem |

Pendiente conocido pre-despliegue en cualquier proveedor: rebuild de la imagen del portal para que
`PG_NETWORK_CIDR` llegue a las NetworkPolicy de tenants (fix ya en el branch, se construye al merge).
