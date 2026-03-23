import base64
import logging
import threading
import time
import requests
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Per-process token cache lock (protects against duplicate fetches within one worker)
_TOKEN_LOCK = threading.Lock()
# Token refresh margin: refresh 5 minutes before actual expiry
_TOKEN_REFRESH_MARGIN_S = 300
# Assume token lives 55 minutes if the bank doesn't advertise expiry
_TOKEN_TTL_S = 55 * 60

_DEFAULT_BASE_URL = 'https://sip.mc4.com.bo:8443'

# Fake QR image used in demo mode — a minimal SVG QR-looking grid encoded as base64 PNG.
# In practice we just render a labelled SVG so it's obvious it's a demo.
_DEMO_QR_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
  <rect width="200" height="200" fill="white" stroke="#ccc" stroke-width="1"/>
  <!-- top-left anchor -->
  <rect x="10" y="10" width="50" height="50" fill="none" stroke="black" stroke-width="6"/>
  <rect x="22" y="22" width="26" height="26" fill="black"/>
  <!-- top-right anchor -->
  <rect x="140" y="10" width="50" height="50" fill="none" stroke="black" stroke-width="6"/>
  <rect x="152" y="22" width="26" height="26" fill="black"/>
  <!-- bottom-left anchor -->
  <rect x="10" y="140" width="50" height="50" fill="none" stroke="black" stroke-width="6"/>
  <rect x="22" y="152" width="26" height="26" fill="black"/>
  <!-- data dots -->
  <rect x="72" y="10" width="8" height="8" fill="black"/>
  <rect x="84" y="10" width="8" height="8" fill="black"/>
  <rect x="72" y="22" width="8" height="8" fill="black"/>
  <rect x="84" y="84" width="8" height="8" fill="black"/>
  <rect x="96" y="84" width="8" height="8" fill="black"/>
  <rect x="108" y="72" width="8" height="8" fill="black"/>
  <rect x="120" y="60" width="8" height="8" fill="black"/>
  <rect x="132" y="96" width="8" height="8" fill="black"/>
  <rect x="72" y="108" width="8" height="8" fill="black"/>
  <rect x="96" y="120" width="8" height="8" fill="black"/>
  <rect x="108" y="132" width="8" height="8" fill="black"/>
  <rect x="120" y="144" width="8" height="8" fill="black"/>
  <rect x="132" y="156" width="8" height="8" fill="black"/>
  <rect x="144" y="168" width="8" height="8" fill="black"/>
  <rect x="156" y="132" width="8" height="8" fill="black"/>
  <rect x="168" y="144" width="8" height="8" fill="black"/>
  <!-- DEMO label -->
  <rect x="60" y="80" width="80" height="40" rx="4" fill="#ff5722" opacity="0.9"/>
  <text x="100" y="107" font-family="Arial,sans-serif" font-size="18" font-weight="bold"
        fill="white" text-anchor="middle">DEMO</text>
</svg>"""
_DEMO_QR_B64 = base64.b64encode(_DEMO_QR_SVG.encode()).decode()


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('qr_mercantil', 'QR Mercantil')],
        ondelete={'qr_mercantil': 'set default'},
    )

    # ── Credentials ─────────────────────────────────────────────────────────
    qr_mercantil_api_key = fields.Char(
        string='API Key (Login)',
        help='Header "apikey" para el endpoint de autenticación.',
        required_if_provider='qr_mercantil',
    )
    qr_mercantil_api_key_service = fields.Char(
        string='API Key Servicio',
        help='Header "apikeyServicio" para los endpoints de QR.',
        required_if_provider='qr_mercantil',
    )
    qr_mercantil_username = fields.Char(
        string='Usuario API',
        required_if_provider='qr_mercantil',
    )
    qr_mercantil_password = fields.Char(
        string='Contraseña API',
        required_if_provider='qr_mercantil',
    )
    qr_mercantil_base_url = fields.Char(
        string='URL Base API',
        default=_DEFAULT_BASE_URL,
        required_if_provider='qr_mercantil',
    )

    # ── Token cache (shared across workers via DB) ────────────────────────────
    qr_mercantil_token_cache = fields.Char(
        string='JWT Token (caché)',
        copy=False,
        help='Caché interno del JWT. No editar manualmente.',
    )
    qr_mercantil_token_expires = fields.Float(
        string='Token expira (epoch)',
        copy=False,
        help='Timestamp UNIX en que el token expira.',
    )
    qr_mercantil_webhook_url = fields.Char(
        string='Webhook URL (Callback)',
        help=(
            'URL que el banco llamará cuando se complete un pago QR. '
            'Se envía como campo "callback" en cada llamada a generaQr. '
            'Ejemplo: https://admin.aeisoftware.com/payment/qr_mercantil/webhook\n'
            'Si se deja vacío se usa el dominio configurado en Ajustes → Parámetros técnicos → web.base.url'
        ),
    )
    # Modo demo/test: se controla con el campo nativo `state` de Odoo.
    # Cuando state == 'test' el proveedor opera en modo demo (sin llamadas reales al banco).
    # Ver: _qr_mercantil_generate_qr(), _qr_mercantil_get_status(), _qr_mercantil_get_token().

    # ── API helpers ──────────────────────────────────────────────────────────

    def _qr_mercantil_get_token(self):
        """Obtiene (o devuelve cacheado) un JWT token del banco.

        El token se guarda en campos DB para compartirlo entre workers Odoo.
        Un lock de threading evita solicitudes duplicadas dentro del mismo proceso.

        En modo demo nunca se autentica con el banco — lanza un error explícito
        para que el caller sepa que no debe llamar a este método en demo mode.
        """
        self.ensure_one()
        if self.state == 'test':
            raise ValidationError(
                _("QR Mercantil [TEST]: _qr_mercantil_get_token() no debe llamarse "
                  "en modo test. Revisa el código que invocó este método.")
            )
        now = time.time()

        # 1. Fast path: check DB cache (shared across all workers)
        # Re-read directly from DB to avoid ORM cache staleness
        self.env.cr.execute(
            "SELECT qr_mercantil_token_cache, qr_mercantil_token_expires "
            "FROM payment_provider WHERE id = %s",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        cached_token = row[0] if row else None
        cached_expires = row[1] if row else 0.0

        if cached_token and cached_expires and now < (cached_expires - _TOKEN_REFRESH_MARGIN_S):
            _logger.debug(
                "QR Mercantil: usando token cacheado (expira en %.0fs)",
                cached_expires - now,
            )
            return cached_token

        # 2. Slow path: fetch a new token (serialise within this process)
        with _TOKEN_LOCK:
            # Re-check after acquiring lock (another thread may have refreshed)
            self.env.cr.execute(
                "SELECT qr_mercantil_token_cache, qr_mercantil_token_expires "
                "FROM payment_provider WHERE id = %s",
                (self.id,),
            )
            row = self.env.cr.fetchone()
            cached_token = row[0] if row else None
            cached_expires = row[1] if row else 0.0
            now = time.time()
            if cached_token and cached_expires and now < (cached_expires - _TOKEN_REFRESH_MARGIN_S):
                return cached_token

            token = self._qr_mercantil_fetch_token()
            expires_at = now + _TOKEN_TTL_S
            # Save to DB so other workers can use it
            self.env.cr.execute(
                "UPDATE payment_provider "
                "SET qr_mercantil_token_cache = %s, qr_mercantil_token_expires = %s "
                "WHERE id = %s",
                (token, expires_at, self.id),
            )
            return token

    def _qr_mercantil_fetch_token(self):
        """Hace la llamada HTTP real al banco para obtener un token nuevo."""
        self.ensure_one()
        url = f"{self.qr_mercantil_base_url}/autenticacion/v1/generarToken"
        _logger.info(
            "QR Mercantil: obteniendo token → url=%s apikey_len=%d user=%s",
            url,
            len(self.qr_mercantil_api_key or ''),
            self.qr_mercantil_username or '(vacío)',
        )
        try:
            resp = requests.post(
                url,
                headers={
                    'apikey': self.qr_mercantil_api_key,
                    'Content-Type': 'application/json',
                },
                json={
                    'username': self.qr_mercantil_username,
                    'password': self.qr_mercantil_password,
                },
                timeout=15,
                verify=True,
            )
            _logger.info(
                "QR Mercantil: respuesta token → status=%s body=%s",
                resp.status_code,
                resp.text[:300],
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                token = (
                    data.get('token')
                    or data.get('accessToken')
                    or data.get('access_token')
                    or (data.get('objeto') or {}).get('token')
                    or (data.get('objeto') or {}).get('accessToken')
                    or ''
                )
                _logger.info(
                    "QR Mercantil: token obtenido → keys=%s token_len=%d",
                    list(data.keys()),
                    len(token),
                )
                return token
            token = str(data)
            _logger.info("QR Mercantil: token raw len=%d", len(token))
            return token
        except requests.exceptions.RequestException as exc:
            _logger.error(
                "QR Mercantil: error al obtener token: %s body=%s",
                exc,
                getattr(exc.response, 'text', '')[:300] if hasattr(exc, 'response') else '',
            )
            raise ValidationError(
                _("No se pudo autenticar con QR Mercantil: %s") % exc
            )

    def _qr_mercantil_generate_qr(
        self, alias, amount, currency_name, description, callback_url, due_date=None
    ):
        """Genera un QR en el banco y retorna el payload de respuesta.

        En modo demo devuelve un payload ficticio sin llamar al banco.
        """
        self.ensure_one()

        # ── Test mode: return a fake QR without any bank calls ─────────────────
        # Odoo's native `state == 'test'` replaces the former qr_mercantil_demo_mode field.
        if self.state == 'test':
            _logger.info(
                "QR Mercantil [TEST]: generando QR ficticio para alias=%s amount=%s",
                alias, amount,
            )
            return {
                'objeto': {
                    'imagenQr': _DEMO_QR_B64,
                    'idQr': f'TEST-{alias}',
                }
            }

        token = self._qr_mercantil_get_token()
        if not due_date:
            due_date = (datetime.now() + timedelta(days=1)).strftime('%d/%m/%Y')

        url = f"{self.qr_mercantil_base_url}/api/v1/generaQr"
        payload = {
            'alias': alias,
            'callback': callback_url,
            'detalleGlosa': description,
            'monto': float(amount),
            'moneda': currency_name,
            'fechaVencimiento': due_date,
            'tipoSolicitud': 'API',
        }
        _logger.info(
            "QR Mercantil: generando QR → url=%s apikeyServicio_len=%d token_len=%d payload=%s",
            url,
            len(self.qr_mercantil_api_key_service or ''),
            len(token or ''),
            payload,
        )
        try:
            resp = requests.post(
                url,
                headers={
                    'apikeyServicio': self.qr_mercantil_api_key_service,
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=15,
                verify=True,
            )
            _logger.info(
                "QR Mercantil: respuesta generaQr → status=%s body=%s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            _logger.error(
                "QR Mercantil: error al generar QR (alias=%s): %s body=%s",
                alias,
                exc,
                getattr(exc.response, 'text', '')[:500] if hasattr(exc, 'response') else '',
            )
            raise ValidationError(
                _("No se pudo generar el QR Mercantil: %s") % exc
            )

    def _qr_mercantil_get_status(self, alias):
        """Consulta el estado de una transacción por alias.

        En modo demo devuelve un payload ficticio sin contactar al banco.
        """
        self.ensure_one()
        if self.state == 'test':
            _logger.info(
                "QR Mercantil [TEST]: _qr_mercantil_get_status() "
                "llamado en modo test — devolviendo estado ficticio para alias=%s", alias,
            )
            return {'objeto': {'estadoActual': 'PENDIENTE', 'test': True}}
        token = self._qr_mercantil_get_token()
        url = f"{self.qr_mercantil_base_url}/api/v1/estadoTransaccion"
        try:
            resp = requests.post(
                url,
                headers={
                    'apikeyServicio': self.qr_mercantil_api_key_service,
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                json={'alias': alias},
                timeout=15,
                verify=True,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            _logger.error("QR Mercantil: error al consultar estado (alias=%s): %s", alias, exc)
            return {}

    # ── Odoo 18 payment flow ─────────────────────────────────────────────────
    # NOTE: _get_specific_rendering_values is defined on PaymentTransaction
    #       (see payment_transaction.py) — Odoo 18 calls it on the TX model.
