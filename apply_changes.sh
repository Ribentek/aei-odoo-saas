#!/bin/bash
git add .
git commit -m "fix: rename Odoo 17 custom template to AEI SaaS Starter Odoo 17.0 (Mensual)"
git push origin 18.0
git checkout main
git merge 18.0
git push origin main
git checkout 18.0

ssh -i /tmp/k3s_rsa -o StrictHostKeyChecking=no ubuntu@10.40.2.158 "TOKEN=\$(KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get secret git-credentials -n odoo-admin -o jsonpath='{.data.GIT_TOKEN}' | base64 -d) && curl -s -u \"git:\$TOKEN\" -H \"Accept: application/vnd.github.v3.raw\" https://api.github.com/repos/Ribentek/aei-odoo-saas/contents/k8s/07-staging.yaml?ref=main | KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl apply -f -"
