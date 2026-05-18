#!/bin/bash
ssh -i /tmp/k3s_rsa -o StrictHostKeyChecking=no -o BatchMode=yes ubuntu@10.40.2.158 "KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl logs -n odoo-administrator-sub00178 deploy/odoo --tail=200" > /home/kali/aei-odoo-saas/tenant_logs.log 2>&1
