"""
Robust reader for the attendance log stored on a ZKTeco device.

pyzk's own `get_attendance()` has two problems that can lose records:

1. It derives the record size as `total_bytes / record_count`, where
   `record_count` is read in a separate command *before* the log buffer.
   If someone punches in between, the division is no longer exactly 8/16/40,
   and pyzk silently parses the whole log with the wrong layout (garbage).
2. One record with an undecodable timestamp raises inside the loop and the
   entire read fails - forever, on every retry - so nothing is collected.

This module uses pyzk only for the transport (connect / read buffer) and does
its own validated parsing. Field semantics and user_id formatting are kept
IDENTICAL to pyzk so keys match rows already inserted by the old app.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
from datetime import datetime
from struct import unpack

from zk import const

RECORD_SIZES = (40, 16, 8)


class AttendanceReadError(Exception):
    """The read was inconsistent/incomplete. Nothing was lost; retry later."""


@dataclass(frozen=True)
class DeviceRecord:
    user_id: str
    timestamp: datetime
    status: int      # verify method (1 = fingerprint, 15 = face, ...)
    punch: int       # punch state (0 = check-in, 1 = check-out, ...)


@dataclass(frozen=True)
class BadRecord:
    raw_hex: str
    error: str


@dataclass
class ReadResult:
    records: list
    bad: list
    record_size: int
    device_count: int     # records the device reported before the read
    capacity: int         # attendance log capacity (0 if unknown)


def decode_time(raw4: bytes) -> datetime:
    """Same algorithm as pyzk / zkemsdk.c DecodeTime; raises ValueError."""
    t = unpack("<I", raw4)[0]
    second = t % 60
    t //= 60
    minute = t % 60
    t //= 60
    hour = t % 24
    t //= 24
    day = t % 31 + 1
    t //= 31
    month = t % 12 + 1
    t //= 12
    year = t + 2000
    return datetime(year, month, day, hour, minute, second)


def detect_record_size(total_size: int, expected_count: int) -> int:
    """
    Pick the record layout whose record count is consistent with the count
    the device reported. Raises AttendanceReadError if none is consistent.
    """
    candidates = [s for s in RECORD_SIZES if total_size % s == 0]
    if not candidates:
        raise AttendanceReadError(
            f"log size {total_size} bytes is not a multiple of 8/16/40")
    if expected_count <= 0:
        # Device reported 0 records but sent data; only trust an unambiguous size.
        if len(candidates) == 1:
            return candidates[0]
        raise AttendanceReadError(
            f"device reported 0 records but sent {total_size} bytes")
    best = min(candidates, key=lambda s: abs(total_size // s - expected_count))
    diff = abs(total_size // best - expected_count)
    # A few punches can happen between the count and the read; anything more
    # means the read is not trustworthy.
    tolerance = max(20, expected_count // 50)
    if diff > tolerance:
        raise AttendanceReadError(
            f"log size {total_size} bytes does not match reported count "
            f"{expected_count} for any record layout")
    return best


def parse_attendance(data: bytes, record_size: int, users: list) -> tuple[list, list]:
    """Parse the record area (without the 4-byte length header)."""
    records: list[DeviceRecord] = []
    bad: list[BadRecord] = []

    uid_to_user_id = {u.uid: u.user_id for u in users}

    for off in range(0, len(data) - record_size + 1, record_size):
        chunk = data[off:off + record_size]
        try:
            if record_size == 8:
                uid, status, ts, punch = unpack("<HB4sB", chunk)
                user_id = uid_to_user_id.get(uid, str(uid))
            elif record_size == 16:
                user_id, ts, status, punch, _res, _wc = unpack("<I4sBB2sI", chunk)
                user_id = str(user_id)
            else:
                _uid, user_id, status, ts, punch, _sp = unpack("<H24sB4sB8s", chunk)
                user_id = user_id.split(b"\x00")[0].decode(errors="ignore")
            timestamp = decode_time(ts)
            if not user_id or not str(user_id).strip():
                raise ValueError("empty user_id")
            records.append(DeviceRecord(str(user_id), timestamp, int(status), int(punch)))
        except Exception as exc:  # corrupt record: keep it, never drop silently
            bad.append(BadRecord(binascii.hexlify(chunk).decode(), f"{type(exc).__name__}: {exc}"))

    return records, bad


def read_attendance(zk_conn) -> ReadResult:
    """
    Read and validate the full attendance log from a connected pyzk ZK object.
    Raises AttendanceReadError (or a pyzk/network error) if the read cannot be
    trusted; in that case nothing is returned, so nothing is half-processed.
    """
    # Same order as pyzk.get_attendance(): users first (needed for 8-byte layout).
    users = zk_conn.get_users()          # also calls read_sizes()
    user_packet_size = zk_conn.user_packet_size
    device_users = zk_conn.users
    zk_conn.read_sizes()
    expected = zk_conn.records
    capacity = zk_conn.rec_cap

    if expected == 0:
        return ReadResult([], [], 0, 0, capacity)

    data, size = zk_conn.read_with_buffer(const.CMD_ATTLOG_RRQ)
    if not data or size < 4 or len(data) < 4:
        raise AttendanceReadError(
            f"device reports {expected} records but returned no log data")

    total_size = unpack("<I", data[:4])[0]
    body = data[4:]
    if len(body) < total_size:
        raise AttendanceReadError(
            f"truncated log: expected {total_size} bytes, got {len(body)}")
    body = body[:total_size]

    record_size = detect_record_size(total_size, expected)

    if record_size == 8 and device_users > 0:
        # 8-byte records only carry the internal uid; mapping to user_id needs
        # a correct user list, otherwise records would get the wrong USERID.
        if not users or user_packet_size not in (28, 72):
            raise AttendanceReadError(
                "user list could not be read reliably (needed to map uid -> user_id)")

    records, bad = parse_attendance(body, record_size, users)
    if len(records) + len(bad) != total_size // record_size:
        raise AttendanceReadError("parsed record count mismatch")

    return ReadResult(records, bad, record_size, expected, capacity)
