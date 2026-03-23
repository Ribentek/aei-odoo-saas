import logging

from odoo import _, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    qr_mercantil_alias = fields.Char(string='QR Alias')
    qr_mercantil_image = fields.Text(string='QR Image (base64)')
    qr_mercantil_qr_id = fields.Char(string='QR ID Banco')
    qr_mercantil_last_polled = fields.Datetime(
        string='Último polling banco',
        copy=False,
        help='Timestamp del último polling al banco (throttle cross-worker).',
    )

    # ── Odoo 18 payment flow: rendering values for redirect form ─────────────

    def _get_specific_rendering_values(self, processing_values):
        """Return provider-specific values used to render the redirect form.

        Odoo 18 calls this on the Transaction (not on the Provider) inside
        payment.transaction._get_processing_values(). The returned dict is
        passed directly to ir.qweb._render(redirect_form_view_id, …).
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'qr_mercantil':
            return res

        provider = self.provider_id
        reference = processing_values.get('reference') or self.reference
        amount = processing_values.get('amount') or self.amount
        currency_name = self.currency_id.name if self.currency_id else 'BOB'

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        callback_url = (
            provider.qr_mercantil_webhook_url.strip()
            if provider.qr_mercantil_webhook_url
            else f"{base_url}/payment/qr_mercantil/webhook"
        )

        qr_image = ''
        qr_id = ''
        try:
            qr_data = provider._qr_mercantil_generate_qr(
                alias=reference,
                amount=amount,
                currency_name=currency_name,
                description=f"Pedido {reference}",
                callback_url=callback_url,
            )
            # MC4 wraps all response data inside 'objeto'
            qr_objeto = qr_data.get('objeto') or {}
            qr_image = (
                qr_objeto.get('imagenQr')
                or qr_objeto.get('qrImage')
                or qr_data.get('qrImage')
                or qr_data.get('imagenQr')
                or qr_data.get('qr_image')
                or qr_data.get('image')
                or ''
            )
            qr_id = (
                qr_objeto.get('idQr')
                or qr_objeto.get('id')
                or qr_data.get('idQr')
                or qr_data.get('id_qr')
                or qr_data.get('id')
                or ''
            )
        except Exception:
            _logger.exception(
                "QR Mercantil: fallo al generar QR para referencia %s", reference
            )

        # Persist QR data on this transaction record
        self.sudo().write({
            'qr_mercantil_alias': reference,
            'qr_mercantil_image': qr_image,
            'qr_mercantil_qr_id': qr_id,
        })

        _logger.info(
            "QR Mercantil: rendering values para ref=%s qr_id=%s image_len=%d",
            reference, qr_id, len(qr_image),
        )

        return {
            'reference': reference,        # clave para qr_mercantil_redirect_form template
            'qr_image': qr_image,
            'qr_id': qr_id,
            'alias': reference,
            'amount': amount,
            'currency': currency_name,
            'is_demo': provider.state == 'test',
            'status_url': f"{base_url}/payment/qr_mercantil/status",
            'simulate_url': f"{base_url}/payment/qr_mercantil/simulate",
            'landing_route': processing_values.get('landing_route', '/payment/status'),
        }

    # ── Find transaction from webhook ────────────────────────────────────────

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'qr_mercantil' or len(tx) == 1:
            return tx

        alias = notification_data.get('alias')
        if not alias:
            raise ValidationError(
                _("QR Mercantil webhook: campo 'alias' ausente en la notificación.")
            )

        tx = self.search([
            ('qr_mercantil_alias', '=', alias),
            ('provider_code', '=', 'qr_mercantil'),
        ])
        if not tx:
            raise ValidationError(
                _("QR Mercantil: no se encontró transacción con alias '%s'.") % alias
            )
        return tx

    # ── Process webhook payload ──────────────────────────────────────────────

    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code != 'qr_mercantil':
            return

        # Webhook payload fields from the bank:
        # alias, numeroOrdenOriginante, monto, idQr,
        # moneda, fechaProceso, cuentaCliente, nombreCliente, documentoClient
        monto = notification_data.get('monto')
        id_qr = notification_data.get('idQr', '')
        nombre_cliente = notification_data.get('nombreCliente', '')

        _logger.info(
            "QR Mercantil: webhook recibido para tx=%s alias=%s monto=%s idQr=%s cliente=%s",
            self.reference,
            self.qr_mercantil_alias,
            monto,
            id_qr,
            nombre_cliente,
        )

        # Save bank QR ID if not already stored
        if id_qr and not self.qr_mercantil_qr_id:
            self.qr_mercantil_qr_id = id_qr

        # Confirm the payment — bank only calls webhook on successful payment
        self._set_done()
        _logger.info(
            "QR Mercantil: transacción %s marcada como DONE", self.reference
        )
