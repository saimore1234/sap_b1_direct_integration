# sap_b1_direct_integration 2.0 (generic Frappe app)

Lightweight GST (e-Invoice / e-Way Bill) API for SAP Business One. **Creates no ERPNext Sales Invoice, Customer, Item, stock or accounting entry.** SAP sends the invoice as JSON; the app validates it, builds the NIC payload and calls the GSP/IRP through the installed india_compliance client classes (`EInvoiceAPI` / `EWaybillAPI`, no document required), then returns the real result.

* API: `sap_b1_direct_integration.api.gst.*` - see `docs/API.md` of the add-on repository.
* DocTypes: **SAP Integration Profile** (configuration) and **SAP GST Transaction** (minimal metadata, unique reference = primary key = lock).
* Core (`core/`) is framework independent and unit tested with plain `unittest`: `contract`, `validation`, `nic_payload`, `service` (idempotency, locking, reconciliation, status machine); Frappe bindings in `core/bindings.py`, adapter in `core/gsp.py`.
* Install on a **staging bench first**: `bench get-app ...`, `bench --site <site> install-app sap_b1_direct_integration`, `bench --site <site> migrate`; create the API user and one profile per SAP company.
* Tests: `PYTHONPATH=. python -m unittest discover -s tests -t .` (no bench needed); bench assertions in `tests/test_bench_zero_sales_invoice.py` (throw-away site only).
* Retention: `tasks.cleanup_transactions` (failed records only, per-profile days) and `tasks.cleanup_integration_requests` (india_compliance logs its payloads as Integration Requests; removed after 30 days).

STATUS: framework-independent core and API layer tested with a fake `frappe`; **not yet executed on a real bench or against a real GSP sandbox**.
