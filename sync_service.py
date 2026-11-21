"""
ZKTeco -> Oracle SQL Attendance Sync Service (zero-loss design)
===============================================================

Why the previous version lost records
-------------------------------------
* pyzk's live_capture() drops events (only the first event of a TCP read is
  parsed, several packet sizes are not handled, non-numeric user ids crash it)
  and never notices a dead connection, so a device could sit "ONLINE" while
  nothing was received. Back-fill only ran on reconnect, which then never came.
* Failed Oracle inserts were logged and forgotten.
* pyzk's get_attendance() can mis-parse the whole log if someone punches
  during the read, and one corrupt record breaks every read.
* Every ORA-00001 was treated as "duplicate", even when it was the ID
  primary key colliding, so a genuinely new punch was thrown away.

How this version works
----------------------
1. DeviceWorker (one per device) polls: connect -> read the device's FULL
   stored log (validated parser, see zk_reader.py) -> save every record not
   yet known into the local SQLite journal (fsync'd) -> disconnect.
   The device's own memory is the source of truth; a punch made while the
   network/app/Oracle was down is simply picked up on the next poll.
   Every poll is a fresh connection, so reconnection is built in; failures
   back off exponentially (5s .. 60s) and retry forever.
2. OracleSyncWorker sends journal records that are not yet synced to Oracle,
   then READS THEM BACK from Oracle. Only records found in Oracle are marked
   synced. Anything else stays pending and is retried with back-off.
3. AuditWorker periodically compares all synced journal records (last N days)
   with Oracle and re-sends anything Oracle is missing.
4. Supervisor reloads device_machines periodically (cached locally so the
   service keeps collecting even if Oracle is down at startup) and logs a
   health summary.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

import oracledb
from zk import ZK

from config import APP_DIR, DB_CONFIG, SYNC_CONFIG
from journal import Journal, TS_FMT, str_to_ts, ts_to_str
from zk_reader import AttendanceReadError, read_attendance


def _cfg(key, default):
    return SYNC_CONFIG.get(key, default)


def _app_path(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(APP_DIR, p)


# ── Logging ────────────────────────────────────────────────────────────────────
class _OneLineFormatter(logging.Formatter):
    """Keep each message on one line (Oracle errors contain newlines)."""

    def formatMessage(self, record):
        record.message = record.message.replace("\r", "").replace("\n", " | ")
        return super().formatMessage(record)


def setup_logging():
    fmt = _OneLineFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        _app_path(_cfg("log_file", "zkteco_sync.log")),
        maxBytes=int(_cfg("log_max_bytes", 10 * 1024 * 1024)),
        backupCount=int(_cfg("log_backup_count", 10)),
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    handlers = [fh]
    if sys.stdout is not None:          # no console in the windowed .exe
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        handlers.append(sh)
    root.handlers[:] = handlers


# ── Single instance lock (only one process may collect at a time) ─────────────
LOCK_PATH = _app_path("zkteco_sync.lock")


class InstanceLock:
    def __init__(self, path: str = LOCK_PATH):
        self.path = path
        self._f = None

    def acquire(self) -> bool:
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self):
        if self._f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._f.close()
        self._f = None


def watch_launcher(on_exit) -> bool:
    """
    A one-file PyInstaller .exe runs as two processes: a small launcher and
    this program. If the launcher is killed (Task Scheduler "End", Task
    Manager), this process would keep running orphaned. Watch the launcher
    and call on_exit() when it disappears so we shut down cleanly.
    Returns True if a watcher was started.
    """
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    SYNCHRONIZE, QUERY_LIMITED = 0x00100000, 0x1000
    handle = k32.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, os.getppid())
    if not handle:
        return False
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(len(buf))
    if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return False
    # Only the launcher of this same .exe counts (never e.g. explorer.exe).
    if os.path.normcase(os.path.abspath(buf.value)) != os.path.normcase(os.path.abspath(sys.executable)):
        return False

    def wait():
        k32.WaitForSingleObject(handle, 0xFFFFFFFF)
        logging.getLogger("main").info("Launcher process ended - shutting down.")
        on_exit()

    threading.Thread(target=wait, name="LauncherWatch", daemon=True).start()
    return True


def service_running_elsewhere() -> bool:
    """True if another process currently holds the collector lock."""
    probe = InstanceLock()
    if probe.acquire():
        probe.release()
        return False
    return True


# ── Check type mapping ─────────────────────────────────────────────────────────
_IN_STATES = {0, 3, 4}     # Check-In, Break-In, OT-In
_OUT_STATES = {1, 2, 5}    # Check-Out, Break-Out, OT-Out


def checktype_for(status, punch) -> str:
    if _cfg("checktype_mode", "punch") == "status":
        return "I" if status == 1 else "O"       # legacy behaviour
    if punch in _OUT_STATES:
        return "O"
    return "I"   # _IN_STATES, and unknown states (e.g. 255 on devices without a state key)


# ── Device status board (device threads never touch Oracle directly) ──────────
class StatusBoard:
    HEARTBEAT = 60  # re-write unchanged ONLINE status at least this often

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[int, dict] = {}

    def set(self, device_id: int, status: str):
        with self._lock:
            st = self._state.setdefault(device_id, {"status": None, "written": None, "written_at": 0.0})
            st["status"] = status

    def due(self) -> list[tuple[int, str]]:
        now = time.monotonic()
        with self._lock:
            return [(d, s["status"]) for d, s in self._state.items()
                    if s["status"] and (s["status"] != s["written"] or now - s["written_at"] >= self.HEARTBEAT)]

    def mark_written(self, items):
        now = time.monotonic()
        with self._lock:
            for d, status in items:
                st = self._state.get(d)
                if st:
                    st["written"] = status
                    st["written_at"] = now


# ── Oracle access ──────────────────────────────────────────────────────────────
INSERT_SQL = """
    INSERT INTO checkinout (USERID, CHECKTIME, CHECKTYPE, VERIFYCODE, DEVICEID)
    VALUES (:1, :2, :3, :4, :5)
"""


class OracleSink:
    def __init__(self):
        self.log = logging.getLogger("oracle")
        self._pool = None
        self._lock = threading.Lock()

    def _get_pool(self):
        with self._lock:
            if self._pool is None:
                self._pool = oracledb.create_pool(
                    user=DB_CONFIG["user"],
                    password=DB_CONFIG["password"],
                    dsn=DB_CONFIG["dsn"],
                    min=1, max=4, increment=1,
                    getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                    wait_timeout=30000,
                    ping_interval=30,
                    tcp_connect_timeout=10,
                )
                self.log.debug("Oracle connection pool created.")
            return self._pool

    def _acquire(self):
        try:
            conn = self._get_pool().acquire()
        except Exception:
            self._reset_pool()
            raise
        conn.call_timeout = int(_cfg("oracle_call_timeout_seconds", 60)) * 1000
        return conn

    def _release(self, conn, healthy: bool):
        pool = self._pool
        try:
            if pool is not None and not healthy:
                pool.drop(conn)
            else:
                conn.close()
        except Exception:
            pass

    def _reset_pool(self):
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close(force=True)
                except Exception:
                    pass
            self._pool = None

    def _run(self, fn):
        conn = self._acquire()
        healthy = False
        try:
            result = fn(conn)
            healthy = True
            return result
        finally:
            self._release(conn, healthy)

    def close(self):
        self._reset_pool()

    # -- devices --------------------------------------------------------------
    def fetch_devices(self) -> list[dict]:
        def q(conn):
            cur = conn.cursor()
            cur.execute("SELECT ID, IP, PORT FROM device_machines ORDER BY ID")
            return [{"device_id": int(r[0]), "ip": str(r[1]).strip(),
                     "port": int(r[2]) if r[2] is not None else 4370}
                    for r in cur.fetchall()]
        return self._run(q)

    def write_statuses(self, items):
        if not items:
            return

        def q(conn):
            cur = conn.cursor()
            cur.executemany(
                "UPDATE device_machines SET STATUS = :1, STATUS_TIME = SYSTIMESTAMP WHERE ID = :2",
                [(status, dev) for dev, status in items])
            conn.commit()
        self._run(q)

    # -- punches ----------------------------------------------------------------
    @staticmethod
    def _keys_in_window(cur, device_id: int, start: datetime, end: datetime) -> set:
        cur.execute(
            "SELECT USERID, CHECKTIME FROM checkinout "
            "WHERE DEVICEID = :1 AND CHECKTIME >= :2 AND CHECKTIME < :3",
            [device_id, start, end])
        out = set()
        for uid, ct in cur.fetchall():
            if ct is None or getattr(ct, "microsecond", 0):
                continue
            out.add((str(uid), ct.strftime(TS_FMT)))
        return out

    def push(self, rows: list[tuple]):
        """
        rows: (device_id, user_id, checktime_str, status, punch, attempts)
        Inserts the rows, commits, then reads them back.
        Returns (confirmed_keys, failures{key: (attempts, error)}, inserted, dup)
        """
        def q(conn):
            cur = conn.cursor()
            params = [(uid, str_to_ts(ct), checktype_for(st, pu), st, dev)
                      for dev, uid, ct, st, pu, _a in rows]
            cur.executemany(INSERT_SQL, params, batcherrors=True)
            errors = {e.offset: e for e in cur.getbatcherrors()}
            conn.commit()

            # ---- read-back verification -----------------------------------
            windows: dict[tuple[int, str], None] = {}
            for dev, _uid, ct, *_ in rows:
                windows[(dev, ct[:10])] = None
            present: set = set()
            for dev, day in windows:
                start = datetime.strptime(day, "%Y-%m-%d")
                for uid, ct in self._keys_in_window(cur, dev, start, start + timedelta(days=1)):
                    present.add((dev, uid, ct))

            confirmed, failures = [], {}
            inserted = dup = 0
            for i, (dev, uid, ct, _st, _pu, attempts) in enumerate(rows):
                key = (dev, uid, ct)
                err = errors.get(i)
                if key in present:
                    confirmed.append(key)
                    if err is None:
                        inserted += 1
                    else:
                        dup += 1
                    continue
                if err is None:
                    msg = "row not found in Oracle after commit"
                else:
                    msg = str(err.message).strip()
                    if not msg.startswith("ORA-"):
                        msg = f"ORA-{err.code:05d}: {msg}"
                    if err.code == 1:
                        msg += (" | unique violation but this punch is NOT in checkinout: the ID "
                                "primary key/identity probably collided with existing IDs")
                failures[key] = (attempts, msg)
            return confirmed, failures, inserted, dup

        return self._run(q)

    def keys_since(self, device_id: int, since: datetime) -> set:
        def q(conn):
            cur = conn.cursor()
            cur.arraysize = 5000
            return self._keys_in_window(cur, device_id, since, datetime(9999, 1, 1))
        return self._run(q)


# ── Per-device collector ───────────────────────────────────────────────────────
class DeviceWorker(threading.Thread):
    def __init__(self, device: dict, journal: Journal, board: StatusBoard):
        super().__init__(name=f"Device-{device['device_id']}", daemon=True)
        self.device = device
        self.device_id = device["device_id"]
        self.journal = journal
        self.board = board
        self.stop_event = threading.Event()
        self.log = logging.getLogger(self.name)

        self.last_count = None          # record count at last full read
        self.last_full_read = 0.0       # monotonic
        self.last_success = None        # datetime of last successful poll
        self.device_records = None
        self.capacity = 0
        self.consecutive_failures = 0
        self.last_error = None
        self._last_capacity_warn = 0.0

    def stop(self):
        self.stop_event.set()

    def _make_zk(self) -> ZK:
        return ZK(
            self.device["ip"],
            port=self.device["port"],
            timeout=int(_cfg("device_timeout_seconds", 15)),
            password=int(_cfg("device_password", 0)),
            force_udp=bool(_cfg("force_udp", False)),
            # pyzk's ping uses the Windows `ping` command, which is slow and
            # unreliable; the TCP connect itself tells us if the device is up.
            ommit_ping=True,
        )

    @staticmethod
    def _close(zk_conn):
        try:
            zk_conn.disconnect()
        except Exception:
            pass
        try:
            zk_conn._ZK__sock.close()   # make sure the socket is released
        except Exception:
            pass

    def _read_with_retries(self, zk_conn):
        last_exc = None
        for attempt in range(1, 4):
            try:
                return read_attendance(zk_conn)
            except AttendanceReadError as exc:
                last_exc = exc
                self.log.warning("Inconsistent log read (attempt %d/3): %s", attempt, exc)
                time.sleep(1)
        raise last_exc

    def _check_log_reduction(self, zk_conn, count: int) -> int:
        """
        The device's record count can only go DOWN if records were deleted
        (by ZKTeco software "delete after download", a scheduled clear, or on
        the device menu). Records deleted before this app read them cannot be
        recovered, so make it loud. Works across restarts (count is persisted).
        """
        state = self.journal.device_state(self.device_id)
        if not state or state["last_count"] is None or count >= state["last_count"]:
            return count
        # Re-check once so a glitchy reading never raises a false alarm.
        zk_conn.read_sizes()
        count = zk_conn.records
        if count >= state["last_count"]:
            return count
        last_read = state["last_read_at"] or "unknown"
        self.journal.record_log_reduction(self.device_id, state["last_count"], count, state["last_read_at"])
        self.log.critical(
            "ATTENDANCE LOG ON THIS DEVICE WAS DELETED: it had %d record(s) at the last read (%s), now %d. "
            "Something else cleared it (ZKTeco software 'delete after download', a scheduled clear, or the "
            "device menu). Every record read before %s is safe in the journal, but punches made AFTER %s "
            "and before the clear were never read and CANNOT be recovered. Turn off any 'delete/clear "
            "after download' option in other software. (If you cleared it yourself after checking "
            "'Waiting for Oracle' was 0 in the Devices tab, nothing was lost.)",
            state["last_count"], last_read, count, last_read, last_read)
        return count

    def _poll_once(self):
        zk_conn = self._make_zk().connect()
        try:
            zk_conn.read_sizes()
            count = zk_conn.records
            self.device_records = count
            self.capacity = zk_conn.rec_cap
            count = self._check_log_reduction(zk_conn, count)

            full_due = time.monotonic() - self.last_full_read >= int(_cfg("full_read_interval_seconds", 600))
            if self.last_count is not None and count == self.last_count and not full_due:
                return  # nothing new on the device

            disabled = False
            if _cfg("disable_device_during_read", False):
                zk_conn.disable_device()
                disabled = True
            try:
                result = self._read_with_retries(zk_conn)
            finally:
                if disabled:
                    try:
                        zk_conn.enable_device()
                    except Exception as exc:
                        self.log.error("Could not re-enable device: %s", exc)

            # --- persist (fsync'd) before anything else ---------------------
            added = self.journal.add_records(self.device_id, result.records)
            for b in self.journal.add_bad(self.device_id, result.bad):
                self.log.error("Undecodable record kept in journal.bad_records: %s (%s)", b.raw_hex, b.error)

            # --- double check: every record read is now in the journal ------
            distinct = len({(r.user_id, r.timestamp) for r in result.records})
            in_journal = self.journal.count_for_device(self.device_id)
            if in_journal < distinct:
                raise RuntimeError(
                    f"journal has {in_journal} records but device returned {distinct} distinct records")

            self.last_count = result.device_count
            self.last_full_read = time.monotonic()
            self.journal.set_device_count(self.device_id, result.device_count)
            self.device_records = result.device_count
            if added:
                self.log.info("Read %d record(s) from device (layout %d bytes); %d NEW saved to journal.",
                              len(result.records), result.record_size, added)

            if self.capacity and result.device_count >= 0.9 * self.capacity \
                    and time.monotonic() - self._last_capacity_warn > 3600:
                self._last_capacity_warn = time.monotonic()
                self.log.warning(
                    "Device attendance log is %d%% full (%d/%d). All records are already saved "
                    "in the journal; consider clearing the device log to avoid overwrite/stop.",
                    100 * result.device_count // self.capacity, result.device_count, self.capacity)
        finally:
            self._close(zk_conn)

    def run(self):
        poll = int(_cfg("poll_interval_seconds", 60))
        min_delay = int(_cfg("reconnect_min_delay_seconds", 5))
        max_delay = int(_cfg("reconnect_max_delay_seconds", 60))
        delay = min_delay
        self.log.info("Collector started for %s:%s.", self.device["ip"], self.device["port"])

        while not self.stop_event.is_set():
            try:
                self._poll_once()
                if self.consecutive_failures:
                    self.log.info("Device reachable again after %d failed attempt(s).",
                                  self.consecutive_failures)
                self.consecutive_failures = 0
                self.last_error = None
                self.last_success = datetime.now()
                self.board.set(self.device_id, "ONLINE")
                delay = min_delay
                self.stop_event.wait(poll)
            except Exception as exc:
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.board.set(self.device_id, "OFFLINE")
                if self.consecutive_failures == 1:
                    self.log.error("Device poll failed: %s", self.last_error, exc_info=True)
                else:
                    self.log.warning("Device still unreachable (attempt %d): %s",
                                     self.consecutive_failures, self.last_error)
                self.log.info("Reconnecting in %ss...", delay)
                self.stop_event.wait(delay)
                delay = min(delay * 2, max_delay)

        self.log.info("Collector stopped.")


# ── Journal -> Oracle ──────────────────────────────────────────────────────────
class OracleSyncWorker(threading.Thread):
    def __init__(self, journal: Journal, sink: OracleSink, board: StatusBoard, stop: threading.Event):
        super().__init__(name="OracleSync", daemon=False)
        self.journal = journal
        self.sink = sink
        self.board = board
        self.stop = stop
        self.log = logging.getLogger(self.name)
        self._down_since = None
        self._last_down_log = 0.0
        self._last_status_err = 0.0

    def _write_statuses(self):
        """Device status is informational: a failure here must never block records."""
        items = self.board.due()
        if not items:
            return
        try:
            self.sink.write_statuses(items)
            self.board.mark_written(items)
        except Exception as exc:
            if time.monotonic() - self._last_status_err >= 600:
                self._last_status_err = time.monotonic()
                self.log.warning("Could not update device_machines.STATUS (records are not affected): %s", exc)

    def _cycle(self):
        self._write_statuses()

        batch = int(_cfg("oracle_batch_size", 500))
        while True:
            rows = self.journal.fetch_pending(batch)
            if not rows:
                return
            confirmed, failures, inserted, dup = self.sink.push(rows)
            self.journal.mark_synced(confirmed)
            self.journal.mark_failed(failures)
            self.log.info("Oracle sync: %d sent, %d inserted, %d already present, %d verified, %d failed.",
                          len(rows), inserted, dup, len(confirmed), len(failures))
            for (dev, uid, ct), (attempts, err) in failures.items():
                self.log.error("NOT synced (kept in journal, will retry) device=%s user=%s time=%s "
                               "attempt=%d: %s", dev, uid, ct, attempts + 1, err)
            if len(rows) < batch or self.stop.is_set():
                return

    def run(self):
        interval = int(_cfg("oracle_sync_interval_seconds", 5))
        backoff = 2
        self.log.info("Oracle sync worker started.")
        while True:
            final = self.stop.is_set()
            try:
                self._cycle()
                if self._down_since:
                    self.log.info("Oracle reachable again (was down since %s).",
                                  self._down_since.strftime(TS_FMT))
                self._down_since = None
                backoff = 2
                wait = interval
            except Exception as exc:
                now = time.monotonic()
                if self._down_since is None:
                    self._down_since = datetime.now()
                    self._last_down_log = now
                    self.log.error("Oracle sync failed (records stay safe in journal): %s", exc)
                elif now - self._last_down_log >= 600:
                    self._last_down_log = now
                    self.log.error("Oracle still unavailable since %s: %s",
                                   self._down_since.strftime(TS_FMT), exc)
                wait = backoff
                backoff = min(backoff * 2, 60)
            if final:
                break
            self.stop.wait(wait)
        self.log.info("Oracle sync worker stopped.")


# ── Audit: journal vs Oracle ───────────────────────────────────────────────────
class AuditWorker(threading.Thread):
    def __init__(self, journal: Journal, sink: OracleSink, stop: threading.Event):
        super().__init__(name="Audit", daemon=True)
        self.journal = journal
        self.sink = sink
        self.stop = stop
        self.log = logging.getLogger(self.name)

    def audit_once(self) -> int:
        days = int(_cfg("audit_lookback_days", 31))
        since = (datetime.now() - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        since_str = ts_to_str(since)
        total_missing = 0
        for dev in self.journal.device_ids():
            if self.stop.is_set():
                break
            # Journal first, then Oracle: every key read here was verified in
            # Oracle before being marked synced, so a miss means it vanished.
            j = self.journal.synced_keys_since(dev, since_str)
            if not j:
                continue
            o = self.sink.keys_since(dev, since)
            missing = j - o
            if missing:
                total_missing += len(missing)
                self.journal.mark_unsynced([(dev, uid, ct) for uid, ct in missing])
                self.log.warning("Device %s: %d synced record(s) missing in Oracle -> re-queued. e.g. %s",
                                 dev, len(missing), sorted(missing)[:5])
            else:
                self.log.info("Device %s: all %d journal record(s) since %s present in Oracle.",
                              dev, len(j), since.date())
        return total_missing

    def run(self):
        interval = int(_cfg("audit_interval_seconds", 3600))
        if self.stop.wait(300):  # first audit 5 minutes after start
            return
        while not self.stop.is_set():
            try:
                self.audit_once()
            except Exception as exc:
                self.log.error("Audit failed (will retry): %s", exc)
            self.stop.wait(interval)


# ── Supervisor ─────────────────────────────────────────────────────────────────
class Supervisor:
    def __init__(self):
        self.log = logging.getLogger("main")
        self.stop = threading.Event()
        self.journal = Journal(_app_path(_cfg("journal_path", "zkteco_journal.db")))
        self.sink = OracleSink()
        self.board = StatusBoard()
        self.workers: dict[int, DeviceWorker] = {}

    def _load_devices(self):
        try:
            devices = self.sink.fetch_devices()
            self.journal.cache_devices(devices)
            return devices
        except Exception as exc:
            cached = self.journal.cached_devices()
            if cached:
                self.log.warning("Cannot read device_machines from Oracle (%s); using %d cached device(s).",
                                 exc, len(cached))
                return cached
            self.log.error("Cannot read device_machines from Oracle and no cached list yet: %s", exc)
            return None

    def _apply_devices(self, devices):
        wanted = {d["device_id"]: d for d in devices}
        for dev_id, w in list(self.workers.items()):
            d = wanted.get(dev_id)
            if d is None or (d["ip"], d["port"]) != (w.device["ip"], w.device["port"]) or not w.is_alive():
                reason = "removed" if d is None else "changed/restarted"
                self.log.info("Stopping collector for device %s (%s).", dev_id, reason)
                w.stop()
                w.join(timeout=60)
                del self.workers[dev_id]
        for dev_id, d in wanted.items():
            if dev_id not in self.workers:
                w = DeviceWorker(d, self.journal, self.board)
                self.workers[dev_id] = w
                w.start()

    def _summary(self):
        stats = self.journal.stats()
        for dev_id, w in sorted(self.workers.items()):
            s = stats.get(dev_id, {"total": 0, "pending": 0, "failing": 0})
            age = (datetime.now() - w.last_success).total_seconds() if w.last_success else None
            self.log.info(
                "[summary] device %s %s:%s | %s | last OK read: %s | on device: %s | journal: %d "
                "| pending Oracle: %d | failing: %d",
                dev_id, w.device["ip"], w.device["port"],
                "ONLINE" if w.consecutive_failures == 0 and w.last_success else "OFFLINE",
                w.last_success.strftime(TS_FMT) if w.last_success else "never",
                w.device_records if w.device_records is not None else "?",
                s["total"], s["pending"], s["failing"])
            if age is None or age > 600:
                self.log.warning("[summary] device %s has not been read for %s. Last error: %s",
                                 dev_id, "ever" if age is None else f"{int(age // 60)} min", w.last_error)
        for dev, uid, ct, attempts, err in self.journal.failing_samples():
            self.log.warning("[summary] failing record device=%s user=%s time=%s attempts=%d: %s",
                             dev, uid, ct, attempts, err)
        bad = self.journal.bad_count()
        if bad:
            self.log.warning("[summary] %d undecodable record(s) stored in journal table bad_records.", bad)
        day_ago = (datetime.now() - timedelta(days=1)).strftime(TS_FMT)
        for dev, st in sorted(self.journal.all_device_states().items()):
            if st["cleared_at"] and st["cleared_at"] >= day_ago:
                self.log.error("[summary] device %s: attendance log was deleted at %s (%s -> %s records); "
                               "punches after %s and before that may be lost.",
                               dev, st["cleared_at"], st["cleared_from"], st["cleared_to"],
                               st["cleared_after"] or "the last read")

    def run(self, install_signals: bool = True):
        self.log.info("Starting ZKTeco sync service. Journal: %s", self.journal.path)

        def _shutdown(sig, frame):
            self.log.info("Shutdown signal received - stopping...")
            self.stop.set()

        if install_signals:
            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, _shutdown)

        sync = OracleSyncWorker(self.journal, self.sink, self.board, self.stop)
        audit = AuditWorker(self.journal, self.sink, self.stop)
        sync.start()
        audit.start()

        refresh = int(_cfg("device_refresh_interval_seconds", 300))
        summary_every = int(_cfg("status_summary_interval_seconds", 600))
        next_refresh = 0.0
        next_summary = time.monotonic() + 120
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= next_refresh:
                devices = self._load_devices()
                if devices is not None:
                    if not devices:
                        self.log.error("No devices in device_machines.")
                    self._apply_devices(devices)
                    next_refresh = now + refresh
                else:
                    next_refresh = now + 30
            if now >= next_summary:
                try:
                    self._summary()
                except Exception as exc:
                    self.log.error("Summary failed: %s", exc)
                next_summary = now + summary_every
            self.stop.wait(1)

        for w in self.workers.values():
            w.stop()
        for w in self.workers.values():
            w.join(timeout=60)
        sync.join(timeout=120)   # final flush of pending records to Oracle
        self.sink.close()
        self.log.info("Stopped. Unsynced records (if any) remain in the journal and "
                      "will be sent on next start.")


def print_status():
    """`--status`: print what the local journal holds (no device/Oracle access)."""
    path = _app_path(_cfg("journal_path", "zkteco_journal.db"))
    if not os.path.exists(path):
        print(f"No journal yet at {path}")
        return
    j = Journal(path)
    stats = j.stats()
    last = dict(j._conn().execute(
        "SELECT device_id, MAX(checktime) FROM punches GROUP BY device_id").fetchall())
    print(f"Journal: {path}\n")
    print(f"{'Device':>6} {'Records':>9} {'Pending':>8} {'Failing':>8}  Latest punch")
    for dev in sorted(stats):
        s = stats[dev]
        print(f"{dev:>6} {s['total']:>9} {s['pending']:>8} {s['failing']:>8}  {last.get(dev, '')}")
    if not stats:
        print("   (no records yet)")
    for dev, uid, ct, attempts, err in j.failing_samples(20):
        print(f"\nFAILING device={dev} user={uid} time={ct} attempts={attempts}\n   {err}")
    bad = j.bad_count()
    if bad:
        print(f"\n{bad} undecodable record(s) in table bad_records.")
    pending = sum(s["pending"] for s in stats.values())
    print("\nAll records are in Oracle." if pending == 0 else
          f"\n{pending} record(s) not yet in Oracle (they will be retried automatically).")


def run_headless():
    """Run the collector without UI (console, Task Scheduler)."""
    setup_logging()
    lock = InstanceLock()
    if not lock.acquire():
        logging.getLogger("main").error(
            "Another copy of the sync service is already running (lock: %s). Exiting.", LOCK_PATH)
        sys.exit(2)
    try:
        sup = Supervisor()
        watch_launcher(sup.stop.set)
        sup.run()
    finally:
        lock.release()


def main():
    if "--status" in sys.argv[1:]:
        print_status()
        return
    run_headless()


if __name__ == "__main__":
    main()
