"""Test doubles. No Frappe, no network, no real GSP. Fictional identities only."""

import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

from sap_b1_direct_integration.core import validation as V
from sap_b1_direct_integration.core.service import (GspRejected, GspUncertain, Profile, ProfileRepo, SiteInfo, TxnStore, Gsp)

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def with_checksum(base14: str) -> str:
    total = 0
    for i, ch in enumerate(base14):
        prod = _ALPHABET.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return base14 + _ALPHABET[(36 - total % 36) % 36]


SELLER_GSTIN = with_checksum("27AABCT1234F1Z")          # fictional but checksum-valid
BUYER_GSTIN = with_checksum("29AAACB5678K1Z")
BUYER_B_GSTIN = with_checksum("27AAACD1111A1Z")
TRANSPORTER_GSTIN = with_checksum("27AAACT9999A1Z")
assert V.gstin_checksum_ok(SELLER_GSTIN) and V.GSTIN_RX.match(SELLER_GSTIN), SELLER_GSTIN
assert V.GSTIN_RX.match(BUYER_GSTIN), BUYER_GSTIN


def make_invoice(doc_entry=2624, igst=True, **over):
    taxable = 50000
    inv = {
        "integration_id": "IT1", "sap_database": "DB_ONE", "object_type": "13", "doc_entry": doc_entry, "doc_num": 67, "document_type": "INV",
        "document_number": f"INV/26/{doc_entry}"[:16], "document_date": (date.today() - timedelta(days=1)).isoformat(), "document_due_date": date.today().isoformat(),
        "currency": "INR", "supply_type": "B2B", "place_of_supply": "29" if igst else "27",
        "seller": {"gstin": SELLER_GSTIN, "legal_name": "Seller Ltd", "address": "1 Factory Road", "city": "Thane", "state_code": "27", "pincode": "400601"},
        "buyer": {"gstin": BUYER_GSTIN if igst else BUYER_B_GSTIN, "legal_name": "Buyer Pvt Ltd", "address": "2 Market Street", "city": "Bengaluru", "state_code": "29" if igst else "27", "pincode": "560001" if igst else "400701"},
        "items": [{
            "line": 1, "item_code": "ITEM001", "description": "Plastic closures", "hsn_code": "39235010", "quantity": 100, "unit": "KGS", "unit_price": 500, "taxable_value": taxable,
            "igst_rate": 18 if igst else 0, "igst_amount": 9000 if igst else 0, "cgst_rate": 0 if igst else 9, "sgst_rate": 0 if igst else 9,
            "cgst_amount": 0 if igst else 4500, "sgst_amount": 0 if igst else 4500,
        }],
        "tax_totals": {"taxable_value": taxable, "cgst": 0 if igst else 4500, "sgst": 0 if igst else 4500, "igst": 9000 if igst else 0, "cess": 0, "round_off": 0, "invoice_total": 59000},
        "transport": {"transporter_id": TRANSPORTER_GSTIN, "vehicle_number": "MH04AB1234", "transport_mode": "Road", "distance": 450},
    }
    inv.update(over)
    return inv


class Profiles(ProfileRepo):
    def __init__(self, *profiles):
        self.p = {x.integration_id: x for x in profiles}

    def get(self, iid):
        return self.p.get(iid)


def profile(iid="IT1", db="DB_ONE", gstin=None, env="Sandbox", user="api@site", **kw):
    return Profile(integration_id=iid, enabled=True, sap_database=db, company="Co", seller_gstin=gstin or SELLER_GSTIN, environment=env, api_user=user, **kw)


class Site(SiteInfo):
    def __init__(self, user="api@site", sandbox=True, company_gstin=None, creds=True):
        self._user, self._sandbox, self._cg, self._creds = user, sandbox, company_gstin, creds

    def user(self):
        return self._user

    def sandbox_mode(self):
        return self._sandbox

    def company_gstin(self, company):
        return self._cg

    def has_credentials(self, gstin):
        return self._creds


class MemoryStore(TxnStore):
    """Atomic create (unique reference) like the DocType primary key."""

    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.touched = []          # every DocType-like access, to prove nothing else is used

    def get(self, ref):
        with self.lock:
            r = self.rows.get(ref)
            return dict(r) if r else None

    def create(self, ref, fields):
        with self.lock:
            if ref in self.rows:
                return False
            self.rows[ref] = dict(fields)
            return True

    def update(self, ref, **fields):
        with self.lock:
            self.rows[ref].update(fields)


class FakeGsp(Gsp):
    """
    Mimics the government side: one IRN per (gstin, doc number) - a second generate returns the existing one, like the real IRP (Duplicate IRN).
    Behaviour flags simulate rejections, timeouts before/after processing, slow calls.
    """

    registry = {}                      # shared 'government' state across adapters: (gstin, docno) -> irn record
    ewb_registry = {}

    def __init__(self, gstin="", calls=None):
        self.gstin = gstin
        self.calls = calls if calls is not None else []
        self.behaviour = {}

    # ---- e-Invoice
    def generate_irn(self, payload):
        self.calls.append("generate_irn")
        b = self.behaviour
        if b.get("slow"):
            b["slow"].wait(5)
        if b.get("reject"):
            raise GspRejected(b["reject"], code="2172")
        key = (payload["SellerDtls"]["Gstin"], payload["DocDtls"]["No"])
        if b.get("timeout_before"):
            raise GspUncertain("timeout (request never reached the IRP)")
        rec = FakeGsp.registry.get(key)
        if rec is None:
            n = len(FakeGsp.registry) + 1
            rec = {"irn": f"{n:064x}", "ack_no": 1100000000 + n, "ack_date": "2026-11-09 10:00:00", "signed_invoice": "SIGNED.INV." + str(n), "signed_qr": "SIGNED.QR." + str(n), "status": "ACT", "payload": payload}
            FakeGsp.registry[key] = rec
        if b.get("timeout_after"):
            raise GspUncertain("timeout (the IRP processed the request but the answer was lost)")
        return {k: v for k, v in rec.items() if k != "payload"}

    def get_irn(self, irn):
        self.calls.append("get_irn")
        if self.behaviour.get("status_timeout"):
            raise GspUncertain("timeout")
        for rec in FakeGsp.registry.values():
            if rec["irn"] == irn:
                return {k: v for k, v in rec.items() if k != "payload"}
        raise GspRejected("IRN not found", code="2148")

    def cancel_irn(self, payload):
        self.calls.append("cancel_irn")
        if self.behaviour.get("cancel_reject"):
            raise GspRejected(self.behaviour["cancel_reject"], code="9999")
        if self.behaviour.get("cancel_timeout"):
            raise GspUncertain("timeout")
        if not self.behaviour.get("cancel_noop"):
            for rec in FakeGsp.registry.values():
                if rec["irn"] == payload["Irn"]:
                    rec["status"] = "CNL"
        return {"irn": payload["Irn"]}

    # ---- e-Way Bill
    def generate_ewb(self, payload, with_irn):
        self.calls.append("generate_ewb_irn" if with_irn else "generate_ewb")
        b = self.behaviour
        if b.get("slow"):
            b["slow"].wait(5)
        if b.get("ewb_reject"):
            raise GspRejected(b["ewb_reject"], code="604")
        doc_no = payload.get("docNo") or payload.get("Irn")
        if b.get("timeout_before"):
            raise GspUncertain("timeout")
        rec = FakeGsp.ewb_registry.get(doc_no)
        if rec is None:
            n = len(FakeGsp.ewb_registry) + 1
            rec = {"ewb_no": str(341000000000 + n), "ewb_date": "2026-11-09 11:00:00", "ewb_valid": "2026-11-10 23:59:00", "status": "ACT", "vehicle_no": payload.get("vehicleNo") or payload.get("VehNo"), "docNo": doc_no}
            FakeGsp.ewb_registry[doc_no] = rec
        if b.get("timeout_after"):
            raise GspUncertain("timeout (answer lost)")
        return dict(rec)

    def get_ewb(self, ewb_no):
        self.calls.append("get_ewb")
        if self.behaviour.get("status_timeout"):
            raise GspUncertain("timeout")
        for rec in FakeGsp.ewb_registry.values():
            if rec["ewb_no"] == ewb_no:
                return dict(rec)
        raise GspRejected("not found", code="320")

    def find_ewb(self, doc_no, doc_date, from_gstin):
        self.calls.append("find_ewb")
        if self.behaviour.get("status_timeout"):
            raise GspUncertain("timeout")
        rec = FakeGsp.ewb_registry.get(doc_no)
        return dict(rec) if rec else None

    def cancel_ewb(self, payload):
        self.calls.append("cancel_ewb")
        for rec in FakeGsp.ewb_registry.values():
            if str(rec["ewb_no"]) == str(payload["ewbNo"]) and not self.behaviour.get("cancel_noop"):
                rec["status"] = "CNL"
        return {"ewb_no": payload["ewbNo"]}

    def update_vehicle(self, payload):
        self.calls.append("update_vehicle")
        if self.behaviour.get("timeout_before"):
            raise GspUncertain("timeout")
        for rec in FakeGsp.ewb_registry.values():
            if str(rec["ewb_no"]) == str(payload["ewbNo"]):
                rec["vehicle_no"] = payload["vehicleNo"]
        if self.behaviour.get("timeout_after"):
            raise GspUncertain("timeout (answer lost)")
        return {"ewb_valid": "2026-11-11 23:59:00", "updated": "2026-11-09 12:00:00"}

    def update_transporter(self, payload):
        self.calls.append("update_transporter")
        return {"updated": "2026-11-09 12:00:00"}


class Clock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 11, 9, 12, 0, 0)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)
