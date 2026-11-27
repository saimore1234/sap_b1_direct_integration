"""
GST transaction service: authorisation, validation, idempotency, locking, reconciliation and status management.

Framework-independent: persistence, the GSP client, the clock and the site information are injected, so the whole workflow is
unit-tested without Frappe. NO ERPNext Sales Invoice, Customer, Item, stock or accounting document is read or written anywhere here.
An IRN / e-Way Bill number only ever comes from the GSP adapter's real response.
"""

import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from . import constants as C
from . import nic_payload as N
from . import validation as V
from .contract import ContractError, Invoice, Transport, dec, parse_invoice, text, parse_date


# ------------------------------------------------------------------------------------------------ errors / DTOs
class AccessDenied(Exception):
    pass


class GspRejected(Exception):
    """The IRP / e-Way Bill system answered with a definitive business error."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class GspUncertain(Exception):
    """Timeout / gateway / connection failure: the outcome at the government system is unknown."""


@dataclass
class Profile:
    integration_id: str
    enabled: bool
    sap_database: str
    company: str
    seller_gstin: str
    environment: str                  # Sandbox | Production
    api_user: str
    allow_einvoice: bool = True
    allow_ewaybill: bool = True
    allow_cancel: bool = True
    allow_updates: bool = True
    retention_days: int = 90
    rounding_tolerance: Decimal = Decimal("1.00")
    min_hsn_digits: int = 4


class ProfileRepo:
    def get(self, integration_id: str) -> Optional[Profile]:
        raise NotImplementedError


class SiteInfo:
    def user(self) -> str:
        raise NotImplementedError

    def sandbox_mode(self) -> bool:
        raise NotImplementedError

    def company_gstin(self, company: str) -> Optional[str]:
        raise NotImplementedError

    def has_credentials(self, gstin: str) -> bool:
        raise NotImplementedError


class TxnStore:
    def get(self, ref: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def create(self, ref: str, fields: Dict[str, Any]) -> bool:
        """Atomically create the record. Returns False if the reference already exists (the unique-reference lock)."""
        raise NotImplementedError

    def update(self, ref: str, **fields):
        raise NotImplementedError


class Gsp:
    """Adapter contract. Implemented by IndiaComplianceAdapter (production) and by fakes in tests."""

    def generate_irn(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def get_irn(self, irn: str) -> Dict[str, Any]: ...
    def cancel_irn(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def generate_ewb(self, payload: Dict[str, Any], with_irn: bool) -> Dict[str, Any]: ...
    def get_ewb(self, ewb_no: str) -> Dict[str, Any]: ...
    def find_ewb(self, doc_no: str, doc_date: date, from_gstin: str) -> Optional[Dict[str, Any]]: ...
    def cancel_ewb(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def update_vehicle(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def update_transporter(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def extend_validity(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def lookup_gstin(self, gstin: str) -> Dict[str, Any]: ...


# ------------------------------------------------------------------------------------------------ GSTIN search
GSTIN_STATUSES = {"active": "Active", "inactive": "Inactive", "cancelled": "Cancelled", "suspended": "Suspended", "invalid": "Invalid"}
_ADDRESS_KEYS = (("building_number", "bno"), ("floor", "flno"), ("building_name", "bnm"), ("street", "st"), ("locality", "loc"),
                 ("city", "city"), ("district", "dst"), ("state_name", "stcd"), ("pincode", "pncd"), ("landmark", "landMark"))


def _gstin_address(entry: Any, kind: str) -> Optional[Dict[str, str]]:
    raw = (entry or {}).get("addr") or {}
    out = {name: text(raw.get(key)) for name, key in _ADDRESS_KEYS}
    out["landmark"] = out["landmark"] or text(raw.get("locality"))
    if not any(out.values()):
        return None
    out["type"] = kind
    return out


def gstin_details(gstin: str, res: Dict[str, Any]) -> Dict[str, Any]:
    """The public registration data of one GSTIN, from the GST search response. Nothing is guessed: a missing value stays empty."""
    raw_status = text(res.get("sts"))
    addresses = [_gstin_address(res.get("pradr"), "principal")] + [_gstin_address(a, "additional") for a in (res.get("adadr") or [])]
    return {
        "gstin": gstin, "status": GSTIN_STATUSES.get(raw_status.lower(), "Unknown"), "status_raw": raw_status,
        "legal_name": text(res.get("lgnm")), "trade_name": text(res.get("tradeNam")), "taxpayer_type": text(res.get("dty")),
        "constitution": text(res.get("ctb")), "registration_date": text(res.get("rgdt")), "cancellation_date": text(res.get("cxdt")),
        "state_code": gstin[:2], "addresses": [a for a in addresses if a],
    }


# ------------------------------------------------------------------------------------------------ helpers
def safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", s or "")


def make_reference(integration_id: str, sap_database: str, object_type: str, doc_entry: int, operation: str, suffix: str = "") -> str:
    ref = f"{safe(integration_id)}_{safe(sap_database)}_{safe(object_type)}_{doc_entry}_{operation}"
    return f"{ref}_{suffix}" if suffix else ref


def _fmt_dt(d: Optional[datetime]) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else ""


def _parse_dt(s: Any) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %I:%M %p", "%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s)[:26].strip(), fmt)
        except ValueError:
            continue
    return None


class GstApi:
    LOCK_SECONDS = 180

    def __init__(self, profiles: ProfileRepo, site: SiteInfo, store: TxnStore, gsp_factory: Callable[[str], Gsp],
                 now: Callable[[], datetime] = datetime.now, log: Callable[..., None] = lambda *a, **k: None):
        self.profiles, self.site, self.store, self.gsp_factory, self.now, self.log = profiles, site, store, gsp_factory, now, log

    # ============================================================================================ envelope
    @staticmethod
    def _env(rid: str, ok: bool, status: str, reference: str = "", data: Optional[dict] = None, findings: Optional[V.Findings] = None,
             message: str = "", warnings: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "ok": ok, "status": status, "request_id": rid, "reference": reference, "message": message, "data": data or {},
            "errors": findings.errors if findings else [], "warnings": (findings.warnings if findings else []) + [{"severity": "warning", "code": "NOTE", "field": "", "message": w} for w in (warnings or [])],
        }

    # ============================================================================================ security
    def _authorise(self, req: Dict[str, Any], operation: str, invoice: Optional[Invoice] = None) -> Profile:
        iid = text(req.get("integration_id"))
        profile = self.profiles.get(iid) if iid else None
        if profile is None or not profile.enabled:
            raise AccessDenied("Unknown or disabled integration profile.")
        user = self.site.user()
        if not profile.api_user or profile.api_user != user:
            raise AccessDenied("This API user is not allowed to use the integration profile.")
        flag = {C.OP_EINVOICE: profile.allow_einvoice, C.OP_EINVOICE_CANCEL: profile.allow_einvoice and profile.allow_cancel,
                C.OP_EWAYBILL: profile.allow_ewaybill, C.OP_EWAYBILL_CANCEL: profile.allow_ewaybill and profile.allow_cancel,
                C.OP_VEHICLE: profile.allow_ewaybill and profile.allow_updates, C.OP_EXTEND: profile.allow_ewaybill and profile.allow_updates, C.OP_TRANSPORTER: profile.allow_ewaybill and profile.allow_updates}.get(operation, True)
        if not flag:
            raise AccessDenied(f"The operation {operation} is not permitted for this integration profile.")
        db = text(req.get("sap_database") or (invoice.sap_database if invoice else ""))
        if not db or db.lower() != profile.sap_database.lower():
            raise AccessDenied("The SAP database does not match the integration profile.")
        if invoice is not None and invoice.seller.gstin and invoice.seller.gstin.upper() != profile.seller_gstin.upper():
            raise AccessDenied("The seller GSTIN in the request does not match the integration profile.")
        env = text(req.get("environment") or (invoice.environment if invoice else ""))
        if env and env.lower() != profile.environment.lower():
            raise AccessDenied(f"Environment mismatch: the request says {env}, the profile is {profile.environment}.")
        site_sandbox = self.site.sandbox_mode()
        if profile.environment.lower() == "sandbox" and not site_sandbox:
            raise AccessDenied("The profile is Sandbox but the ERPNext GST Settings are in PRODUCTION mode. Refused: a real IRN could be created.")
        if profile.environment.lower() == "production" and site_sandbox:
            raise AccessDenied("The profile is Production but the ERPNext GST Settings are in sandbox mode.")
        cg = self.site.company_gstin(profile.company)
        if cg and cg.upper() != profile.seller_gstin.upper():
            raise AccessDenied("The seller GSTIN of the profile differs from the GSTIN of the ERPNext company.")
        if not self.site.has_credentials(profile.seller_gstin):
            raise AccessDenied("No GSP credentials are configured in ERPNext GST Settings for the seller GSTIN.")
        return profile

    def _identity(self, req: Dict[str, Any], need_doc_number: bool = False) -> Dict[str, Any]:
        try:
            doc_entry = int(req.get("doc_entry"))
        except (TypeError, ValueError):
            raise ContractError("doc_entry must be an integer", "doc_entry")
        return {"integration_id": text(req.get("integration_id")), "sap_database": text(req.get("sap_database")), "object_type": text(req.get("object_type") or "13"), "doc_entry": doc_entry}

    def _rid(self, req: Dict[str, Any]) -> str:
        r = text(req.get("request_id"))
        return r if re.fullmatch(r"[A-Za-z0-9_.:-]{8,64}", r or "") else uuid.uuid4().hex

    # ============================================================================================ GSTIN search (nothing is stored)
    def verify_gstin(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """
        ok=True means the question was answered (status Active / Inactive / Cancelled / Suspended / Invalid / Unknown).
        ok=False with "Verification Failed" means the GST service could not be asked: it says nothing about the GSTIN.
        """
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_GSTIN)
        gstin = text(req.get("gstin")).upper()
        # loose format on purpose: UIN / TDS / non-resident numbers differ from the ordinary taxpayer pattern
        if not (V.GSTIN_LOOSE_RX.match(gstin) and V.gstin_checksum_ok(gstin)):
            return self._env(rid, True, "Invalid", data={"gstin": gstin, "status": "Invalid", "addresses": []},
                             message="The GSTIN has a wrong format or check character. The GST service was not called.")
        try:
            res = self.gsp_factory(profile.seller_gstin).lookup_gstin(gstin)
            if res is None or (text(res.get("gstin")) and text(res.get("gstin")).upper() != gstin):
                raise GspRejected("The GST service returned no result for this GSTIN.")
        except GspUncertain:
            self.log("gstin_check_failed", request_id=rid, reason="timeout")
            return self._env(rid, False, "Verification Failed", message="Unable to verify: the GST service did not answer. Try again later.")
        except GspRejected as e:
            self.log("gstin_check_failed", request_id=rid, reason="service")
            return self._env(rid, False, "Verification Failed", message=("Unable to verify: " + str(e))[:300])
        data = gstin_details(gstin, res)
        self.log("gstin_checked", request_id=rid, result=data["status"])
        message = {"Invalid": "The GST service reports that this GSTIN does not exist.", "Unknown": "The GST service returned no registration status."}.get(data["status"], "GSTIN registration is " + data["status"] + ".")
        return self._env(rid, True, data["status"], data=data, message=message)

    # ============================================================================================ validation (no persistence)
    def _parse_and_validate(self, req: Dict[str, Any], profile_for_flags: Optional[Profile], sandbox: bool, for_einvoice: bool = True):
        inv = parse_invoice(req.get("invoice") if "invoice" in req else req)
        return inv

    def validate_invoice(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        body = req.get("invoice") if "invoice" in req else req
        try:
            inv = parse_invoice(body)
        except ContractError as e:
            f = V.Findings(); f.error("CONTRACT", e.field, f"{e.field}: {e}")
            return self._env(rid, False, C.ST_VALIDATION_FAILED, findings=f, message="The request is not a valid invoice contract.")
        inv.integration_id = inv.integration_id or text(req.get("integration_id"))
        profile = self._authorise({**req, "integration_id": inv.integration_id, "sap_database": inv.sap_database}, "VALIDATE", inv)
        sandbox = profile.environment.lower() == "sandbox"
        # the add-on marks an E-Way Bill validation, which has its own recipient rules (an unregistered consignee is allowed)
        f = V.validate_invoice(inv, sandbox=sandbox, min_hsn_digits=profile.min_hsn_digits, tolerance=profile.rounding_tolerance, for_einvoice=text(body.get("purpose")) != "EWAYBILL")
        subs = N.apply_sandbox_identities(inv) if sandbox else []      # reported, after validation: sandbox values never mask problems in the real data
        if not inv.transport.vehicle_number and not inv.transport.transporter_id and inv.transport.distance == 0:
            pass  # transport is optional for e-Invoice
        else:
            tf = V.validate_transport(inv.transport)
            f.items.extend(tf.items)
        status = C.ST_VALIDATING if f.ok else C.ST_VALIDATION_FAILED
        return self._env(rid, f.ok, "Valid" if f.ok else status, data={"sandbox_substitutions": subs, "environment": profile.environment}, findings=f,
                         message="Validation passed. Nothing was sent to the IRP." if f.ok else "Validation failed. Nothing was sent to the IRP.")

    # ============================================================================================ e-Invoice generation
    def generate_e_invoice(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        body = req.get("invoice") if "invoice" in req else req
        try:
            inv = parse_invoice(body)
        except ContractError as e:
            f = V.Findings(); f.error("CONTRACT", e.field, f"{e.field}: {e}")
            return self._env(rid, False, C.ST_VALIDATION_FAILED, findings=f, message="The request is not a valid invoice contract.")
        inv.integration_id = inv.integration_id or text(req.get("integration_id"))
        profile = self._authorise({**req, "integration_id": inv.integration_id, "sap_database": inv.sap_database}, C.OP_EINVOICE, inv)
        sandbox = profile.environment.lower() == "sandbox"
        ref = make_reference(inv.integration_id, inv.sap_database, inv.object_type, inv.doc_entry, C.OP_EINVOICE)

        f = V.validate_invoice(inv, sandbox=sandbox, min_hsn_digits=profile.min_hsn_digits, tolerance=profile.rounding_tolerance)
        if not f.ok:
            self.log("validation_failed", request_id=rid, reference=ref, errors=len(f.errors))
            return self._env(rid, False, C.ST_VALIDATION_FAILED, ref, findings=f, message="Validation failed. Nothing was sent to the IRP.")
        subs = N.apply_sandbox_identities(inv) if sandbox else []
        payload = N.build_einvoice(inv)
        rhash = N.request_hash(payload)

        existing = self.store.get(ref)
        if existing:
            decision = self._decide_existing(existing, rhash, rid, ref)
            if decision is not None:
                return decision
            # Failed / Unknown(+reconcile) paths continue below with the existing record
        else:
            fields = self._base_fields(inv, ref, C.OP_EINVOICE, rhash, rid)
            if not self.store.create(ref, fields):                    # lost the race for the unique reference
                other = self.store.get(ref) or {}
                return self._env(rid, False, other.get("processing_status", C.ST_PROCESSING), ref, message="Another request for this document is already being processed.")
        # the record exists and we own the processing lock
        self.store.update(ref, processing_status=C.ST_PROCESSING, request_hash=rhash, request_id=rid, locked_until=self.now() + timedelta(seconds=self.LOCK_SECONDS), error_code="", error_message="")
        gsp = self.gsp_factory(profile.seller_gstin)
        try:
            res = gsp.generate_irn(payload)
        except GspUncertain as e:
            self.store.update(ref, processing_status=C.ST_UNKNOWN, error_code="TIMEOUT", error_message=str(e)[:140], locked_until=None)
            self.log("uncertain", request_id=rid, reference=ref)
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="The IRP answer was not received. The outcome is UNKNOWN; use Refresh status (reconciliation). Nothing was retried.", warnings=subs)
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=str(e), data={"error_code": e.code}, warnings=subs)
        return self._finish_einvoice(ref, rid, res, subs)

    def _finish_einvoice(self, ref: str, rid: str, res: Dict[str, Any], subs: List[str]) -> Dict[str, Any]:
        irn = res.get("irn")
        if not irn:                                                       # never report success without a real IRN
            self.store.update(ref, processing_status=C.ST_FAILED, error_code="NO_IRN", error_message="The IRP response did not contain an IRN.", locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message="The IRP response did not contain an IRN.", warnings=subs)
        ack = _parse_dt(res.get("ack_date"))
        self.store.update(ref, processing_status=C.ST_GENERATED, irn=irn, acknowledgement_number=str(res.get("ack_no") or ""), acknowledgement_date=ack, locked_until=None,
                          error_code="", error_message="", ewaybill_number=str(res.get("ewb_no") or ""))
        data = {"irn": irn, "ack_no": str(res.get("ack_no") or ""), "ack_date": res.get("ack_date"), "signed_invoice": res.get("signed_invoice"),
                "signed_qr": res.get("signed_qr"), "irp_status": res.get("status"), "ewb_no": res.get("ewb_no"), "ewb_date": res.get("ewb_date"), "ewb_valid_till": res.get("ewb_valid")}
        return self._env(rid, True, C.ST_GENERATED, ref, data=data, message="e-Invoice generated by the IRP.", warnings=subs)

    def _base_fields(self, inv: Invoice, ref: str, op: str, rhash: str, rid: str) -> Dict[str, Any]:
        return {
            "transaction_reference": ref, "integration_id": inv.integration_id, "sap_database": inv.sap_database, "sap_object_type": inv.object_type, "sap_doc_entry": inv.doc_entry,
            "sap_doc_num": inv.doc_num, "operation_type": op, "request_hash": rhash, "request_id": rid, "processing_status": C.ST_PROCESSING,
            "locked_until": self.now() + timedelta(seconds=self.LOCK_SECONDS), "document_number": inv.document_number, "document_date": inv.document_date,
            "seller_gstin": inv.seller.gstin,
        }

    def _decide_existing(self, rec: Dict[str, Any], rhash: str, rid: str, ref: str) -> Optional[Dict[str, Any]]:
        """Returns a final response, or None when this request may (re)process the record."""
        st = rec.get("processing_status")
        if st == C.ST_GENERATED:
            return self._env(rid, True, C.ST_GENERATED, ref, data=self._stored_data(rec), message="Already generated. The existing result is returned; nothing was sent to the IRP.")
        if st == C.ST_CANCELLED:
            return self._env(rid, False, C.ST_CANCELLED, ref, message="This e-Invoice was cancelled. The IRP does not allow the document number to be reused; issue a new document number.")
        if st in (C.ST_PROCESSING, C.ST_VALIDATING, C.ST_PENDING):
            lu = rec.get("locked_until")
            if lu and lu > self.now():
                return self._env(rid, False, C.ST_PROCESSING, ref, message="This document is currently being processed. Retry later; nothing was sent.")
            self.store.update(ref, processing_status=C.ST_UNKNOWN, error_code="STALE_LOCK", error_message="Processing did not finish; outcome unknown.")
            rec["processing_status"] = C.ST_UNKNOWN
            st = C.ST_UNKNOWN
        if st == C.ST_UNKNOWN and rec.get("request_hash") and rec["request_hash"] != rhash:
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="A previous attempt for this document has an unknown outcome and the invoice data has since changed. Reconcile it with the identical data, or contact support. Nothing was sent.")
        return None                                                        # Failed (definitive) or Unknown with identical payload: may proceed

    @staticmethod
    def _stored_data(rec: Dict[str, Any]) -> Dict[str, Any]:
        ad = rec.get("acknowledgement_date")
        return {"irn": rec.get("irn"), "ack_no": rec.get("acknowledgement_number"), "ack_date": ad.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ad, datetime) else ad,
                "ewb_no": rec.get("ewaybill_number"), "signed_invoice": None, "signed_qr": None}

    # ============================================================================================ e-Invoice status / reconciliation
    def get_e_invoice_status(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_EINVOICE)
        ident = self._identity(req)
        ref = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EINVOICE)
        rec = self.store.get(ref)
        if not rec:
            return self._env(rid, True, "Not generated", ref, message="No e-Invoice transaction exists for this document.")
        st = rec.get("processing_status")
        if st == C.ST_GENERATED and rec.get("irn") and req.get("refresh", True):
            try:                                                           # fresh signed data / active-or-cancelled state from the IRP (read only)
                det = self.gsp_factory(profile.seller_gstin).get_irn(rec["irn"])
                data = self._stored_data(rec); data.update({"signed_invoice": det.get("signed_invoice"), "signed_qr": det.get("signed_qr"), "irp_status": det.get("status")})
                if str(det.get("status")).upper() == "CNL":
                    self.store.update(ref, processing_status=C.ST_CANCELLED)
                    return self._env(rid, True, C.ST_CANCELLED, ref, data=data, message="The IRP reports this IRN as cancelled.")
                return self._env(rid, True, C.ST_GENERATED, ref, data=data)
            except (GspUncertain, GspRejected) as e:
                return self._env(rid, True, C.ST_GENERATED, ref, data=self._stored_data(rec), warnings=[f"IRP details could not be refreshed now ({e}); stored values returned."])
        if st in (C.ST_UNKNOWN, C.ST_PROCESSING) and isinstance(req.get("invoice"), dict):
            return self._reconcile_einvoice(rec, ref, rid, profile, req)
        return self._env(rid, st == C.ST_GENERATED, st, ref, data=self._stored_data(rec) if st == C.ST_GENERATED else {"error_code": rec.get("error_code"), "error_message": rec.get("error_message")},
                         message="Reconciliation needs the invoice data: send the same invoice again with the status request." if st == C.ST_UNKNOWN else "")

    def _reconcile_einvoice(self, rec: Dict[str, Any], ref: str, rid: str, profile: Profile, req: Dict[str, Any]) -> Dict[str, Any]:
        """
        Reconcile an uncertain generation. Safe because (1) only identical data (same request hash) is accepted and (2) the IRP itself guarantees one IRN per
        document number: re-sending the identical document returns the existing IRN (Duplicate IRN) instead of creating a second one.
        """
        inv = parse_invoice(req["invoice"])
        inv.integration_id = inv.integration_id or text(req.get("integration_id"))
        sandbox = profile.environment.lower() == "sandbox"
        if sandbox:
            N.apply_sandbox_identities(inv)
        payload = N.build_einvoice(inv)
        if N.request_hash(payload) != rec.get("request_hash"):
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="The invoice data differs from the original request; reconciliation refused.")
        self.store.update(ref, processing_status=C.ST_PROCESSING, locked_until=self.now() + timedelta(seconds=self.LOCK_SECONDS), request_id=rid)
        try:
            res = self.gsp_factory(profile.seller_gstin).generate_irn(payload)
        except GspUncertain as e:
            self.store.update(ref, processing_status=C.ST_UNKNOWN, error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="Still unknown: the IRP did not answer. Try again later.")
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=f"Reconciled: the IRP has no e-Invoice for this document and rejected it: {e}")
        return self._finish_einvoice(ref, rid, res, [])

    # ============================================================================================ e-Invoice cancellation
    def cancel_e_invoice(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_EINVOICE_CANCEL)
        ident = self._identity(req)
        reason, remark = text(req.get("reason")), text(req.get("remark"), 100)
        src = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EINVOICE)
        ref = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EINVOICE_CANCEL)
        rec = self.store.get(src)
        f = V.Findings()
        if reason not in C.EINVOICE_CANCEL_REASONS:
            f.error("REASON", "reason", f"Cancellation reason must be one of {sorted(C.EINVOICE_CANCEL_REASONS)}")
        if not rec or rec.get("processing_status") != C.ST_GENERATED or not rec.get("irn"):
            st = rec.get("processing_status") if rec else "none"
            if st == C.ST_CANCELLED:
                return self._env(rid, True, C.ST_CANCELLED, ref, message="Already cancelled.")
            f.error("NO_IRN", "irn", f"There is no active IRN for this document (status: {st})")
        elif rec.get("acknowledgement_date") and rec["acknowledgement_date"] + timedelta(days=1) < self.now():
            f.error("CANCEL_WINDOW", "irn", "An e-Invoice can be cancelled only within 24 hours of generation")
        if not f.ok:
            return self._env(rid, False, C.ST_VALIDATION_FAILED, ref, findings=f, message="Cancellation not possible. Nothing was sent to the IRP.")
        prev = self.store.get(ref)
        if prev and prev.get("processing_status") == C.ST_CANCELLED:
            return self._env(rid, True, C.ST_CANCELLED, ref, message="Already cancelled.")
        if prev and prev.get("processing_status") in (C.ST_PROCESSING,) and prev.get("locked_until") and prev["locked_until"] > self.now():
            return self._env(rid, False, C.ST_PROCESSING, ref, message="A cancellation is already in progress.")
        fields = {"transaction_reference": ref, "integration_id": ident["integration_id"], "sap_database": ident["sap_database"], "sap_object_type": ident["object_type"], "sap_doc_entry": ident["doc_entry"],
                  "operation_type": C.OP_EINVOICE_CANCEL, "request_id": rid, "processing_status": C.ST_PROCESSING, "locked_until": self.now() + timedelta(seconds=self.LOCK_SECONDS), "irn": rec["irn"]}
        if prev is None:
            if not self.store.create(ref, fields):
                return self._env(rid, False, C.ST_PROCESSING, ref, message="A cancellation is already in progress.")
        else:
            self.store.update(ref, processing_status=C.ST_PROCESSING, locked_until=self.now() + timedelta(seconds=self.LOCK_SECONDS), request_id=rid)
        gsp = self.gsp_factory(profile.seller_gstin)
        try:
            gsp.cancel_irn(N.build_cancel_irn(rec["irn"], reason, remark))
        except GspUncertain:
            return self._reconcile_irn_cancel(gsp, src, ref, rec["irn"], rid)
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=str(e))
        return self._confirm_irn_cancel(gsp, src, ref, rec["irn"], rid, trust_call=True)

    def _confirm_irn_cancel(self, gsp: Gsp, src: str, ref: str, irn: str, rid: str, trust_call: bool) -> Dict[str, Any]:
        """Cancellation is only reported after the IRP itself says the IRN is cancelled (status CNL)."""
        try:
            det = gsp.get_irn(irn)
            cancelled = str(det.get("status")).upper() == "CNL"
        except (GspUncertain, GspRejected):
            cancelled = False
        if cancelled:
            self.store.update(ref, processing_status=C.ST_CANCELLED, locked_until=None, error_code="", error_message="")
            self.store.update(src, processing_status=C.ST_CANCELLED)
            return self._env(rid, True, C.ST_CANCELLED, ref, message="e-Invoice cancelled (confirmed by the IRP).")
        self.store.update(ref, processing_status=C.ST_UNKNOWN, locked_until=None, error_message="Cancellation sent but not yet confirmed.")
        return self._env(rid, False, C.ST_UNKNOWN, ref, message="The cancellation request was sent but the IRP has not confirmed it. Use Refresh status.")

    def _reconcile_irn_cancel(self, gsp: Gsp, src: str, ref: str, irn: str, rid: str) -> Dict[str, Any]:
        self.store.update(ref, processing_status=C.ST_UNKNOWN, error_code="TIMEOUT", locked_until=None)
        try:
            det = gsp.get_irn(irn)
        except (GspUncertain, GspRejected):
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="No answer from the IRP. The cancellation outcome is unknown; use Refresh status.")
        if str(det.get("status")).upper() == "CNL":
            self.store.update(ref, processing_status=C.ST_CANCELLED)
            self.store.update(src, processing_status=C.ST_CANCELLED)
            return self._env(rid, True, C.ST_CANCELLED, ref, message="Reconciled: the IRP shows the IRN as cancelled.")
        self.store.update(ref, processing_status=C.ST_FAILED, error_message="The IRN is still active at the IRP.")
        return self._env(rid, False, C.ST_FAILED, ref, message="Reconciled: the IRN is still active; the cancellation did not happen.")

    # ============================================================================================ e-Way Bill
    def generate_e_waybill(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        body = req.get("invoice") if "invoice" in req else req
        try:
            inv = parse_invoice(body)
        except ContractError as e:
            f = V.Findings(); f.error("CONTRACT", e.field, f"{e.field}: {e}")
            return self._env(rid, False, C.ST_VALIDATION_FAILED, findings=f, message="The request is not a valid invoice contract.")
        inv.integration_id = inv.integration_id or text(req.get("integration_id"))
        profile = self._authorise({**req, "integration_id": inv.integration_id, "sap_database": inv.sap_database}, C.OP_EWAYBILL, inv)
        sandbox = profile.environment.lower() == "sandbox"
        ref = make_reference(inv.integration_id, inv.sap_database, inv.object_type, inv.doc_entry, C.OP_EWAYBILL)
        einv_ref = make_reference(inv.integration_id, inv.sap_database, inv.object_type, inv.doc_entry, C.OP_EINVOICE)

        f = V.validate_invoice(inv, sandbox=sandbox, min_hsn_digits=profile.min_hsn_digits, tolerance=profile.rounding_tolerance, for_einvoice=False)
        f.items.extend(V.validate_transport(inv.transport).items)
        if inv.supply_type in C.EXPORT_SUPPLY_TYPES:
            st = inv.ship_to
            if not st or not (st.pincode or "").isdigit() or len(st.pincode) != 6 or not (st.state_code or "").isdigit() or st.state_code in ("96", "97", "99") or st.pincode == "999999":
                f.error("EXPORT_SHIP_TO", "ship_to", "For an export the e-Way Bill covers the movement inside India: enter the delivery point (port / airport / warehouse in India: place, 6-digit PIN code and state) as the Ship To in the E-Way Bill details of the SAP invoice.")
        if not f.ok:
            return self._env(rid, False, C.ST_VALIDATION_FAILED, ref, findings=f, message="Validation failed. Nothing was sent to the e-Way Bill system.")
        subs = N.apply_sandbox_identities(inv) if sandbox else []
        einv = self.store.get(einv_ref)
        with_irn = bool(einv and einv.get("processing_status") == C.ST_GENERATED and einv.get("irn") and inv.document_type == "INV" and inv.supply_type not in C.EXPORT_SUPPLY_TYPES
                        and all(not it.hsn_code.startswith(C.SERVICE_HSN_PREFIX) for it in inv.items))
        payload = N.build_ewb_with_irn(einv["irn"], inv.transport) if with_irn else N.build_ewb_standalone(inv)
        rhash = N.request_hash(payload)

        existing = self.store.get(ref)
        if existing:
            st = existing.get("processing_status")
            if st == C.ST_GENERATED:
                return self._env(rid, True, C.ST_GENERATED, ref, data=self._ewb_data(existing), message="Already generated. The existing result is returned; nothing was sent.")
            if st == C.ST_CANCELLED:
                return self._env(rid, False, C.ST_CANCELLED, ref, message="The e-Way Bill was cancelled. Generate a new one with a different document/transport reference as per GST rules.")
            if st in (C.ST_PROCESSING, C.ST_VALIDATING, C.ST_PENDING) and existing.get("locked_until") and existing["locked_until"] > self.now():
                return self._env(rid, False, C.ST_PROCESSING, ref, message="This document is currently being processed. Nothing was sent.")
            if st == C.ST_UNKNOWN or st in (C.ST_PROCESSING, C.ST_VALIDATING, C.ST_PENDING):
                return self._reconcile_ewb(existing, ref, rid, profile, inv)      # never blind retry: look the e-Way Bill up first
        else:
            fields = self._base_fields(inv, ref, C.OP_EWAYBILL, rhash, rid)
            if not self.store.create(ref, fields):
                return self._env(rid, False, C.ST_PROCESSING, ref, message="Another request for this document is already being processed.")
        self.store.update(ref, processing_status=C.ST_PROCESSING, request_hash=rhash, request_id=rid, locked_until=self.now() + timedelta(seconds=self.LOCK_SECONDS), error_code="", error_message="")
        try:
            res = self.gsp_factory(profile.seller_gstin).generate_ewb(payload, with_irn)
        except GspUncertain as e:
            self.store.update(ref, processing_status=C.ST_UNKNOWN, error_code="TIMEOUT", error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="The e-Way Bill system did not answer. The outcome is UNKNOWN; use Refresh status. Nothing was retried.", warnings=subs)
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=str(e), data={"error_code": e.code}, warnings=subs)
        return self._finish_ewb(ref, rid, res, subs)

    def _finish_ewb(self, ref: str, rid: str, res: Dict[str, Any], subs: List[str]) -> Dict[str, Any]:
        no = res.get("ewb_no")
        if not no:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code="NO_EWB", error_message="The response did not contain an e-Way Bill number.", locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message="The response did not contain an e-Way Bill number.", warnings=subs)
        self.store.update(ref, processing_status=C.ST_GENERATED, ewaybill_number=str(no), ewaybill_date=_parse_dt(res.get("ewb_date")), ewaybill_validity=_parse_dt(res.get("ewb_valid")), locked_until=None, error_code="", error_message="")
        pending = not res.get("ewb_valid")
        return self._env(rid, True, C.ST_GENERATED, ref, data={"ewb_no": str(no), "ewb_date": _fmt_dt(_parse_dt(res.get("ewb_date"))) or res.get("ewb_date"), "ewb_valid_till": _fmt_dt(_parse_dt(res.get("ewb_valid"))) or res.get("ewb_valid"), "part_b_pending": pending},
                         message="Part-A only: Part-B (vehicle / transport document) is still pending. Goods must not move yet." if pending else "e-Way Bill generated.", warnings=subs)

    @staticmethod
    def _ewb_data(rec: Dict[str, Any]) -> Dict[str, Any]:
        f = lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v
        return {"ewb_no": rec.get("ewaybill_number"), "ewb_date": f(rec.get("ewaybill_date")), "ewb_valid_till": f(rec.get("ewaybill_validity")),
                "part_b_pending": bool(rec.get("ewaybill_number")) and not rec.get("ewaybill_validity")}

    def _reconcile_ewb(self, rec: Dict[str, Any], ref: str, rid: str, profile: Profile, inv: Invoice) -> Dict[str, Any]:
        """Look the e-Way Bill up (by document number/date at the portal) before anything else; only 'not found' makes a new attempt possible."""
        try:
            found = self.gsp_factory(profile.seller_gstin).find_ewb(inv.document_number, inv.document_date, inv.seller.gstin)
        except (GspUncertain, GspRejected) as e:
            self.store.update(ref, processing_status=C.ST_UNKNOWN, locked_until=None)
            return self._env(rid, False, C.ST_UNKNOWN, ref, message=f"Reconciliation could not reach the e-Way Bill system ({e}). Still unknown.")
        if found and found.get("ewb_no"):
            return self._finish_ewb(ref, rid, found, ["Reconciled: an e-Way Bill already existed at the portal."])
        self.store.update(ref, processing_status=C.ST_FAILED, error_code="NOT_FOUND", error_message="Reconciled: no e-Way Bill exists at the portal; it can be generated again.", locked_until=None)
        return self._env(rid, False, C.ST_FAILED, ref, message="Reconciled: no e-Way Bill exists at the portal. You can generate it again.")

    def get_e_waybill_status(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_EWAYBILL)
        ident = self._identity(req)
        ref = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EWAYBILL)
        rec = self.store.get(ref)
        if not rec:
            return self._env(rid, True, "Not generated", ref, message="No e-Way Bill transaction exists for this document.")
        st = rec.get("processing_status")
        if st in (C.ST_UNKNOWN, C.ST_PROCESSING) and isinstance(req.get("invoice"), dict):
            inv = parse_invoice(req["invoice"]); inv.integration_id = inv.integration_id or text(req.get("integration_id"))
            return self._reconcile_ewb(rec, ref, rid, profile, inv)
        if st == C.ST_GENERATED and rec.get("ewaybill_number") and req.get("refresh", True):
            try:
                det = self.gsp_factory(profile.seller_gstin).get_ewb(rec["ewaybill_number"])
                data = self._ewb_data(rec); data.update({"vehicle_no": det.get("vehicle_no"), "portal_status": det.get("status")})
                if str(det.get("status")).upper() in ("CNL", "CANCELLED"):
                    self.store.update(ref, processing_status=C.ST_CANCELLED)
                    return self._env(rid, True, C.ST_CANCELLED, ref, data=data, message="The e-Way Bill system reports this e-Way Bill as cancelled.")
                if det.get("ewb_valid"):
                    self.store.update(ref, ewaybill_validity=_parse_dt(det["ewb_valid"]))
                    data["ewb_valid_till"] = det["ewb_valid"]
                data["part_b_pending"] = not det.get("ewb_valid")      # the portal itself is the authority
                return self._env(rid, True, C.ST_GENERATED, ref, data=data)
            except (GspUncertain, GspRejected) as e:
                return self._env(rid, True, C.ST_GENERATED, ref, data=self._ewb_data(rec), warnings=[f"Portal details could not be refreshed now ({e}); stored values returned."])
        return self._env(rid, st == C.ST_GENERATED, st, ref, data=self._ewb_data(rec) if st == C.ST_GENERATED else {"error_code": rec.get("error_code"), "error_message": rec.get("error_message")},
                         message="Send the invoice with the status request to reconcile." if st == C.ST_UNKNOWN else "")

    def cancel_e_waybill(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_EWAYBILL_CANCEL)
        ident = self._identity(req)
        reason, remark = text(req.get("reason")), text(req.get("remark"), 100)
        src = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EWAYBILL)
        ref = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EWAYBILL_CANCEL)
        rec = self.store.get(src)
        f = V.Findings()
        if reason not in C.EWAYBILL_CANCEL_REASONS:
            f.error("REASON", "reason", f"Cancellation reason must be one of {sorted(C.EWAYBILL_CANCEL_REASONS)}")
        if rec and rec.get("processing_status") == C.ST_CANCELLED:
            return self._env(rid, True, C.ST_CANCELLED, ref, message="Already cancelled.")
        if not rec or rec.get("processing_status") != C.ST_GENERATED or not rec.get("ewaybill_number"):
            f.error("NO_EWB", "ewaybill", "There is no active e-Way Bill for this document")
        elif rec.get("ewaybill_date") and rec["ewaybill_date"] + timedelta(days=1) < self.now():
            f.error("CANCEL_WINDOW", "ewaybill", "An e-Way Bill can be cancelled only within 24 hours of generation")
        if not f.ok:
            return self._env(rid, False, C.ST_VALIDATION_FAILED, ref, findings=f, message="Cancellation not possible. Nothing was sent.")
        prev = self.store.get(ref)
        if prev and prev.get("processing_status") == C.ST_PROCESSING and prev.get("locked_until") and prev["locked_until"] > self.now():
            return self._env(rid, False, C.ST_PROCESSING, ref, message="A cancellation is already in progress.")
        fields = {"transaction_reference": ref, "integration_id": ident["integration_id"], "sap_database": ident["sap_database"], "sap_object_type": ident["object_type"], "sap_doc_entry": ident["doc_entry"],
                  "operation_type": C.OP_EWAYBILL_CANCEL, "request_id": rid, "processing_status": C.ST_PROCESSING, "locked_until": self.now() + timedelta(seconds=self.LOCK_SECONDS), "ewaybill_number": rec["ewaybill_number"]}
        if prev is None:
            if not self.store.create(ref, fields):
                return self._env(rid, False, C.ST_PROCESSING, ref, message="A cancellation is already in progress.")
        else:
            self.store.update(ref, processing_status=C.ST_PROCESSING, locked_until=self.now() + timedelta(seconds=self.LOCK_SECONDS), request_id=rid)
        gsp = self.gsp_factory(profile.seller_gstin)
        uncertain = False
        try:
            gsp.cancel_ewb(N.build_cancel_ewb(rec["ewaybill_number"], reason, remark))
        except GspUncertain:
            uncertain = True
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=str(e))
        try:                                                              # confirm with the portal before reporting success
            det = gsp.get_ewb(rec["ewaybill_number"])
            cancelled = str(det.get("status")).upper() in ("CNL", "CANCELLED")
        except (GspUncertain, GspRejected):
            cancelled = False
        if cancelled:
            self.store.update(ref, processing_status=C.ST_CANCELLED, locked_until=None)
            self.store.update(src, processing_status=C.ST_CANCELLED)
            return self._env(rid, True, C.ST_CANCELLED, ref, message="e-Way Bill cancelled (confirmed by the portal).")
        self.store.update(ref, processing_status=C.ST_UNKNOWN, locked_until=None, error_message="Cancellation not yet confirmed.")
        return self._env(rid, False, C.ST_UNKNOWN, ref, message="The cancellation was sent but not yet confirmed" if not uncertain else "No answer from the portal; the cancellation outcome is unknown. Use Refresh status.")

    # ============================================================================================ vehicle / transporter updates
    def update_vehicle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_VEHICLE)
        ident = self._identity(req)
        src = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EWAYBILL)
        rec = self.store.get(src)
        veh = text(req.get("vehicle_number")).replace(" ", "").upper()
        reason, remark, place, state = text(req.get("reason")), text(req.get("remark"), 50), text(req.get("place"), 50), text(req.get("state_code"))
        f = V.Findings()
        mode = text(req.get("transport_mode") or "Road")
        doc_date = self._doc_date(req, f)
        tr = Transport(vehicle_number=veh, transport_mode=mode, vehicle_type=text(req.get("vehicle_type") or "Regular"), doc_number=text(req.get("doc_number")), doc_date=doc_date)
        if mode not in C.TRANSPORT_MODES:
            f.error("MODE", "transport_mode", "Transport mode must be Road, Rail, Air or Ship")
        elif mode != "Road" and (not tr.doc_number or not tr.doc_date):
            f.error("TRANSPORT_DOC", "doc_number", f"{mode} transport requires the transport document number and date")
        if tr.vehicle_type not in C.VEHICLE_TYPES:
            f.error("VEHICLE_TYPE", "vehicle_type", "Vehicle type must be Regular or Over Dimensional Cargo (ODC)")
        if not rec or rec.get("processing_status") != C.ST_GENERATED:
            f.error("NO_EWB", "ewaybill", "There is no active e-Way Bill for this document")
        if (mode == "Road" or veh) and not V.VEHICLE_RX.match(veh):
            f.error("VEHICLE", "vehicle_number", f"Vehicle number '{veh}' is not a valid format")
        if reason not in C.VEHICLE_UPDATE_REASONS:
            f.error("REASON", "reason", f"Reason must be one of {sorted(C.VEHICLE_UPDATE_REASONS)}")
        if len(place) < 3:
            f.error("PLACE", "place", "Place of change is required")
        if state not in C.STATE_CODES:
            f.error("STATE", "state_code", "A valid GST state code is required")
        if rec and rec.get("ewaybill_validity") and rec["ewaybill_validity"] < self.now():
            f.error("EXPIRED", "ewaybill", "The e-Way Bill validity has expired")
        if not f.ok:
            return self._env(rid, False, C.ST_VALIDATION_FAILED, findings=f, message="Update not possible. Nothing was sent.")
        payload = N.build_update_vehicle(rec["ewaybill_number"], veh, tr, reason, remark, place, state)
        ref = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_VEHICLE, N.request_hash(payload)[:10])
        prev = self.store.get(ref)
        if prev and prev.get("processing_status") == C.ST_GENERATED:
            # going back to an earlier vehicle (A -> B -> A) is a real change: skip only when the portal still shows this vehicle
            try:
                shown = (self.gsp_factory(profile.seller_gstin).get_ewb(rec["ewaybill_number"]).get("vehicle_no") or "").replace(" ", "").upper()
            except (GspUncertain, GspRejected):
                shown = veh
            if shown == veh:
                return self._env(rid, True, C.ST_GENERATED, ref, data=self._ewb_data(rec), message="This identical vehicle update was already applied.")
        fields = {"transaction_reference": ref, "integration_id": ident["integration_id"], "sap_database": ident["sap_database"], "sap_object_type": ident["object_type"], "sap_doc_entry": ident["doc_entry"],
                  "operation_type": C.OP_VEHICLE, "request_hash": N.request_hash(payload), "request_id": rid, "processing_status": C.ST_PROCESSING, "locked_until": self.now() + timedelta(seconds=self.LOCK_SECONDS), "ewaybill_number": rec["ewaybill_number"]}
        if prev is None:
            if not self.store.create(ref, fields):
                return self._env(rid, False, C.ST_PROCESSING, ref, message="This update is already in progress.")
        elif prev.get("processing_status") == C.ST_PROCESSING and prev.get("locked_until") and prev["locked_until"] > self.now():
            return self._env(rid, False, C.ST_PROCESSING, ref, message="This update is already in progress.")
        if prev is not None:
            self.store.update(ref, processing_status=C.ST_PROCESSING, request_id=rid, locked_until=self.now() + timedelta(seconds=self.LOCK_SECONDS))
        gsp = self.gsp_factory(profile.seller_gstin)
        try:
            res = gsp.update_vehicle(payload)
        except GspUncertain:
            try:                                                           # reconcile by reading the e-Way Bill back
                det = gsp.get_ewb(rec["ewaybill_number"])
                if (det.get("vehicle_no") or "").replace(" ", "").upper() == veh:
                    self.store.update(ref, processing_status=C.ST_GENERATED, locked_until=None)
                    return self._env(rid, True, C.ST_GENERATED, ref, data={"vehicle_no": veh, "ewb_valid_till": det.get("ewb_valid")}, message="Reconciled: the portal already shows the new vehicle.")
            except (GspUncertain, GspRejected):
                pass
            self.store.update(ref, processing_status=C.ST_UNKNOWN, error_code="TIMEOUT", locked_until=None)
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="No confirmed answer. The vehicle update outcome is unknown; use Refresh status.")
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=str(e))
        self.store.update(ref, processing_status=C.ST_GENERATED, locked_until=None)
        if res.get("ewb_valid"):
            self.store.update(src, ewaybill_validity=_parse_dt(res["ewb_valid"]))      # Part-B completed: validity starts now
        return self._env(rid, True, C.ST_GENERATED, ref, data={"vehicle_no": veh, "ewb_valid_till": res.get("ewb_valid"), "updated": res.get("updated")}, message="Vehicle updated (confirmed by the portal).")

    @staticmethod
    def _doc_date(req: Dict[str, Any], f) -> Optional[date]:
        if not req.get("doc_date"):
            return None
        try:
            return parse_date(req["doc_date"], "doc_date")
        except ContractError:
            f.error("TRANSPORT_DOC_DATE", "doc_date", "Transport document date is not a valid date (YYYY-MM-DD)")
            return None

    def extend_e_waybill_validity(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_EXTEND)
        ident = self._identity(req)
        src = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EWAYBILL)
        rec = self.store.get(src)
        f = V.Findings()
        if not rec or rec.get("processing_status") != C.ST_GENERATED or not rec.get("ewaybill_number"):
            f.error("NO_EWB", "ewaybill", "There is no active e-Way Bill for this document")
            return self._env(rid, False, C.ST_VALIDATION_FAILED, findings=f, message="Extension not possible. Nothing was sent.")
        gsp = self.gsp_factory(profile.seller_gstin)
        try:                                                              # eligibility is checked against the portal, not the stored copy
            det = gsp.get_ewb(rec["ewaybill_number"])
        except (GspUncertain, GspRejected) as e:
            return self._env(rid, False, C.ST_FAILED, src, message=f"The current e-Way Bill status could not be read ({e}). Nothing was sent.")
        valid = _parse_dt(det.get("ewb_valid"))
        if str(det.get("status")).upper() in ("CNL", "CANCELLED"):
            f.error("CANCELLED", "ewaybill", "The e-Way Bill is cancelled")
        elif not valid:
            f.error("PART_B_PENDING", "ewaybill", "Part-B is not completed, so there is no validity to extend")
        else:
            window = timedelta(hours=C.EXTEND_WINDOW_HOURS)
            if not valid - window <= self.now() <= valid + window:
                f.error("EXTEND_WINDOW", "ewaybill", f"Validity can be extended only from {C.EXTEND_WINDOW_HOURS} hours before to {C.EXTEND_WINDOW_HOURS} hours after expiry ({_fmt_dt(valid)})")

        d = {k: text(req.get(k), n) for k, n in (("place", 50), ("state_code", 2), ("pincode", 6), ("remaining_distance", 4), ("reason", 40), ("remark", 50),
                                                 ("consignment_status", 20), ("transit_type", 20), ("address1", 100), ("address2", 100), ("address3", 100))}
        d["consignment_status"] = d["consignment_status"] or "In Movement"
        veh = text(req.get("vehicle_number")).replace(" ", "").upper() or str(det.get("vehicle_no") or "").replace(" ", "").upper()
        mode = text(req.get("transport_mode") or "Road")
        tr = Transport(vehicle_number=veh, transport_mode=mode, doc_number=text(req.get("doc_number")), doc_date=self._doc_date(req, f))
        if d["reason"] not in C.EXTEND_REASONS:
            f.error("REASON", "reason", f"Reason must be one of {sorted(C.EXTEND_REASONS)}")
        if len(d["place"]) < 3:
            f.error("PLACE", "place", "Current place is required")
        if d["state_code"] not in C.STATE_CODES:
            f.error("STATE", "state_code", "A valid GST state code is required")
        if not (d["pincode"].isdigit() and len(d["pincode"]) == 6):
            f.error("PINCODE", "pincode", "The 6-digit PIN code of the current place is required")
        if not (d["remaining_distance"].isdigit() and 0 < int(d["remaining_distance"]) <= 4000):
            f.error("DISTANCE", "remaining_distance", "Remaining distance (1-4000 km) is required")
        if mode not in C.TRANSPORT_MODES:
            f.error("MODE", "transport_mode", "Transport mode must be Road, Rail, Air or Ship")
        elif mode == "Road" and not V.VEHICLE_RX.match(veh):
            f.error("VEHICLE", "vehicle_number", "A valid vehicle number is required")
        elif mode != "Road" and (not tr.doc_number or not tr.doc_date):
            f.error("TRANSPORT_DOC", "doc_number", f"{mode} transport requires the transport document number and date")
        if d["consignment_status"] not in C.CONSIGNMENT_STATUS:
            f.error("CONSIGNMENT", "consignment_status", "Consignment status must be In Movement or In Transit")
        elif d["consignment_status"] == "In Transit" and (d["transit_type"] not in C.TRANSIT_TYPES or len(d["address1"]) < 3):
            f.error("TRANSIT", "transit_type", "For In Transit give the transit type (Road, In Warehouse or Others) and the address")
        if not f.ok:
            return self._env(rid, False, C.ST_VALIDATION_FAILED, findings=f, message="Extension not possible. Nothing was sent.")

        payload = N.build_extend_validity(rec["ewaybill_number"], tr, d)
        # keyed on the validity being extended: the same expiry is never extended twice, a later one can be
        ref = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EXTEND, valid.strftime("%y%m%d%H%M"))
        fields = {"transaction_reference": ref, "integration_id": ident["integration_id"], "sap_database": ident["sap_database"], "sap_object_type": ident["object_type"], "sap_doc_entry": ident["doc_entry"],
                  "operation_type": C.OP_EXTEND, "request_hash": N.request_hash(payload), "request_id": rid, "processing_status": C.ST_PROCESSING, "locked_until": self.now() + timedelta(seconds=self.LOCK_SECONDS), "ewaybill_number": rec["ewaybill_number"]}
        prev = self.store.get(ref)
        if prev is None:
            if not self.store.create(ref, fields):
                return self._env(rid, False, C.ST_PROCESSING, ref, message="This extension is already in progress.")
        elif prev.get("processing_status") == C.ST_PROCESSING and prev.get("locked_until") and prev["locked_until"] > self.now():
            return self._env(rid, False, C.ST_PROCESSING, ref, message="This extension is already in progress.")
        else:
            self.store.update(ref, processing_status=C.ST_PROCESSING, request_id=rid, locked_until=self.now() + timedelta(seconds=self.LOCK_SECONDS))
        try:
            res = gsp.extend_validity(payload)
        except GspUncertain:
            try:                                                           # reconcile: a later validity at the portal means it was applied
                new = _parse_dt(gsp.get_ewb(rec["ewaybill_number"]).get("ewb_valid"))
            except (GspUncertain, GspRejected):
                new = None
            if not new or new <= valid:
                self.store.update(ref, processing_status=C.ST_UNKNOWN, error_code="TIMEOUT", locked_until=None)
                return self._env(rid, False, C.ST_UNKNOWN, ref, message="No confirmed answer. The extension outcome is unknown; use Refresh status.")
            res = {"ewb_valid": _fmt_dt(new)}
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=str(e), data={"error_code": e.code})
        new = _parse_dt(res.get("ewb_valid"))
        self.store.update(ref, processing_status=C.ST_GENERATED, locked_until=None)
        if new:
            self.store.update(src, ewaybill_validity=new)
        return self._env(rid, True, C.ST_GENERATED, ref, data={"ewb_valid_till": _fmt_dt(new) or res.get("ewb_valid"), "vehicle_no": veh}, message="Validity extended (confirmed by the portal).")

    def update_transporter(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = self._rid(req)
        profile = self._authorise(req, C.OP_TRANSPORTER)
        ident = self._identity(req)
        src = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_EWAYBILL)
        rec = self.store.get(src)
        tid = text(req.get("transporter_id")).upper()
        f = V.Findings()
        if not rec or rec.get("processing_status") != C.ST_GENERATED:
            f.error("NO_EWB", "ewaybill", "There is no active e-Way Bill for this document")
        if not V.GSTIN_LOOSE_RX.match(tid):
            f.error("TRANSPORTER_ID", "transporter_id", "Transporter ID must be a valid 15-character GSTIN")
        if rec and rec.get("ewaybill_validity") and rec["ewaybill_validity"] < self.now():
            f.error("EXPIRED", "ewaybill", "The e-Way Bill validity has expired")
        if not f.ok:
            return self._env(rid, False, C.ST_VALIDATION_FAILED, findings=f, message="Update not possible. Nothing was sent.")
        payload = N.build_update_transporter(rec["ewaybill_number"], tid)
        ref = make_reference(ident["integration_id"], ident["sap_database"], ident["object_type"], ident["doc_entry"], C.OP_TRANSPORTER, N.request_hash(payload)[:10])
        prev = self.store.get(ref)
        if prev and prev.get("processing_status") == C.ST_GENERATED:
            return self._env(rid, True, C.ST_GENERATED, ref, message="This identical transporter update was already applied.")
        fields = {"transaction_reference": ref, "integration_id": ident["integration_id"], "sap_database": ident["sap_database"], "sap_object_type": ident["object_type"], "sap_doc_entry": ident["doc_entry"],
                  "operation_type": C.OP_TRANSPORTER, "request_hash": N.request_hash(payload), "request_id": rid, "processing_status": C.ST_PROCESSING, "locked_until": self.now() + timedelta(seconds=self.LOCK_SECONDS), "ewaybill_number": rec["ewaybill_number"]}
        if prev is None:
            if not self.store.create(ref, fields):
                return self._env(rid, False, C.ST_PROCESSING, ref, message="This update is already in progress.")
        elif prev.get("processing_status") == C.ST_PROCESSING and prev.get("locked_until") and prev["locked_until"] > self.now():
            return self._env(rid, False, C.ST_PROCESSING, ref, message="This update is already in progress.")
        gsp = self.gsp_factory(profile.seller_gstin)
        try:
            gsp.update_transporter(payload)
        except GspUncertain:
            self.store.update(ref, processing_status=C.ST_UNKNOWN, error_code="TIMEOUT", locked_until=None)
            return self._env(rid, False, C.ST_UNKNOWN, ref, message="No confirmed answer. The transporter update outcome is unknown; use Refresh status.")
        except GspRejected as e:
            self.store.update(ref, processing_status=C.ST_FAILED, error_code=e.code[:40], error_message=str(e)[:140], locked_until=None)
            return self._env(rid, False, C.ST_FAILED, ref, message=str(e))
        self.store.update(ref, processing_status=C.ST_GENERATED, locked_until=None)
        return self._env(rid, True, C.ST_GENERATED, ref, data={"transporter_id": tid}, message="Transporter updated (confirmed by the portal).")
