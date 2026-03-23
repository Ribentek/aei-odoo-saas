# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID
from . import controllers, models


def _post_init_hook(env):
    """Ensure provider is created if data wasn't loaded."""
    pass


def _uninstall_hook(env):
    """Archive the provider on uninstall to avoid orphaned records."""
    provider = env.ref(
        'payment_qr_mercantil.payment_provider_qr_mercantil', raise_if_not_found=False
    )
    if provider:
        provider.write({'state': 'disabled', 'is_published': False})
