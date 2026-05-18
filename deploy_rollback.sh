#!/bin/bash
echo "=== 1. Guardando y subiendo los cambios de rollback ==="
git add .
git commit -m "chore(cleanup): remove Odoo 17 custom image building and revert XML template data"
git push origin 18.0

echo "=== 2. Fusionando cambios a Staging (main) ==="
git checkout main
git merge 18.0
git push origin main
git checkout 18.0

echo "=== 3. Esperando 10 segundos para que inicie la compilación ==="
sleep 10

echo "=== 4. Ejecutando Rollout Restart en el clúster ==="
# Reiniciar portal de staging
ssh -i /tmp/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158 "KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout restart deployment portal-stg -n staging"

# Reiniciar odoo de staging
ssh -i /tmp/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158 "KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout restart deployment odoo-stg -n staging"

echo "=== 5. Verificando estado de los deploys ==="
ssh -i /tmp/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158 "KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout status deployment portal-stg -n staging"
ssh -i /tmp/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158 "KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout status deployment odoo-stg -n staging"

echo "=== ¡Proceso Finalizado con Éxito! ==="
