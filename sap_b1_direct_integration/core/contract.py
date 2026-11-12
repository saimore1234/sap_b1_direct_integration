"""
Generic SAP -> ERPNext GST invoice contract.

The contract is parsed into plain Python objects with Decimal amounts (never float). Nothing here knows a customer,
GSTIN, state, HSN or tax value: everything comes from the request. Original values are preserved exactly; derived values are
computed separately by nic_payload.py and never written back.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

ZERO = Decimal("0")


class ContractError(Exception):
    """The request is not a well-formed contract (different from a GST validation finding)."""

    def __init__(self, message: str, field_name: str = ""):
        super().__init__(message)
        self.field = field_name


def dec(value: Any, field_name: str = "", default: Optional[Decimal] = ZERO) -> Decimal:
    if value is None or value == "":
        if default is None:
            raise ContractError("is required", field_name)
        return default
    if isinstance(value, bool):
        raise ContractError("must be a number", field_name)
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ContractError("must be a number", field_name)
    if not d.is_finite():
        raise ContractError("must be a finite number", field_name)
    return d


def text(value: Any, max_len: int = 0) -> str:
    s = "" if value is None else str(value).strip()
    return s[:max_len] if max_len else s


def parse_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = text(value)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    raise ContractError("must be a date (YYYY-MM-DD)", field_name)


@dataclass
class Party:
    gstin: str = ""
    legal_name: str = ""
    trade_name: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    state_code: str = ""
    pincode: str = ""


@dataclass
class Item:
    line: int = 0
    item_code: str = ""
    description: str = ""
    hsn_code: str = ""
    quantity: Decimal = ZERO
    unit: str = ""
    unit_price: Decimal = ZERO
    discount: Decimal = ZERO
    taxable_value: Decimal = ZERO
    cgst_rate: Decimal = ZERO
    sgst_rate: Decimal = ZERO
    igst_rate: Decimal = ZERO
    cess_rate: Decimal = ZERO
    cgst_amount: Decimal = ZERO
    sgst_amount: Decimal = ZERO
    igst_amount: Decimal = ZERO
    cess_amount: Decimal = ZERO
    other_charges: Decimal = ZERO

    @property
    def gst_rate(self) -> Decimal:
        return self.igst_rate if self.igst_rate else self.cgst_rate + self.sgst_rate


@dataclass
class TaxTotals:
    taxable_value: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO
    cess: Decimal = ZERO
    discount: Decimal = ZERO
    other_charges: Decimal = ZERO       # freight / packing etc.
    round_off: Decimal = ZERO
    invoice_total: Decimal = ZERO
    invoice_total_fc: Decimal = ZERO


@dataclass
class Transport:
    transporter_id: str = ""
    transporter_name: str = ""
    vehicle_number: str = ""
    vehicle_type: str = "Regular"
    transport_mode: str = ""
    distance: Decimal = ZERO
    doc_number: str = ""
    doc_date: Optional[date] = None


@dataclass
class ExportDetails:
    country_code: str = ""
    port_code: str = ""
    shipping_bill_no: str = ""
    shipping_bill_date: Optional[date] = None
    foreign_currency: str = ""


@dataclass
class Invoice:
    integration_id: str = ""
    sap_database: str = ""
    object_type: str = "13"
    doc_entry: int = 0
    doc_num: int = 0
    document_type: str = "INV"
    document_number: str = ""
    document_date: Optional[date] = None
    due_date: Optional[date] = None
    supply_type: str = "B2B"
    reverse_charge: bool = False
    currency: str = "INR"
    exchange_rate: Decimal = Decimal("1")
    environment: str = ""
    seller: Party = field(default_factory=Party)
    buyer: Party = field(default_factory=Party)
    ship_to: Optional[Party] = None
    dispatch_from: Optional[Party] = None
    place_of_supply: str = ""
    items: List[Item] = field(default_factory=list)
    totals: TaxTotals = field(default_factory=TaxTotals)
    transport: Transport = field(default_factory=Transport)
    export: Optional[ExportDetails] = None
    original_document_number: str = ""     # for credit / debit notes
    original_document_date: Optional[date] = None
    ecommerce_gstin: str = ""


def _party(d: Optional[Dict[str, Any]], name: str) -> Party:
    if not isinstance(d, dict):
        raise ContractError("is required", name)
    return Party(
        gstin=text(d.get("gstin")).upper(), legal_name=text(d.get("legal_name")), trade_name=text(d.get("trade_name")),
        address1=text(d.get("address") or d.get("address1")), address2=text(d.get("address2")), city=text(d.get("city")),
        state_code=text(d.get("state_code")), pincode=text(d.get("pincode")),
    )


def parse_invoice(data: Dict[str, Any]) -> Invoice:
    """Turn the JSON request body into an Invoice. Raises ContractError for malformed input (types, missing structure)."""
    if not isinstance(data, dict):
        raise ContractError("invoice must be a JSON object", "invoice")
    inv = Invoice()
    inv.integration_id = text(data.get("integration_id"))
    inv.sap_database = text(data.get("sap_database"))
    inv.object_type = text(data.get("object_type") or "13")
    try:
        inv.doc_entry = int(data.get("doc_entry"))
    except (TypeError, ValueError):
        raise ContractError("must be an integer", "doc_entry")
    inv.doc_num = int(data.get("doc_num") or 0)
    inv.document_type = text(data.get("document_type") or "INV").upper()
    inv.document_number = text(data.get("document_number"))
    inv.document_date = parse_date(data.get("document_date"), "document_date")
    inv.due_date = parse_date(data["document_due_date"], "document_due_date") if data.get("document_due_date") else None
    inv.supply_type = text(data.get("supply_type") or "B2B").upper()
    inv.reverse_charge = bool(data.get("reverse_charge"))
    inv.currency = text(data.get("currency") or "INR").upper()
    inv.exchange_rate = dec(data.get("exchange_rate"), "exchange_rate", Decimal("1"))
    inv.environment = text(data.get("environment"))
    inv.seller = _party(data.get("seller"), "seller")
    inv.buyer = _party(data.get("buyer"), "buyer")
    if isinstance(data.get("ship_to"), dict):
        inv.ship_to = _party(data["ship_to"], "ship_to")
    if isinstance(data.get("dispatch_from"), dict):
        inv.dispatch_from = _party(data["dispatch_from"], "dispatch_from")
    inv.place_of_supply = text(data.get("place_of_supply"))
    inv.ecommerce_gstin = text(data.get("ecommerce_gstin")).upper()
    inv.original_document_number = text(data.get("original_document_number"))
    if data.get("original_document_date"):
        inv.original_document_date = parse_date(data["original_document_date"], "original_document_date")

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ContractError("at least one item is required", "items")
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            raise ContractError("must be an object", f"items[{i}]")
        p = f"items[{i}]."
        inv.items.append(Item(
            line=int(it.get("line") or i), item_code=text(it.get("item_code")), description=text(it.get("description")),
            hsn_code=text(it.get("hsn_code")), quantity=dec(it.get("quantity"), p + "quantity"), unit=text(it.get("unit")).upper(),
            unit_price=dec(it.get("unit_price"), p + "unit_price"), discount=dec(it.get("discount"), p + "discount"),
            taxable_value=dec(it.get("taxable_value"), p + "taxable_value", None),
            cgst_rate=dec(it.get("cgst_rate"), p + "cgst_rate"), sgst_rate=dec(it.get("sgst_rate"), p + "sgst_rate"),
            igst_rate=dec(it.get("igst_rate"), p + "igst_rate"), cess_rate=dec(it.get("cess_rate"), p + "cess_rate"),
            cgst_amount=dec(it.get("cgst_amount"), p + "cgst_amount"), sgst_amount=dec(it.get("sgst_amount"), p + "sgst_amount"),
            igst_amount=dec(it.get("igst_amount"), p + "igst_amount"), cess_amount=dec(it.get("cess_amount"), p + "cess_amount"),
            other_charges=dec(it.get("other_charges"), p + "other_charges"),
        ))

    t = data.get("tax_totals")
    if not isinstance(t, dict):
        raise ContractError("is required", "tax_totals")
    inv.totals = TaxTotals(
        taxable_value=dec(t.get("taxable_value"), "tax_totals.taxable_value", None), cgst=dec(t.get("cgst"), "tax_totals.cgst"),
        sgst=dec(t.get("sgst"), "tax_totals.sgst"), igst=dec(t.get("igst"), "tax_totals.igst"), cess=dec(t.get("cess"), "tax_totals.cess"),
        discount=dec(t.get("discount"), "tax_totals.discount"), other_charges=dec(t.get("other_charges") or t.get("freight"), "tax_totals.other_charges"),
        round_off=dec(t.get("round_off"), "tax_totals.round_off"), invoice_total=dec(t.get("invoice_total"), "tax_totals.invoice_total", None),
        invoice_total_fc=dec(t.get("invoice_total_fc"), "tax_totals.invoice_total_fc"),
    )

    tr = data.get("transport") or {}
    if not isinstance(tr, dict):
        raise ContractError("must be an object", "transport")
    inv.transport = Transport(
        transporter_id=text(tr.get("transporter_id")).upper(), transporter_name=text(tr.get("transporter_name")),
        vehicle_number=text(tr.get("vehicle_number")).replace(" ", "").upper(), vehicle_type=text(tr.get("vehicle_type") or "Regular"),
        transport_mode=text(tr.get("transport_mode")), distance=dec(tr.get("distance"), "transport.distance"), doc_number=text(tr.get("doc_number")),
        doc_date=parse_date(tr["doc_date"], "transport.doc_date") if tr.get("doc_date") else None,
    )

    ex = data.get("export")
    if isinstance(ex, dict):
        inv.export = ExportDetails(
            country_code=text(ex.get("country_code")).upper(), port_code=text(ex.get("port_code")).upper(), shipping_bill_no=text(ex.get("shipping_bill_no")),
            shipping_bill_date=parse_date(ex["shipping_bill_date"], "export.shipping_bill_date") if ex.get("shipping_bill_date") else None,
            foreign_currency=text(ex.get("foreign_currency")).upper(),
        )
    return inv
