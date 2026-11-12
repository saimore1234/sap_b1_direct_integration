"""API layer tests with a tiny fake `frappe` module (no bench). Proves endpoint wiring, error mapping and log redaction."""

import json
import sys
import types
import unittest


def _install_fake_frappe():
    f = types.ModuleType("frappe")
    f.PermissionError = type("PermissionError", (Exception,), {})
    f.ValidationError = type("ValidationError", (Exception,), {})
    f.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
    f.form_dict = {}
    f.session = types.SimpleNamespace(user="api@site")
    f.logged = []
    f.errors = []
    f._ = lambda s: s

    def whitelist(*a, **k):
        def deco(fn):
            fn.whitelisted = True
            return fn
        return deco(a[0]) if a and callable(a[0]) else deco
    f.whitelist = whitelist

    class _Logger:
        def info(self, msg):
            f.logged.append(msg)
    f.logger = lambda *a, **k: _Logger()
    f.log_error = lambda **k: f.errors.append(k)

    def throw(msg, exc=None):
        raise (exc or Exception)(msg)
    f.throw = throw
    rl = types.ModuleType("frappe.rate_limiter")
    rl.rate_limit = lambda **k: (lambda fn: fn)
    f.rate_limiter = rl
    sys.modules["frappe"] = f
    sys.modules["frappe.rate_limiter"] = rl
    return f


frappe = _install_fake_frappe()

from sap_b1_direct_integration.api import gst            # noqa: E402  (needs the fake first)
from sap_b1_direct_integration.core.service import GstApi   # noqa: E402
from tests.helpers import FakeGsp, MemoryStore, Profiles, Site, make_invoice, profile   # noqa: E402

ENDPOINTS = ["validate_invoice", "generate_e_invoice", "get_e_invoice_status", "cancel_e_invoice", "generate_e_waybill", "get_e_waybill_status", "cancel_e_waybill", "update_vehicle", "update_transporter"]


class ApiLayerTests(unittest.TestCase):
    def setUp(self):
        FakeGsp.registry.clear(); FakeGsp.ewb_registry.clear()
        frappe.logged.clear(); frappe.errors.clear()
        self.store, self.calls = MemoryStore(), []
        self.site = Site()
        gst._service = lambda: GstApi(Profiles(profile()), self.site, self.store, lambda g: FakeGsp(calls=self.calls), log=gst._log)

    def test_all_required_endpoints_exist_and_are_whitelisted(self):
        for name in ENDPOINTS + ["ping"]:
            self.assertTrue(getattr(gst, name).whitelisted, name)

    def test_generate_through_the_endpoint(self):
        inv = make_invoice()
        frappe.form_dict = {"cmd": "x", "integration_id": "IT1", "sap_database": "DB_ONE", "object_type": "13", "doc_entry": "2624", "invoice": json.dumps(inv)}
        r = gst.generate_e_invoice()
        self.assertTrue(r["ok"], r); self.assertEqual(len(r["data"]["irn"]), 64); self.assertEqual(r["status"], "Generated")

    def test_denied_request_becomes_a_permission_error_not_a_business_response(self):
        self.site = Site(user="intruder@site")
        gst._service = lambda: GstApi(Profiles(profile()), self.site, self.store, lambda g: FakeGsp(calls=self.calls), log=gst._log)
        frappe.form_dict = {"integration_id": "IT1", "sap_database": "DB_ONE", "doc_entry": "1", "invoice": make_invoice()}
        with self.assertRaises(frappe.PermissionError):
            gst.generate_e_invoice()
        self.assertEqual(self.calls, [])

    def test_bad_invoice_json_is_a_validation_error(self):
        frappe.form_dict = {"integration_id": "IT1", "invoice": "{not json"}
        with self.assertRaises(frappe.ValidationError):
            gst.validate_invoice()

    def test_audit_log_redacts_secret_like_fields_and_carries_the_request_id(self):
        gst._log("probe", request_id="rid-123456", api_secret="TOPSECRET", gsp_password="PW", authorization="token k:s", reference="R1")
        line = frappe.logged[-1]
        self.assertIn("request_id=rid-123456", line); self.assertIn("reference=R1", line)
        for forbidden in ("TOPSECRET", "PW", "token k:s"):
            self.assertNotIn(forbidden, line)

    def test_a_full_call_logs_no_payload_or_gstin(self):
        inv = make_invoice()
        frappe.form_dict = {"integration_id": "IT1", "sap_database": "DB_ONE", "doc_entry": "2624", "invoice": json.dumps(inv)}
        gst.generate_e_invoice()
        text = " ".join(frappe.logged)
        for forbidden in (inv["seller"]["gstin"], inv["buyer"]["gstin"], "Plastic closures", "SIGNED"):
            self.assertNotIn(forbidden, text)
        self.assertIn("request_id=", text)

    def test_unexpected_errors_are_logged_without_the_request_body_and_return_a_generic_response(self):
        def boom(*a, **k):
            raise RuntimeError("db exploded")
        class Exploding(GstApi):
            generate_e_invoice = boom
        gst._service = lambda: Exploding(Profiles(profile()), self.site, self.store, lambda g: FakeGsp(), log=gst._log)
        frappe.form_dict = {"integration_id": "IT1", "invoice": make_invoice()}
        r = gst.generate_e_invoice()
        self.assertFalse(r["ok"]); self.assertEqual(r["status"], "Failed"); self.assertNotIn("exploded", json.dumps(r))
        self.assertTrue(frappe.errors)

    def test_ping_reveals_no_credentials(self):
        frappe.get_attr = lambda path: (lambda: {"frappe": {"version": "15.108.0"}, "erpnext": {"version": "15.108.3"}, "india_compliance": {"version": "15.29.0"}})
        frappe.get_cached_doc = lambda name: types.SimpleNamespace(sandbox_mode=1)
        out = gst.ping()
        self.assertEqual(out["india_compliance"], "15.29.0"); self.assertTrue(out["gst_sandbox_mode"])
        self.assertNotIn("password", json.dumps(out).lower())


if __name__ == "__main__":
    unittest.main()
