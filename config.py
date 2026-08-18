"""
Configuration for ZKTeco → Oracle SQL Sync Service
All device definitions are now read from the `devices` table in the database.
This file only contains the database connection and sync behaviour settings.
"""
import os

# Read from environment file if it exists
def load_env():
    try:
        with open("settings.env", "r") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    key, val = line.strip().split("=", 1)
                    os.environ[key] = val
    except FileNotFoundError:
        pass  # Use defaults below

load_env()

# ── Oracle Database ────────────────────────────────────────────────────────────
# python-oracledb thin mode — no Oracle Client libraries required.
# dsn examples:
#   "localhost:1521/XEPDB1"          ← XE pluggable DB
#   "localhost:1521:ORCL"            ← SID style
#   "//host:port/service_name"       ← Easy Connect


DB_CONFIG = {
    "user": os.getenv("DB_USER", "system"),
    "password": os.getenv("DB_PASSWORD", "12345"),
    "dsn": os.getenv("DB_DSN", "localhost:1521/attendance"),
}

# ── Sync Behaviour ─────────────────────────────────────────────────────────────
SYNC_CONFIG = {
    "backfill_on_start":   True,   # Pull stored logs from each device on connect
    "retry_delay_seconds": 60,     # Seconds to wait before reconnecting a failed device
    "max_retries":         0,      # 0 = retry forever; set >0 to cap retry attempts
}