"""Frappe-bound implementations of the service interfaces. The ONLY DocTypes touched are 'SAP Integration Profile' and 'SAP GST Transaction' (read: Company, GST Settings)."""

from decimal import Decimal
from typing import Any, Dict, Optional

import frappe

from .service import Profile, ProfileRepo, SiteInfo, TxnStore

TXN = "SAP GST Transaction"
PROFILE = "SAP Integration Profile"


class FrappeProfileRepo(ProfileRepo):
    def get(self, integration_id: str) -> Optional[Profile]:
        if not frappe.db.exists(PROFILE, integration_id):
            return None
        p = frappe.get_cached_doc(PROFILE, integration_id)
        return Profile(
            integration_id=p.name, enabled=bool(p.enabled), sap_database=p.sap_database or "", company=p.company or "", seller_gstin=(p.seller_gstin or "").upper(),
            environment=p.environment or "Production", api_user=p.api_user or "", allow_einvoice=bool(p.allow_einvoice), allow_ewaybill=bool(p.allow_ewaybill),
            allow_cancel=bool(p.allow_cancel), allow_updates=bool(p.allow_updates), retention_days=int(p.retention_days or 90),
            rounding_tolerance=Decimal(str(p.rounding_tolerance or "1.00")), min_hsn_digits=int(p.min_hsn_digits or 4),
        )


class FrappeSite(SiteInfo):
    def user(self) -> str:
        return frappe.session.user

    def sandbox_mode(self) -> bool:
        return bool(frappe.get_cached_doc("GST Settings").sandbox_mode)

    def company_gstin(self, company: str) -> Optional[str]:
        return frappe.db.get_value("Company", company, "gstin") if company else None

    def has_credentials(self, gstin: str) -> bool:
        settings = frappe.get_cached_doc("GST Settings")
        return any((row.gstin or "").upper() == gstin.upper() for row in (settings.get("credentials") or []))


class FrappeStore(TxnStore):
    """Minimal metadata only. The primary key (name) IS the unique transaction reference, so concurrent creates cannot both succeed."""

    def get(self, ref: str) -> Optional[Dict[str, Any]]:
        row = frappe.db.get_value(TXN, ref, "*", as_dict=True, for_update=True)    # row lock for the duration of the request transaction
        return dict(row) if row else None

    def create(self, ref: str, fields: Dict[str, Any]) -> bool:
        doc = frappe.get_doc({"doctype": TXN, **fields})
        doc.name = ref
        try:
            doc.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
            return False
        frappe.db.commit()                                                          # the lock must be visible to concurrent requests before the GSP call starts
        return True

    def update(self, ref: str, **fields):
        frappe.db.set_value(TXN, ref, fields, update_modified=True)
        frappe.db.commit()                                                          # status changes are durable even if the request later fails
