"""
models/sale_order.py

When the eCommerce cart already contains a SaaS subscription product and the
customer tries to add another one, the purchase is *allowed* but a warning
message is injected into the cart-update response so the frontend can display
a toast notification.
"""
import logging
from odoo import models, _

logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _cart_update(self, product_id, line_id=None, add_qty=0, set_qty=0, **kwargs):
        """Aggregate subscriptions into a single line to support multi-month prepay."""
        product = self.env["product.product"].browse(product_id)
        is_sub = getattr(product, "subscribable", False)

        warning = False
        if is_sub:
            # Warn guest users that they need an account
            if self.env.user._is_public():
                warning = _(
                    "Necesitas iniciar sesión o crear una cuenta para "
                    "completar la compra de una suscripción."
                )

            # Enforce grouping: if the cart already has this product, use that line
            if not line_id:
                existing_line = self.order_line.filtered(lambda l: l.product_id.id == product_id)
                if existing_line:
                    line_id = existing_line[0].id

            # Info message about multi-month prepaid
            existing_line = self.order_line.filtered(lambda l: l.product_id.id == product_id)
            if existing_line:
                info_msg = _(
                    "Has agregado un mes más a la subscripción."
                )
                warning = (warning + " " + info_msg) if warning else info_msg

        # Always let the purchase proceed
        result = super()._cart_update(
            product_id, line_id=line_id, add_qty=add_qty, set_qty=set_qty, **kwargs
        )

        if warning and isinstance(result, dict):
            result["warning"] = warning

        return result

    def create_subscription(self, lines, subscription_tmpl):
        """
        Intercept subscription creation to handle prepaid months (quantity > 1).
        We split the lines by quantity, so products with different prepaid months
        get their own subscriptions.
        """
        from collections import defaultdict
        
        # Group the provided lines by their quantity
        qty_groups = defaultdict(list)
        for line in lines:
            qty_groups[line.product_uom_qty].append(line)
        
        for qty, sub_lines in qty_groups.items():
            # Create the subscription(s) for this specific quantity group
            super().create_subscription(sub_lines, subscription_tmpl)
            
            # Now find the subscription just created for this template
            # Ordering by id desc limit 1 ensures we get the latest one
            created_sub = self.env["sale.subscription"].search([
                ("sale_order_id", "=", self.id),
                ("template_id", "=", subscription_tmpl.id)
            ], order="id desc", limit=1)
            
            if created_sub and qty > 1:
                logger.info(
                    "Subscription %s created with quantity %s. "
                    "Pushing next billing date by %s intervals.",
                    created_sub.name, qty, qty
                )
                interval = subscription_tmpl.recurring_interval * int(qty)
                created_sub.recurring_next_date = self.get_next_interval(
                    subscription_tmpl.recurring_rule_type,
                    interval,
                )
                # Reset line quantities to 1.0 for future renewals
                for sub_line in created_sub.sale_subscription_line_ids:
                    sub_line.product_uom_qty = 1.0
