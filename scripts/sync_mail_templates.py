# Sincroniza mail.template desde los XML del addon (bypass de noupdate)
from lxml import etree

FILES = [
    ("/mnt/extra-addons/odoo_k8s_saas/data/mail_template.xml", "odoo_k8s_saas"),
    ("/mnt/extra-addons/odoo_k8s_saas/data/mail_template_data.xml", "odoo_k8s_saas"),
    ("/mnt/extra-addons/odoo_k8s_saas_subscription/data/mail_template_dunning.xml", "odoo_k8s_saas_subscription"),
    ("/mnt/extra-addons/odoo_k8s_saas_subscription/data/mail_template_renewal.xml", "odoo_k8s_saas_subscription"),
]

MODEL_BY_REF = {
    "odoo_k8s_saas.model_saas_instance": "saas.instance",
    "account.model_account_move": "account.move",
}

results = []
for path, module in FILES:
    tree = etree.parse(path)
    for rec in tree.findall(".//record[@model='mail.template']"):
        xid = rec.get("id")
        vals = {}
        model_name = None
        for f in rec.findall("field"):
            fname = f.get("name")
            if fname == "model_id":
                model_name = MODEL_BY_REF.get(f.get("ref"))
            elif fname == "body_html":
                inner = (f.text or "") + "".join(
                    etree.tostring(c, encoding="unicode") for c in f
                )
                vals["body_html"] = inner.strip()
            elif fname in ("name", "subject", "email_from", "email_to"):
                vals[fname] = (f.text or "").strip()
        full_xid = module + "." + xid
        tmpl = env.ref(full_xid, raise_if_not_found=False)
        if tmpl:
            tmpl.write(vals)
            results.append("updated " + xid)
        else:
            model_rec = env["ir.model"]._get(model_name or "saas.instance")
            vals["model_id"] = model_rec.id
            tmpl = env["mail.template"].create(vals)
            env["ir.model.data"].create({
                "module": module, "name": xid,
                "model": "mail.template", "res_id": tmpl.id,
                "noupdate": True,
            })
            results.append("created " + xid)

env.cr.commit()

cred = env.ref("odoo_k8s_saas.email_template_saas_credentials")
prov = env.ref("odoo_k8s_saas.mail_template_instance_provisioned")
d1 = env.ref("odoo_k8s_saas_subscription.email_template_dunning_level1")
ren = env.ref("odoo_k8s_saas_subscription.email_template_renewal_invoice")
checks = "credES=" + str("listo" in str(cred.subject)) \
    + " provES=" + str("preparando" in str(prov.subject)) \
    + " dunAbs=" + str("get_base_url" in str(d1.body_html)) \
    + " renAbs=" + str("get_base_url" in str(ren.body_html))
print("RESULT>> " + "; ".join(results))
print("RESULT>> " + checks)
