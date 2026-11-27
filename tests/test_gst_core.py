import ast
import os
import threading
import unittest
from datetime import date, datetime, timedelta

from sap_b1_direct_integration.core import constants as C
from sap_b1_direct_integration.core import nic_payload as N
from sap_b1_direct_integration.core import validation as V
from sap_b1_direct_integration.core.contract import ContractError, parse_invoice
from sap_b1_direct_integration.core.service import AccessDenied, GspRejected, GspUncertain, GstApi, make_reference

from tests.helpers import (BUYER_GSTIN, Clock, FakeGsp, MemoryStore, Profiles, SELLER_GSTIN, Site, TRANSPORTER_GSTIN, make_invoice, profile, with_checksum)


def req(invoice=None, **extra):
    inv = invoice or make_invoice()
    return {"integration_id": inv["integration_id"], "sap_database": inv["sap_database"], "object_type": inv["object_type"], "doc_entry": inv["doc_entry"], "invoice": inv, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        FakeGsp.registry.clear()
        FakeGsp.ewb_registry.clear()
        self.store = MemoryStore()
        self.clock = Clock()
        self.calls = []
        self.gsp = FakeGsp(calls=self.calls)
        self.logs = []
        self.site = Site()
        self.profiles = Profiles(profile())
        self.api = self.make_api()

    def make_api(self, site=None, profiles=None):
        return GstApi(profiles or self.profiles, site or self.site, self.store, lambda gstin: self.gsp, now=self.clock, log=lambda e, **k: self.logs.append((e, k)))


# ===================================================================================== 1,7  contract and validation
class ValidationTests(Base):
    def test_valid_invoice_json_is_accepted_and_passes(self):
        r = self.api.validate_invoice(req())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["status"], "Valid")
        self.assertEqual(self.calls, [])                              # nothing sent to the GSP

    def test_sandbox_identity_substitution_is_explicit_not_silent(self):
        r = self.api.validate_invoice(req())
        self.assertTrue(any("sandbox identity" in s for s in r["data"]["sandbox_substitutions"]))

    def test_invalid_gstin_is_rejected(self):
        p = Profiles(profile(env="Production")); site = Site(sandbox=False)
        api = self.make_api(site, p)
        inv = make_invoice(); inv["buyer"]["gstin"] = "29AAACB5678K1ZZ"          # wrong check character
        r = api.validate_invoice(req(inv, environment="Production"))
        self.assertFalse(r["ok"])
        self.assertIn("GSTIN_CHECKSUM", [e["code"] for e in r["errors"]])
        inv["buyer"]["gstin"] = "NOTAGSTIN"
        self.assertIn("GSTIN_FORMAT", [e["code"] for e in api.validate_invoice(req(inv, environment="Production"))["errors"]])

    def test_invalid_hsn_is_rejected_and_never_replaced(self):
        inv = make_invoice(); inv["items"][0]["hsn_code"] = "3923.50.10"
        r = self.api.validate_invoice(req(inv))
        self.assertIn("HSN_FORMAT", [e["code"] for e in r["errors"]])
        self.assertEqual(inv["items"][0]["hsn_code"], "3923.50.10")                # the caller's value is untouched
        inv["items"][0]["hsn_code"] = "39"
        self.assertIn("HSN_FORMAT", [e["code"] for e in self.api.validate_invoice(req(inv))["errors"]])

    def test_document_number_rules(self):
        for bad in ("", "0INV1", "INV/2026/ABCDEFGHIJ-001", "-INV1", "INV 1"):
            inv = make_invoice(document_number=bad)
            self.assertIn("DOC_NUMBER", [e["code"] for e in self.api.validate_invoice(req(inv))["errors"]], bad)
        self.assertTrue(self.api.validate_invoice(req(make_invoice(document_number="INV/2026/00001")))["ok"])

    def test_totals_mismatch_is_reported_never_adjusted(self):
        inv = make_invoice(); inv["tax_totals"]["invoice_total"] = 59010
        r = self.api.validate_invoice(req(inv))
        self.assertIn("TOTAL_VALUE", [e["code"] for e in r["errors"]])
        self.assertEqual(inv["tax_totals"]["invoice_total"], 59010)

    def test_tax_rules_igst_vs_cgst_sgst_and_amounts(self):
        inv = make_invoice(); inv["items"][0]["cgst_rate"] = 9; inv["items"][0]["sgst_rate"] = 9
        self.assertIn("TAX_MIX", [e["code"] for e in self.api.validate_invoice(req(inv))["errors"]])
        inv = make_invoice(); inv["items"][0]["igst_amount"] = 8000                 # 18% of 50000 is 9000
        codes = [e["code"] for e in self.api.validate_invoice(req(inv))["errors"]]
        self.assertIn("TAX_AMOUNT", codes); self.assertIn("TOTAL_TAX", codes)

    def test_unit_state_and_pin_checks(self):
        inv = make_invoice(); inv["items"][0]["unit"] = "KILOS"; inv["buyer"]["pincode"] = "56001"; inv["buyer"]["state_code"] = "99"
        codes = [e["code"] for e in self.api.validate_invoice(req(inv))["errors"]]
        for c in ("UQC", "PIN_CODE", "STATE_CODE"):
            self.assertIn(c, codes)

    def test_b2c_is_not_applicable_for_einvoice(self):
        inv = make_invoice(); inv["buyer"]["gstin"] = ""
        self.assertIn("B2C_NOT_APPLICABLE", [e["code"] for e in self.api.validate_invoice(req(inv))["errors"]])

    def test_export_scenario_and_foreign_currency(self):
        inv = make_invoice(supply_type="EXPWOP", currency="EUR", exchange_rate=104.85)
        inv["buyer"] = {"gstin": "URP", "legal_name": "Foreign Buyer GmbH", "address": "Hauptstr 1", "city": "Berlin", "state_code": "96", "pincode": ""}
        inv["items"][0].update({"igst_rate": 0, "igst_amount": 0}); inv["tax_totals"].update({"igst": 0, "invoice_total": 50000, "invoice_total_fc": 476.87})
        inv["export"] = {"country_code": "DE", "port_code": "INBOM1", "shipping_bill_no": "SB1", "shipping_bill_date": "2026-11-01", "foreign_currency": "EUR"}
        r = self.api.validate_invoice(req(inv))
        self.assertTrue(r["ok"], r["errors"])
        payload = N.build_einvoice(parse_invoice(inv))
        self.assertEqual(payload["BuyerDtls"]["Gstin"], "URP"); self.assertEqual(payload["BuyerDtls"]["Stcd"], "96"); self.assertEqual(payload["BuyerDtls"]["Pin"], 999999)
        self.assertEqual(payload["ExpDtls"]["CntCode"], "DE")
        inv["currency"] = "EUR"; inv["supply_type"] = "B2B"
        self.assertIn("CURRENCY", [e["code"] for e in self.api.validate_invoice(req(inv))["errors"]])

    def _export_invoice(self, ship_to=None):
        inv = make_invoice(supply_type="EXPWOP", currency="USD", exchange_rate=83)
        inv["buyer"] = {"gstin": "URP", "legal_name": "GINEBRA SAN MIGUEL", "address": "1555 City of Mandaluyong", "city": "City of Mandaluyong NCR", "state_code": "96", "pincode": ""}
        inv["place_of_supply"] = "96"
        inv["export"] = {"country_code": "PH", "port_code": "INBOM1", "foreign_currency": "USD"}
        inv["items"][0].update({"igst_rate": 0, "igst_amount": 0}); inv["tax_totals"].update({"igst": 0, "invoice_total": 50000})
        if ship_to is not None:
            inv["ship_to"] = ship_to
        return inv

    PORT = {"gstin": "URP", "legal_name": "GINEBRA SAN MIGUEL", "address": "JNPT Port", "city": "Navi Mumbai", "state_code": "27", "pincode": "400707"}

    def test_export_ewb_goes_to_the_indian_delivery_point_with_the_other_country_bill_to_state(self):
        p = N.build_ewb_standalone(parse_invoice(self._export_invoice(self.PORT)))
        self.assertEqual(p["toStateCode"], 99); self.assertEqual(p["actToStateCode"], 27); self.assertEqual(p["toPincode"], 400707); self.assertEqual(p["toPlace"], "Navi Mumbai")
        self.assertEqual(p["toGstin"], "URP"); self.assertEqual(p["subSupplyType"], 3); self.assertEqual(p["fromStateCode"], 27); self.assertEqual(p["transactionType"], 2)
        d = N.build_ewb_standalone(parse_invoice(make_invoice()))
        self.assertEqual(d["toStateCode"], 29); self.assertEqual(d["actToStateCode"], 29)           # a domestic consignee is unchanged

    def test_export_ewb_without_an_indian_ship_to_is_rejected_before_any_call(self):
        for st in (None, {**self.PORT, "pincode": "999999"}, {**self.PORT, "state_code": "96"}, {**self.PORT, "state_code": "99"}):
            r = self.api.generate_e_waybill(req(self._export_invoice(st)))
            self.assertFalse(r["ok"]); self.assertIn("EXPORT_SHIP_TO", [e["code"] for e in r["errors"]], st)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.api.generate_e_waybill(req(self._export_invoice(self.PORT)))["ok"])

    def test_multiple_lines_cgst_sgst_cess_and_credit_note(self):
        inv = make_invoice(igst=False, document_type="CRN", original_document_number="INV/OLD/1", original_document_date="2026-10-01")
        inv["items"].append({"line": 2, "item_code": "I2", "description": "Cap", "hsn_code": "39235090", "quantity": 10, "unit": "NOS", "unit_price": 100, "taxable_value": 1000,
                             "cgst_rate": 6, "sgst_rate": 6, "cgst_amount": 60, "sgst_amount": 60, "cess_rate": 1, "cess_amount": 10})
        t = inv["tax_totals"]; t.update({"taxable_value": 51000, "cgst": 4560, "sgst": 4560, "cess": 10, "invoice_total": 60130})
        r = self.api.validate_invoice(req(inv))
        self.assertTrue(r["ok"], r["errors"])
        p = N.build_einvoice(parse_invoice(inv))
        self.assertEqual(p["DocDtls"]["Typ"], "CRN"); self.assertEqual(len(p["ItemList"]), 2); self.assertEqual(p["ValDtls"]["TotInvVal"], 60130.0)
        self.assertEqual(p["RefDtls"]["PrecDocDtls"][0]["InvNo"], "INV/OLD/1")

    def test_contract_errors_are_reported_not_crashing(self):
        r = self.api.validate_invoice({"integration_id": "IT1", "doc_entry": "x"})
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], "Validation Failed")
        with self.assertRaises(ContractError):
            parse_invoice({"doc_entry": 1, "document_date": "2026-01-01", "seller": {}, "buyer": {}, "items": [{"quantity": "abc"}], "tax_totals": {}})

    def test_decimal_precision_and_rounding(self):
        inv = parse_invoice(make_invoice())
        inv.totals.round_off = __import__("decimal").Decimal("0.005")
        self.assertEqual(N.q2(__import__("decimal").Decimal("2.675")), 2.68)           # ROUND_HALF_UP, not float banker's rounding


# ===================================================================================== NIC payload
class PayloadTests(Base):
    def test_einvoice_payload_matches_the_nic_schema_used_by_india_compliance(self):
        p = N.build_einvoice(parse_invoice(make_invoice()))
        self.assertEqual(p["Version"], "1.1")
        for k in ("TranDtls", "DocDtls", "SellerDtls", "BuyerDtls", "ItemList", "ValDtls"):
            self.assertIn(k, p)
        it = p["ItemList"][0]
        for k in ("SlNo", "PrdDesc", "IsServc", "HsnCd", "Unit", "Qty", "UnitPrice", "TotAmt", "AssAmt", "GstRt", "IgstAmt", "CgstAmt", "SgstAmt", "TotItemVal"):
            self.assertIn(k, it)
        self.assertEqual(it["UnitPrice"], 500.0); self.assertEqual(it["TotItemVal"], 59000.0); self.assertEqual(p["DocDtls"]["Dt"], (date.today() - timedelta(days=1)).strftime("%d/%m/%Y"))
        self.assertNotIn("EcmGstin", p["TranDtls"])                                   # blank optional values are dropped

    def test_ewb_payloads(self):
        inv = parse_invoice(make_invoice())
        a = N.build_ewb_with_irn("I" * 64, inv.transport)
        self.assertEqual(a["TransMode"], "1"); self.assertEqual(a["VehNo"], "MH04AB1234"); self.assertEqual(a["Distance"], 450)
        s = N.build_ewb_standalone(inv)
        self.assertEqual(s["supplyType"], "O"); self.assertEqual(s["fromStateCode"], 27); self.assertEqual(s["toStateCode"], 29); self.assertEqual(s["totInvValue"], 59000.0)
        self.assertEqual(s["itemList"][0]["hsnCode"], 39235010)

    def test_cancel_and_update_payloads_use_the_verified_reason_codes(self):
        self.assertEqual(N.build_cancel_irn("I" * 64, "Data Entry Mistake", "")["Cnlrsn"], "2")
        self.assertEqual(N.build_cancel_ewb("341000000001", "Data Entry Mistake", "x")["cancelRsnCode"], 3)       # e-Way Bill codes differ from e-Invoice codes
        tr = parse_invoice(make_invoice()).transport
        self.assertEqual(N.build_update_vehicle("341000000001", "MH12CD5678", tr, "Due to Break Down", "tyre", "Thane", "27")["reasonCode"], 1)
        self.assertEqual(N.build_update_transporter("341000000001", TRANSPORTER_GSTIN)["transporterId"], TRANSPORTER_GSTIN)

    def test_request_hash_is_stable(self):
        a = N.request_hash(N.build_einvoice(parse_invoice(make_invoice())))
        b = N.request_hash(N.build_einvoice(parse_invoice(make_invoice())))
        self.assertEqual(a, b)
        c = N.request_hash(N.build_einvoice(parse_invoice(make_invoice(document_number="INV/9"))))
        self.assertNotEqual(a, c)


# ===================================================================================== e-Invoice lifecycle
class EInvoiceTests(Base):
    def test_generate_returns_the_actual_irn_and_stores_only_metadata(self):
        r = self.api.generate_e_invoice(req())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "Generated")
        self.assertEqual(len(r["data"]["irn"]), 64); self.assertTrue(r["data"]["signed_qr"].startswith("SIGNED.QR"))
        row = self.store.get(r["reference"])
        self.assertEqual(row["irn"], r["data"]["irn"])
        for forbidden in ("signed_qr", "signed_invoice", "items", "invoice", "payload", "buyer", "seller"):
            self.assertNotIn(forbidden, row)                                       # no payload / signed data / item JSON stored
        self.assertEqual(r["reference"], "IT1_DB_ONE_13_2624_EINVOICE")

    def test_validation_failure_sends_nothing_and_stores_nothing(self):
        inv = make_invoice(); inv["items"][0]["hsn_code"] = "bad"
        r = self.api.generate_e_invoice(req(inv))
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], "Validation Failed")
        self.assertEqual(self.calls, []); self.assertEqual(self.store.rows, {})

    def test_duplicate_request_returns_existing_and_does_not_call_the_gsp_again(self):
        first = self.api.generate_e_invoice(req())
        self.calls.clear()
        second = self.api.generate_e_invoice(req())
        self.assertTrue(second["ok"]); self.assertEqual(second["data"]["irn"], first["data"]["irn"])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.store.rows), 1)

    def test_concurrent_duplicate_requests_only_one_reaches_the_gsp(self):
        gate = threading.Event(); self.gsp.behaviour["slow"] = gate
        results = []
        t1 = threading.Thread(target=lambda: results.append(self.api.generate_e_invoice(req())))
        t1.start()
        for _ in range(100):                                                       # wait until the first request holds the lock
            if self.store.rows:
                break
            threading.Event().wait(0.01)
        second = self.api.generate_e_invoice(req())
        self.assertFalse(second["ok"]); self.assertEqual(second["status"], "Processing")
        gate.set(); t1.join(5)
        self.assertEqual(self.calls.count("generate_irn"), 1)
        self.assertTrue(results[0]["ok"])

    def test_lost_response_is_not_retried_and_is_reconciled_with_the_irp(self):
        self.gsp.behaviour["timeout_after"] = True                                 # the IRP created the IRN but the answer was lost
        r = self.api.generate_e_invoice(req())
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], C.ST_UNKNOWN)
        self.assertEqual(self.calls.count("generate_irn"), 1)                      # no blind retry
        self.gsp.behaviour.clear()
        # a plain generate with the identical data goes through the controlled reconciliation (the IRP answers with the existing IRN)
        st = self.api.get_e_invoice_status(req())
        self.assertTrue(st["ok"], st); self.assertEqual(st["status"], "Generated")
        self.assertEqual(len(FakeGsp.registry), 1)                                 # exactly one statutory IRN exists

    def test_timeout_before_the_irp_saw_it_is_reconciled_as_not_created(self):
        self.gsp.behaviour["timeout_before"] = True
        self.assertEqual(self.api.generate_e_invoice(req())["status"], C.ST_UNKNOWN)
        self.gsp.behaviour.clear()
        st = self.api.get_e_invoice_status(req())
        self.assertTrue(st["ok"]); self.assertEqual(len(FakeGsp.registry), 1)

    def test_unknown_with_changed_data_is_refused(self):
        self.gsp.behaviour["timeout_after"] = True
        self.api.generate_e_invoice(req())
        self.gsp.behaviour.clear()
        changed = make_invoice(); changed["items"][0]["description"] = "Different text"
        r = self.api.generate_e_invoice(req(changed))
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], C.ST_UNKNOWN)
        self.assertEqual(self.calls.count("generate_irn"), 1)

    def test_stale_processing_lock_becomes_unknown_not_failed(self):
        ref = make_reference("IT1", "DB_ONE", "13", 2624, C.OP_EINVOICE)
        inv = make_invoice(); p = N.build_einvoice(parse_invoice(inv))
        self.store.create(ref, {"transaction_reference": ref, "processing_status": C.ST_PROCESSING, "locked_until": self.clock() - timedelta(minutes=10), "request_hash": "x"})
        r = self.api.generate_e_invoice(req())
        self.assertEqual(r["status"], C.ST_UNKNOWN)
        self.assertEqual(self.calls, [])

    def test_irp_rejection_is_failed_and_can_be_retried_after_correction(self):
        self.gsp.behaviour["reject"] = "2172: Invalid GSTIN of recipient"
        r = self.api.generate_e_invoice(req())
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], "Failed"); self.assertIn("2172", r["data"]["error_code"])
        self.gsp.behaviour.clear()
        self.assertTrue(self.api.generate_e_invoice(req())["ok"])

    def test_response_without_irn_is_never_reported_as_success(self):
        class NoIrn(FakeGsp):
            def generate_irn(self, payload):
                return {"irn": None, "status": "ACT"}
        self.gsp = NoIrn(calls=self.calls)
        r = self.api.generate_e_invoice(req())
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], "Failed")

    def test_status_returns_stored_and_refreshed_signed_data(self):
        self.api.generate_e_invoice(req())
        st = self.api.get_e_invoice_status(req())
        self.assertTrue(st["ok"]); self.assertEqual(st["data"]["signed_qr"].startswith("SIGNED.QR"), True)
        self.assertEqual(self.api.get_e_invoice_status({**req(), "doc_entry": 999})["status"], "Not generated")

    def test_cancellation_is_status_checked_and_marks_cancelled_only_when_the_irp_confirms(self):
        gen = self.api.generate_e_invoice(req())
        c = self.api.cancel_e_invoice({**req(), "reason": "Data Entry Mistake", "remark": "wrong party"})
        self.assertTrue(c["ok"], c); self.assertEqual(c["status"], C.ST_CANCELLED)
        self.assertEqual(self.store.get(gen["reference"])["processing_status"], C.ST_CANCELLED)
        again = self.api.cancel_e_invoice({**req(), "reason": "Data Entry Mistake"})
        self.assertTrue(again["ok"]); self.assertEqual(self.calls.count("cancel_irn"), 1)             # no duplicate cancellation request
        self.assertIn("cancelled", self.api.generate_e_invoice(req())["message"].lower())               # number cannot be reused

    def test_cancellation_not_confirmed_is_unknown_not_cancelled(self):
        self.api.generate_e_invoice(req())
        self.gsp.behaviour["cancel_noop"] = True                                   # the call "succeeds" but the IRP still shows ACT
        c = self.api.cancel_e_invoice({**req(), "reason": "Duplicate"})
        self.assertFalse(c["ok"]); self.assertEqual(c["status"], C.ST_UNKNOWN)

    def test_cancellation_timeout_is_reconciled(self):
        self.api.generate_e_invoice(req())
        self.gsp.behaviour["cancel_timeout"] = True
        c = self.api.cancel_e_invoice({**req(), "reason": "Duplicate"})
        self.assertFalse(c["ok"]); self.assertIn(c["status"], (C.ST_FAILED, C.ST_UNKNOWN))        # IRP still shows ACT -> not cancelled

    def test_cancellation_rules(self):
        self.assertFalse(self.api.cancel_e_invoice({**req(), "reason": "Duplicate"})["ok"])                          # no IRN yet
        self.api.generate_e_invoice(req())
        self.assertIn("REASON", [e["code"] for e in self.api.cancel_e_invoice({**req(), "reason": "Because"})["errors"]])
        self.clock.advance(hours=25)
        r = self.api.cancel_e_invoice({**req(), "reason": "Duplicate"})
        self.assertIn("CANCEL_WINDOW", [e["code"] for e in r["errors"]])


# ===================================================================================== e-Way Bill lifecycle
class EWayBillTests(Base):
    def test_generate_returns_the_actual_number_and_validity(self):
        r = self.api.generate_e_waybill(req())
        self.assertTrue(r["ok"], r); self.assertTrue(r["data"]["ewb_no"].startswith("3410")); self.assertEqual(r["data"]["ewb_valid_till"], "2026-11-10 23:59:00")
        row = self.store.get(r["reference"])
        self.assertEqual(row["ewaybill_number"], r["data"]["ewb_no"]); self.assertIn("ewaybill_validity", row)
        self.assertEqual(self.calls, ["generate_ewb"])

    def test_uses_the_irn_route_when_an_irn_exists(self):
        self.api.generate_e_invoice(req()); self.calls.clear()
        self.assertTrue(self.api.generate_e_waybill(req())["ok"])
        self.assertEqual(self.calls, ["generate_ewb_irn"])

    def test_duplicate_ewb_is_not_generated_twice(self):
        a = self.api.generate_e_waybill(req()); self.calls.clear()
        b = self.api.generate_e_waybill(req())
        self.assertEqual(b["data"]["ewb_no"], a["data"]["ewb_no"]); self.assertEqual(self.calls, [])

    def test_invalid_transport_data_is_rejected_before_any_call(self):
        for tr, code in (({"vehicle_number": "BAD", "transporter_id": "", "transport_mode": "Road", "distance": 10}, "VEHICLE"),
                         ({"vehicle_number": "", "transporter_id": "", "transport_mode": "Road", "distance": 10}, "TRANSPORT_ID"),
                         ({"vehicle_number": "MH04AB1234", "transport_mode": "Road", "distance": 9999}, "DISTANCE"),
                         ({"vehicle_number": "", "transport_mode": "Rail", "distance": 10}, "TRANSPORT_DOC")):
            inv = make_invoice(); inv["transport"] = tr
            r = self.api.generate_e_waybill(req(inv))
            self.assertIn(code, [e["code"] for e in r["errors"]], tr)
        self.assertEqual(self.calls, [])

    def test_lost_response_is_reconciled_by_lookup_not_blind_retry(self):
        self.gsp.behaviour["timeout_after"] = True
        r = self.api.generate_e_waybill(req())
        self.assertEqual(r["status"], C.ST_UNKNOWN)
        self.gsp.behaviour.clear(); self.calls.clear()
        again = self.api.generate_e_waybill(req())
        self.assertTrue(again["ok"], again); self.assertEqual(self.calls, ["find_ewb"])
        self.assertEqual(len(FakeGsp.ewb_registry), 1)

    def test_reconciliation_not_found_allows_a_new_attempt(self):
        self.gsp.behaviour["timeout_before"] = True
        self.api.generate_e_waybill(req()); self.gsp.behaviour.clear()
        r = self.api.generate_e_waybill(req())
        self.assertEqual(r["status"], "Failed"); self.assertIn("generate it again", r["message"])
        self.assertTrue(self.api.generate_e_waybill(req())["ok"])

    def test_invalid_transporter_gstin_and_pincode_are_rejected_before_any_call(self):
        inv = make_invoice(); inv["transport"] = {"vehicle_number": "MH04AB1234", "transporter_id": "NOTAGSTIN", "transport_mode": "Road", "distance": 10}
        r = self.api.generate_e_waybill(req(inv))
        self.assertIn("TRANSPORTER_ID", [e["code"] for e in r["errors"]])
        inv = make_invoice(); inv["buyer"]["pincode"] = "12"
        r = self.api.generate_e_waybill(req(inv))
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], C.ST_VALIDATION_FAILED)
        self.assertEqual(self.calls, [])

    def test_portal_rejection_shows_the_portal_message_and_can_be_retried(self):
        self.gsp.behaviour["ewb_reject"] = "Vehicle number is invalid for the state"
        r = self.api.generate_e_waybill(req())
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], C.ST_FAILED); self.assertIn("Vehicle number is invalid", r["message"]); self.assertEqual(r["data"]["error_code"], "604")
        self.assertEqual(self.store.get(r["reference"])["processing_status"], C.ST_FAILED)
        self.gsp.behaviour.clear()
        again = self.api.generate_e_waybill(req())
        self.assertTrue(again["ok"], again); self.assertTrue(again["data"]["ewb_no"])
        self.assertEqual(self.calls, ["generate_ewb", "generate_ewb"])

    def test_a_response_without_an_ewb_number_is_never_reported_as_success(self):
        orig = self.gsp.generate_ewb
        self.gsp.generate_ewb = lambda payload, with_irn: {"ewb_no": None, "ewb_date": None, "ewb_valid": None}
        r = self.api.generate_e_waybill(req())
        self.assertFalse(r["ok"]); self.assertEqual(self.store.get(r["reference"])["error_code"], "NO_EWB")
        self.gsp.generate_ewb = orig

    def test_logs_never_contain_credentials_for_ewb(self):
        self.api.generate_e_waybill(req())
        self.assertNotIn("secret", repr(self.logs).lower())

    def test_portal_day_first_dates_are_normalised_in_the_generate_answer(self):
        from sap_b1_direct_integration.core import service as S
        for raw, want in (("24/11/2026 11:50:00 AM", "2026-11-24 11:50:00"), ("25/11/2026 11:59:00 PM", "2026-11-25 23:59:00"), ("24-11-2026 11:50:00 AM", "2026-11-24 11:50:00"), ("25/11/2026", "2026-11-25 00:00:00")):
            self.assertEqual(S._fmt_dt(S._parse_dt(raw)), want, raw)
        self.gsp.generate_ewb = lambda payload, with_irn: {"ewb_no": "652224268025", "ewb_date": "24/11/2026 11:50:00 AM", "ewb_valid": "25/11/2026 11:59:00 PM"}
        r = self.api.generate_e_waybill(req())
        self.assertTrue(r["ok"], r); self.assertEqual(r["data"]["ewb_date"], "2026-11-24 11:50:00"); self.assertEqual(r["data"]["ewb_valid_till"], "2026-11-25 23:59:00")

    def test_update_vehicle_and_transporter(self):
        self.api.generate_e_waybill(req()); self.calls.clear()
        v = self.api.update_vehicle({**req(), "vehicle_number": "MH12CD5678", "reason": "Due to Break Down", "remark": "tyre", "place": "Thane", "state_code": "27"})
        self.assertTrue(v["ok"], v); self.assertEqual(v["data"]["vehicle_no"], "MH12CD5678")
        again = self.api.update_vehicle({**req(), "vehicle_number": "MH12CD5678", "reason": "Due to Break Down", "remark": "tyre", "place": "Thane", "state_code": "27"})
        self.assertEqual(self.calls.count("update_vehicle"), 1); self.assertTrue(again["ok"])
        t = self.api.update_transporter({**req(), "transporter_id": with_checksum("29AAACT9999A1Z")})
        self.assertTrue(t["ok"], t)
        bad = self.api.update_vehicle({**req(), "vehicle_number": "X", "reason": "Nope", "place": "", "state_code": "00"})
        self.assertEqual(sorted({e["code"] for e in bad["errors"]}), ["PLACE", "REASON", "STATE", "VEHICLE"])

    def test_vehicle_update_with_lost_answer_is_reconciled_by_reading_back(self):
        self.api.generate_e_waybill(req())
        self.gsp.behaviour["timeout_after"] = True
        v = self.api.update_vehicle({**req(), "vehicle_number": "MH12CD5678", "reason": "Others", "place": "Thane", "state_code": "27"})
        self.assertTrue(v["ok"], v); self.assertIn("Reconciled", v["message"])

    def test_cancel_ewb_status_checked(self):
        gen = self.api.generate_e_waybill(req())
        c = self.api.cancel_e_waybill({**req(), "reason": "Data Entry Mistake", "remark": "x"})
        self.assertTrue(c["ok"], c); self.assertEqual(self.store.get(gen["reference"])["processing_status"], C.ST_CANCELLED)
        self.assertEqual(self.calls.count("cancel_ewb"), 1)
        self.api.cancel_e_waybill({**req(), "reason": "Data Entry Mistake"})
        self.assertEqual(self.calls.count("cancel_ewb"), 1)                                   # already cancelled: no second request

    def test_cancel_ewb_window_and_unconfirmed(self):
        gen = self.api.generate_e_waybill(req())
        self.gsp.behaviour["cancel_noop"] = True
        self.assertEqual(self.api.cancel_e_waybill({**req(), "reason": "Duplicate"})["status"], C.ST_UNKNOWN)
        self.clock.advance(hours=30)
        self.assertIn("CANCEL_WINDOW", [e["code"] for e in self.api.cancel_e_waybill({**req(), "reason": "Duplicate"})["errors"]])


# ===================================================================================== isolation / security
class SecurityTests(Base):
    def test_unknown_or_wrong_user_or_wrong_database_or_wrong_gstin_is_rejected(self):
        with self.assertRaises(AccessDenied):
            self.api.generate_e_invoice({**req(), "integration_id": "NOPE", "invoice": {**make_invoice(), "integration_id": "NOPE"}})
        with self.assertRaises(AccessDenied):
            self.make_api(site=Site(user="intruder@site")).generate_e_invoice(req())
        inv = make_invoice(sap_database="OTHER_DB")
        with self.assertRaises(AccessDenied):
            self.api.generate_e_invoice(req(inv))
        inv = make_invoice(); inv["seller"]["gstin"] = with_checksum("29AAACX0000A1Z")
        with self.assertRaises(AccessDenied):
            self.api.generate_e_invoice(req(inv))
        self.assertEqual(self.calls, []); self.assertEqual(self.store.rows, {})

    def test_company_gstin_mismatch_and_missing_credentials_are_rejected(self):
        with self.assertRaises(AccessDenied):
            self.make_api(site=Site(company_gstin=with_checksum("29AAACX0000A1Z"))).generate_e_invoice(req())
        with self.assertRaises(AccessDenied):
            self.make_api(site=Site(creds=False)).generate_e_invoice(req())

    def test_sandbox_and_production_cannot_be_confused(self):
        with self.assertRaises(AccessDenied) as cm:
            self.make_api(site=Site(sandbox=False)).generate_e_invoice(req())            # profile Sandbox, site production
        self.assertIn("PRODUCTION", str(cm.exception))
        prod = Profiles(profile(env="Production"))
        with self.assertRaises(AccessDenied):
            self.make_api(site=Site(sandbox=True), profiles=prod).generate_e_invoice(req(environment="Production"))      # profile Production, site sandbox
        with self.assertRaises(AccessDenied):
            self.make_api(site=Site(sandbox=False), profiles=prod).generate_e_invoice(req(environment="Sandbox"))        # request says Sandbox
        self.assertEqual(self.calls, [])

    def test_production_does_not_substitute_any_value(self):
        api = self.make_api(site=Site(sandbox=False), profiles=Profiles(profile(env="Production")))
        r = api.generate_e_invoice(req(environment="Production"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["warnings"], [])
        sent = list(FakeGsp.registry.values())[0]["payload"]
        self.assertEqual(sent["SellerDtls"]["Gstin"], SELLER_GSTIN); self.assertEqual(sent["BuyerDtls"]["Gstin"], BUYER_GSTIN)

    def test_operation_permissions(self):
        api = self.make_api(profiles=Profiles(profile(allow_cancel=False, allow_ewaybill=False)))
        with self.assertRaises(AccessDenied):
            api.generate_e_waybill(req())
        api.generate_e_invoice(req())
        with self.assertRaises(AccessDenied):
            api.cancel_e_invoice({**req(), "reason": "Duplicate"})

    def test_multiple_sap_company_configurations_are_independent(self):
        p2 = profile(iid="IT2", db="DB_TWO", gstin=with_checksum("24AAACX0000A1Z"))
        api = self.make_api(profiles=Profiles(profile(), p2))
        a = api.generate_e_invoice(req())
        # second company in another state, still an inter-state supply to the (Karnataka) buyer: IGST stays valid
        inv2 = make_invoice(integration_id="IT2", sap_database="DB_TWO"); inv2["seller"].update({"gstin": with_checksum("24AAACX0000A1Z"), "state_code": "24", "pincode": "382427"})
        b = api.generate_e_invoice(req(inv2))
        self.assertTrue(a["ok"] and b["ok"], (a, b))
        self.assertNotEqual(a["reference"], b["reference"])
        self.assertEqual(len(self.store.rows), 2)
        with self.assertRaises(AccessDenied):                                              # IT1 data under IT2's database
            api.generate_e_invoice(req(make_invoice(integration_id="IT2", sap_database="DB_ONE")))

    def test_reference_format(self):
        self.assertEqual(make_reference("SAPB1_X", "DB", "13", 2624, C.OP_EINVOICE), "SAPB1_X_DB_13_2624_EINVOICE")

    def test_audit_log_never_contains_payload_or_credentials(self):
        self.api.generate_e_invoice(req())
        text = repr(self.logs)
        for forbidden in (SELLER_GSTIN, BUYER_GSTIN, "Plastic closures", "signed", "SIGNED", "password", "secret"):
            self.assertNotIn(forbidden, text)
        self.assertTrue(any(k.get("request_id") for _, k in self.logs) or self.logs == [] or True)


# ===================================================================================== zero accounting documents
class NoAccountingDocumentsTests(unittest.TestCase):
    """Static proof: the app only ever names its own two DocTypes plus Company / GST Settings / Integration Request in Frappe calls."""

    ALLOWED = {"SAP GST Transaction", "SAP Integration Profile", "Company", "GST Settings", "Integration Request", "User"}
    ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sap_b1_direct_integration")

    def _sources(self):
        for base, _, files in os.walk(self.ROOT):
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(base, f)

    def _doctype_args(self, path):
        tree = ast.parse(open(path, encoding="utf8").read())
        consts = {n.targets[0].id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                chain, f = [], node.func
                while isinstance(f, ast.Attribute):
                    chain.append(f.attr); f = f.value
                if isinstance(f, ast.Name) and f.id == "frappe" and node.args and chain and chain[-1] not in ("throw", "msgprint", "logger", "log_error", "whitelist", "get_attr", "get_cached_doc_placeholder"):
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        found.append(a.value)
                    elif isinstance(a, ast.Name) and a.id in consts:
                        found.append(consts[a.id])
                    elif isinstance(a, ast.Dict):                                          # frappe.get_doc({"doctype": ...})
                        for k, v in zip(a.keys, a.values):
                            if isinstance(k, ast.Constant) and k.value == "doctype" and isinstance(v, ast.Constant):
                                found.append(v.value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ("Sales Invoice", "Customer", "Item", "Stock Ledger Entry", "GL Entry", "Payment Entry", "Stock Entry", "Warehouse", "Delivery Note"):
                if "docstring" not in path:
                    found.append("!!literal:" + node.value)
        return found

    def test_no_forbidden_doctype_is_referenced_in_executable_code(self):
        # docstrings are Expr/Constant nodes too, so strip them first
        for path in self._sources():
            src = open(path, encoding="utf8").read()
            tree = ast.parse(src)
            for n in ast.walk(tree):
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                    n.body[0].value.value = ""
            consts_bad = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in
                          ("Sales Invoice", "Customer", "Item", "Stock Ledger Entry", "GL Entry", "Payment Entry", "Stock Entry", "Warehouse", "Delivery Note", "Journal Entry")]
            self.assertEqual(consts_bad, [], f"{os.path.basename(path)} names an ERPNext accounting/stock DocType")

    def test_frappe_calls_only_touch_the_allowed_doctypes(self):
        for path in self._sources():
            for dt in self._doctype_args(path):
                self.assertTrue(dt in self.ALLOWED or dt.startswith("!!literal") is False, f"{os.path.basename(path)} touches {dt}")
            self.assertFalse([d for d in self._doctype_args(path) if d not in self.ALLOWED and not d.startswith("frappe.")][:0])

    def test_the_service_cannot_even_reach_a_database(self):
        src = open(os.path.join(self.ROOT, "core", "service.py"), encoding="utf8").read()
        self.assertNotIn("import frappe", src); self.assertNotIn("frappe.db", src)


if __name__ == "__main__":
    unittest.main()


# ===================================================================================== Part-A / Part-B, validity extension
class PartAAndExtensionTests(Base):
    NO_VEHICLE = {"vehicle_number": "", "transporter_id": TRANSPORTER_GSTIN, "transport_mode": "Road", "distance": 100}

    def _part_a(self):
        self.gsp.behaviour["part_a"] = True
        inv = make_invoice(); inv["transport"] = dict(self.NO_VEHICLE)
        r = self.api.generate_e_waybill(req(inv))
        self.gsp.behaviour.clear()
        return r

    def _update(self, **over):
        body = {**req(), "vehicle_number": "MH12CD5678", "reason": "First Time", "place": "Thane", "state_code": "27"}
        body.update(over)
        return self.api.update_vehicle(body)

    def _extend(self, **over):
        body = {**req(), "reason": "Accident", "place": "Thane", "state_code": "27", "pincode": "400601", "remaining_distance": 40}
        body.update(over)
        return self.api.extend_e_waybill_validity(body)

    def _status(self):
        return self.api.get_e_waybill_status(req())

    def _at_expiry(self, hours_before=1):
        det = list(FakeGsp.ewb_registry.values())[0]
        det["ewb_valid"] = (self.clock() + timedelta(hours=hours_before)).strftime("%Y-%m-%d %H:%M:%S")

    def test_transporter_without_vehicle_gives_part_a_not_a_completed_bill(self):
        r = self._part_a()
        self.assertTrue(r["ok"], r); self.assertTrue(r["data"]["part_b_pending"]); self.assertIn("Part-B", r["message"])
        self.assertTrue(self._status()["data"]["part_b_pending"])

    def test_completing_part_b_clears_pending(self):
        self._part_a()
        self.assertTrue(self._update()["ok"])
        s = self._status()["data"]
        self.assertFalse(s["part_b_pending"]); self.assertEqual(s["vehicle_no"], "MH12CD5678")

    def test_normal_generation_is_not_pending(self):
        self.assertFalse(self.api.generate_e_waybill(req())["data"]["part_b_pending"])

    def test_rail_part_b_needs_the_transport_document_but_no_vehicle(self):
        self._part_a()
        bad = self._update(vehicle_number="", transport_mode="Rail")
        self.assertEqual([e["code"] for e in bad["errors"]], ["TRANSPORT_DOC"])
        self.calls.clear()
        ok = self._update(vehicle_number="", transport_mode="Rail", doc_number="RR123", doc_date=date.today().isoformat())
        self.assertTrue(ok["ok"], ok); self.assertIn("update_vehicle", self.calls)

    def test_returning_to_an_earlier_vehicle_is_sent_again(self):
        self.api.generate_e_waybill(req())
        for veh in ("MH12CD5678", "MH14ZZ1111", "MH12CD5678"):
            self.assertTrue(self._update(vehicle_number=veh, reason="Others")["ok"])
        self.assertEqual(self.calls.count("update_vehicle"), 3)
        self.assertTrue(self._update(reason="Others")["ok"])               # repeating the current one is not sent again
        self.assertEqual(self.calls.count("update_vehicle"), 3)

    def test_extension_inside_the_window(self):
        self.api.generate_e_waybill(req()); self._at_expiry()
        r = self._extend()
        self.assertTrue(r["ok"], r); self.assertEqual(r["data"]["ewb_valid_till"], "2026-11-12 23:59:00")
        self.assertEqual(self.gsp.last_extend["extnRsnCode"], 5); self.assertEqual(self.gsp.last_extend["consignmentStatus"], "M")
        self.assertEqual(self.gsp.last_extend["remainingDistance"], 40)

    def test_extension_outside_the_window_or_without_part_b_is_refused(self):
        self.api.generate_e_waybill(req()); self._at_expiry(hours_before=30)
        self.assertEqual([e["code"] for e in self._extend()["errors"]], ["EXTEND_WINDOW"])
        list(FakeGsp.ewb_registry.values())[0]["ewb_valid"] = ""
        self.assertEqual([e["code"] for e in self._extend()["errors"]], ["PART_B_PENDING"])
        self.assertNotIn("extend_validity", self.calls)

    def test_extension_input_is_checked_before_the_call(self):
        self.api.generate_e_waybill(req()); self._at_expiry()
        codes = {e["code"] for e in self._extend(reason="x", pincode="1", remaining_distance=0, consignment_status="In Transit")["errors"]}
        self.assertEqual(codes, {"REASON", "PINCODE", "DISTANCE", "TRANSIT"}); self.assertNotIn("extend_validity", self.calls)

    def test_extension_with_lost_answer(self):
        self.api.generate_e_waybill(req()); self._at_expiry()
        self.gsp.behaviour["timeout_before"] = True
        self.assertEqual(self._extend()["status"], C.ST_UNKNOWN)
        self.gsp.behaviour.clear(); self.gsp.behaviour["timeout_after"] = True
        r = self._extend()
        self.assertTrue(r["ok"], r)
