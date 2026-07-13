# Custom Odoo 18 image for the AEI SaaS.
# Adds Python deps missing from the official image:
#   - email_validator: required by OCA auth_signup_verify_email
# Everything else (addons, config) is still injected at runtime by the init
# containers, so this image stays thin and rarely needs rebuilding.
FROM odoo:18

USER root
RUN pip3 install --no-cache-dir --break-system-packages email_validator
USER odoo
