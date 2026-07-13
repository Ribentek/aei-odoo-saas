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

from odoo import models, fields, api, _, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError

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
            ("pending_delete", "Pending Delete"),
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

    # ── config / logs / addons / credentials ──────────────────────────────────
    admin_password = fields.Char(
        string="App Admin Password",
        copy=False,
        help="The randomly generated password for the application's admin user.",
    )
    odoo_version = fields.Selection([
        ('17.0', 'Odoo 17.0 (Official)'),
        ('18.0', 'Odoo 18.0 (Official)'),
        ('19.0', 'Odoo 19.0 (Official)'),
        ('custom', 'Custom Image'),
    ], string="Odoo Version", default='18.0', required=True)
    custom_image = fields.Char(string="Custom Odoo Image")
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
    install_modules = fields.Char(
        string="Install Modules",
        help="Comma-separated list of modules to install on DB creation (e.g., 'commission,account_reconcile').",
    )

    _sql_constraints = [
        ("tenant_id_unique", "UNIQUE(tenant_id)", "Tenant ID must be unique."),
    ]

    @api.constrains("tenant_id")
    def _check_tenant_id(self):
        import re
        pattern = re.compile(r"^[a-z0-9][a-z0-9\-]{0,46}[a-z0-9]$")
        for rec in self:
            tid = rec.tenant_id
            if not tid:
                continue
            if len(tid) < 2:
                raise ValidationError(
                    _("Tenant ID must be at least 2 characters long (got '%s').") % tid
                )
            if not pattern.match(tid):
                raise ValidationError(
                    _("Tenant ID '%s' is invalid. Use only lowercase letters, "
                      "digits, and hyphens. Must start and end with alphanumeric.") % tid
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
                "odoo_version": self.odoo_version or "18.0",
                "custom_image": self.custom_image if self.custom_image else None,
                "install_modules": self.install_modules or "",
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
                "admin_password": data.get("app_admin_password"),
                "error_msg": False,
            })
        except Exception as exc:
            self.write({"state": "error", "error_msg": str(exc)})
            raise UserError(f"Provisioning failed: {exc}") from exc

    def action_upgrade(self):
        """Upgrade compute resources for a running instance (plan change).

        Unlike action_provision() which only works on draft/error instances,
        this method works on 'ready' instances and sends a PATCH to the portal
        to update ConfigMap (workers) and Deployment (CPU/RAM) in-place.
        """
        self.ensure_one()
        if self.state not in ("ready",):
            logger.warning(
                "action_upgrade(%s): skipping — instance state is '%s', not 'ready'.",
                self.tenant_id, self.state,
            )
            return

        try:
            body = {
                "plan": self.plan,
                "storage_gi": self.storage_gi,
            }
            resp = requests.patch(
                f"{PORTAL_URL}/api/v1/instances/{self.tenant_id}/upgrade",
                json=body,
                headers={"X-API-Key": PORTAL_KEY},
                timeout=30,
            )
            resp.raise_for_status()
            logger.info(
                "action_upgrade(%s): upgraded to plan '%s' (storage=%dGi).",
                self.tenant_id, self.plan, self.storage_gi,
            )
        except Exception as exc:
            self.write({"error_msg": f"Upgrade failed: {exc}"})
            logger.exception("action_upgrade(%s): failed", self.tenant_id)

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
                    rec.action_send_credentials_email()
            except Exception as exc:
                logger.warning("Status check failed for %s: %s", rec.tenant_id, exc)

    def action_send_credentials_email(self):
        """Send the credentials email.

        Called from three contexts with very different env.uid: a logged-in
        backend user (manual), the reconciliation cron (OdooBot), and the
        portal's auth='none' webhook (no session at all → env.uid is None,
        env.user is an EMPTY recordset). mail.thread's message_post() calls
        env.user._is_public(), which does ensure_one() and raises
        "Expected singleton: res.users()" on that empty recordset — even
        though the email itself already sent successfully, the exception
        propagates and the webhook logs a false "credentials email failed".
        Pin to SUPERUSER_ID (a real user row) so message_post always has a
        valid singleton user, regardless of the caller. See DEPLOY.md
        incident 2026-07-10.
        """
        self.ensure_one()
        rec = self if self.env.uid else self.with_user(SUPERUSER_ID)
        template = rec.env.ref("odoo_k8s_saas.email_template_saas_credentials", raise_if_not_found=False)
        if template and rec.partner_id and rec.partner_id.email:
            template.send_mail(rec.id, force_send=True)
            rec.message_post(body=_("Credentials email dispatched to %s") % rec.partner_id.email)

    def action_request_delete(self):
        """Mark instance for async deletion (instant for the user).

        The actual K8s namespace + DB deletion is done by the
        _cron_process_pending_deletes() cron, avoiding blocking the UI.
        """
        for rec in self:
            if rec.state not in ("deleted",):
                logger.info("Instance %s marked for async deletion.", rec.tenant_id)
                rec.write({"state": "pending_delete", "error_msg": False})

    def action_delete(self):
        """Synchronous delete — calls the portal API to destroy K8s resources.

        Prefer action_request_delete() for UI actions (non-blocking).
        This method is still used by crons and direct API calls.
        """
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

    @api.model
    def _cron_process_pending_deletes(self):
        """Cron: process instances in 'pending_delete' state.

        Calls the portal DELETE API for each, then marks as 'deleted'.
        Runs every 2 minutes so cleanup is near-instant after user closes.
        """
        pending = self.search([("state", "=", "pending_delete")])
        if not pending:
            return
        logger.info(
            "_cron_process_pending_deletes: processing %d instances", len(pending)
        )
        for inst in pending:
            try:
                resp = requests.delete(
                    f"{PORTAL_URL}/api/v1/instances/{inst.tenant_id}",
                    headers={"X-API-Key": PORTAL_KEY},
                    timeout=30,
                )
                if resp.status_code not in (204, 404):
                    resp.raise_for_status()
                inst.write({"state": "deleted", "error_msg": False})
                logger.info(
                    "_cron_process_pending_deletes: deleted %s", inst.tenant_id
                )
                # Commit after each successful delete so partial progress is saved
                self.env.cr.commit()  # pylint: disable=invalid-commit
            except Exception:
                logger.exception(
                    "_cron_process_pending_deletes: failed to delete %s",
                    inst.tenant_id,
                )
                inst.write({"error_msg": "Async delete failed — will retry next cron run."})
                self.env.cr.commit()  # pylint: disable=invalid-commit

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

    @api.model
    def _cron_reconcile_all(self):
        """Cron: full reconciliation between K8s and Odoo records.

        Calls GET /api/v1/instances (list all) and:
        - provisioning → ready:   K8s reports ready, Odoo still says provisioning
        - ready → error:          K8s reports not_ready, Odoo says ready (drift)
        - ready/suspended → error: namespace missing from K8s entirely
        - orphan detection:       namespace exists in K8s but no Odoo record (logs warning)

        Runs every 2 minutes. Complements _cron_process_pending_deletes.
        """
        try:
            resp = requests.get(
                f"{PORTAL_URL}/api/v1/instances/list",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            k8s_tenants = {t["tenant_id"]: t for t in resp.json()}
        except Exception as exc:
            logger.warning("_cron_reconcile_all: could not fetch instance list: %s", exc)
            return

        # ── 1. Reconcile K8s → Odoo ──────────────────────────────────────────
        for tenant_id, info in k8s_tenants.items():
            k8s_status = info.get("status", "unknown")
            rec = self.search([("tenant_id", "=", tenant_id)], limit=1)

            if not rec:
                logger.warning(
                    "_cron_reconcile_all: orphan namespace odoo-%s exists in K8s "
                    "but has no Odoo record — manual review required.", tenant_id
                )
                continue

            if rec.state == "provisioning" and k8s_status == "ready":
                rec.write({"state": "ready", "error_msg": False})
                rec.action_send_credentials_email()
                logger.info("_cron_reconcile_all: %s provisioning→ready", tenant_id)

            elif rec.state == "ready" and k8s_status == "not_ready":
                rec.write({
                    "state": "error",
                    "error_msg": "Pod not ready in K8s — check logs in the portal.",
                })
                logger.warning("_cron_reconcile_all: %s ready→error (pod not ready)", tenant_id)

        # ── 2. Detect Odoo records whose namespace no longer exists in K8s ────
        live_states = ("provisioning", "ready", "suspended")
        active_recs = self.search([("state", "in", list(live_states))])
        for rec in active_recs:
            if rec.tenant_id not in k8s_tenants:
                rec.write({
                    "state": "error",
                    "error_msg": "Namespace not found in K8s — may have been deleted externally.",
                })
                logger.warning(
                    "_cron_reconcile_all: %s marked error — namespace missing from K8s",
                    rec.tenant_id,
                )

    @api.model
    def _cron_gc_orphaned_dbs(self):
        """Cron: drop Postgres DBs whose K8s namespace no longer exists.

        Calls DELETE /api/v1/gc/dbs on the portal. The portal compares every
        odoo_* DB against live K8s namespaces and drops orphans (protected DBs
        like admin/staging/templates are always excluded on the portal side).

        Runs daily. Results are logged; failures do not raise so the cron stays
        active even if the portal is temporarily unreachable.
        """
        try:
            resp = requests.delete(
                f"{PORTAL_URL}/api/v1/gc/dbs",
                headers={"X-API-Key": PORTAL_KEY},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            dropped = data.get("dropped", [])
            errors = data.get("errors", [])
            if dropped:
                logger.info(
                    "_cron_gc_orphaned_dbs: dropped %d orphaned DB(s): %s",
                    len(dropped), [d["db"] for d in dropped],
                )
            if errors:
                logger.warning(
                    "_cron_gc_orphaned_dbs: %d error(s): %s", len(errors), errors
                )
            if not dropped and not errors:
                logger.info("_cron_gc_orphaned_dbs: no orphaned DBs found.")
        except Exception as exc:
            logger.warning("_cron_gc_orphaned_dbs: portal unreachable: %s", exc)

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
