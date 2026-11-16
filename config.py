"""
Configuration for ZKTeco -> Oracle SQL Sync Service
Device definitions are read from the `device_machines` table in the database
(and cached locally so collection continues even if Oracle is down at startup).
This file only contains the database connection and sync behaviour settings.
"""
import os
import sys


def _app_dir() -> str:
    """Folder of the .exe (PyInstaller) or of this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()


# Read from environment file if it exists (current dir first, then app dir,
# then the PyInstaller bundle dir).
def load_env():
    candidates = [os.path.join(os.getcwd(), "settings.env"),
                  os.path.join(APP_DIR, "settings.env")]
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "settings.env"))
    for path in candidates:
        try:
            with open(path, "r") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        key, val = line.strip().split("=", 1)
                        os.environ[key] = val
            return
        except FileNotFoundError:
            continue


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
    # --- Device collection -------------------------------------------------
    # Every poll the service connects to the device, checks the record count
    # and (if it changed) reads the FULL attendance log stored on the device.
    # Anything not yet in the local journal is saved. Nothing depends on
    # real-time events, so a missed event can never lose a record.
    "poll_interval_seconds":      60,
    # Even if the record count did not change, re-read the full log at least
    # this often (covers devices that overwrite old records when full).
    "full_read_interval_seconds": 600,
    "device_timeout_seconds":     15,    # socket timeout for device commands
    "device_password":            0,     # device comm key (0 = none)
    "force_udp":                  False,
    # Lock the keypad while reading the log. Not needed for correctness and
    # stops people punching during the read, so off by default.
    "disable_device_during_read": False,

    # --- Reconnection ------------------------------------------------------
    # On any device error the worker reconnects forever with exponential
    # back-off between these two values.
    "reconnect_min_delay_seconds": 5,
    "reconnect_max_delay_seconds": 60,

    # --- Local journal (store-and-forward) ---------------------------------
    # Every record read from a device is written to this SQLite file (fsync'd)
    # BEFORE it is sent to Oracle, and is only marked "synced" after it has
    # been read back from Oracle. Relative paths are relative to the app folder.
    "journal_path": "zkteco_journal.db",

    # --- Oracle sync -------------------------------------------------------
    "oracle_sync_interval_seconds": 5,
    "oracle_batch_size":            500,
    "oracle_call_timeout_seconds":  60,

    # --- Audit (independent re-check journal vs Oracle) --------------------
    # Periodically compares every synced journal record of the last N days
    # with Oracle and re-sends anything Oracle is missing.
    "audit_interval_seconds": 3600,
    "audit_lookback_days":    31,

    # --- Devices -------------------------------------------------------------
    # How often to re-read device_machines (devices added/removed/changed
    # are picked up without restarting).
    "device_refresh_interval_seconds": 300,

    # --- Check type mapping ------------------------------------------------
    # "punch"  : use the device's punch state (0=Check-In, 1=Check-Out,
    #            2=Break-Out, 3=Break-In, 4=OT-In, 5=OT-Out). Correct mapping.
    # "status" : legacy behaviour of the old app (status==1 -> 'I' else 'O').
    #            NOTE: `status` is really the verify method (1=finger, 15=face).
    "checktype_mode": "punch",

    # --- Logging -------------------------------------------------------------
    "log_file":           "zkteco_sync.log",
    "log_max_bytes":      10 * 1024 * 1024,
    "log_backup_count":   10,
    "status_summary_interval_seconds": 600,
}
