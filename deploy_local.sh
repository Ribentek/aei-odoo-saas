#!/bin/bash
set -e
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl patch configmap odoo-admin-conf -n odoo-admin -p '{"data":{"addon-git-branch":"feature/multi-version-support"}}'
echo "Restarting odoo-admin deployment..."
kubectl rollout restart deployment/odoo-admin -n odoo-admin
kubectl rollout status deployment/odoo-admin -n odoo-admin --timeout=180s

POD=$(kubectl get pod -n odoo-admin -l app=odoo-admin -o jsonpath='{.items[0].metadata.name}')
echo "Updating Odoo database module on $POD..."
kubectl exec -n odoo-admin $POD -- odoo -u odoo_k8s_saas -d admin --stop-after-init || echo "Module update reported an error, but continuing..."

echo "Patching Portal deployment image to feature branch..."
kubectl set image deployment/portal portal=ghcr.io/ribentek/aei-odoo-saas/portal:feature-multi-version-support -n aeisoftware
kubectl rollout restart deployment/portal -n aeisoftware
kubectl rollout status deployment/portal -n aeisoftware --timeout=180s
echo "Done!"
