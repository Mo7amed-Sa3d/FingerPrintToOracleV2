"""
ZKTeco -> Oracle SQL Real-Time Attendance Sync Service
=======================================================

Design goals:
- One listener thread per ZKTeco device.
- Device listeners never wait for Oracle INSERT/COMMIT operations.
- Attendance events are placed into a thread-safe queue immediately.
- Dedicated database workers batch writes to Oracle.
- Duplicate rows are ignored only for ORA-00001.
- Other database errors are retried and are never falsely reported as duplicates.
- Device monitoring does not create a second ZKTeco connection.
- Historical device records are queued before live capture without disabling the device.
- On reconnect, device memory is back-filled again, allowing recovery of records that
  were generated while the application/database was unavailable (assuming the device
  retains those attendance records).
"""

import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import oracledb
from zk import ZK

from config import DB_CONFIG, SYNC_CONFIG


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("zkteco_sync.log"),
        logging.StreamHandler(sys.stdout),
    ],
)


# -----------------------------------------------------------------------------
# Configuration helpers
# -----------------------------------------------------------------------------
QUEUE_BATCH_SIZE = int(SYNC_CONFIG.get("db_batch_size", 100))
DB_WORKER_COUNT = int(SYNC_CONFIG.get("db_workers", 4))
DB_RETRY_DELAY = float(SYNC_CONFIG.get("db_retry_delay_seconds", 2))
DB_MAX_RETRIES = int(SYNC_CONFIG.get("db_max_retries", 0))
RETRY_DELAY = float(SYNC_CONFIG.get("retry_delay_seconds", 5))
MAX_DEVICE_RETRIES = int(SYNC_CONFIG.get("max_retries", 0))


# -----------------------------------------------------------------------------
# Oracle connection pool
# -----------------------------------------------------------------------------
_pool: oracledb.ConnectionPool | None = None


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

    logging.getLogger("db").info(
        "Oracle connection pool created (max=%s).", pool_size
    )


def get_db() -> oracledb.Connection:
    if _pool is None:
        raise RuntimeError("Oracle connection pool has not been initialized")
    return _pool.acquire()


# -----------------------------------------------------------------------------
# Attendance queue
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Punch:
    user_id: str
    punch_time: Any
    punch_status: Any
    verify_code: int
    device_id: int
    source: str = "live"


# Unbounded on purpose: the device listener must not block waiting for Oracle.
# Device memory/backfill provides recovery after a process/device reconnect, while
# this queue prevents normal database latency from blocking socket consumption.
punch_queue: queue.Queue[Punch] = queue.Queue()


# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------
def get_devices_from_db(conn=None) -> list[dict]:
    """Read devices from device_machines."""
    if conn is None:
        conn = get_db()
        own_conn = True
    else:
        own_conn = False

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ID, IP, PORT
            FROM device_machines
        """)

        return [
            {
                "device_id": row[0],
                "ip": row[1],
                "port": row[2] if row[2] is not None else 4370,
            }
            for row in cur.fetchall()
        ]
    finally:
        if own_conn:
            conn.close()


def set_device_status(device_id: int, status: str):
    """Update device status without creating another ZKTeco connection."""
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


def _checktype(punch_status) -> str:
    # Original application semantics: status=1 => IN, otherwise OUT.
    return "I" if punch_status == 1 else "O"


def _insert_single_punch(conn, punch: Punch) -> str:
    """
    Insert one punch using an existing connection.

    Returns:
      INSERTED  -> new row
      DUP       -> ORA-00001 only

    Any other database error is raised so the caller can retry it instead of
    incorrectly treating it as a duplicate.
    """
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO checkinout
                (USERID, checktime, Checktype, verifycode, DeviceID)
            VALUES (:p_uid, :p_ts, :p_ct, :p_vc, :p_did)
            """,
            p_uid=str(punch.user_id),
            p_ts=punch.punch_time,
            p_ct=_checktype(punch.punch_status),
            p_vc=punch.verify_code,
            p_did=punch.device_id,
        )
        return "INSERTED"

    except oracledb.IntegrityError as exc:
        error_obj, = exc.args
        if getattr(error_obj, "code", None) == 1:
            return "DUP"
        raise


def _insert_batch(conn, punches: list[Punch]) -> tuple[int, int]:
    """
    Insert a batch in one transaction.

    Oracle batcherrors allows duplicate rows to be identified without rolling
    back successful rows. Any non-duplicate error is raised so the batch can be
    retried safely and, if necessary, isolated to the individual record.
    """
    cur = conn.cursor()

    sql = """
        INSERT INTO checkinout
            (USERID, checktime, Checktype, verifycode, DeviceID)
        VALUES (:p_uid, :p_ts, :p_ct, :p_vc, :p_did)
    """

    params = [
        {
            "p_uid": str(p.user_id),
            "p_ts": p.punch_time,
            "p_ct": _checktype(p.punch_status),
            "p_vc": p.verify_code,
            "p_did": p.device_id,
        }
        for p in punches
    ]

    try:
        cur.executemany(sql, params, batcherrors=True)
        errors = cur.getbatcherrors()

        inserted = len(punches)
        duplicates = 0
        non_duplicate_errors = []

        for error in errors:
            if getattr(error, "code", None) == 1:
                duplicates += 1
                inserted -= 1
            else:
                non_duplicate_errors.append(error)

        if non_duplicate_errors:
            # Do not commit a batch containing an unknown failure. The caller
            # will rollback and retry records individually.
            raise non_duplicate_errors[0]

        conn.commit()
        return inserted, duplicates

    except Exception:
        conn.rollback()
        raise


# -----------------------------------------------------------------------------
# Database writer
# -----------------------------------------------------------------------------
class DatabaseWorker(threading.Thread):
    """Consumes punches independently of the ZKTeco listener threads."""

    def __init__(self, worker_id: int, stop_event: threading.Event):
        super().__init__(name=f"DB-Worker-{worker_id}", daemon=False)
        self.stop_event = stop_event
        self.log = logging.getLogger(self.name)

    def _get_batch(self) -> list[Punch]:
        """Get at least one item, then drain up to QUEUE_BATCH_SIZE."""
        try:
            first = punch_queue.get(timeout=0.5)
        except queue.Empty:
            return []

        batch = [first]

        while len(batch) < QUEUE_BATCH_SIZE:
            try:
                batch.append(punch_queue.get_nowait())
            except queue.Empty:
                break

        return batch

    def _finish_batch(self, batch: list[Punch]):
        for _ in batch:
            punch_queue.task_done()

    def _write_batch(self, batch: list[Punch]):
        conn = get_db()
        try:
            inserted, duplicates = _insert_batch(conn, batch)
            self.log.info(
                "DB batch: %d received, %d inserted, %d duplicate(s). queue=%d",
                len(batch),
                inserted,
                duplicates,
                punch_queue.qsize(),
            )
            self._finish_batch(batch)
            return
        except Exception as batch_exc:
            conn.close()
            conn = None
            self.log.warning(
                "Batch insert failed for %d record(s); retrying individually: %s",
                len(batch),
                batch_exc,
            )

        # Isolate failures so one bad record cannot block all other records.
        for punch in batch:
            self._write_one_with_retry(punch)
            punch_queue.task_done()

        if conn is not None:
            conn.close()

    def _write_one_with_retry(self, punch: Punch):
        attempt = 0

        while True:
            attempt += 1
            conn = None
            try:
                conn = get_db()
                result = _insert_single_punch(conn, punch)
                conn.commit()

                if result == "INSERTED":
                    self.log.info(
                        "[IN] user=%s time=%s device=%s source=%s",
                        punch.user_id,
                        punch.punch_time,
                        punch.device_id,
                        punch.source,
                    )
                else:
                    self.log.info(
                        "[DUP] user=%s time=%s device=%s source=%s",
                        punch.user_id,
                        punch.punch_time,
                        punch.device_id,
                        punch.source,
                    )
                return

            except oracledb.IntegrityError as exc:
                # A non-duplicate integrity error is a real data problem.
                # Retry according to policy; never label it DUP.
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                self.log.error(
                    "Non-duplicate integrity error for user=%s time=%s device=%s: %s",
                    punch.user_id,
                    punch.punch_time,
                    punch.device_id,
                    exc,
                )

            except Exception as exc:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                self.log.error(
                    "DB insert failed for user=%s time=%s device=%s attempt=%d: %s",
                    punch.user_id,
                    punch.punch_time,
                    punch.device_id,
                    attempt,
                    exc,
                )

            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

            if DB_MAX_RETRIES and attempt >= DB_MAX_RETRIES:
                # Keep the record from disappearing silently. It remains in the
                # device memory and will be attempted again by backfill after a
                # future reconnect, assuming the device retains its attendance log.
                self.log.critical(
                    "Giving up after %d DB attempts; device=%s user=%s time=%s",
                    attempt,
                    punch.device_id,
                    punch.user_id,
                    punch.punch_time,
                )
                return

            self.stop_event.wait(DB_RETRY_DELAY)

    def run(self):
        self.log.info("Database worker started.")

        while not self.stop_event.is_set() or not punch_queue.empty():
            batch = self._get_batch()
            if not batch:
                continue

            self._write_batch(batch)

        self.log.info("Database worker stopped. queue=%d", punch_queue.qsize())


# -----------------------------------------------------------------------------
# Per-device worker
# -----------------------------------------------------------------------------
class DeviceWorker(threading.Thread):
    def __init__(self, device: dict, stop_event: threading.Event):
        super().__init__(name=f"Device-{device['device_id']}", daemon=True)
        self.device = device
        self.stop_event = stop_event
        self.log = logging.getLogger(self.name)
        self.device_id = device["device_id"]
        self._retry_count = 0

    def _make_zk(self) -> ZK:
        return ZK(
            self.device["ip"],
            port=self.device["port"],
            timeout=10,
            password=0,
            force_udp=False,
            ommit_ping=False,
        )

    def _enqueue(self, rec, source: str):
        punch_queue.put(
            Punch(
                user_id=str(rec.user_id),
                punch_time=rec.timestamp,
                punch_status=rec.status,
                verify_code=rec.status,
                device_id=self.device_id,
                source=source,
            )
        )

    def _backfill(self, zk_conn):
        """
        Queue historical records from device memory.

        The device is intentionally not disabled here. Disabling the device
        creates an unnecessary period during which users may be unable to punch.
        The device's stored attendance log is used for recovery after reconnect.
        """
        self.log.info("Back-filling historical logs…")
        records = zk_conn.get_attendance()
        count = 0

        for rec in records:
            if self.stop_event.is_set():
                break
            self._enqueue(rec, source="backfill")
            count += 1

        self.log.info(
            "Back-fill queued: %d record(s). queue=%d",
            count,
            punch_queue.qsize(),
        )
 
    def _live_capture(self, zk_conn):
        """Receive real-time punches and enqueue them immediately."""
        self.log.info("Live capture started.")

        for attendance in zk_conn.live_capture():
            if self.stop_event.is_set():
                break

            if attendance is None:
                # live_capture yields None after its socket timeout. This is not
                # an error; the generator remains alive and continues listening.
                continue

            # IMPORTANT: no Oracle operation is performed here.
            # This keeps the ZKTeco socket consumer fast.
            self._enqueue(attendance, source="live")

    def run(self):
        while not self.stop_event.is_set():
            zk_conn = None

            try:
                self.log.info(
                    "Connecting to %s:%s (attempt %s)…",
                    self.device["ip"],
                    self.device["port"],
                    self._retry_count + 1,
                )

                zk_conn = self._make_zk().connect()
                self._retry_count = 0

                set_device_status(self.device_id, "ONLINE")
                self.log.info("Device ONLINE.")

                # Do not disable the device during backfill. The original code
                # created a large gap before live capture when the device had a
                # large attendance history.
                if SYNC_CONFIG.get("backfill_on_start", True):
                    self._backfill(zk_conn)

                # live_capture() itself enables/registers the device as needed.
                self._live_capture(zk_conn)

            except Exception as exc:
                self._retry_count += 1
                self.log.error(
                    "Error (attempt %s): %s",
                    self._retry_count,
                    exc,
                    exc_info=True,
                )

                try:
                    set_device_status(self.device_id, "OFFLINE")
                except Exception:
                    pass

                if MAX_DEVICE_RETRIES and self._retry_count >= MAX_DEVICE_RETRIES:
                    self.log.error(
                        "Reached max_retries=%s. Worker stopping.",
                        MAX_DEVICE_RETRIES,
                    )
                    break

            finally:
                if zk_conn:
                    try:
                        zk_conn.disconnect()
                    except Exception:
                        pass

            if not self.stop_event.is_set():
                self.log.info("Reconnecting in %ss…", RETRY_DELAY)
                self.stop_event.wait(timeout=RETRY_DELAY)

        self.log.info("Worker stopped.")
        try:
            set_device_status(self.device_id, "OFFLINE")
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    log = logging.getLogger("main")

    try:
        direct_conn = oracledb.connect(
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            dsn=DB_CONFIG["dsn"],
        )
    except oracledb.DatabaseError as exc:
        log.error("Failed to connect to Oracle: %s", exc)
        log.error("Please check your DB_CONFIG settings.")
        sys.exit(1)

    try:
        devices = get_devices_from_db(direct_conn)
    finally:
        direct_conn.close()

    if not devices:
        log.error("No devices found in 'device_machines' – exiting.")
        sys.exit(1)

    log.info("Found %d device(s) in database.", len(devices))

    # DB workers, not device workers, determine the amount of concurrent Oracle
    # write traffic. Keep enough spare pool connections for device status reads.
    db_workers = max(1, min(DB_WORKER_COUNT, max(1, len(devices))))
    pool_size = max(db_workers + 3, min(len(devices) + 3, 32))
    _init_pool(pool_size)

    stop_event = threading.Event()

    db_threads = [
        DatabaseWorker(i + 1, stop_event)
        for i in range(db_workers)
    ]

    device_workers = [
        DeviceWorker(device, stop_event)
        for device in devices
    ]

    def _shutdown(sig, frame):
        log.info("Shutdown signal received — stopping workers…")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "Starting %d device worker(s) and %d database worker(s)…",
        len(device_workers),
        len(db_threads),
    )

    for worker in db_threads:
        worker.start()

    for worker in device_workers:
        worker.start()

    try:
        for worker in device_workers:
            worker.join()
    finally:
        # Device listeners have stopped producing new queue items. Let database
        # workers finish everything already received before shutting them down.
        log.info("Device workers stopped. Waiting for database queue to drain…")
        punch_queue.join()

        stop_event.set()

        for worker in db_threads:
            worker.join()

        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass

    log.info("All workers stopped. Exiting.")


if __name__ == "__main__":
    main()