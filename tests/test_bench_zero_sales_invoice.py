"""
BENCH-ONLY database assertions (run on a throw-away test site, never on production):

    bench --site <test-site> run-tests --app sap_b1_direct_integration --module sap_b1_direct_integration.tests.test_bench_zero_sales_invoice

They prove that GST operations leave the accounting / master / stock tables untouched. Skipped automatically when Frappe is not importable
(for example in the plain-Python unit test run).
"""

import importlib.util
import unittest

def _have_bench():
    try:
        return importlib.util.find_spec("frappe") is not None and importlib.util.find_spec("frappe.tests") is not None
    except (ValueError, ImportError):          # a fake frappe module (unit test run) has no spec
        return False


HAVE_FRAPPE = _have_bench()


@unittest.skipUnless(HAVE_FRAPPE, "needs a Frappe bench")
class TestZeroAccountingDocuments(unittest.TestCase):
    TABLES = ["tabSales Invoice", "tabCustomer", "tabItem", "tabWarehouse", "tabPayment Entry", "tabStock Entry", "tabStock Ledger Entry", "tabGL Entry", "tabJournal Entry"]

    def counts(self):
        import frappe
        return {t: frappe.db.sql(f"select count(*) from `{t}`")[0][0] for t in self.TABLES}

    def test_gst_operations_create_no_accounting_master_or_stock_records(self):
        import frappe
        from sap_b1_direct_integration.core.service import GstApi
        from sap_b1_direct_integration.core import bindings
        from tests.helpers import FakeGsp, make_invoice

        before = self.counts()
        tx_before = frappe.db.count("SAP GST Transaction")
        # real FrappeStore/FrappeProfileRepo/FrappeSite, fake GSP (never a real IRP): requires a profile 'IT1' bound to the test user on the test site
        api = GstApi(bindings.FrappeProfileRepo(), bindings.FrappeSite(), bindings.FrappeStore(), lambda g: FakeGsp())
        inv = make_invoice()
        req = {"integration_id": "IT1", "sap_database": "DB_ONE", "object_type": "13", "doc_entry": 2624, "invoice": inv}
        api.validate_invoice(req)
        api.generate_e_invoice(req)
        api.generate_e_invoice(req)                                                    # duplicate
        api.generate_e_waybill(req)
        after = self.counts()
        self.assertEqual(before, after, "an accounting/master/stock table changed")
        self.assertEqual(after["tabSales Invoice"], before["tabSales Invoice"])       # the number of new Sales Invoices is ZERO
        self.assertGreaterEqual(frappe.db.count("SAP GST Transaction"), tx_before)    # only the minimal metadata table grew
