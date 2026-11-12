"""Retention. Never deletes Generated / Cancelled / Unknown / Processing records (audit and unresolved transactions)."""

import frappe
from frappe.utils import add_days, now_datetime


def cleanup_transactions():
    """Definitive failures older than the profile retention (default 90 days)."""
    for p in frappe.get_all("SAP Integration Profile", fields=["name", "retention_days"]):
        cutoff = add_days(now_datetime(), -int(p.retention_days or 90))
        names = frappe.get_all(
            "SAP GST Transaction",
            filters={"integration_id": p.name, "processing_status": ["in", ["Failed", "Validation Failed"]], "modified": ["<", cutoff]},
            pluck="name",
        )
        for name in names:
            frappe.delete_doc("SAP GST Transaction", name, force=True, ignore_permissions=True)
    frappe.db.commit()


def cleanup_integration_requests():
    """
    india_compliance logs every GSP call (payload included) as an Integration Request. Calls made by this app have no reference document.
    Remove those after 30 days so that invoice data does not accumulate in ERPNext.
    """
    cutoff = add_days(now_datetime(), -30)
    frappe.db.delete(
        "Integration Request",
        {"integration_request_service": "India Compliance API", "reference_doctype": ["is", "not set"], "creation": ["<", cutoff]},
    )
    frappe.db.commit()
