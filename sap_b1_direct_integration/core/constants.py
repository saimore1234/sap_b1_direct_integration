"""Statutory code lists (public NIC / GST master data). Contains NO customer, GSTIN or tax-account values."""

# GST state codes (state name by 2-digit code). 96 = Other Countries (exports), 97 = Other Territory.
STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "96": "Other Countries", "97": "Other Territory",
}

# Unit quantity codes accepted by the NIC e-Invoice / e-Waybill APIs.
UQC = {
    "BAG", "BAL", "BDL", "BKL", "BOU", "BOX", "BTL", "BUN", "CAN", "CBM", "CCM", "CMS", "CTN", "DOZ", "DRM", "GGK", "GMS",
    "GRS", "GYD", "KGS", "KLR", "KME", "LTR", "MLT", "MTR", "MTS", "NOS", "OTH", "PAC", "PCS", "PRS", "QTL", "ROL", "SET",
    "SQF", "SQM", "SQY", "TBS", "TGM", "THD", "TON", "TUB", "UGS", "UNT", "YDS",
}

DOCUMENT_TYPES = {"INV", "CRN", "DBN"}
SUPPLY_TYPES = {"B2B", "SEZWP", "SEZWOP", "EXPWP", "EXPWOP", "DEXP"}
EXPORT_SUPPLY_TYPES = {"EXPWP", "EXPWOP"}
SEZ_SUPPLY_TYPES = {"SEZWP", "SEZWOP"}

# Rates that are normally valid; anything else is reported as a warning (the IRP master decides).
USUAL_GST_RATES = {0, 0.1, 0.25, 0.5, 1, 1.5, 3, 5, 6, 7.5, 12, 14, 18, 28, 40}

TRANSPORT_MODES = {"Road": 1, "Rail": 2, "Air": 3, "Ship": 4}
VEHICLE_TYPES = {"Regular": "R", "Over Dimensional Cargo (ODC)": "O", "ODC": "O"}

# Verified in india_compliance 15.29.0 (constants/e_invoice.py and constants/e_waybill.py)
EINVOICE_CANCEL_REASONS = {"Duplicate": "1", "Data Entry Mistake": "2", "Order Cancelled": "3", "Others": "4"}
EWAYBILL_CANCEL_REASONS = {"Duplicate": "1", "Order Cancelled": "2", "Data Entry Mistake": "3", "Others": "4"}
VEHICLE_UPDATE_REASONS = {"Due to Break Down": "1", "Due to Trans Shipment": "2", "Others": "3", "First Time": "4"}

SERVICE_HSN_PREFIX = "99"

# Operations (also the suffix of the unique transaction reference)
OP_EINVOICE = "EINVOICE"
OP_EINVOICE_CANCEL = "EINVOICE_CANCEL"
OP_EWAYBILL = "EWAYBILL"
OP_EWAYBILL_CANCEL = "EWAYBILL_CANCEL"
OP_VEHICLE = "VEHICLE"
OP_TRANSPORTER = "TRANSPORTER"

# Transaction statuses
ST_PENDING = "Pending"
ST_VALIDATING = "Validating"
ST_VALIDATION_FAILED = "Validation Failed"
ST_PROCESSING = "Processing"
ST_GENERATED = "Generated"
ST_FAILED = "Failed"
ST_UNKNOWN = "Unknown / Reconciliation Required"
ST_CANCELLED = "Cancelled"
