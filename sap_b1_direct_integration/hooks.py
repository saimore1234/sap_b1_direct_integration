app_name = "sap_b1_direct_integration"
app_title = "SAP B1 Direct Integration"
app_publisher = "Integration vendor"
app_description = "Lightweight GST (e-Invoice / e-Way Bill) API for SAP Business One. Creates no ERPNext accounting documents."
app_email = "support@example.invalid"
app_license = "MIT"
required_apps = ["erpnext", "india_compliance"]

scheduler_events = {
    "daily": [
        "sap_b1_direct_integration.tasks.cleanup_transactions",
        "sap_b1_direct_integration.tasks.cleanup_integration_requests",
    ]
}
