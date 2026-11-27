"""
Public REST API (Frappe token authentication: Authorization: token API_KEY:API_SECRET).

    POST /api/method/sap_b1_direct_integration.api.gst.<endpoint>

validate_invoice, generate_e_invoice, get_e_invoice_status, cancel_e_invoice, generate_e_waybill, get_e_waybill_status,
cancel_e_waybill, update_vehicle, update_transporter, extend_e_waybill_validity, verify_gstin   (+ ping for the connection test)

All endpoints return {"ok", "status", "request_id", "reference", "message", "data", "errors", "warnings"} inside Frappe's {"message": ...} envelope.
Business outcomes (validation failed, failed, unknown, ...) are normal 200 responses with ok=false; HTTP errors are only used for
authentication / authorisation. No ERPNext Sales Invoice, Customer, Item, stock or accounting document is read or written by this module.
"""

import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from sap_b1_direct_integration.core import bindings
from sap_b1_direct_integration.core.contract import ContractError
from sap_b1_direct_integration.core.gsp import IndiaComplianceAdapter
from sap_b1_direct_integration.core.service import AccessDenied, GstApi

_SECRET_KEYS = ("password", "secret", "app_key", "api_key", "authorization")


def _log(event: str, **kw):
    """Audit log (file). Never contains payloads, GSTIN lists, tokens or credentials: only ids, statuses and counters."""
    safe = {k: v for k, v in kw.items() if not any(s in k.lower() for s in _SECRET_KEYS)}
    frappe.logger("sap_b1_gst", allow_site=True, file_count=10).info(event + " " + " ".join(f"{k}={v}" for k, v in safe.items()))


def _service() -> GstApi:
    return GstApi(bindings.FrappeProfileRepo(), bindings.FrappeSite(), bindings.FrappeStore(), IndiaComplianceAdapter, log=_log)


def _body() -> dict:
    data = dict(frappe.form_dict)
    for k in ("cmd", "data"):
        data.pop(k, None)
    inv = data.get("invoice")
    if isinstance(inv, str):
        try:
            data["invoice"] = json.loads(inv)
        except ValueError:
            frappe.throw(_("invoice must be valid JSON"), frappe.ValidationError)
    return data


def _run(name: str):
    req = _body()
    try:
        result = getattr(_service(), name)(req)
    except AccessDenied as e:
        _log("denied", endpoint=name, user=frappe.session.user, reason=str(e)[:80])
        frappe.throw(str(e), frappe.PermissionError)
    except ContractError as e:
        return {"ok": False, "status": "Validation Failed", "request_id": req.get("request_id", ""), "reference": "", "message": f"{e.field}: {e}", "data": {}, "errors": [{"severity": "error", "code": "CONTRACT", "field": e.field, "message": str(e)}], "warnings": []}
    except frappe.PermissionError:
        raise
    except Exception:
        frappe.log_error(title="SAP GST API error (" + name + ")")                 # traceback only; request bodies are not included
        return {"ok": False, "status": "Failed", "request_id": req.get("request_id", ""), "reference": "", "message": "Unexpected server error. See the ERPNext error log.", "data": {}, "errors": [], "warnings": []}
    _log("done", endpoint=name, request_id=result.get("request_id"), reference=result.get("reference"), status=result.get("status"), ok=result.get("ok"))
    return result


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=300, seconds=60, ip_based=False)
def validate_invoice():
    return _run("validate_invoice")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=120, seconds=60, ip_based=False)
def generate_e_invoice():
    return _run("generate_e_invoice")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=300, seconds=60, ip_based=False)
def get_e_invoice_status():
    return _run("get_e_invoice_status")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=60, seconds=60, ip_based=False)
def cancel_e_invoice():
    return _run("cancel_e_invoice")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=120, seconds=60, ip_based=False)
def generate_e_waybill():
    return _run("generate_e_waybill")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=300, seconds=60, ip_based=False)
def get_e_waybill_status():
    return _run("get_e_waybill_status")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=60, seconds=60, ip_based=False)
def cancel_e_waybill():
    return _run("cancel_e_waybill")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=120, seconds=60, ip_based=False)
def update_vehicle():
    return _run("update_vehicle")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=120, seconds=60, ip_based=False)
def update_transporter():
    return _run("update_transporter")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=60, seconds=60, ip_based=False)
def extend_e_waybill_validity():
    return _run("extend_e_waybill_validity")


@frappe.whitelist(methods=["POST"])
@rate_limit(key="integration_id", limit=60, seconds=60, ip_based=False)
def verify_gstin():
    return _run("verify_gstin")


@frappe.whitelist(methods=["GET", "POST"])
def ping(integration_id: str | None = None):
    """Connection test: authenticated user, versions, and (optionally) a profile / environment consistency check. Reveals no credentials."""
    versions = frappe.get_attr("frappe.utils.change_log.get_versions")()
    out = {
        "user": frappe.session.user, "app": "sap_b1_direct_integration", "api_version": "2.0",
        "frappe": versions.get("frappe", {}).get("version"), "erpnext": versions.get("erpnext", {}).get("version"),
        "india_compliance": versions.get("india_compliance", {}).get("version"), "gst_sandbox_mode": bool(frappe.get_cached_doc("GST Settings").sandbox_mode),
    }
    if integration_id:
        p = bindings.FrappeProfileRepo().get(integration_id)
        out["profile"] = None if p is None else {"enabled": p.enabled, "environment": p.environment, "api_user_matches": p.api_user == frappe.session.user, "sap_database": p.sap_database,
                                                 "environment_consistent": (p.environment.lower() == "sandbox") == out["gst_sandbox_mode"]}
    return out
