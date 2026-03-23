-- ============================================================
-- ODOO SAAS MVP — RESET DE DATOS TRANSACCIONALES v2
-- Conserva: usuarios, empresa, productos, proveedores de pago,
--            plan de cuentas, secuencias, journals, configuración.
-- Elimina:   pedidos, facturas, pagos, suscripciones, instancias K8s.
-- ============================================================
-- MODO DE USO:
--   kubectl exec -n aeisoftware postgres-0 -- \
--     psql -U odoo -d admin -f /tmp/reset_transactional_data.sql
-- ============================================================

BEGIN;

-- Bypass FK constraints temporalmente para evitar problemas de orden
SET session_replication_role = 'replica';

-- ─────────────────────────────────────────────────────────────
-- 1. MÓDULOS SAAS CUSTOM (sin CASCADE — tabla hoja)
-- ─────────────────────────────────────────────────────────────
DELETE FROM saas_instance;
ALTER SEQUENCE saas_instance_id_seq RESTART WITH 1;

-- ─────────────────────────────────────────────────────────────
-- 2. SUSCRIPCIONES
-- ─────────────────────────────────────────────────────────────
DELETE FROM sale_subscription_line;
DELETE FROM sale_subscription;
ALTER SEQUENCE sale_subscription_line_id_seq RESTART WITH 1;
ALTER SEQUENCE sale_subscription_id_seq RESTART WITH 1;

-- ─────────────────────────────────────────────────────────────
-- 3. PAGOS Y TRANSACCIONES
-- ─────────────────────────────────────────────────────────────
DELETE FROM account_payment;
DELETE FROM payment_transaction;
ALTER SEQUENCE account_payment_id_seq RESTART WITH 1;
ALTER SEQUENCE payment_transaction_id_seq RESTART WITH 1;

-- ─────────────────────────────────────────────────────────────
-- 4. CONTABILIDAD: RECONCILIACIONES → LÍNEAS → ASIENTOS
-- ─────────────────────────────────────────────────────────────
DELETE FROM account_partial_reconcile;
DELETE FROM account_full_reconcile;
DELETE FROM account_move_line;
DELETE FROM account_move;
DELETE FROM account_bank_statement_line;
DELETE FROM account_bank_statement;
ALTER SEQUENCE account_partial_reconcile_id_seq RESTART WITH 1;
ALTER SEQUENCE account_full_reconcile_id_seq RESTART WITH 1;
ALTER SEQUENCE account_move_line_id_seq RESTART WITH 1;
ALTER SEQUENCE account_move_id_seq RESTART WITH 1;
ALTER SEQUENCE account_bank_statement_id_seq RESTART WITH 1;
ALTER SEQUENCE account_bank_statement_line_id_seq RESTART WITH 1;

-- ─────────────────────────────────────────────────────────────
-- 5. VENTAS
-- ─────────────────────────────────────────────────────────────
DELETE FROM sale_order_line;
DELETE FROM sale_order;
ALTER SEQUENCE sale_order_line_id_seq RESTART WITH 1;
ALTER SEQUENCE sale_order_id_seq RESTART WITH 1;

-- ─────────────────────────────────────────────────────────────
-- 6. MENSAJERÍA: solo mensajes de los modelos transaccionales
--    (NO usamos CASCADE para no tocar config de mail_template)
-- ─────────────────────────────────────────────────────────────
DELETE FROM mail_notification
WHERE mail_message_id IN (
    SELECT id FROM mail_message
    WHERE model IN (
        'sale.order', 'sale.subscription', 'account.move',
        'account.payment', 'payment.transaction', 'saas.instance'
    )
);

DELETE FROM mail_message_res_partner_rel
WHERE mail_message_id IN (
    SELECT id FROM mail_message
    WHERE model IN (
        'sale.order', 'sale.subscription', 'account.move',
        'account.payment', 'payment.transaction', 'saas.instance'
    )
);

DELETE FROM mail_message
WHERE model IN (
    'sale.order', 'sale.subscription', 'account.move',
    'account.payment', 'payment.transaction', 'saas.instance'
);

DELETE FROM mail_followers
WHERE res_model IN (
    'sale.order', 'sale.subscription', 'account.move',
    'account.payment', 'payment.transaction', 'saas.instance'
);

DELETE FROM mail_activity
WHERE res_model IN (
    'sale.order', 'sale.subscription', 'account.move',
    'account.payment', 'payment.transaction', 'saas.instance'
);

-- ─────────────────────────────────────────────────────────────
-- 7. RESETEAR SECUENCIAS DE ODOO (numeración de documentos)
-- ─────────────────────────────────────────────────────────────
UPDATE ir_sequence SET number_next_actual = 1
WHERE code IN (
    'sale.order',
    'account.payment.customer.invoice',
    'account.payment.customer.receipt',
    'account.payment.supplier.invoice',
    'account.payment.supplier.receipt',
    'sale.subscription',
    'saas.tenant.id'
);

-- Restaurar FK
SET session_replication_role = 'origin';

COMMIT;

-- ─────────────────────────────────────────────────────────────
-- VERIFICACIÓN POST-RESET
-- ─────────────────────────────────────────────────────────────
SELECT 'saas_instance'      AS tabla, COUNT(*) AS registros FROM saas_instance
UNION ALL
SELECT 'sale_subscription',           COUNT(*) FROM sale_subscription
UNION ALL
SELECT 'payment_transaction',         COUNT(*) FROM payment_transaction
UNION ALL
SELECT 'account_move',                COUNT(*) FROM account_move
UNION ALL
SELECT 'account_payment',             COUNT(*) FROM account_payment
UNION ALL
SELECT 'sale_order',                  COUNT(*) FROM sale_order
ORDER BY tabla;
