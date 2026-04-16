"""
pre-migrate.py — 18.0.1.4.0

Adds dunning and grace-period columns to saas_instance if they do not exist.

New fields:
  - dunning_level (Integer, default 0): escalation level (0-3)
  - dunning_last_sent (Date): when the last dunning email was sent
  - closed_date (Datetime): when the linked subscription was closed

These columns are also created by Odoo's ORM on module upgrade, but
doing it here in pre-migrate avoids any NOT NULL constraint issues
if the table already has rows.
"""
import logging

logger = logging.getLogger(__name__)


def migrate(cr, version):
    logger.info("pre-migrate 18.0.1.4.0: adding dunning/grace-period columns to saas_instance")

    columns = [
        ("dunning_level", "INTEGER DEFAULT 0"),
        ("dunning_last_sent", "DATE"),
        ("closed_date", "TIMESTAMP"),
    ]

    for col_name, col_def in columns:
        cr.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'saas_instance' AND column_name = %s
            """,
            (col_name,),
        )
        if not cr.fetchone():
            cr.execute(
                f"ALTER TABLE saas_instance ADD COLUMN {col_name} {col_def}"
            )
            logger.info("pre-migrate: added column saas_instance.%s", col_name)
        else:
            logger.info("pre-migrate: column saas_instance.%s already exists — skipping", col_name)

    logger.info("pre-migrate 18.0.1.4.0: done")
