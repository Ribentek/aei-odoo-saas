import logging
import time

from odoo import fields as odoo_fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Known "paid" states from MC4 / Bolivian bank APIs
_PAID_STATES = frozenset({
    'PAGADO', 'PAGADA', 'EJECUTADO', 'EJECUTADA',
    'APROBADO', 'APROBADA', 'COMPLETADO', 'COMPLETADA',
    'PROCESADO', 'PROCESADA', 'DONE', 'PAID', 'SUCCESS',
})

# How often (seconds) each transaction is polled at the bank
_BANK_POLL_INTERVAL = 10


class QRMercantilController(http.Controller):

    # ── Redirect target — shown after "Pagar ahora" is clicked ───────────────

    @http.route(
        '/payment/qr_mercantil/display',
        type='http',
        auth='public',
        website=True,
        methods=['GET'],
        sitemap=False,
    )
    def display_qr(self, reference=None, **kwargs):
        """Show the QR code page and start polling for payment confirmation."""
        if not reference:
            return request.redirect('/payment/status')

        tx = request.env['payment.transaction'].sudo().search(
            [('reference', '=', reference), ('provider_code', '=', 'qr_mercantil')],
            limit=1,
        )
        if not tx:
            _logger.warning("QR Mercantil: transaction not found for reference=%s", reference)
            return request.redirect('/payment/status')

        base_url = request.httprequest.host_url.rstrip('/')
        is_demo = tx.provider_id.state == 'test'

        return request.render('payment_qr_mercantil.qr_mercantil_display', {
            'reference': reference,
            'qr_image': tx.qr_mercantil_image or '',
            'amount': tx.amount,
            'currency': tx.currency_id.name if tx.currency_id else 'BOB',
            'landing_route': tx.landing_route or '/payment/status',
            'is_demo': is_demo,
            'simulate_url': f'{base_url}/payment/qr_mercantil/simulate',
        })

    # ── Webhook — called by the bank when QR is paid (best-effort) ───────────

    @http.route(
        '/payment/qr_mercantil/webhook',
        type='json',
        auth='public',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def webhook(self, **kwargs):
        """Receive payment notification from Banco Mercantil (best-effort)."""
        notification_data = request.get_json_data()
        _logger.info("QR Mercantil webhook recibido: %s", notification_data)

        try:
            request.env['payment.transaction'].sudo()._handle_notification_data(
                'qr_mercantil', notification_data
            )
        except Exception:
            _logger.exception("QR Mercantil: error procesando webhook")
            return {'status': 'error', 'message': 'processing error'}

    # ── Demo: simulate a payment without calling the bank ───────────────────

    @http.route(
        '/payment/qr_mercantil/simulate',
        type='json',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def simulate_payment(self, reference=None, **kwargs):
        """[Demo only] Mark a QR transaction as paid without a real bank call.

        Follows the Odoo 18 payment_demo pattern (action_demo_set_done):
        find the TX by reference, then call _set_done() directly.
        """
        if not reference:
            return {'status': 'error', 'message': 'missing reference'}

        tx = request.env['payment.transaction'].sudo().search(
            [('reference', '=', reference), ('provider_code', '=', 'qr_mercantil')],
            limit=1,
        )
        if not tx:
            return {'status': 'error', 'message': 'transaction not found'}

        if tx.provider_id.state != 'test':
            _logger.warning(
                "QR Mercantil: intento de simular pago en proveedor sin test mode (ref=%s)",
                reference,
            )
            return {'status': 'error', 'message': 'test mode not enabled'}

        if tx.state in ('done', 'cancel', 'error'):
            return {
                'status': 'already_done',
                'state': tx.state,
                'landing_route': tx.landing_route or '/payment/status',
            }

        _logger.info("QR Mercantil [DEMO]: simulando pago para ref=%s", reference)
        try:
            # In Odoo 18, _set_done() internally enqueues _post_process() via the
            # payment post-processing mechanism. Calling _post_process() explicitly
            # after _set_done() would run it twice, which can:
            #   - Confirm the SO twice (generates duplicate-confirm log errors)
            #   - Attempt to create+validate the invoice a second time (raises exceptions)
            #   - Trigger SaaS provisioning twice → duplicate saas.instance records
            # Therefore: call ONLY _set_done() and let Odoo handle post-processing.
            tx._set_done()
            _logger.info(
                "QR Mercantil [DEMO]: transacción %s marcada como DONE (post-process encolado automáticamente)", reference
            )
        except Exception:
            _logger.exception("QR Mercantil [DEMO]: error al simular pago ref=%s", reference)
            return {'status': 'error', 'message': 'simulation failed'}

        tx.invalidate_recordset()
        return {
            'status': 'ok',
            'state': tx.state,
            'landing_route': tx.landing_route or '/payment/status',
        }

    # ── Status polling — called by frontend JS every ~3 seconds ──────────────

    @http.route(
        '/payment/qr_mercantil/status',
        type='json',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def check_status(self, reference=None, **kwargs):
        """Return Odoo tx state; poll bank API every 10 s as webhook fallback.

        Throttle is stored in the DB field `qr_mercantil_last_polled` so all
        Odoo workers share it. SELECT … FOR UPDATE SKIP LOCKED ensures only
        one worker polls the bank at a time per transaction.
        """
        if not reference:
            return {'state': 'error', 'message': 'missing reference'}

        tx = request.env['payment.transaction'].sudo().search(
            [('reference', '=', reference), ('provider_code', '=', 'qr_mercantil')],
            limit=1,
        )
        if not tx:
            return {'state': 'error', 'message': 'transaction not found'}

        # Short-circuit: already in a terminal state
        if tx.state in ('done', 'cancel', 'error'):
            return {
                'state': tx.state,
                'reference': tx.reference,
                'landing_route': tx.landing_route or '/payment/status',
            }

        # ── Test mode: skip bank polling, just return current state ─────────────
        if tx.provider_id.state == 'test':
            return {
                'state': tx.state,
                'reference': tx.reference,
                'is_demo': True,
                'landing_route': tx.landing_route or '/payment/status',
            }

        # ── Webhook fallback: poll bank's estadoTransaccion ──────────────────
        # Use DB-level throttle + SELECT FOR UPDATE SKIP LOCKED to prevent
        # duplicate bank calls from multiple Odoo workers.
        try:
            cr = request.env.cr
            # Try to acquire an advisory row lock on this TX.
            # SKIP LOCKED means other workers won't wait — they just skip.
            cr.execute(
                """
                SELECT id, qr_mercantil_last_polled
                FROM payment_transaction
                WHERE id = %s
                FOR UPDATE SKIP LOCKED
                """,
                (tx.id,),
            )
            locked_row = cr.fetchone()

            if locked_row is None:
                # Another worker has this TX locked right now — skip polling
                _logger.debug(
                    "QR Mercantil: polling skipped (otro worker activo) ref=%s", reference
                )
            else:
                last_polled_dt = locked_row[1]  # may be None or a datetime
                now_epoch = time.time()
                if last_polled_dt is not None:
                    import datetime as _dt
                    last_epoch = last_polled_dt.timestamp() if hasattr(last_polled_dt, 'timestamp') else 0
                else:
                    last_epoch = 0

                if now_epoch - last_epoch >= _BANK_POLL_INTERVAL:
                    # Update timestamp BEFORE polling so other workers see it immediately
                    cr.execute(
                        "UPDATE payment_transaction SET qr_mercantil_last_polled = NOW() WHERE id = %s",
                        (tx.id,),
                    )

                    alias = tx.qr_mercantil_alias or reference
                    status_data = tx.provider_id._qr_mercantil_get_status(alias)
                    _logger.info(
                        "QR Mercantil: polling estado banco ref=%s alias=%s → %s",
                        reference, alias, status_data,
                    )

                    # MC4 wraps data inside 'objeto'
                    objeto = status_data.get('objeto') or {}
                    estado = (
                        objeto.get('estadoActual')      # MC4 real field name
                        or objeto.get('estado')
                        or objeto.get('estadoTransaccion')
                        or objeto.get('status')
                        or status_data.get('estado')
                        or ''
                    ).upper()

                    is_paid = (
                        estado in _PAID_STATES
                        or objeto.get('pagado') is True
                        or objeto.get('paid') is True
                    )

                    if is_paid:
                        _logger.info(
                            "QR Mercantil: pago confirmado vía polling → ref=%s estado=%s",
                            reference, estado,
                        )
                        notification_data = {
                            'alias': alias,
                            'monto': objeto.get('monto') or tx.amount,
                            'idQr': objeto.get('idQr') or tx.qr_mercantil_qr_id or '',
                        }
                        tx._handle_notification_data('qr_mercantil', notification_data)
                else:
                    _logger.debug(
                        "QR Mercantil: polling throttled (%.1fs restantes) ref=%s",
                        _BANK_POLL_INTERVAL - (now_epoch - last_epoch),
                        reference,
                    )

        except Exception:
            _logger.exception(
                "QR Mercantil: error al consultar estado banco ref=%s", reference
            )

        # Re-read after possible state change
        tx.invalidate_recordset()
        return {
            'state': tx.state,
            'reference': tx.reference,
            'landing_route': tx.landing_route or '/payment/status',
        }
