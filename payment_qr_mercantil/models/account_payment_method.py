"""
models/account_payment_method.py

Registers the QR Mercantil payment method in Odoo's accounting layer.

Without this, account_payment._cron_post_process → _create_payment()
fails with "Por favor, defina un método de pago en su pago" because
the journal associated with the QR Mercantil provider has no
payment_method_line for the 'qr_mercantil' code.

The base account_payment module already loops over all provider codes
and registers them as mode='electronic', type=('bank',) — BUT that loop
only runs from account.payment.method, not from payment.provider.
Since payment_provider.py was incorrectly implementing this method on the
wrong model (payment.provider instead of account.payment.method), the
registration was silently ignored.
"""

from odoo import api, models


class AccountPaymentMethod(models.Model):
    _inherit = "account.payment.method"

    @api.model
    def _get_payment_method_information(self):
        res = super()._get_payment_method_information()
        # Register qr_mercantil as an electronic inbound method
        # that works on bank journals.  'type': ('bank',) means it will
        # only appear in bank journals (not cash or misc).
        res["qr_mercantil"] = {
            "mode": "electronic",
            "type": ("bank",),
        }
        return res
