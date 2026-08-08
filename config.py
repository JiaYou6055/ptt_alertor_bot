import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
COMPACT_NOTIFICATION = os.getenv("COMPACT_NOTIFICATION", "true").lower() in ("true", "1", "yes")

# User Whitelist & Super Admin
raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = [int(uid.strip()) for uid in raw_allowed.split(",") if uid.strip().isdigit()]
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0").strip()) if os.getenv("ADMIN_USER_ID", "").strip().isdigit() else (ALLOWED_USER_IDS[0] if ALLOWED_USER_IDS else 0)

# Night Mode Settings
NIGHT_MODE_ENABLED = os.getenv("NIGHT_MODE_ENABLED", "true").lower() in ("true", "1", "yes")
NIGHT_START_HOUR = int(os.getenv("NIGHT_START_HOUR", "1"))  # 01:00 AM
NIGHT_END_HOUR = int(os.getenv("NIGHT_END_HOUR", "7"))      # 07:00 AM
NIGHT_CHECK_INTERVAL_SECONDS = int(os.getenv("NIGHT_CHECK_INTERVAL_SECONDS", "1800"))  # 30 minutes

DB_PATH = os.getenv("DB_PATH", "ptt_alert.db")
PTT_DOMAIN = os.getenv("PTT_DOMAIN", "https://www.ptt.cc")


def is_night_mode() -> bool:
    """Check if current time is within configured Night Mode hours."""
    if not NIGHT_MODE_ENABLED:
        return False
    current_hour = datetime.now().hour
    if NIGHT_START_HOUR <= NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= current_hour < NIGHT_END_HOUR
    else:  # Cross midnight, e.g. 23:00 to 07:00
        return current_hour >= NIGHT_START_HOUR or current_hour < NIGHT_END_HOUR


def get_current_check_interval() -> int:
    """Return 1800s (30m) during night mode, or 300s (5m) during daytime."""
    return NIGHT_CHECK_INTERVAL_SECONDS if is_night_mode() else CHECK_INTERVAL_SECONDS
