"""
GST validation of an invoice contract before anything is sent to the IRP / e-Way Bill system.
Findings are reported, never silently corrected. Each finding: {severity: error|warning, code, field, message}.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from . import constants as C
from .contract import Invoice, Item, Party, ZERO

GSTIN_RX = re.compile(r"^[0-9]{2}[A-Z]{3}[ABCFGHLJPTK][A-Z][0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
GSTIN_LOOSE_RX = re.compile(r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}$")           # sandbox test identities do not follow the checksum rules
PIN_RX = re.compile(r"^[1-9][0-9]{5}$")
DOCNO_RX = re.compile(r"^[A-Za-z1-9][A-Za-z0-9/-]{0,15}$")                   # NIC: max 16, no leading 0 / '/' / '-'
HSN_RX = re.compile(r"^[0-9]{4,8}$")
VEHICLE_RX = re.compile(r"^([A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}|[0-9]{2}BH[0-9]{4}[A-Z]{1,2}|TM[0-9]{6}[A-Z]?)$")
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

TWO = Decimal("0.01")


def gstin_checksum_ok(gstin: str) -> bool:
    """GSTIN check character (position 15), base-36 weighted sum."""
    if len(gstin) != 15 or any(c not in _ALPHABET for c in gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        prod = _ALPHABET.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return _ALPHABET[(36 - total % 36) % 36] == gstin[14]


class Findings:
    def __init__(self):
        self.items: List[Dict[str, str]] = []

    def error(self, code, field, message):
        self.items.append({"severity": "error", "code": code, "field": field, "message": message})

    def warn(self, code, field, message):
        self.items.append({"severity": "warning", "code": code, "field": field, "message": message})

    @property
    def errors(self):
        return [i for i in self.items if i["severity"] == "error"]

    @property
    def warnings(self):
        return [i for i in self.items if i["severity"] == "warning"]

    @property
    def ok(self):
        return not self.errors


def _gstin(f: Findings, who: str, gstin: str, sandbox: bool, allow_urp: bool = False):
    if allow_urp and gstin == "URP":
        return
    if not gstin:
        f.error("GSTIN_MISSING", who + ".gstin", f"{who} GSTIN is missing")
        return
    if not (GSTIN_LOOSE_RX.match(gstin) if sandbox else GSTIN_RX.match(gstin)):
        f.error("GSTIN_FORMAT", who + ".gstin", f"{who} GSTIN '{gstin}' has an invalid format")
        return
    if not sandbox and not gstin_checksum_ok(gstin):
        f.error("GSTIN_CHECKSUM", who + ".gstin", f"{who} GSTIN '{gstin}' fails the check-character validation (probably mistyped)")
    if gstin[:2] not in C.STATE_CODES:
        f.error("GSTIN_STATE", who + ".gstin", f"{who} GSTIN '{gstin}' starts with an unknown state code")


def _party_address(f: Findings, who: str, p: Party, gstin_state_must_match: bool, export: bool = False):
    if not p.legal_name:
        f.error("NAME_MISSING", who + ".legal_name", f"{who} legal name is missing")
    if not p.address1:
        f.error("ADDRESS_MISSING", who + ".address", f"{who} address is missing")
    if not p.state_code or p.state_code not in C.STATE_CODES:
        f.error("STATE_CODE", who + ".state_code", f"{who} state code '{p.state_code}' is not a valid GST state code")
    elif gstin_state_must_match and p.gstin and p.gstin != "URP" and p.gstin[:2] != p.state_code:
        f.error("STATE_GSTIN_MISMATCH", who + ".state_code", f"{who} state code {p.state_code} differs from the GSTIN state {p.gstin[:2]}")
    if len(p.city or "") < 3:
        f.error("CITY", who + ".city", f"{who} city/location must be at least 3 characters")
    if export:
        return
    if not PIN_RX.match(p.pincode or ""):
        f.error("PIN_CODE", who + ".pincode", f"{who} PIN code '{p.pincode}' must be 6 digits (100000-999999)")


def validate_invoice(inv: Invoice, *, sandbox: bool, today: Optional[date] = None, min_hsn_digits: int = 4,
                     tolerance: Decimal = Decimal("1.00"), for_einvoice: bool = True) -> Findings:
    f = Findings()
    today = today or date.today()

    # ---- document
    if inv.document_type not in C.DOCUMENT_TYPES:
        f.error("DOC_TYPE", "document_type", f"Document type '{inv.document_type}' is not supported (INV, CRN, DBN)")
    if not DOCNO_RX.match(inv.document_number or ""):
        f.error("DOC_NUMBER", "document_number",
                f"Document number '{inv.document_number}' is not accepted by the IRP: maximum 16 characters, letters/digits/'/'/'-', not starting with 0, '/' or '-'")
    if inv.document_date is None:
        f.error("DOC_DATE", "document_date", "Document date is missing")
    elif inv.document_date > today:
        f.error("DOC_DATE_FUTURE", "document_date", "Document date is in the future")
    if inv.supply_type not in C.SUPPLY_TYPES:
        f.error("SUPPLY_TYPE", "supply_type", f"Supply type '{inv.supply_type}' is not one of {sorted(C.SUPPLY_TYPES)}")
    if inv.document_type in ("CRN", "DBN") and not inv.original_document_number:
        f.warn("ORIGINAL_DOC", "original_document_number", "Credit/debit note without a reference to the original invoice")
    if inv.currency != "INR" and inv.supply_type not in C.EXPORT_SUPPLY_TYPES:
        f.error("CURRENCY", "currency", f"Currency {inv.currency} is only allowed for export supplies (amounts must be in INR)")
    if inv.currency != "INR" and inv.exchange_rate <= 0:
        f.error("EXCHANGE_RATE", "exchange_rate", "Exchange rate must be positive for foreign-currency documents")

    # ---- parties
    export = inv.supply_type in C.EXPORT_SUPPLY_TYPES
    if for_einvoice and inv.supply_type == "B2B" and inv.buyer.gstin in ("", "URP"):
        f.error("B2C_NOT_APPLICABLE", "buyer.gstin", "The buyer is unregistered (no GSTIN / URP): e-Invoice is not applicable to B2C supplies")
    _gstin(f, "Seller", inv.seller.gstin, sandbox)
    if not export:
        _gstin(f, "Buyer", inv.buyer.gstin, sandbox, allow_urp=not for_einvoice)
    elif inv.buyer.gstin not in ("", "URP"):
        f.warn("EXPORT_BUYER_GSTIN", "buyer.gstin", "Export buyers normally carry GSTIN 'URP'")
    if inv.seller.gstin and inv.seller.gstin == inv.buyer.gstin:
        f.error("SAME_GSTIN", "buyer.gstin", "Seller and buyer GSTIN are the same")
    _party_address(f, "Seller", inv.seller, True)
    _party_address(f, "Buyer", inv.buyer, not export, export=export)
    if export and inv.buyer.state_code != "96":
        f.error("EXPORT_STATE", "buyer.state_code", "Export supplies must use state code 96 (Other Countries)")
    if inv.place_of_supply and inv.place_of_supply not in C.STATE_CODES:
        f.error("POS", "place_of_supply", f"Place of supply '{inv.place_of_supply}' is not a valid state code")
    if export:
        ex = inv.export
        if ex is None or not ex.country_code:
            f.error("EXPORT_COUNTRY", "export.country_code", "Export details with the destination country code are required")
        if ex is not None and ex.shipping_bill_no and ex.shipping_bill_date is None:
            f.error("EXPORT_SB_DATE", "export.shipping_bill_date", "Shipping bill date is required with the shipping bill number")

    # ---- items
    inter_state = None
    for it in inv.items:
        _item(f, inv, it, min_hsn_digits, tolerance)
        if it.igst_rate or it.igst_amount:
            inter_state = True if inter_state is None else inter_state and True
        if it.cgst_rate or it.sgst_rate:
            inter_state = False if inter_state is None else inter_state and False
    sez = inv.supply_type in C.SEZ_SUPPLY_TYPES
    if for_einvoice:
        if sez and any(i.cgst_amount or i.sgst_amount for i in inv.items):
            f.error("SEZ_TAX", "items", "A supply to an SEZ is inter-state: only IGST applies, not CGST/SGST")
        if inv.supply_type in ("EXPWOP", "SEZWOP") and any(i.igst_amount or i.cgst_amount or i.sgst_amount for i in inv.items):
            f.error("TAX_WITHOUT_PAYMENT", "supply_type", f"Supply type {inv.supply_type} is without payment of tax, but the invoice charges GST")
        if inv.supply_type == "B2B" and inv.items and all(not i.gst_rate for i in inv.items):
            f.warn("NO_TAXABLE_ITEM", "items", "No line carries GST. A bill of supply (only exempt, nil-rated or non-GST goods) does not need an e-Invoice")
    if inter_state is True and not export and not (sez and for_einvoice) and inv.seller.state_code and inv.buyer.state_code and inv.seller.state_code == (inv.place_of_supply or inv.buyer.state_code):
        f.error("IGST_SAME_STATE", "items", "IGST is charged although the seller state equals the place of supply. For an intra-state supply only CGST and SGST apply; the IRP rejects this invoice. Correct the tax code or the place of supply in SAP.")
    if inter_state is False and not export and inv.seller.state_code and (inv.place_of_supply or inv.buyer.state_code) != inv.seller.state_code:
        f.error("CGST_OTHER_STATE", "items", "CGST/SGST is charged although the place of supply (" + str(inv.place_of_supply or inv.buyer.state_code) + ") is in another state than the seller (" + str(inv.seller.state_code) + "). For an inter-state supply only IGST applies; the IRP rejects this invoice (error 2174). Correct the tax code or the place of supply in SAP.")

    _totals(f, inv, tolerance)
    return f


def _item(f: Findings, inv: Invoice, it: Item, min_hsn: int, tol: Decimal):
    p = f"items[{it.line}]"
    if not it.description and not it.item_code:
        f.error("ITEM_DESC", p, f"Line {it.line}: description is missing")
    if not HSN_RX.match(it.hsn_code or ""):
        f.error("HSN_FORMAT", p + ".hsn_code", f"Line {it.line}: HSN/SAC '{it.hsn_code}' must be 4-8 digits (no dots)")
    elif len(it.hsn_code) < min_hsn:
        f.error("HSN_DIGITS", p + ".hsn_code", f"Line {it.line}: HSN/SAC '{it.hsn_code}' has fewer than the required {min_hsn} digits")
    if it.unit not in C.UQC:
        f.error("UQC", p + ".unit", f"Line {it.line}: unit '{it.unit}' is not a valid UQC code (map the SAP unit to one of the GST units, e.g. KGS, NOS, PCS, OTH)")
    if it.quantity < 0:
        f.error("QTY", p + ".quantity", f"Line {it.line}: quantity cannot be negative")
    if it.unit_price < 0:
        f.error("PRICE", p + ".unit_price", f"Line {it.line}: unit price cannot be negative")
    if it.taxable_value < 0 and inv.document_type == "INV":
        f.error("TAXABLE", p + ".taxable_value", f"Line {it.line}: taxable value cannot be negative on an invoice")
    if (it.igst_rate or it.igst_amount) and (it.cgst_rate or it.sgst_rate or it.cgst_amount or it.sgst_amount):
        f.error("TAX_MIX", p, f"Line {it.line}: IGST cannot be combined with CGST/SGST on the same line")
    if it.cgst_rate != it.sgst_rate:
        f.error("CGST_SGST_RATE", p, f"Line {it.line}: CGST rate {it.cgst_rate} and SGST rate {it.sgst_rate} must be equal")
    for name, rate in (("cgst_rate", it.cgst_rate), ("sgst_rate", it.sgst_rate), ("igst_rate", it.igst_rate), ("cess_rate", it.cess_rate)):
        if rate < 0 or rate > 100:
            f.error("RATE", p + "." + name, f"Line {it.line}: {name} {rate} is out of range")
    if float(it.gst_rate) not in C.USUAL_GST_RATES:
        f.warn("RATE_UNUSUAL", p, f"Line {it.line}: GST rate {it.gst_rate}% is not a usual slab; the IRP master decides")
    # IRP rule: each tax amount must agree with taxable value x rate (rupee tolerance)
    for name, rate, amount in (("CGST", it.cgst_rate, it.cgst_amount), ("SGST", it.sgst_rate, it.sgst_amount), ("IGST", it.igst_rate, it.igst_amount), ("Cess", it.cess_rate, it.cess_amount)):
        expected = (it.taxable_value * rate / Decimal(100)).quantize(TWO)
        if abs(expected - amount) > tol:
            f.error("TAX_AMOUNT", p, f"Line {it.line}: {name} amount {amount} differs from taxable value x rate ({expected}) by more than {tol}")
    fx = inv.exchange_rate if inv.currency != "INR" else Decimal(1)        # unit prices are in the document currency, taxable values in INR
    gross = (it.quantity * it.unit_price * fx).quantize(TWO)
    line_tol = tol if fx == 1 else max(tol, abs(it.taxable_value) * Decimal("0.005"))
    if it.unit_price and abs(gross - it.discount * fx - it.taxable_value) > line_tol:
        f.warn("LINE_VALUE", p, f"Line {it.line}: quantity x price - discount ({gross - it.discount}) differs from the taxable value {it.taxable_value}; the taxable value is used as given")


def _totals(f: Findings, inv: Invoice, tol: Decimal):
    t = inv.totals
    lines_taxable = sum((i.taxable_value for i in inv.items), ZERO)
    if abs(lines_taxable - t.taxable_value) > tol:
        f.error("TOTAL_TAXABLE", "tax_totals.taxable_value", f"Total taxable value {t.taxable_value} differs from the sum of the lines {lines_taxable}")
    for name, total, line_sum in (
        ("CGST", t.cgst, sum((i.cgst_amount for i in inv.items), ZERO)), ("SGST", t.sgst, sum((i.sgst_amount for i in inv.items), ZERO)),
        ("IGST", t.igst, sum((i.igst_amount for i in inv.items), ZERO)), ("Cess", t.cess, sum((i.cess_amount for i in inv.items), ZERO)),
    ):
        if abs(total - line_sum) > tol:
            f.error("TOTAL_TAX", "tax_totals." + name.lower(), f"Total {name} {total} differs from the sum of the lines {line_sum}")
    expected = t.taxable_value + t.cgst + t.sgst + t.igst + t.cess + t.other_charges + t.round_off - t.discount
    if abs(expected - t.invoice_total) > tol:
        f.error("TOTAL_VALUE", "tax_totals.invoice_total",
                f"Invoice total {t.invoice_total} differs from taxable + taxes + other charges + round-off - discount ({expected}) by {abs(expected - t.invoice_total)}; nothing was adjusted")
    elif abs(t.round_off) > Decimal("1.00"):
        f.warn("ROUND_OFF", "tax_totals.round_off", f"Round-off {t.round_off} is unusually large")


# ------------------------------------------------------------------------------------------ e-Way Bill transport
def validate_transport(tr, *, require_distance_range: bool = True, mode_required: bool = True) -> Findings:
    f = Findings()
    mode = tr.transport_mode or ("Road" if tr.vehicle_number or tr.transporter_id else "")
    if mode_required and mode not in C.TRANSPORT_MODES:
        f.error("MODE", "transport.transport_mode", "Transport mode must be Road, Rail, Air or Ship")
    if tr.distance < 0 or tr.distance > 4000:
        f.error("DISTANCE", "transport.distance", "Distance must be between 0 (auto-calculated by the portal) and 4000 km")
    if mode == "Road" and not tr.vehicle_number and not tr.transporter_id:
        f.error("TRANSPORT_ID", "transport", "For road transport enter a vehicle number or a transporter GSTIN")
    if tr.vehicle_number and not VEHICLE_RX.match(tr.vehicle_number):
        f.error("VEHICLE", "transport.vehicle_number", f"Vehicle number '{tr.vehicle_number}' is not a valid format (e.g. MH04AB1234)")
    if tr.transporter_id and not GSTIN_LOOSE_RX.match(tr.transporter_id):
        f.error("TRANSPORTER_ID", "transport.transporter_id", "Transporter ID must be a valid 15-character GSTIN")
    if mode in ("Rail", "Air", "Ship") and not (tr.doc_number and tr.doc_date):
        f.error("TRANSPORT_DOC", "transport.doc_number", f"{mode} transport requires the transport document number and date")
    if tr.doc_number and not tr.doc_date:
        f.error("TRANSPORT_DOC_DATE", "transport.doc_date", "Transport document date is required with the document number")
    if tr.vehicle_type not in C.VEHICLE_TYPES:
        f.error("VEHICLE_TYPE", "transport.vehicle_type", "Vehicle type must be Regular or Over Dimensional Cargo (ODC)")
    return f
