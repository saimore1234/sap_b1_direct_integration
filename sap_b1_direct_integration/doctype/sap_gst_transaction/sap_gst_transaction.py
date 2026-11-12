from frappe.model.document import Document


class SAPGSTTransaction(Document):
    """Minimal metadata only (no invoice lines, no payload, no signed data). The name is the unique transaction reference."""
