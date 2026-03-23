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
| Imagen portal | `ghcr.io/jpvargassoruco/odoo-saas-mvp/portal:latest` |
| Repo en initContainer | `https://github.com/jpvargassoruco/odoo-saas-mvp.git` (branch `main`, `--depth=1`) |
| Addons copiados | `payment_qr_mercantil`, `odoo_k8s_saas`, `odoo_k8s_saas_subscription` (del repo principal) + `subscription_oca` (clonado de [odoo18-oca-contract](https://github.com/jpvargassoruco/odoo18-oca-contract)) |

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
| `subscription_oca` | Manual | Contratos recurrentes (fork OCA 18.0, clonado de repo externo) |

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

## Notas importantes

- El initContainer `copy-addon` clona `main` con `--depth=1` en **cada restart** del pod.
  Siempre hacer `push` **antes** de `rollout restart`.
- **Ningún módulo se auto-actualiza.** El container Odoo inicia sin flag `-u`.
  Correr el comando `odoo -u <módulo> --stop-after-init` manualmente tras cambios de esquema.
- La BD `postgres` es la instancia admin. Las BDs de clientes SaaS son dinámicas (creadas por el portal).
- El campo `odoo.conf` se renderiza en runtime vía `sed` (placeholders `REPLACE_*`) — **no hay secretos en git**.
- En modo `state=test` (Prueba) el proveedor QR Mercantil **no llama al banco** y usa QRs demo SVG.
