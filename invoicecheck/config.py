"""Settings. One business, one currency."""
import os

CURRENCY = os.environ.get("INVOICECHECK_CURRENCY", "AUD")
MAX_UPLOAD_BYTES = int(os.environ.get("INVOICECHECK_MAX_UPLOAD_BYTES", 5_000_000))
MAX_FILES_PER_UPLOAD = int(os.environ.get("INVOICECHECK_MAX_FILES", 50))
