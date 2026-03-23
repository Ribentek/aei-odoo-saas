{
    'name': 'Odoo K8s SaaS',
    'version': '18.0.3.0.0',
    'summary': 'SaaS management: provision tenants, view logs, edit config, sync addon repos',
    'category': 'Technical',
    'author': 'AEI Software',
    'license': 'LGPL-3',
    'depends': ['base', 'web', 'mail', 'sale', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'data/product_category.xml',
        'data/mail_template.xml',
        'views/saas_instance_views.xml',
        'data/ir_cron.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
