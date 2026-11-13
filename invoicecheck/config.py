"""Settings. One business per account, one currency. Every limit can be set
with an environment variable of the same name prefixed INVOICECHECK_."""
import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(f"INVOICECHECK_{name}", default))


CURRENCY = os.environ.get("INVOICECHECK_CURRENCY", "AUD")

# Upload limits
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 5_000_000)          # one PDF
MAX_FILES_PER_UPLOAD = _int("MAX_FILES", 50)                    # one upload
MAX_REQUEST_BYTES = _int("MAX_REQUEST_BYTES", 30_000_000)       # one HTTP request, checked before the body is read
MAX_ORDER_FILE_BYTES = _int("MAX_ORDER_FILE_BYTES", 1_000_000)  # the purchase-order CSV

# Usage caps, per account
MAX_INVOICES = _int("MAX_INVOICES", 200)                        # files stored at one time
MAX_STORAGE_BYTES = _int("MAX_STORAGE_BYTES", 100_000_000)      # PDF bytes stored at one time
MAX_FILES_PER_DAY = _int("MAX_FILES_PER_DAY", 100)              # new files accepted per UTC day
MAX_ORDER_LINES = _int("MAX_ORDER_LINES", 5_000)                # purchase-order lines stored at one time

# Model reading of invoices that no supplier template matches. Off unless INVOICECHECK_MODEL_EXTRACTION=1;
# needs ANTHROPIC_API_KEY in the server's environment. When on, the invoice text is sent to the Anthropic API.
MODEL_EXTRACTION = os.environ.get("INVOICECHECK_MODEL_EXTRACTION", "") == "1"
MODEL = os.environ.get("INVOICECHECK_MODEL", "claude-opus-5-5")
MODEL_EFFORT = os.environ.get("INVOICECHECK_MODEL_EFFORT", "low")
MODEL_TIMEOUT_SECONDS = _int("MODEL_TIMEOUT_SECONDS", 60)
MAX_MODEL_CALLS_PER_DAY = _int("MAX_MODEL_CALLS_PER_DAY", 30)   # per account, per UTC day
MAX_SCAN_PAGES = _int("MAX_SCAN_PAGES", 5)                     # a longer scan is not sent to the model

# Accounts
MAX_ACCOUNTS = _int("MAX_ACCOUNTS", 25)
INVITE_CODE = os.environ.get("INVOICECHECK_INVITE_CODE", "")    # when set, signing up needs it
SESSION_DAYS = _int("SESSION_DAYS", 14)
COOKIE_SECURE = os.environ.get("INVOICECHECK_COOKIE_SECURE", "") == "1"   # https requests always get a Secure cookie
MIN_PASSWORD_CHARS = 10
LOGIN_WINDOW_MINUTES = 15
MAX_LOGIN_FAILURES_PER_EMAIL = 5
MAX_LOGIN_FAILURES_PER_IP = 30
