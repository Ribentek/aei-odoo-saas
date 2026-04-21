"""
controllers/webhook.py

Internal webhook receiver for the SaaS portal (FastAPI) to push
instance status changes to Odoo without waiting for the 2-minute cron.

Endpoint:  POST /saas/webhook/instance-status
Auth:      X-Webhook-Key header (must match SAAS_WEBHOOK_KEY env var)
Body:      JSON { "tenant_id": "...", "status": "ready"|"error"|"provisioning"|... }

Security:
- No CSRF (server-to-server call, not browser)
- API key authentication via header
- auth='none' — validated manually; avoids session overhead

The 2-minute _cron_reconcile_all() cron remains as a safety net.
This webhook is an optimization that reduces notification latency from
0-2 minutes to a few seconds.
"""
import logging
import os

from odoo import http
from odoo.http import request
from werkzeug.exceptions import Unauthorized

logger = logging.getLogger(__name__)

WEBHOOK_KEY = os.getenv("SAAS_WEBHOOK_KEY", "")


class SaaSWebhookController(http.Controller):

    @http.route(
        "/saas/webhook/instance-status",
        type="json",
        auth="none",
        csrf=False,
        methods=["POST"],
        save_session=False,
    )
    def instance_status_webhook(self, **kwargs):
        """Receive a push notification from the portal about an instance status change."""
        # ── Authentication ─────────────────────────────────────────────────────
        incoming_key = request.httprequest.headers.get("X-Webhook-Key", "")
        if not WEBHOOK_KEY:
            logger.warning(
                "instance_status_webhook: SAAS_WEBHOOK_KEY not set — rejecting all requests"
            )
            return {"error": "Webhook not configured", "status": 503}

        if incoming_key != WEBHOOK_KEY:
            logger.warning(
                "instance_status_webhook: invalid key from %s",
                request.httprequest.remote_addr,
            )
            raise Unauthorized()

        # ── Parse payload ──────────────────────────────────────────────────────
        data = request.get_json_data() or {}
        tenant_id = data.get("tenant_id", "").strip()
        new_status = data.get("status", "").strip()

        if not tenant_id or not new_status:
            return {"error": "Missing tenant_id or status", "status": 400}

        logger.info(
            "instance_status_webhook: received status=%s for tenant_id=%s",
            new_status, tenant_id,
        )

        # ── Find and update the instance ───────────────────────────────────────
        env = request.env(su=True)  # superuser — bypass portal ACLs
        instance = env["saas.instance"].search(
            [("tenant_id", "=", tenant_id)], limit=1
        )
        if not instance:
            logger.warning(
                "instance_status_webhook: tenant_id '%s' not found in Odoo", tenant_id
            )
            return {"error": "Instance not found", "status": 404}

        old_state = instance.state

        # Map portal status to Odoo state machine
        STATUS_MAP = {
            "ready": "ready",
            "provisioning": "provisioning",
            "error": "error",
            "stopped": "suspended",
            "deleted": "deleted",
        }
        odoo_state = STATUS_MAP.get(new_status)
        if not odoo_state:
            logger.warning(
                "instance_status_webhook: unknown status '%s' for %s",
                new_status, tenant_id,
            )
            return {"error": f"Unknown status: {new_status}", "status": 400}

        # Only transition if the state actually changes and is valid
        if old_state == odoo_state:
            return {"ok": True, "state": odoo_state, "changed": False}

        try:
            instance.write({"state": odoo_state})
        except Exception as e:
            logger.exception(
                "instance_status_webhook: failed to write state for %s", tenant_id
            )
            return {"error": str(e), "status": 500}

        logger.info(
            "instance_status_webhook: %s state %s → %s",
            tenant_id, old_state, odoo_state,
        )

        # provisioning → ready: send credentials email (best-effort, never rolls back the state write)
        if old_state == "provisioning" and odoo_state == "ready":
            logger.info(
                "instance_status_webhook: %s is ready — sending credentials email",
                tenant_id,
            )
            try:
                instance.action_send_credentials_email()
            except Exception:
                logger.warning(
                    "instance_status_webhook: credentials email failed for %s (state still updated)",
                    tenant_id,
                )

        return {"ok": True, "state": odoo_state, "changed": True}
