"""
models/sale_order.py

Restricts the eCommerce cart to a single SaaS subscription product.
Customers cannot add more than one SaaS plan to a cart at a time.
"""
import logging
from odoo import models, _
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _cart_update(self, product_id, line_id=None, add_qty=0, set_qty=0, **kwargs):
        """Block adding a second SaaS subscription to the eCommerce cart.

        If the cart already has a SaaS subscription product and the user
        tries to add another one (different product or same with qty > 1),
        raise a UserError so the website shows a friendly error message.
        """
        # Detect if website_sale is installed — skip guard if not
        website_sale_installed = self.env["ir.module.module"].sudo().search_count(
            [("name", "=", "website_sale"), ("state", "=", "installed")]
        )
        if not website_sale_installed:
            return super()._cart_update(
                product_id, line_id=line_id, add_qty=add_qty, set_qty=set_qty, **kwargs
            )

        product = self.env["product.product"].browse(product_id)
        saas_categ = self.env.ref(
            "odoo_k8s_saas.product_category_odoo_saas", raise_if_not_found=False
        )

        def _is_saas(prod):
            """Return True if prod belongs to the SaaS category."""
            if not prod:
                return False
            if saas_categ:
                categ = prod.categ_id
                while categ:
                    if categ.id == saas_categ.id:
                        return True
                    categ = categ.parent_id
            # Fallback: name-based detection
            return "saas" in (prod.name or "").lower()

        # Only apply restriction when adding a SaaS product
        if _is_saas(product):
            # Check existing cart lines for any SaaS product (different from the
            # line being updated — updating qty on the *same* line is allowed)
            for line in self.order_line:
                if line.product_id.id == product_id and line.id == line_id:
                    # Same line update (qty change): allowed
                    continue
                if _is_saas(line.product_id):
                    raise UserError(
                        _(
                            "Solo puedes adquirir un plan SaaS por pedido. "
                            "Si deseas cambiar de plan, elimina el producto "
                            "actual del carrito antes de agregar uno nuevo."
                        )
                    )

        return super()._cart_update(
            product_id, line_id=line_id, add_qty=add_qty, set_qty=set_qty, **kwargs
        )
