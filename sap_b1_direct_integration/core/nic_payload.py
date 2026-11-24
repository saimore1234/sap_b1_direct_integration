"""
Builds the NIC IRP (e-Invoice schema 1.1) and e-Way Bill payloads from an invoice contract - without any ERPNext document.

Field names and structure follow the payloads produced by india_compliance 15.29.0 (utils/e_invoice.py, utils/e_waybill.py)
so that the SAME installed GSP client classes (EInvoiceAPI / EWaybillAPI) can transmit them. Amounts use Decimal and ROUND_HALF_UP.
Original SAP values are never modified; derived values (unit price per taxable unit, item totals) are computed here only.
In sandbox mode the GSP only accepts registered test identities; the substitution is explicit and returned to the caller.
"""

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import constants as C
from .contract import Invoice, Item, Party, Transport, ZERO

D2 = Decimal("0.01")
D3 = Decimal("0.001")

# NIC sandbox test identities (public, from the GSP sandbox documentation). Used ONLY when sandbox mode is on.
SANDBOX_SELLER = {"gstin": "02AMBPG7773M002", "state_code": "02", "pincode": "171302"}
SANDBOX_BUYER = {"gstin": "36AMBPG7773M002", "state_code": "36", "pincode": "500055"}


def q2(v: Decimal) -> float:
    return float(v.quantize(D2, rounding=ROUND_HALF_UP))


def q3(v: Decimal) -> float:
    return float(v.quantize(D3, rounding=ROUND_HALF_UP))


def fmt_date(d: Optional[date]) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def sanitize(obj: Any) -> Any:
    """Drop empty strings / None recursively (the NIC schemas reject blank optional values)."""
    if isinstance(obj, dict):
        out = {k: sanitize(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in ("", None) and v != {} and v != []}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def request_hash(payload: Dict[str, Any]) -> str:
    """Stable fingerprint of what is sent; the only thing stored about the request."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _is_export(inv: Invoice) -> bool:
    return inv.supply_type in C.EXPORT_SUPPLY_TYPES


def apply_sandbox_identities(inv: Invoice) -> List[str]:
    """Sandbox only. Returns the list of replaced values so that nothing is substituted silently."""
    notes = []
    s, b = SANDBOX_SELLER, SANDBOX_BUYER
    if inv.seller.gstin != s["gstin"]:
        notes.append(f"seller GSTIN/state/PIN replaced by sandbox identity ({inv.seller.gstin} -> {s['gstin']})")
    inv.seller.gstin, inv.seller.state_code, inv.seller.pincode = s["gstin"], s["state_code"], s["pincode"]
    if not _is_export(inv):
        if inv.buyer.gstin != b["gstin"]:
            notes.append(f"buyer GSTIN/state/PIN replaced by sandbox identity ({inv.buyer.gstin} -> {b['gstin']})")
        inv.buyer.gstin, inv.buyer.state_code, inv.buyer.pincode = b["gstin"], b["state_code"], b["pincode"]
        inv.place_of_supply = b["state_code"] if inv.totals.igst > 0 else s["state_code"]
    return notes


def _item_total(it: Item) -> Decimal:
    return it.taxable_value + it.cgst_amount + it.sgst_amount + it.igst_amount + it.cess_amount + it.other_charges


def build_einvoice(inv: Invoice) -> Dict[str, Any]:
    export = _is_export(inv)
    t = inv.totals
    pos = inv.place_of_supply or inv.buyer.state_code
    items = []
    for it in inv.items:
        unit_rate = (it.taxable_value / it.quantity) if it.quantity else it.taxable_value
        items.append({
            "SlNo": str(it.line), "PrdDesc": (it.description or it.item_code)[:300],
            "IsServc": "Y" if it.hsn_code.startswith(C.SERVICE_HSN_PREFIX) else "N", "HsnCd": it.hsn_code, "Unit": it.unit,
            "Qty": float(it.quantity), "UnitPrice": q3(abs(unit_rate)), "TotAmt": q2(it.taxable_value), "Discount": 0, "AssAmt": q2(it.taxable_value),
            "GstRt": float(it.gst_rate), "IgstAmt": q2(it.igst_amount), "CgstAmt": q2(it.cgst_amount), "SgstAmt": q2(it.sgst_amount),
            "CesRt": float(it.cess_rate), "CesAmt": q2(it.cess_amount), "OthChrg": q2(it.other_charges), "TotItemVal": q2(_item_total(it)),
        })
    seller, buyer = inv.seller, inv.buyer
    data: Dict[str, Any] = {
        "Version": "1.1",
        "TranDtls": {"TaxSch": "GST", "SupTyp": inv.supply_type, "RegRev": "Y" if inv.reverse_charge else "N", "EcmGstin": inv.ecommerce_gstin},
        "DocDtls": {"Typ": inv.document_type, "No": inv.document_number, "Dt": fmt_date(inv.document_date)},
        "SellerDtls": {
            "Gstin": seller.gstin, "LglNm": seller.legal_name, "TrdNm": seller.trade_name or seller.legal_name, "Addr1": seller.address1,
            "Addr2": seller.address2, "Loc": seller.city, "Pin": int(seller.pincode or 0), "Stcd": seller.state_code,
        },
        "BuyerDtls": {
            "Gstin": "URP" if export else buyer.gstin, "LglNm": buyer.legal_name, "TrdNm": buyer.trade_name or buyer.legal_name, "Pos": "96" if export else pos,
            "Addr1": buyer.address1, "Addr2": buyer.address2, "Loc": buyer.city, "Pin": 999999 if export else int(buyer.pincode or 0), "Stcd": "96" if export else buyer.state_code,
        },
        "ItemList": items,
        "ValDtls": {
            "AssVal": q2(t.taxable_value), "CgstVal": q2(t.cgst), "SgstVal": q2(t.sgst), "IgstVal": q2(t.igst), "CesVal": q2(t.cess),
            "Discount": q2(t.discount), "OthChrg": q2(t.other_charges), "RndOffAmt": q2(t.round_off), "TotInvVal": q2(t.invoice_total),
        },
    }
    if export:
        data["ValDtls"]["TotInvValFc"] = q2(t.invoice_total_fc)
        ex = inv.export
        if ex:
            data["ExpDtls"] = {"ShipBNo": ex.shipping_bill_no, "ShipBDt": fmt_date(ex.shipping_bill_date), "Port": ex.port_code, "ForCur": ex.foreign_currency or inv.currency, "CntCode": ex.country_code}
    if inv.ship_to:
        s = inv.ship_to
        data["ShipDtls"] = {"Gstin": s.gstin, "LglNm": s.legal_name, "TrdNm": s.trade_name or s.legal_name, "Addr1": s.address1, "Addr2": s.address2, "Loc": s.city, "Pin": int(s.pincode or 0), "Stcd": s.state_code}
    if inv.dispatch_from:
        d = inv.dispatch_from
        data["DispDtls"] = {"Nm": d.legal_name, "Addr1": d.address1, "Addr2": d.address2, "Loc": d.city, "Pin": int(d.pincode or 0), "Stcd": d.state_code}
    if inv.document_type in ("CRN", "DBN") and inv.original_document_number:
        data["RefDtls"] = {"PrecDocDtls": [{"InvNo": inv.original_document_number, "InvDt": fmt_date(inv.original_document_date)}]}
    return sanitize(data)


def _transport_values(tr: Transport) -> Dict[str, Any]:
    mode = tr.transport_mode or ("Road" if tr.vehicle_number or tr.transporter_id else "")
    return {
        "mode": C.TRANSPORT_MODES.get(mode, ""), "vehicle_type": C.VEHICLE_TYPES.get(tr.vehicle_type, "R"), "distance": int(tr.distance),
    }


def build_ewb_with_irn(irn: str, tr: Transport) -> Dict[str, Any]:
    v = _transport_values(tr)
    return sanitize({
        "Irn": irn, "Distance": v["distance"], "TransMode": str(v["mode"]), "TransId": tr.transporter_id, "TransName": tr.transporter_name,
        "TransDocDt": fmt_date(tr.doc_date), "TransDocNo": tr.doc_number, "VehNo": tr.vehicle_number, "VehType": v["vehicle_type"],
    })


_EWB_DOC_TYPE = {"INV": "INV", "CRN": "CNT", "DBN": "OTH"}


def build_ewb_standalone(inv: Invoice) -> Dict[str, Any]:
    t, tr, s, b = inv.totals, inv.transport, inv.seller, inv.buyer
    v = _transport_values(tr)
    export = _is_export(inv)
    pos = inv.place_of_supply or b.state_code
    ship_to = inv.ship_to or b
    ship_from = inv.dispatch_from or s
    txn_type = 1
    if inv.ship_to and inv.dispatch_from:
        txn_type = 4
    elif inv.ship_to:
        txn_type = 2
    elif inv.dispatch_from:
        txn_type = 3
    main = max(inv.items, key=lambda i: i.taxable_value).hsn_code if inv.items else ""
    items = [{
        "itemNo": it.line, "productName": "", "productDesc": (it.description or it.item_code)[:100], "hsnCode": int(it.hsn_code), "qtyUnit": it.unit,
        "quantity": float(it.quantity), "taxableAmount": q2(it.taxable_value + it.other_charges), "sgstRate": float(it.sgst_rate), "cgstRate": float(it.cgst_rate),
        "igstRate": float(it.igst_rate), "cessRate": float(it.cess_rate), "cessNonAdvol": 0,
    } for it in inv.items]
    data = {
        "userGstin": s.gstin, "supplyType": "O", "subSupplyType": 3 if export else 1, "docType": _EWB_DOC_TYPE.get(inv.document_type, "INV"),
        "docNo": inv.document_number, "docDate": fmt_date(inv.document_date), "transactionType": txn_type,
        "fromTrdName": s.legal_name, "fromGstin": s.gstin, "fromAddr1": ship_from.address1, "fromAddr2": ship_from.address2, "fromPlace": ship_from.city,
        "fromPincode": int(ship_from.pincode or 0), "fromStateCode": int(s.state_code), "actFromStateCode": int(ship_from.state_code or s.state_code),
        "toTrdName": b.legal_name, "toGstin": "URP" if (export or not b.gstin) else b.gstin, "toAddr1": ship_to.address1, "toAddr2": ship_to.address2, "toPlace": ship_to.city,
        "toPincode": 999999 if export else int(ship_to.pincode or 0), "toStateCode": C.EWB_OTHER_COUNTRY_STATE_CODE if export else int(pos or 0),
        "actToStateCode": C.EWB_OTHER_COUNTRY_STATE_CODE if export else int(ship_to.state_code or pos or 0),
        "totalValue": q2(t.taxable_value), "cgstValue": q2(t.cgst), "sgstValue": q2(t.sgst), "igstValue": q2(t.igst), "cessValue": q2(t.cess), "cessNonAdvolValue": 0,
        "otherValue": q2(t.round_off + t.other_charges - t.discount), "totInvValue": q2(t.invoice_total),
        "transMode": v["mode"], "transDistance": v["distance"], "transporterName": tr.transporter_name, "transporterId": tr.transporter_id,
        "transDocNo": tr.doc_number, "transDocDate": fmt_date(tr.doc_date), "vehicleNo": tr.vehicle_number, "vehicleType": v["vehicle_type"],
        "itemList": items, "mainHsnCode": int(main) if main else "",
    }
    return sanitize(data)


def build_cancel_irn(irn: str, reason: str, remark: str) -> Dict[str, Any]:
    return {"Irn": irn, "Cnlrsn": C.EINVOICE_CANCEL_REASONS[reason], "Cnlrem": remark or reason}


def build_cancel_ewb(ewb_no: str, reason: str, remark: str) -> Dict[str, Any]:
    return {"ewbNo": int(ewb_no), "cancelRsnCode": int(C.EWAYBILL_CANCEL_REASONS[reason]), "cancelRmrk": remark or reason}


def build_update_vehicle(ewb_no: str, vehicle_no: str, tr: Transport, reason: str, remark: str, place: str, state_code: str) -> Dict[str, Any]:
    v = _transport_values(tr)
    return sanitize({
        "ewbNo": int(ewb_no), "vehicleNo": vehicle_no, "fromPlace": place, "fromState": int(state_code), "reasonCode": int(C.VEHICLE_UPDATE_REASONS[reason]),
        "reasonRem": remark or reason, "transDocNo": tr.doc_number, "transDocDate": fmt_date(tr.doc_date), "transMode": v["mode"], "vehicleType": v["vehicle_type"],
    })


def build_update_transporter(ewb_no: str, transporter_id: str) -> Dict[str, Any]:
    return {"ewbNo": int(ewb_no), "transporterId": transporter_id}


def financial_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"
