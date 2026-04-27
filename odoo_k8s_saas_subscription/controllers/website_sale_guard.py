"""
controllers/website_sale_guard.py

Two concerns handled here:
1. Defense-in-depth: redirect unauthenticated users to login when their cart
   contains SaaS subscription products.
2. Address form: restrict required fields to name/email/phone/company/NIT only;
   street, city, zip are optional for SaaS (digital product, no shipping).
"""
import logging
from odoo import http
from odoo.http import request
from odoo.addons.website_sale.controllers.main import WebsiteSale

logger = logging.getLogger(__name__)


class WebsiteSaleSaaSGuard(WebsiteSale):

    @http.route()
    def checkout(self, **post):
        """Redirect to login if user is public and cart contains SaaS products."""
        if request.env.user._is_public():
            order = request.website.sale_get_order()
            if order and self._cart_has_saas(order):
                logger.info(
                    "Blocking guest checkout for order %s — SaaS products in cart",
                    order.name,
                )
                return request.redirect("/web/login?redirect=/shop/checkout")

        return super().checkout(**post)

    @http.route()
    def shop_address_submit(self, **kw):
        """Pass-through — mandatory field overrides apply via helper methods below."""
        return super().shop_address_submit(**kw)

    # ── Mandatory field overrides ────────────────────────────────────────────

    def _get_mandatory_fields(self):
        """Required billing fields (portal layer): name, email, phone, company, NIT."""
        return ["name", "email", "phone", "company_name", "vat"]

    def _get_mandatory_address_fields(self, country_sudo):
        """Required address fields: only name/phone/email + country (always Bolivia)."""
        return {"name", "phone", "email", "country_id"}

    def _get_mandatory_billing_address_fields(self, country_sudo):
        """Odoo 18 entry point for /shop/address billing validation.
        Override directly so the exact set is enforced regardless of chain changes."""
        return {"name", "email", "phone", "company_name", "vat", "country_id"}

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _cart_has_saas(self, order):
        """Return True if any order line is a SaaS subscription product."""
        saas_categ = request.env.ref(
            "odoo_k8s_saas.product_category_odoo_saas", raise_if_not_found=False
        )
        for line in order.order_line:
            if not line.product_id:
                continue
            if saas_categ:
                categ = line.product_id.categ_id
                while categ:
                    if categ.id == saas_categ.id:
                        return True
                    categ = categ.parent_id
            if "saas" in (line.product_id.name or "").lower():
                return True
        return False
