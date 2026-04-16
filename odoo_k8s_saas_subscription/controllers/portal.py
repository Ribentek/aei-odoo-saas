"""
controllers/portal.py

Customer portal for subscriptions — /my/subscriptions list + detail.
Follows the same pattern as contract.controllers.main.PortalContract.

Self-service actions (POST routes, require auth=user):
  POST /my/subscriptions/<id>/cancel           — cancel subscription
  GET  /my/subscriptions/<id>/upgrade          — upgrade confirmation page
  POST /my/subscriptions/<id>/upgrade          — confirm plan change
  GET  /my/subscriptions/<id>/backup           — download full backup (DB + filestore)
"""
import logging
import os
from datetime import timedelta

import requests as _requests

from odoo import _, http
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.fields import Datetime as OdooDatetime
from odoo.http import request, Response

from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.portal.controllers.portal import pager as portal_pager

logger = logging.getLogger(__name__)


class PortalSubscription(CustomerPortal):
    # ── Portal Home ────────────────────────────────────────────────
    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if "subscription_count" in counters:
            partner = request.env.user.partner_id
            sub_model = request.env["sale.subscription"]
            domain = [("partner_id", "child_of", partner.ids)]
            subscription_count = (
                sub_model.search_count(domain)
                if sub_model.has_access("read")
                else 0
            )
            values["subscription_count"] = subscription_count
        return values

    # ── helpers ─────────────────────────────────────────────────────
    def _subscription_get_page_view_values(self, subscription, access_token, **kwargs):
        # Fetch linked SaaS instances (use sudo because portal users
        # may not have direct read access on saas.instance)
        saas_instances = request.env["saas.instance"].sudo().search([
            ("subscription_id", "=", subscription.id),
            ("state", "not in", ["deleted"]),
        ])
        values = {
            "page_name": "Subscriptions",
            "subscription": subscription,
            "saas_instances": saas_instances,
        }
        return self._get_page_view_values(
            subscription,
            access_token,
            values,
            "my_subscriptions_history",
            False,
            **kwargs,
        )

    # ── List ────────────────────────────────────────────────────────
    @http.route(
        ["/my/subscriptions", "/my/subscriptions/page/<int:page>"],
        type="http",
        auth="user",
        website=True,
    )
    def portal_my_subscriptions(
        self, page=1, date_begin=None, date_end=None, sortby=None, **kw
    ):
        values = self._prepare_portal_layout_values()
        sub_obj = request.env["sale.subscription"]

        if not sub_obj.has_access("read"):
            return request.redirect("/my")

        # Filter to subscriptions belonging to the logged-in partner (or children)
        partner = request.env.user.partner_id
        domain = [("partner_id", "child_of", partner.ids)]

        searchbar_sortings = {
            "name": {"label": _("Name"), "order": "name desc"},
            "date": {
                "label": _("Next Invoice"),
                "order": "recurring_next_date desc",
            },
            "stage": {"label": _("Stage"), "order": "stage_id asc"},
        }
        if not sortby:
            sortby = "name"
        order = searchbar_sortings[sortby]["order"]

        subscription_count = sub_obj.search_count(domain)
        pager = portal_pager(
            url="/my/subscriptions",
            url_args={
                "date_begin": date_begin,
                "date_end": date_end,
                "sortby": sortby,
            },
            total=subscription_count,
            page=page,
            step=self._items_per_page,
        )
        subscriptions = sub_obj.search(
            domain,
            order=order,
            limit=self._items_per_page,
            offset=pager["offset"],
        )
        request.session["my_subscriptions_history"] = subscriptions.ids[:100]

        values.update(
            {
                "date": date_begin,
                "subscriptions": subscriptions,
                "page_name": "Subscriptions",
                "pager": pager,
                "default_url": "/my/subscriptions",
                "searchbar_sortings": searchbar_sortings,
                "sortby": sortby,
            }
        )
        return request.render(
            "odoo_k8s_saas_subscription.portal_my_subscriptions", values
        )

    # ── Detail ──────────────────────────────────────────────────────
    @http.route(
        ["/my/subscriptions/<int:subscription_id>"],
        type="http",
        auth="public",
        website=True,
    )
    def portal_my_subscription_detail(
        self, subscription_id, access_token=None, **kw
    ):
        try:
            sub_sudo = self._document_check_access(
                "sale.subscription", subscription_id, access_token
            )
        except (AccessError, MissingError):
            return request.redirect("/my")
        values = self._subscription_get_page_view_values(
            sub_sudo, access_token, **kw
        )
        # Pass available SaaS plan templates (excluding current) for the upgrade UI
        values["saas_templates"] = request.env["sale.subscription.template"].sudo().search([
            ("is_saas_plan", "=", True),
            ("id", "!=", sub_sudo.template_id.id),
        ])
        values["cancelled"] = kw.get("cancelled") == "1"
        values["upgraded"] = kw.get("upgraded") == "1"
        return request.render(
            "odoo_k8s_saas_subscription.portal_subscription_page", values
        )

    # ── Self-service: Cancel ─────────────────────────────────────────
    @http.route(
        ["/my/subscriptions/<int:subscription_id>/cancel"],
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def portal_cancel_subscription(self, subscription_id, access_token=None, **kw):
        """Cancel the subscription — moves to Closed, triggers suspension."""
        try:
            sub_sudo = self._document_check_access(
                "sale.subscription", subscription_id, access_token
            )
        except (AccessError, MissingError):
            return request.redirect("/my")

        try:
            sub_sudo.action_portal_cancel()
        except UserError as e:
            logger.warning("portal_cancel_subscription: %s", e)
        return request.redirect(f"/my/subscriptions/{subscription_id}?cancelled=1")

    # ── Self-service: Upgrade confirmation page ──────────────────────
    @http.route(
        ["/my/subscriptions/<int:subscription_id>/upgrade"],
        type="http",
        auth="user",
        website=True,
        methods=["GET"],
    )
    def portal_upgrade_plan_page(self, subscription_id, access_token=None, **kw):
        """Show a plan comparison and confirmation page before upgrading."""
        try:
            sub_sudo = self._document_check_access(
                "sale.subscription", subscription_id, access_token
            )
        except (AccessError, MissingError):
            return request.redirect("/my")

        saas_templates = request.env["sale.subscription.template"].sudo().search([
            ("is_saas_plan", "=", True),
            ("id", "!=", sub_sudo.template_id.id),
        ])
        return request.render(
            "odoo_k8s_saas_subscription.portal_upgrade_confirmation",
            {
                "subscription": sub_sudo,
                "saas_templates": saas_templates,
                "page_name": "Subscriptions",
            },
        )

    # ── Self-service: Upgrade confirm (POST) ─────────────────────────
    @http.route(
        ["/my/subscriptions/<int:subscription_id>/upgrade"],
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def portal_upgrade_plan_confirm(
        self, subscription_id, access_token=None, new_template_id=None, **kw
    ):
        """Execute the plan change after the user confirms."""
        try:
            sub_sudo = self._document_check_access(
                "sale.subscription", subscription_id, access_token
            )
        except (AccessError, MissingError):
            return request.redirect("/my")

        if not new_template_id:
            return request.redirect(f"/my/subscriptions/{subscription_id}/upgrade")

        try:
            sub_sudo.action_portal_upgrade(int(new_template_id))
        except (UserError, ValueError) as e:
            logger.warning("portal_upgrade_plan_confirm: %s", e)
        return request.redirect(f"/my/subscriptions/{subscription_id}?upgraded=1")

    # ── Self-service: Backup download ─────────────────────────────────────
    @http.route(
        ["/my/subscriptions/<int:subscription_id>/backup"],
        type="http",
        auth="user",
        website=True,
        methods=["GET"],
    )
    def portal_backup_subscription(self, subscription_id, access_token=None, **kw):
        """Stream a full backup (DB + filestore) through the portal API.

        The Portal FastAPI reads the tenant's admin_passwd from its K8s Secret
        and calls /web/database/backup on the tenant Odoo internally.
        The customer never sees the master password.
        Rate limit: 1 backup per 24 hours per instance.
        """
        try:
            sub_sudo = self._document_check_access(
                "sale.subscription", subscription_id, access_token
            )
        except (AccessError, MissingError):
            return request.redirect("/my")

        # Find active linked saas.instance
        instance = request.env["saas.instance"].sudo().search([
            ("subscription_id", "=", sub_sudo.id),
            ("state", "not in", ["deleted", "pending_delete"]),
        ], limit=1)

        if not instance:
            return request.make_response(
                "No active SaaS instance found for this subscription.",
                headers=[("Content-Type", "text/plain; charset=utf-8")],
            )

        if instance.state != "ready":
            state_label = dict(instance._fields["state"].selection).get(
                instance.state, instance.state
            )
            return request.make_response(
                f"Tu instancia está {state_label}. "
                "El backup solo está disponible cuando la instancia está activa (Ready). "
                "Contacta soporte si necesitas recuperar tus datos.",
                headers=[("Content-Type", "text/plain; charset=utf-8")],
            )

        # Rate limit: 1 backup per 24h
        if instance.last_backup_at:
            elapsed = OdooDatetime.now() - instance.last_backup_at
            if elapsed < timedelta(hours=24):
                remaining = timedelta(hours=24) - elapsed
                remaining_h = int(remaining.total_seconds() // 3600)
                remaining_m = int((remaining.total_seconds() % 3600) // 60)
                return request.make_response(
                    f"Límite de backups alcanzado. Próximo backup disponible en "
                    f"{remaining_h}h {remaining_m}m.",
                    headers=[("Content-Type", "text/plain; charset=utf-8")],
                )

        # Call Portal FastAPI backup endpoint
        portal_url = os.getenv("SAAS_PORTAL_URL", "http://portal.aeisoftware.svc.cluster.local:8000")
        portal_key = os.getenv("SAAS_PORTAL_KEY", "")

        try:
            resp = _requests.get(
                f"{portal_url}/api/v1/instances/{instance.tenant_id}/backup",
                headers={"X-API-Key": portal_key},
                stream=True,
                timeout=(15, 600),  # connect 15s, read 10min
            )
            resp.raise_for_status()
        except _requests.exceptions.ConnectionError:
            logger.error("backup: portal unreachable for %s", instance.tenant_id)
            return request.make_response(
                "El servicio de backup no está disponible. Intenta más tarde.",
                headers=[("Content-Type", "text/plain; charset=utf-8")],
            )
        except _requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 503:
                return request.make_response(
                    "La instancia no está accesible en este momento. "
                    "Asegúrate de que está en estado Ready antes de descargar el backup.",
                    headers=[("Content-Type", "text/plain; charset=utf-8")],
                )
            logger.error("backup: portal returned %s for %s", status, instance.tenant_id)
            return request.make_response(
                f"Error al generar el backup ({status}). Contacta soporte.",
                headers=[("Content-Type", "text/plain; charset=utf-8")],
            )
        except Exception:
            logger.exception("backup: unexpected error for %s", instance.tenant_id)
            return request.make_response(
                "Error inesperado al generar el backup. Contacta soporte.",
                headers=[("Content-Type", "text/plain; charset=utf-8")],
            )

        # Record timestamp before streaming (rate-limit anchor)
        instance.last_backup_at = OdooDatetime.now()

        # Stream ZIP back to the browser
        from datetime import date
        filename = f"{instance.tenant_id}-backup-{date.today()}.zip"

        def generate():
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk

        return Response(
            response=generate(),
            content_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
            direct_passthrough=True,
        )
