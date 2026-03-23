"""
models/saas_instance.py

Tracks SaaS tenant instances as Odoo records.
Calls the portal API to provision / deprovision.
No dependency on sale, contract, or subscription modules.
"""
import json
import logging
import os
import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)

PORTAL_URL = os.getenv("SAAS_PORTAL_URL", "http://portal.aeisoftware.svc.cluster.local:8000")
PORTAL_KEY = os.getenv("SAAS_PORTAL_KEY", "")


class SaasInstance(models.Model):
    _name = "saas.instance"
    _description = "SaaS Tenant Instance"
    _order = "create_date desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(string="Instance Name", required=True, help="Human-readable name")
    tenant_id = fields.Char(
        string="Tenant ID", required=True, index=True,
        help="Slug used as subdomain: e.g. 'demo' → demo.aeisoftware.com",
    )
    url = fields.Char(string="URL", readonly=True)
    namespace = fields.Char(string="K8s Namespace", readonly=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("provisioning", "Provisioning"),
            ("ready", "Ready"),
            ("suspended", "Suspended"),
            ("error", "Error"),
            ("deleted", "Deleted"),
        ],
        default="draft", required=True, tracking=True,
    )
    plan = fields.Selection(
        [("starter", "Starter"), ("pro", "Pro"), ("enterprise", "Enterprise")],
        default="starter", required=True,
    )
    storage_gi = fields.Integer(string="Storage (GB)", default=10)
    error_msg = fields.Text(string="Error", readonly=True)
    partner_id = fields.Many2one("res.partner", string="Customer")
    sale_order_id = fields.Many2one(
        "sale.order", string="Sale Order", ondelete="set null",
        help="Sale order that triggered this instance's creation.",
    )

    # ── config / logs / addons ─────────────────────────────────────────────
    odoo_conf = fields.Text(
        string="odoo.conf",
        help="Current odoo.conf content (fetched from the running instance).",
    )
    pod_logs = fields.Text(
        string="Pod Logs",
        help="Recent container logs (fetched on demand from K8s).",
    )
    addons_repos_json = fields.Text(
        string="Addon Repositories",
        default="[]",
        help='JSON array of {"url": "...", "branch": "..."} objects. '
             'These repos are git-cloned into the tenant pod on provision.',
    )

    # ── actions ───────────────────────────────────────────────────────────────

    def action_check_availability(self):
        """Check if the tenant_id is available (namespace + DB don't exist)."""
        self.ensure_one()
        if not self.tenant_id:
            raise UserError(_("Please enter a Tenant ID first."))
        try:
            resp = requests.get(
                f"{PORTAL_URL}/api/v1/instances/check/{self.tenant_id}",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise UserError(_("Availability check failed: %s") % exc) from exc

        if data.get("available"):
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Available ✓"),
                    "message": _("Tenant ID '%s' is available.") % self.tenant_id,
                    "type": "success",
                    "sticky": False,
                },
            }
        else:
            reasons = []
            if data.get("namespace_exists"):
                reasons.append(_("K8s namespace already exists"))
            if data.get("database_exists"):
                reasons.append(_("Database already exists"))
            raise UserError(
                _("Tenant ID '%s' is NOT available: %s")
                % (self.tenant_id, ", ".join(reasons) or _("already taken"))
            )

    def action_provision(self):
        self.ensure_one()
        if self.state not in ("draft", "error"):
            raise UserError("Can only provision from Draft or Error state.")

        # ── Idempotency guard: check if the K8s namespace already exists ──────
        # This prevents the infinite loop where Odoo transaction rollbacks leave
        # orphan K8s namespaces (the namespace is created outside the transaction).
        try:
            check_resp = requests.get(
                f"{PORTAL_URL}/api/v1/instances/check/{self.tenant_id}",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=10,
            )
            if check_resp.status_code == 200:
                check_data = check_resp.json()
                if not check_data.get("available", True):
                    # Namespace already exists in K8s — don't re-create it,
                    # just sync back the state so the BD reflects reality.
                    logger.warning(
                        "action_provision(%s): namespace already exists in K8s — "
                        "skipping creation, resetting state to 'provisioning'.",
                        self.tenant_id,
                    )
                    # Try to fetch current status from portal
                    try:
                        status_resp = requests.get(
                            f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}",
                            headers={"X-API-Key": PORTAL_KEY},
                            timeout=10,
                        )
                        if status_resp.status_code == 200:
                            status_data = status_resp.json()
                            new_state = "ready" if status_data.get("status") == "ready" else "provisioning"
                            self.write({
                                "state": new_state,
                                "url": status_data.get("url") or self.url,
                                "namespace": status_data.get("namespace") or self.namespace,
                                "error_msg": False,
                            })
                            return
                    except Exception:
                        pass
                    # Fallback: mark as provisioning
                    self.write({"state": "provisioning", "error_msg": False})
                    return
        except Exception as check_exc:
            logger.warning(
                "action_provision(%s): availability check failed (%s) — proceeding with creation.",
                self.tenant_id, check_exc,
            )
        # ─────────────────────────────────────────────────────────────────────

        try:
            body = {
                "tenant_id": self.tenant_id,
                "plan": self.plan,
                "storage_gi": self.storage_gi,
            }
            # Include addon repos if configured
            if self.addons_repos_json:
                try:
                    repos = json.loads(self.addons_repos_json)
                    if repos:
                        body["addons_repos"] = repos
                except (json.JSONDecodeError, TypeError):
                    pass
            resp = requests.post(
                f"{PORTAL_URL}/api/v1/instances",
                json=body,
                headers={"X-API-Key": PORTAL_KEY},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            self.write({
                "state": "provisioning",
                "url": data.get("url"),
                "namespace": data.get("namespace"),
                "error_msg": False,
            })
        except Exception as exc:
            self.write({"state": "error", "error_msg": str(exc)})
            raise UserError(f"Provisioning failed: {exc}") from exc

    def action_check_status(self):
        """Refresh state from portal — useful from buttons or cron."""
        for rec in self.filtered(lambda r: r.state in ("provisioning",)):
            try:
                resp = requests.get(
                    f"{PORTAL_URL}/api/v1/instances/{rec.tenant_id}",
                    headers={"X-API-Key": PORTAL_KEY},
                    timeout=10,
                )
                if resp.status_code == 404:
                    rec.state = "deleted"
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "ready":
                    rec.state = "ready"
            except Exception as exc:
                logger.warning("Status check failed for %s: %s", rec.tenant_id, exc)

    def action_delete(self):
        self.ensure_one()
        try:
            resp = requests.delete(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=30,
            )
            if resp.status_code not in (204, 404):
                resp.raise_for_status()
            self.state = "deleted"
        except Exception as exc:
            self.write({"state": "error", "error_msg": str(exc)})
            raise UserError(f"Delete failed: {exc}") from exc

    def action_stop(self):
        """Suspend the instance (scale to 0 replicas in K8s).

        Calls POST /api/v1/instances/{tenant_id}/stop on the portal.
        Sets state → 'suspended'.
        """
        self.ensure_one()
        if self.state not in ("ready",):
            raise UserError(_("Can only suspend a Ready instance."))
        try:
            resp = requests.post(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}/stop",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=30,
            )
            resp.raise_for_status()
            self.write({"state": "suspended", "error_msg": False})
            logger.info("Instance %s suspended.", self.tenant_id)
        except Exception as exc:
            self.write({"state": "error", "error_msg": str(exc)})
            raise UserError(f"Suspend failed: {exc}") from exc

    def action_resume(self):
        """Resume a suspended instance (scale back to 1 replica).

        Calls POST /api/v1/instances/{tenant_id}/start on the portal.
        Sets state → 'ready'.
        """
        self.ensure_one()
        if self.state not in ("suspended",):
            raise UserError(_("Can only resume a Suspended instance."))
        try:
            resp = requests.post(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}/start",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=30,
            )
            resp.raise_for_status()
            self.write({"state": "ready", "error_msg": False})
            logger.info("Instance %s resumed.", self.tenant_id)
        except Exception as exc:
            self.write({"state": "error", "error_msg": str(exc)})
            raise UserError(f"Resume failed: {exc}") from exc

    def action_open_url(self):
        self.ensure_one()
        if self.url:
            return {
                "type": "ir.actions.act_url",
                "url": self.url,
                "target": "new",
            }

    # ── config / logs / addons actions ─────────────────────────────────────

    def action_fetch_config(self):
        """GET /api/v1/instances/{tenant_id}/config → populate odoo_conf."""
        self.ensure_one()
        if self.state not in ("ready", "provisioning"):
            raise UserError(_("Instance must be Ready or Provisioning to fetch config."))
        try:
            resp = requests.get(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}/config",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self.odoo_conf = data.get("odoo_conf", "")
        except Exception as exc:
            raise UserError(_("Failed to fetch config: %s") % exc) from exc

    def action_save_config(self):
        """PUT /api/v1/instances/{tenant_id}/config → overwrite odoo.conf."""
        self.ensure_one()
        if self.state != "ready":
            raise UserError(_("Instance must be Ready to save config."))
        if not self.odoo_conf:
            raise UserError(_("No config content to save. Fetch it first."))
        try:
            resp = requests.put(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}/config",
                json={"odoo_conf": self.odoo_conf},
                headers={"X-API-Key": PORTAL_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Config Saved ✓"),
                    "message": _("odoo.conf updated and Odoo pod restarted."),
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as exc:
            raise UserError(_("Failed to save config: %s") % exc) from exc

    def action_fetch_logs(self):
        """GET /api/v1/instances/{tenant_id}/logs → populate pod_logs."""
        self.ensure_one()
        if self.state in ("draft", "deleted"):
            raise UserError(_("No logs available for a %s instance.") % self.state)
        try:
            resp = requests.get(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}/logs",
                params={"lines": 200},
                headers={"X-API-Key": PORTAL_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self.pod_logs = data.get("logs", "")
        except Exception as exc:
            raise UserError(_("Failed to fetch logs: %s") % exc) from exc

    def action_patch_addons(self):
        """PATCH /api/v1/instances/{tenant_id}/config with addons_repos."""
        self.ensure_one()
        if self.state != "ready":
            raise UserError(_("Instance must be Ready to sync addons."))
        try:
            repos = json.loads(self.addons_repos_json or "[]")
        except (json.JSONDecodeError, TypeError) as exc:
            raise UserError(_("Invalid JSON in Addon Repositories field.")) from exc
        try:
            resp = requests.patch(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}/config",
                json={"addons_repos": repos},
                headers={"X-API-Key": PORTAL_KEY},
                timeout=30,
            )
            resp.raise_for_status()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Addons Synced ✓"),
                    "message": _("Addon repos updated. Pod will restart."),
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as exc:
            raise UserError(_("Failed to sync addons: %s") % exc) from exc
