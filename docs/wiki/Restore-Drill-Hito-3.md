# Restore Drill — Hito 3

> **Criterio de aceptación:** restore de una DB tenant desde backup con RTO ≤15 min.  
> **Frecuencia recomendada:** mensual.  
> **Relacionado:** [Runbook: Backup and Restore](Runbook-Backup-and-Restore.md) · [Roadmap: Production Readiness 100 Tenants](Roadmap-Production-Readiness-100-Tenants.md)

---

## Registro de drills

### Drill #1 — PENDIENTE

| Campo | Valor |
|---|---|
| **Fecha** | Programado: 2026-04-20 |
| **Ejecutado por** | — |
| **Método** | pg_dump restore (capa 2) |
| **Tenant de prueba** | — |
| **Timestamp del backup** | — |
| **RTO medido** | — |
| **Resultado** | ⏳ Pendiente |
| **Observaciones** | — |

#### Pasos realizados

```
[ ] 1. Identificar dump en S3: s3://pg-backups/pgdump/odoo_<tenant>/<DATE>.dump
[ ] 2. Descargar dump en pod temporal (postgres:16-alpine)
[ ] 3. Restaurar en DB temporal odoo_<tenant>_restore
[ ] 4. Validar conteo de res_users + ir_attachment
[ ] 5. Registrar tiempo total desde paso 1
[ ] 6. Drop de DB temporal (no pisar producción)
[ ] 7. Completar tabla de resultados arriba
```

---

## Plantilla para drills futuros

```markdown
### Drill #N — YYYY-MM-DD

| Campo | Valor |
|---|---|
| **Fecha** | YYYY-MM-DD HH:MM BOT |
| **Ejecutado por** | Nombre |
| **Método** | pg_dump restore / pgBackRest PITR |
| **Tenant de prueba** | odoo_<id> |
| **Timestamp del backup** | YYYY-MM-DD (dump fecha) |
| **RTO medido** | X min Y seg |
| **Resultado** | ✅ OK / ❌ FALLO |
| **Observaciones** | ... |

#### Pasos realizados

1. ...
2. ...

#### Problemas encontrados

- ...
```
