import os

BASE_DIRECTORY = os.getenv("DAMY_BASE_DIR", r"T:\DAMY")
DEFAULT_PSD_DIR = os.getenv("DAMY_PSD_DIR", os.path.join(BASE_DIRECTORY, "1. Order Form"))
LOCAL_APP_ROOT = os.getenv(
    "DAMY_LOCAL_ROOT",
    os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "DAMYComp"),
)
LOCAL_CONFIG_DIR = os.getenv("DAMY_LOCAL_CONFIG_DIR", os.path.join(LOCAL_APP_ROOT, "config"))
LOCAL_AUTH_DIR = os.getenv("DAMY_LOCAL_AUTH_DIR", os.path.join(LOCAL_APP_ROOT, "auth"))
SHARE_ROOT = os.getenv("DAMY_SHARE_ROOT", r"T:\DAMYComp")
SHARED_CONFIG_DIR = os.getenv("DAMY_SHARED_CONFIG_DIR", os.path.join(SHARE_ROOT, "config"))
CALENDAR_TOKEN_PATH = os.getenv(
    "DAMY_CALENDAR_TOKEN_PATH",
    os.path.join(LOCAL_AUTH_DIR, "calendar_token.json"),
)
CALENDAR_CREDENTIALS_PATH = os.getenv(
    "DAMY_CALENDAR_CREDENTIALS_PATH",
    os.path.join(LOCAL_CONFIG_DIR, "credentials.json"),
)
ORDER_IMPORT_TOKEN_PATH = os.getenv(
    "DAMY_ORDER_IMPORT_TOKEN_PATH",
    os.path.join(LOCAL_AUTH_DIR, "gmail_token.json"),
)
ORDER_IMPORT_CREDENTIALS_PATH = os.getenv(
    "DAMY_ORDER_IMPORT_CREDENTIALS_PATH",
    CALENDAR_CREDENTIALS_PATH,
)

DB_HOST = os.getenv("DAMY_DB_HOST", "192.168.1.208")
DB_NAME = os.getenv("DAMY_DB_NAME", "damy_workflow_v2")
DB_USER = os.getenv("DAMY_DB_USER", "damy_app")
DB_PASS = os.getenv("DAMY_DB_PASS", "2357")
DB_PORT = int(os.getenv("DAMY_DB_PORT", "5432"))
