"""
ZKTeco → Oracle SQL Real-Time Attendance Sync Service
=====================================================
Reads devices from the `device_machines` table.
Inserts punches into the `checkinout` table.
"""

import logging
import signal
import sys
import threading
import time

import oracledb
from zk import ZK

from config import DB_CONFIG, SYNC_CONFIG

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("zkteco_sync.log"),
        logging.StreamHandler(sys.stdout),
    ],
)


# ── Oracle connection pool ─────────────────────────────────────────────────────
_pool: oracledb.ConnectionPool | None = None

# ── Devices currently owned by a live DeviceWorker connection ──────────────────
# ZKTeco devices generally accept only ONE active connection at a time.
# DeviceMonitor must never ping a device a worker already has open, or it will
# knock the worker's live_capture() session offline.
_connected_devices: set[int] = set()
_connected_devices_lock = threading.Lock()


def _init_pool(pool_size: int):
    global _pool
    _pool = oracledb.create_pool(
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        dsn=DB_CONFIG["dsn"],
        min=2,
        max=pool_size,
        increment=1,
    )
    logging.getLogger("db").info(f"Oracle connection pool created (max={pool_size}).")


def get_db() -> oracledb.Connection:
    return _pool.acquire()


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_devices_from_db(conn=None) -> list[dict]:
    """
    Read devices from the device_machines table.
    Returns a list of dicts with keys: device_id, ip, port.
    """
    if conn is None:
        conn = get_db()
        own_conn = True
    else:
        own_conn = False

    try:
        cur = conn.cursor()
        # Adjust column names if your table uses different names
        cur.execute("""
            SELECT ID, IP, PORT
            FROM device_machines
        """)
        rows = cur.fetchall()
        devices = []
        for row in rows:
            devices.append({
                "device_id": row[0],
                "ip": row[1],
                "port": row[2] if row[2] is not None else 4370,
            })
        return devices
    finally:
        if own_conn:
            conn.close()


def set_device_status(device_id: int, status: str):
    """Update status and status_time in device_machines."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE device_machines
               SET status = :p_status,
                   status_time = SYSTIMESTAMP
             WHERE ID = :p_id
            """,
            p_status=status,
            p_id=device_id,
        )
        conn.commit()
    finally:
        conn.close()

def insert_punch(user_id: str, punch_time, punch_status, verify_code: int, device_id: int) -> int:
    """
    Insert a single attendance record into checkinout.
    Returns 1 if inserted, 0 if duplicate (unique constraint).
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        # Convert punch_status to Checktype: 'I' (in) or 'O' (out)
        # In the zk library, status=1 often means IN, 0 means OUT.
        # Adjust if your device uses opposite values.
        checktype = 'I' if punch_status == 1 else 'O'
        try:
            cur.execute(
                """
                INSERT INTO checkinout
                    (USERID, checktime, Checktype, verifycode, DeviceID)
                VALUES (:p_uid, :p_ts, :p_ct, :p_vc, :p_did)
                """,
                p_uid=str(user_id),
                p_ts=punch_time,
                p_ct=checktype,
                p_vc=verify_code,
                p_did=device_id,
            )
            conn.commit()
            return 1
        except oracledb.IntegrityError as exc:
            conn.rollback()
            # Oracle error 1 = ORA-00001 unique constraint violation → genuine duplicate.
            # Any other integrity error (NOT NULL, FK, check constraint, etc.) is a
            # real insert failure and must NOT be silently counted as a duplicate.
            error_obj, = exc.args
            if getattr(error_obj, "code", None) == 1:
                return 0
            logging.getLogger("db").error(
                f"Insert failed for user={user_id} time={punch_time} "
                f"device={device_id} (not a duplicate): {error_obj.message}"
            )
            return 0
    finally:
        conn.close()


# ── Per-device worker ──────────────────────────────────────────────────────────
class DeviceWorker(threading.Thread):
    def __init__(self, device: dict, stop_event: threading.Event):
        super().__init__(name=f"Device-{device['device_id']}", daemon=True)
        self.device = device          # contains device_id, ip, port
        self.stop_event = stop_event
        self.log = logging.getLogger(f"Device-{device['device_id']}")
        self.device_id = device["device_id"]   # from device_machines.ID
        self._retry_count = 0

    def _make_zk(self) -> ZK:
        return ZK(
            self.device["ip"],
            port=self.device["port"],
            timeout=10,               # can be made configurable
            password=0,
            force_udp=False,
            ommit_ping=False,
        )

    def _backfill(self, zk_conn):
        """Back-fill stored attendance records from device memory."""
        self.log.info("Back-filling historical logs…")
        try:
            records = zk_conn.get_attendance()
            inserted = skipped = 0
            for rec in records:
                if self.stop_event.is_set():
                    break
                # rec has user_id, timestamp, status (1=IN, 0=OUT), verify?
                # We'll use status for Checktype, and set verifycode to 0.
                rows = insert_punch(
                    user_id=rec.user_id,
                    punch_time=rec.timestamp,
                    punch_status=rec.status,   # 1 or 0 (adjust if needed)
                    verify_code=rec.status,             # default (fingerprint)
                    device_id=self.device_id,
                )
                if rows:
                    inserted += 1
                else:
                    skipped += 1
            self.log.info(f"Back-fill done: {inserted} new, {skipped} duplicate(s).")
        except Exception as exc:
            self.log.warning(f"Back-fill failed (non-fatal): {exc}")

    def _live_capture(self, zk_conn):
        """Stream real-time punches from the device."""
        self.log.info("Live capture started.")
        for attendance in zk_conn.live_capture():
            if self.stop_event.is_set():
                break
            if attendance is None:
                continue
            try:
                rows = insert_punch(
                    user_id=attendance.user_id,
                    punch_time=attendance.timestamp,
                    punch_status=attendance.status,
                    verify_code=attendance.status,
                    device_id=self.device_id,
                )
                tag = "IN" if rows else "DUP"
                self.log.info(f"[{tag}] user={attendance.user_id} time={attendance.timestamp}")
            except Exception as exc:
                # Never let a single bad insert kill the live-capture stream —
                # that would force a full reconnect and can permanently lose
                # this record if the device doesn't re-offer it on backfill.
                self.log.error(
                    f"Failed to insert live punch user={attendance.user_id} "
                    f"time={attendance.timestamp}: {exc}"
                )

    def run(self):
        retry_delay = SYNC_CONFIG["retry_delay_seconds"]
        max_retries = SYNC_CONFIG.get("max_retries", 0)

        while not self.stop_event.is_set():
            zk_conn = None
            try:
                self.log.info(f"Connecting to {self.device['ip']}:{self.device['port']} (attempt {self._retry_count + 1})…")
                zk_conn = self._make_zk().connect()

                self._retry_count = 0
                with _connected_devices_lock:
                    _connected_devices.add(self.device_id)
                set_device_status(self.device_id, "ONLINE")
                self.log.info("Device ONLINE.")

                zk_conn.disable_device()
                if SYNC_CONFIG.get("backfill_on_start", True):
                    self._backfill(zk_conn)

                zk_conn.enable_device()
                self._live_capture(zk_conn)

            except Exception as exc:
                self._retry_count += 1
                self.log.error(f"Error (attempt {self._retry_count}): {exc}")

                try:
                    set_device_status(self.device_id, "OFFLINE")
                except Exception:
                    pass

                if max_retries and self._retry_count >= max_retries:
                    self.log.error(f"Reached max_retries={max_retries}. Worker stopping.")
                    break

            finally:
                with _connected_devices_lock:
                    _connected_devices.discard(self.device_id)
                if zk_conn:
                    try:
                        zk_conn.enable_device()
                        zk_conn.disconnect()
                    except Exception:
                        pass

            if not self.stop_event.is_set():
                self.log.info(f"Reconnecting in {retry_delay}s…")
                self.stop_event.wait(timeout=retry_delay)

        self.log.info("Worker stopped.")
        try:
            set_device_status(self.device_id, "OFFLINE")
        except Exception:
            pass


# ── Device Monitor (Ping every 10 seconds) ────────────────────────────────────
class DeviceMonitor(threading.Thread):
    """
    Periodically pings all devices to update their status in device_machines.
    Runs every 10 seconds (adjustable).
    """

    def __init__(self, stop_event: threading.Event):
        super().__init__(name="DeviceMonitor", daemon=True)
        self.stop_event = stop_event
        self.log = logging.getLogger("monitor")
        self.interval = 10  # seconds

    def ping_device(self, device_ip: str, port: int) -> bool:
        """Attempt a connection and disconnect immediately. Return True on success."""
        try:
            zk = ZK(device_ip, port=port, timeout=3)  # short timeout for ping
            conn = zk.connect()
            conn.disconnect()
            return True
        except Exception:
            return False

    def run(self):
        while not self.stop_event.is_set():
            try:
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT ID, IP, PORT FROM device_machines")
                rows = cur.fetchall()
                conn.close()

                for device_id, ip, port in rows:
                    if self.stop_event.is_set():
                        break

                    with _connected_devices_lock:
                        worker_owns_it = device_id in _connected_devices
                    if worker_owns_it:
                        # A DeviceWorker already holds a live connection and
                        # keeps this device's status current itself — pinging
                        # here would open a second connection and risk
                        # knocking that live_capture() session offline.
                        continue

                    online = self.ping_device(ip, port or 4370)
                    status = "ONLINE" if online else "OFFLINE"
                    set_device_status(device_id, status)
                    if not online:
                        self.log.warning(f"Device {ip} is OFFLINE (ping failed)")

            except Exception as e:
                self.log.error(f"Monitor error: {e}")

            self.stop_event.wait(timeout=self.interval)

        self.log.info("Monitor stopped.")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log = logging.getLogger("main")

    # Test direct connection and read devices
    try:
        direct_conn = oracledb.connect(
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            dsn=DB_CONFIG["dsn"]
        )
    except oracledb.DatabaseError as e:
        log.error(f"Failed to connect to Oracle: {e}")
        log.error("Please check your DB_CONFIG settings.")
        sys.exit(1)

    devices = get_devices_from_db(direct_conn)
    direct_conn.close()

    if not devices:
        log.error("No devices found in 'device_machines' – exiting.")
        sys.exit(1)

    log.info(f"Found {len(devices)} device(s) in database.")

    # Create pool and start workers
    pool_size = min(len(devices) + 2, 32)
    _init_pool(pool_size)

    stop_event = threading.Event()
    workers = [DeviceWorker(device, stop_event) for device in devices]

    # Start monitor
    monitor = DeviceMonitor(stop_event)
    monitor.start()

    def _shutdown(sig, frame):
        log.info("Shutdown signal received — stopping workers and monitor…")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(f"Starting {len(workers)} device worker(s)…")
    for w in workers:
        w.start()

    for w in workers:
        w.join()

    monitor.join(timeout=5)
    log.info("All workers stopped. Exiting.")


if __name__ == "__main__":
    main()