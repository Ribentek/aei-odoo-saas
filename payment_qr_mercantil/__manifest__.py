{
    'name': 'Payment Provider: QR Mercantil',
    'version': '18.0.1.1.0',
    'category': 'Accounting/Payment Providers',
    'summary': 'Pago por QR — Banco Mercantil Santa Cruz (mc4.com.bo)',
    'description': """
        Integra el proveedor de pago QR Mercantil del Banco Mercantil Santa Cruz
        de Bolivia con el e-commerce de Odoo 18.

        Flujo:
        1. Cliente genera un QR en el checkout.
        2. El banco notifica el pago via webhook.
        3. La orden es confirmada automáticamente.
    """,
    'author': 'AEI Software',
    'website': 'https://aeisoftware.com',
    'depends': ['payment', 'website_sale'],
    'data': [
        'security/ir.model.access.csv',
        'views/payment_provider_views.xml',
        'views/payment_qr_mercantil_templates.xml',
        'data/payment_provider_data.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'payment_qr_mercantil/static/src/js/qr_mercantil_form.js',
        ],
    },
    'post_init_hook': '_post_init_hook',
    'uninstall_hook': '_uninstall_hook',
    'application': False,
    'installable': True,
    'license': 'LGPL-3',
}
