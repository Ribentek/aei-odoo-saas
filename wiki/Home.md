# Odoo SaaS MVP — Wiki

Single-node K3s platform that hosts multiple Odoo 18 tenants with automated provisioning.

## Architecture

- [[High-Level Design (HLD)]] — component map, routing, database, security
- [[Low-Level Design (LLD)]] — K8s resources, configs, env vars, probes

## Components

- [[Odoo SaaS Addon]] — `odoo_k8s_saas` module: models, actions, cron, views
- [[Sales Integration]] — **quote-to-provision pipeline**: sale order → invoice → payment → auto-provision
- [[Subscription Integration]] — **recurring billing**: subscription_oca + bridge module for SaaS lifecycle management _(NEW)_
- [[Portal API Reference]] — FastAPI endpoints for provisioning, status, deletion

## Operations

- [[DAY0 Install From Scratch]] — full setup walkthrough for a fresh VM
- [[Operational Runbook]] — DAY1/DAY2 health checks, debugging, scaling, backups
- [[CICD Pipeline]] — GitHub Actions build, push, and deploy workflow
