# Payment QR Mercantil

The `payment_qr_mercantil` module integrates Odoo 18's e-commerce payment flow with **Banco Mercantil Santa Cruz** QR payments (mc4.com.bo API) for the Bolivian market.

## Module Details

| Field | Value |
|:---|:---|
| Technical Name | `payment_qr_mercantil` |
| Version | `18.0.1.1.0` |
| Category | Accounting/Payment Providers |
| License | LGPL-3 |
| Depends | `payment`, `website_sale` |
| Author | AEI Software |

## Payment Flow

```
Customer → Checkout → Select "QR Mercantil"
  │
  ▼
_get_specific_rendering_values()
  │  Calls bank API → POST /api/v1/generaQr
  │  Returns: alias, QR image (base64), qr_id
  │
  ▼
QR code displayed on /payment/qr_mercantil/display
  │  Frontend JS polls /payment/qr_mercantil/status every 3s
  │
  ▼
Customer scans QR with bank app → pays
  │
  ▼
Bank calls webhook → POST /payment/qr_mercantil/webhook
  │  (or: polling detects payment via /api/v1/estadoTransaccion)
  │
  ▼
_process_notification_data() → _set_done()
  │  Transaction marked as DONE
  │  → Odoo confirms SO, creates+validates invoice, registers payment
  │  → account.move._compute_payment_state() triggers SaaS provisioning
```

### Dual Payment Confirmation

The module uses a **belt-and-suspenders** approach:

1. **Webhook** (primary): Bank POSTs to `/payment/qr_mercantil/webhook` when payment succeeds
2. **Polling** (fallback): Frontend JS polls `/payment/qr_mercantil/status` every 3 seconds. Server-side, the bank's `estadoTransaccion` API is queried every 10 seconds per-transaction with `SELECT FOR UPDATE SKIP LOCKED` throttling across workers

## Test/Demo Mode

When the Odoo payment provider is set to `state = 'test'`:

- **No bank API calls** are made — all methods return mocked data
- A **fake SVG QR** with a "DEMO" label is displayed to the customer
- A **"Simular Pago"** button appears on the QR display page
- Clicking it calls `/payment/qr_mercantil/simulate` which:
  1. Calls `_set_done()` on the transaction
  2. Calls `_post_process()` to confirm the SO + create invoice synchronously
  3. Triggers the SaaS provisioning pipeline (identical to a real payment)

This allows full end-to-end testing without a bank sandbox.

## Bank API Integration

Base URL: `https://sip.mc4.com.bo:8443` (production)

### Authentication

```
POST /autenticacion/v1/generarToken
Headers: apikey: <qr_mercantil_api_key>
Body: {"username": "...", "password": "..."}
→ Returns JWT token (cached ~55 minutes)
```

Token caching uses:
- **DB storage** (`qr_mercantil_token_cache`, `qr_mercantil_token_expires`) shared across Odoo workers
- **Thread lock** per process to prevent duplicate fetch within the same worker
- **5-minute refresh margin** before actual expiry

### Generate QR

```
POST /api/v1/generaQr
Headers:
  apikeyServicio: <qr_mercantil_api_key_service>
  Authorization: Bearer <token>
Body: {
  "alias": "<tx_reference>",
  "callback": "<webhook_url>",
  "detalleGlosa": "Pedido <reference>",
  "monto": 99.99,
  "moneda": "BOB",
  "fechaVencimiento": "09/04/2026",
  "tipoSolicitud": "API"
}
→ Returns: {"objeto": {"imagenQr": "<base64>", "idQr": "..."}}
```

### Check Status

```
POST /api/v1/estadoTransaccion
Headers: apikeyServicio + Authorization
Body: {"alias": "<reference>"}
→ Returns: {"objeto": {"estadoActual": "PAGADO"|"PENDIENTE"|...}}
```

Recognized paid states: `PAGADO`, `EJECUTADO`, `APROBADO`, `COMPLETADO`, `PROCESADO`, `DONE`, `PAID`, `SUCCESS` (and feminine variants).

## Configuration

### Provider Credentials

Set via **Invoicing → Payment Providers → QR Mercantil**:

| Field | Description |
|:---|:---|
| API Key (Login) | `apikey` header for authentication endpoint |
| API Key Servicio | `apikeyServicio` header for QR endpoints |
| Usuario API | Username for bank authentication |
| Contraseña API | Password for bank authentication |
| URL Base API | Default: `https://sip.mc4.com.bo:8443` |
| Webhook URL | Override callback URL (or leave empty to use `web.base.url`) |

### Transaction Fields

Added to `payment.transaction`:

| Field | Type | Description |
|:---|:---|:---|
| `qr_mercantil_alias` | `Char` | Transaction reference used as bank alias |
| `qr_mercantil_image` | `Text` | QR image in base64 format |
| `qr_mercantil_qr_id` | `Char` | Bank-assigned QR ID |
| `qr_mercantil_last_polled` | `Datetime` | Cross-worker throttle timestamp |

## HTTP Endpoints

| Endpoint | Method | Auth | Purpose |
|:---|:---|:---|:---|
| `/payment/qr_mercantil/display` | GET | public | Show QR code page to customer |
| `/payment/qr_mercantil/webhook` | POST (JSON) | public | Bank webhook — no CSRF |
| `/payment/qr_mercantil/status` | POST (JSON) | public | Frontend polling + bank fallback |
| `/payment/qr_mercantil/simulate` | POST (JSON) | public | Demo only — simulate payment |

## File Structure

```
payment_qr_mercantil/
├── __init__.py                (post_init_hook + uninstall_hook)
├── __manifest__.py
├── controllers/
│   └── main.py                ← Display, webhook, status, simulate endpoints
├── data/
│   └── payment_provider_data.xml  ← Default provider record + icons
├── models/
│   ├── __init__.py
│   ├── account_payment_method.py  ← Payment method registration
│   ├── payment_provider.py        ← API helpers (token, QR gen, status)
│   └── payment_transaction.py     ← Rendering values + notification processing
├── security/
│   └── ir.model.access.csv
├── static/
│   └── src/js/
│       └── qr_mercantil_form.js   ← Frontend polling + simulate logic
└── views/
    ├── payment_provider_views.xml        ← Provider config form
    └── payment_qr_mercantil_templates.xml ← QR display + redirect form
```

## Integration with SaaS Pipeline

When the customer pays via QR:
1. `_set_done()` → transaction state = DONE
2. Odoo confirms the Sale Order
3. Invoice is auto-created and validated
4. Payment is registered and reconciled
5. `account.move._compute_payment_state()` detects `paid` state
6. `_saas_check_and_provision()` creates `saas.instance` and calls `action_provision()`

This is the **same trigger** as any other payment provider — the QR module doesn't have SaaS-specific code.

## Security Considerations

- Webhook endpoint has `csrf=False` (required for bank callbacks)
- Bank credentials are stored in the Odoo database (covered by Odoo's access control)
- Token caching uses raw SQL for cross-worker consistency (bypasses ORM cache)
- Polling uses `SELECT FOR UPDATE SKIP LOCKED` to prevent thundering herd
- All bank API calls use TLS (`verify=True`)
