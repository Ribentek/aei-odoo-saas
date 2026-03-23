"""
controllers/portal.py

Customer portal for subscriptions — /my/subscriptions list + detail.
Follows the same pattern as contract.controllers.main.PortalContract.
"""
from odoo import _, http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.portal.controllers.portal import pager as portal_pager


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
        return request.render(
            "odoo_k8s_saas_subscription.portal_subscription_page", values
        )
