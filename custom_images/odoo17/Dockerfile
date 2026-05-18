# Heredamos de la imagen oficial
FROM odoo:17

# Pasamos a root para poder instalar dependencias
USER root

# Combinamos todo en un solo RUN para ahorrar capas (layers).
# Agregamos --no-cache-dir para reducir el peso de la imagen.
# Mantenemos --break-system-packages para evitar el bloqueo de PEP 668.
RUN pip3 install --no-cache-dir \
    qifparse \
    ofxparse \
    openpyxl \
    --upgrade requests_toolbelt

# Volvemos al usuario odoo por seguridad
USER odoo
