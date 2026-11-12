import re

import frappe
from frappe import _
from frappe.model.document import Document


class SAPIntegrationProfile(Document):
    def validate(self):
        self.seller_gstin = (self.seller_gstin or "").strip().upper()
        if not re.fullmatch(r"[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}", self.seller_gstin):
            frappe.throw(_("Seller GSTIN has an invalid format"))
        company_gstin = frappe.db.get_value("Company", self.company, "gstin")
        if company_gstin and company_gstin.upper() != self.seller_gstin:
            frappe.throw(_("Seller GSTIN differs from the GSTIN of company {0}").format(self.company))
        if (self.environment == "Sandbox") != bool(frappe.get_cached_doc("GST Settings").sandbox_mode):
            frappe.msgprint(
                _("The profile environment does not match the GST Settings sandbox mode; GST requests will be refused until they match."),
                indicator="orange",
            )
