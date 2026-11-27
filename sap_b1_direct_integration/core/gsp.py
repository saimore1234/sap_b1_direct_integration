"""
GSP adapter on top of the GST app installed in ERPNext (india_compliance >= 15, verified against 15.29.0).

Reused (not re-implemented): GSP authentication / token refresh / session-key encryption, request signing & transport, response decryption,
sandbox routing, credential lookup in GST Settings. These live in india_compliance.gst_india.api_classes.nic.{e_invoice,e_waybill,auth}:
    EInvoiceAPI.create(company_gstin=...)  and  EWaybillAPI.create(company_gstin=...)
whose setup() accepts a bare company GSTIN (no ERPNext document needed). Only the NIC payloads are built by this app (nic_payload.py),
because india_compliance builds them solely from Sales Invoice documents (EInvoiceData(doc) / EWaybillData(doc)) which this integration
deliberately does not use.

Known side effect of the reused client: every GSP call is logged by india_compliance as an "Integration Request" record that contains the payload.
tasks.cleanup_integration_requests removes those records after the retention period.
"""

import base64
import json
from datetime import date
from typing import Any, Dict, Optional

from .service import Gsp, GspRejected, GspUncertain

_UNCERTAIN_NAMES = {"GatewayTimeoutError", "GSPServerError", "GSPLimitExceededError", "ReadTimeout", "ConnectTimeout", "Timeout", "ConnectionError", "ChunkedEncodingError", "ConnectionResetError", "TimeoutError"}


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    """Payload of a signed IRP document (signature NOT verified here, same as india_compliance does for display)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _classify(exc: Exception):
    name = type(exc).__name__
    if name in _UNCERTAIN_NAMES or any(b in _UNCERTAIN_NAMES for b in (c.__name__ for c in type(exc).__mro__)):
        raise GspUncertain(str(exc) or name)
    raise GspRejected(_plain(str(exc)) or name, code=getattr(exc, "error_code", "") or "")


def _plain(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ").strip()


class IndiaComplianceAdapter(Gsp):
    def __init__(self, seller_gstin: str):
        # imported lazily so that the pure core (and its tests) never needs Frappe
        from india_compliance.gst_india.api_classes.nic.e_invoice import EInvoiceAPI
        from india_compliance.gst_india.api_classes.nic.e_waybill import EWaybillAPI
        self._EInvoiceAPI, self._EWaybillAPI, self.gstin = EInvoiceAPI, EWaybillAPI, seller_gstin

    def _einv(self):
        try:
            return self._EInvoiceAPI.create(company_gstin=self.gstin)
        except Exception as e:                  # missing credentials / API disabled / auth failure
            _classify(e)

    def _ewb(self):
        try:
            return self._EWaybillAPI.create(company_gstin=self.gstin)
        except Exception as e:
            _classify(e)

    # ----------------------------------------------------------------------------------------- e-Invoice
    def generate_irn(self, payload):
        api = self._einv()
        try:
            res = api.generate_irn(payload)
        except Exception as e:
            _classify(e)
        if getattr(res, "get", None) and res.get("InfCd") == "DUPIRN":                        # the IRP already holds an IRN for this document number
            return self._from_duplicate(api, res, payload)
        code = getattr(res, "error_code", None)
        if code and not res.get("Irn"):
            raise GspRejected(_plain(getattr(res, "error_message", "") or str(code)), code=str(code))
        return self._irn_result(res)

    def _from_duplicate(self, api, res, payload):
        desc = res.get("Desc") or {}
        irn = desc.get("Irn")
        if not irn:
            raise GspRejected("The IRP reports a duplicate IRN but returned no IRN details.", code="2150")
        try:
            det = api.get_e_invoice_by_irn(irn)
        except Exception as e:
            _classify(e)
        signed = det.get("SignedInvoice") if hasattr(det, "get") else None
        if signed:                                                                              # same checks as india_compliance.verify_e_invoice_details
            inv = json.loads(decode_jwt_payload(signed).get("data", "{}") or "{}")
            if inv.get("BuyerDtls", {}).get("Gstin") != payload.get("BuyerDtls", {}).get("Gstin") or inv.get("ValDtls", {}).get("TotInvVal") != payload.get("ValDtls", {}).get("TotInvVal"):
                raise GspRejected("An e-Invoice already exists for this document number but with different buyer GSTIN or invoice value. Not reused.", code="2150")
            return self._irn_result(det)
        return {"irn": irn, "ack_no": desc.get("AckNo"), "ack_date": desc.get("AckDt"), "signed_invoice": None, "signed_qr": None, "status": "ACT"}

    def get_irn(self, irn):
        try:
            res = self._einv().get_e_invoice_by_irn(irn)
        except Exception as e:
            _classify(e)
        code = getattr(res, "error_code", None)
        if code and not res.get("Irn"):
            raise GspRejected(_plain(getattr(res, "error_message", "") or str(code)), code=str(code))
        out = self._irn_result(res)
        out["status"] = res.get("Status") or out.get("status")
        return out

    def cancel_irn(self, payload):
        try:
            res = self._einv().cancel_irn(payload)
        except Exception as e:
            _classify(e)
        code = getattr(res, "error_code", None)
        if code and code != "9999":                                                              # 9999 = not active (already cancelled): confirmed by get_irn
            raise GspRejected(_plain(getattr(res, "error_message", "") or str(code)), code=str(code))
        return {"irn": res.get("Irn"), "cancel_date": res.get("CancelDate")}

    @staticmethod
    def _irn_result(res) -> Dict[str, Any]:
        return {"irn": res.get("Irn"), "ack_no": res.get("AckNo"), "ack_date": res.get("AckDt"), "signed_invoice": res.get("SignedInvoice"), "signed_qr": res.get("SignedQRCode"),
                "status": res.get("Status"), "ewb_no": res.get("EwbNo"), "ewb_date": res.get("EwbDt"), "ewb_valid": res.get("EwbValidTill")}

    # ----------------------------------------------------------------------------------------- GSTIN search
    def lookup_gstin(self, gstin):
        """Public taxpayer search of the installed GST app (no GSTIN credentials; needs its API key; not available in sandbox mode)."""
        try:
            from india_compliance.gst_india.api_classes.public import PublicAPI
            return PublicAPI().get_gstin_info(gstin)
        except Exception as e:
            _classify(e)

    # ----------------------------------------------------------------------------------------- e-Way Bill
    def generate_ewb(self, payload, with_irn):
        try:
            if with_irn:
                res = self._einv().generate_e_waybill(payload)
                code = getattr(res, "error_code", None)
                if code == "4002":                                                              # already generated for this IRN: fetch it
                    res = self._einv().get_e_waybill_by_irn(payload["Irn"])
                if not res.get("EwbNo"):
                    raise GspRejected(_plain(getattr(res, "error_message", "") or "No e-Way Bill number returned"), code=str(code or ""))
                return {"ewb_no": res.get("EwbNo"), "ewb_date": res.get("EwbDt"), "ewb_valid": res.get("EwbValidTill")}
            res = self._ewb().generate_e_waybill(payload)
        except (GspRejected, GspUncertain):
            raise
        except Exception as e:
            _classify(e)
        code = getattr(res, "error_code", None)
        if code and not res.get("ewayBillNo"):
            raise GspRejected(_plain(getattr(res, "error_message", "") or str(code)), code=str(code))
        return {"ewb_no": res.get("ewayBillNo"), "ewb_date": res.get("ewayBillDate"), "ewb_valid": res.get("validUpto")}

    def get_ewb(self, ewb_no):
        try:
            res = self._ewb().get_e_waybill(ewb_no)
        except Exception as e:
            _classify(e)
        status = {"ACT": "ACT", "CNL": "CNL", "REJ": "REJ"}.get(str(res.get("status") or "").upper(), res.get("status"))
        return {"ewb_no": res.get("ewbNo"), "status": status, "vehicle_no": res.get("vehicleNo") or _latest_vehicle(res), "ewb_valid": res.get("validUpto"),
                "ewb_date": res.get("ewayBillDate")}

    def find_ewb(self, doc_no: str, doc_date: date, from_gstin: str) -> Optional[Dict[str, Any]]:
        try:
            res = self._ewb().get_e_waybills_by_date(doc_date.strftime("%d/%m/%Y"))
        except Exception as e:
            _classify(e)
        rows = res if isinstance(res, list) else (res.get("data") if hasattr(res, "get") else None) or []
        for r in rows:
            if str(r.get("docNo")) == doc_no and (not r.get("fromGstin") or r.get("fromGstin") == from_gstin):
                return {"ewb_no": r.get("ewbNo") or r.get("ewayBillNo"), "ewb_date": r.get("ewayBillDate"), "ewb_valid": r.get("validUpto")}
        return None

    def cancel_ewb(self, payload):
        try:
            res = self._ewb().cancel_e_waybill(payload)
        except Exception as e:
            _classify(e)
        code = getattr(res, "error_code", None)
        if code and code != "312":                                                               # 312 = already cancelled: confirmed by get_ewb
            raise GspRejected(_plain(getattr(res, "error_message", "") or str(code)), code=str(code))
        return {"ewb_no": res.get("ewayBillNo"), "cancel_date": res.get("cancelDate")}

    def update_vehicle(self, payload):
        try:
            res = self._ewb().update_vehicle_info(payload)
        except Exception as e:
            _classify(e)
        return {"ewb_valid": res.get("validUpto"), "updated": res.get("vehUpdDate")}

    def extend_validity(self, payload):
        try:
            res = self._ewb().extend_validity(payload)
        except Exception as e:
            _classify(e)
        code = getattr(res, "error_code", None)
        if code and not res.get("validUpto"):
            raise GspRejected(_plain(getattr(res, "error_message", "") or str(code)), code=str(code))
        return {"ewb_valid": res.get("validUpto"), "updated": res.get("updatedDate")}

    def update_transporter(self, payload):
        try:
            res = self._ewb().update_transporter(payload)
        except Exception as e:
            _classify(e)
        return {"updated": res.get("transUpdateDate")}


def _latest_vehicle(res) -> str:
    try:
        vl = res.get("VehiclListDetails") or []
        return vl[-1].get("vehicleNo") if vl else ""
    except Exception:
        return ""
