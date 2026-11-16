# ZKTeco → Oracle Attendance Sync

Collects every attendance record (fingerprint, face, card, password) from ZKTeco devices on the
local network and stores it in the Oracle table `checkinout`.

* **No record is ever dropped.** Each record is saved locally first, inserted into Oracle, then
  read back from Oracle to confirm it is there, and re-checked every hour.
* **Automatic reconnection.** Devices that go offline and Oracle outages are retried forever.
  Nothing is lost while something is down.
* **Works fully offline.** It needs no internet connection, only the local network.
* Records normally reach Oracle within about **1 minute** of the punch. It is not instant, but it is complete.

---

## Contents
1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Create the database tables](#3-create-the-database-tables)
4. [Register the devices](#4-register-the-devices)
5. [Configure the connection](#5-configure-the-connection)
6. [Run the app](#6-run-the-app)
7. [Check that it is working](#7-check-that-it-is-working)
8. [Run automatically at Windows startup](#8-run-automatically-at-windows-startup)
9. [Build a standalone .exe](#9-build-a-standalone-exe)
10. [Daily operation](#10-daily-operation)
11. [Troubleshooting](#11-troubleshooting)
12. [Settings reference](#12-settings-reference)
13. [How it works](#13-how-it-works)

---

## 1. Requirements

| Item | Details |
|---|---|
| PC / server | Windows 10/11 or Windows Server, always on, on the same network as the devices |
| Python | 3.10 or newer (only needed if you are not using the pre-built `.exe`) |
| Oracle | Any Oracle Database reachable over the network. No Oracle Client install is needed. |
| Devices | ZKTeco fingerprint / face terminals with TCP/IP enabled (default port **4370**) |
| Network | The PC must reach each device on TCP 4370 and Oracle on TCP 1521 (or your listener port) |

---

## 2. Install

### Option A: PC with internet access
```bat
cd C:\ZKTecoSync
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

### Option B: offline PC (no internet)
On **any PC with internet** that has the same Python version and Windows 64-bit:
```bat
pip download -r requirements.txt -d wheels
```
Copy the project folder **and** the `wheels` folder to the offline PC, then:
```bat
cd C:\ZKTecoSync
python -m venv .venv
.venv\Scripts\activate.bat
pip install --no-index --find-links wheels -r requirements.txt
```

> Easiest offline option: build the `.exe` once on a PC with internet ([section 9](#9-build-a-standalone-exe))
> and copy only `zkteco_sync.exe` + `settings.env` to the server. Python is then not needed.

---

## 3. Create the database tables

Skip this step if `device_machines` and `checkinout` already exist.

```bat
sqlplus USER/PASSWORD@//DB_HOST:1521/SERVICE_NAME @setup_db.sql
```

This creates:
* `device_machines`: one row per device (`ID`, `IP`, `PORT`, `STATUS`, `STATUS_TIME`)
* `checkinout`: attendance records (`USERID`, `CHECKTIME`, `CHECKTYPE`, `VERIFYCODE`, `DEVICEID`),
  with a unique key on (`USERID`, `CHECKTIME`, `DEVICEID`) so a record can never be stored twice.

---

## 4. Register the devices

Add one row per device. `ID` is the number that will appear in `checkinout.DEVICEID`.

```sql
INSERT INTO device_machines (ID, IP, PORT) VALUES (1, '192.168.1.201', 4370);
INSERT INTO device_machines (ID, IP, PORT) VALUES (2, '192.168.1.202', 4370);
INSERT INTO device_machines (ID, IP, PORT) VALUES (3, '192.168.1.203', 4370);
COMMIT;
```

* You can add, remove or change devices **while the app is running**. Changes are picked up within 5 minutes.
* On each device check **Menu → Comm → Ethernet** (IP address) and **Comm → PC Connection**
  (Comm Key / port). If a Comm Key is set, put it in `device_password` ([section 12](#12-settings-reference)).
  It is the same key for all devices.

---

## 5. Configure the connection

Edit **`settings.env`** in the app folder:

```ini
DB_USER=attendance_user
DB_PASSWORD=your_password
DB_DSN=192.168.1.10:1521/ORCL
```

`DB_DSN` formats: `host:port/service_name` (most common) or `host:port:SID`.

> Values in `settings.env` override Windows environment variables with the same name.

All other settings (poll interval, retry times …) are in `config.py` and have sensible defaults.
See [section 12](#12-settings-reference).

---

## 6. Run the app

```bat
cd C:\ZKTecoSync
.venv\Scripts\activate.bat
python sync_service.py
```
or, with the built executable:
```bat
zkteco_sync.exe
```

What you should see in the first minute:
```
[INFO] main: Starting ZKTeco sync service. Journal: C:\ZKTecoSync\zkteco_journal.db
[INFO] oracle: Oracle connection pool created.
[INFO] Device-1: Collector started for 192.168.1.201:4370.
[INFO] Device-1: Read 15230 record(s) from device (layout 40 bytes); 15230 NEW saved to journal.
[INFO] OracleSync: Oracle sync: 500 sent, 12 inserted, 488 already present, 500 verified, 0 failed.
```

On the **first run** the app reads every record stored on every device and checks each one against
Oracle. Records that are already there are counted as `already present` and are not duplicated.
This can take a few minutes for large devices. After that, only new records are sent.

**Stop the app** with `Ctrl+C`. It finishes sending what it can and exits. Anything not yet sent
stays in the journal and is sent on the next start, so stopping or even a power cut loses nothing.

---

## 7. Check that it is working

### a) Status command (works any time, even while the app is running)
```bat
python sync_service.py --status
```
or `zkteco_sync.exe --status`
```
Device   Records  Pending  Failing  Latest punch
     1     15230        0        0  2026-11-16 08:41:06
     2      9874        0        0  2026-11-16 08:40:13

All records are in Oracle.
```
* **Records**: records collected from that device so far.
* **Pending**: collected but not yet confirmed in Oracle (normally 0, or small for a few seconds).
* **Failing**: Oracle rejected them. The reason is printed below the table. They are kept and retried.

### b) The log file `zkteco_sync.log`
Every 10 minutes the log writes a `[summary]` line per device:
```
[summary] device 1 192.168.1.201:4370 | ONLINE | last OK read: 2026-11-16 08:41:10 | on device: 15230 | journal: 15230 | pending Oracle: 0 | failing: 0
```
Lines to look out for:

| Log text | Meaning |
|---|---|
| `Device poll failed` / `Device still unreachable` | Device offline or unreachable. Retrying automatically. |
| `Device reachable again` | Device is back. Anything punched meanwhile is collected now. |
| `Oracle sync failed (records stay safe in journal)` | Oracle is down. Records are waiting locally. |
| `Oracle reachable again` | Waiting records are being sent. |
| `NOT synced (kept in journal, will retry)` | Oracle rejected a record. The reason follows, see [Troubleshooting](#11-troubleshooting). |
| `missing in Oracle -> re-queued` | The hourly audit found records deleted from Oracle and is re-inserting them. |
| `attendance log is 90% full` | Clear the device log soon (see [section 10](#10-daily-operation)). |

### c) In Oracle
```sql
-- device status (updated every minute)
SELECT ID, IP, STATUS, STATUS_TIME FROM device_machines;

-- today's records per device
SELECT DEVICEID, COUNT(*) FROM checkinout
 WHERE CHECKTIME >= TRUNC(SYSDATE) GROUP BY DEVICEID;
```

---

## 8. Run automatically at Windows startup

Use the built-in **Task Scheduler** (no extra software needed):

1. Open **Task Scheduler** → **Create Task…**
2. **General**: Name `ZKTeco Sync`; select **Run whether user is logged on or not** and **Run with highest privileges**.
3. **Triggers** → New → **At startup**.
4. **Actions** → New → Start a program:
   * Program: `C:\ZKTecoSync\zkteco_sync.exe`
     (or `C:\ZKTecoSync\.venv\Scripts\python.exe` with argument `sync_service.py`)
   * **Start in**: `C:\ZKTecoSync`   ← required
5. **Settings**: tick **If the task fails, restart every 1 minute**, attempts `999`;
   untick **Stop the task if it runs longer than…**
6. OK, then right-click the task → **Run**.

Only run **one copy** of the app at a time.

---

## 9. Build a standalone .exe

On a PC with Python and the requirements installed:
```bat
pip install pyinstaller
python -m PyInstaller --onefile --add-data "settings.env;." --name zkteco_sync ^
  --hidden-import oracledb ^
  --hidden-import zk ^
  --hidden-import sqlite3 ^
  --hidden-import journal ^
  --hidden-import zk_reader ^
  --hidden-import getpass ^
  --hidden-import socket ^
  --hidden-import struct ^
  --hidden-import threading ^
  --hidden-import signal ^
  --hidden-import cryptography ^
  --hidden-import cryptography.x509 ^
  --hidden-import cryptography.hazmat.primitives ^
  --hidden-import cryptography.hazmat.primitives.kdf ^
  --hidden-import cryptography.hazmat.primitives.kdf.pbkdf2 ^
  --hidden-import cryptography.hazmat.primitives.kdf.scrypt ^
  --hidden-import cryptography.hazmat.primitives.ciphers ^
  --hidden-import cryptography.hazmat.primitives.ciphers.algorithms ^
  --hidden-import cryptography.hazmat.primitives.ciphers.modes ^
  --hidden-import cryptography.hazmat.primitives.asymmetric ^
  --hidden-import cryptography.hazmat.primitives.asymmetric.rsa ^
  --hidden-import cryptography.hazmat.primitives.asymmetric.padding ^
  --hidden-import cryptography.hazmat.primitives.serialization ^
  --hidden-import cryptography.hazmat.primitives.hashes ^
  --hidden-import cryptography.hazmat.primitives.hmac ^
  --hidden-import cryptography.hazmat.backends ^
  --hidden-import cryptography.hazmat.backends.openssl ^
  --hidden-import cryptography.hazmat.backends.openssl.backend ^
  --hidden-import cryptography.hazmat.bindings ^
  --hidden-import cryptography.hazmat.bindings._rust ^
  sync_service.py
```
The result is `dist\zkteco_sync.exe`. Copy it to the server folder together with `settings.env`.
A `settings.env` placed next to the `.exe` overrides the one built into it, so you can change the
password without rebuilding.

The journal (`zkteco_journal.db`) and log are created **next to the .exe**.

---

## 10. Daily operation

### Clearing a device's attendance log safely
ZKTeco devices have limited memory. Before clearing a device's log (on the device, or with ZKTime):
1. Wait at least 2 minutes after the last punch.
2. Run `zkteco_sync.exe --status`. That device must show **Pending 0** and **Failing 0**, and
   **Latest punch** must match the latest punch on the device.
3. Now clear the log.

> The only way a record can be lost is if it is deleted from a device **before the app has read it**,
> for example clearing a device while the app is stopped. Always follow the steps above.

### Backups
Back up the app folder including **`zkteco_journal.db`**. It is a complete local copy of every
record ever collected. **Never delete it** while records are pending.

### Moving the app to another PC
Stop the app, copy the whole folder (including `zkteco_journal.db`), start it on the new PC.

### Deleting test data from Oracle
The app is designed to put back anything missing, so rows deleted from `checkinout` come back:
* records still stored on a device are collected again on the next full read, and
* the hourly audit re-inserts any journal record from the last 31 days.

To remove test punches permanently: clear them from the **device** first (after checking
`--status` as above), then stop the app, delete the rows in Oracle, and delete the same rows
from the journal table `punches` (or delete `zkteco_journal.db` entirely; it is rebuilt from what
is still on the devices). Then start the app again.

### Other attendance software
ZKTeco devices often allow only one PC connection at a time. If ZKTime / ZKBioTime also polls the
devices, some reads will fail while it is connected. They are retried automatically and nothing is
lost, but readings can be delayed.

---

## 11. Troubleshooting

| Problem | Cause / fix |
|---|---|
| `Cannot read device_machines from Oracle and no cached list yet` | Oracle connection settings are wrong or Oracle is down. Check `settings.env`. The app keeps retrying every 30 s. |
| `DPY-6001: Service "..." is not registered with the listener` | Wrong service name in `DB_DSN`, or the database instance is not started. |
| `ORA-01017: invalid username/password` | Fix `DB_USER` / `DB_PASSWORD` in `settings.env`. |
| `Device poll failed: ... timed out` | Wrong IP/port in `device_machines`, device off, cable/switch issue, or firewall. Test with `ping 192.168.1.201` and check that the device's IP is reachable. |
| `Unauthenticated` | The device has a Comm Key. Set `device_password` in `config.py`. |
| `ORA-00001 … this punch is NOT in checkinout: the ID primary key/identity probably collided` | `checkinout.ID` values were inserted manually and the identity counter is behind. Fix: `ALTER TABLE checkinout MODIFY ID GENERATED BY DEFAULT AS IDENTITY (START WITH LIMIT VALUE);`. The waiting records are then inserted automatically. |
| `ORA-02291 integrity constraint … FK_CHECKINOUT_DEVICE` | A device ID was removed from `device_machines` while it still had records. Re-add it. |
| `Inconsistent log read (attempt 1/3)` (occasionally) | Someone punched during the read. The read is retried. This is normal. |
| `Undecodable record kept in journal.bad_records` | The device has a corrupt record (bad date). It is kept in the journal for inspection, and all other records continue normally. |
| Pending count keeps growing | Oracle is unreachable or rejecting records. See the `NOT synced` / `Oracle sync failed` lines in the log. |

---

## 12. Settings reference

Database connection: `settings.env` (`DB_USER`, `DB_PASSWORD`, `DB_DSN`).
Everything else is in `SYNC_CONFIG` in `config.py`:

| Setting | Default | Meaning |
|---|---|---|
| `poll_interval_seconds` | 60 | How often each device is checked for new records |
| `full_read_interval_seconds` | 600 | Re-read the whole device log at least this often, even if the count did not change |
| `device_timeout_seconds` | 15 | Network timeout for device commands |
| `device_password` | 0 | Device Comm Key (0 = none) |
| `force_udp` | False | Use UDP instead of TCP (only for very old devices) |
| `disable_device_during_read` | False | Lock the device keypad while reading (not needed) |
| `reconnect_min_delay_seconds` / `reconnect_max_delay_seconds` | 5 / 60 | Retry delay when a device is unreachable (doubles each time) |
| `journal_path` | `zkteco_journal.db` | Local journal file (relative to the app folder) |
| `oracle_sync_interval_seconds` | 5 | How often waiting records are sent to Oracle |
| `oracle_batch_size` | 500 | Records per Oracle insert batch |
| `oracle_call_timeout_seconds` | 60 | Maximum time for one Oracle call |
| `audit_interval_seconds` | 3600 | How often the journal is re-checked against Oracle |
| `audit_lookback_days` | 31 | How far back the audit checks |
| `device_refresh_interval_seconds` | 300 | How often `device_machines` is re-read |
| `checktype_mode` | `"punch"` | How `CHECKTYPE` is set, see below |
| `log_file`, `log_max_bytes`, `log_backup_count` | `zkteco_sync.log`, 10 MB, 10 | Log file rotation |
| `status_summary_interval_seconds` | 600 | How often the `[summary]` lines are written |

### CHECKTYPE and VERIFYCODE
* `checktype_mode = "punch"` (default): uses the state chosen on the device.
  Check-In / Break-In / OT-In → `I`; Check-Out / Break-Out / OT-Out → `O`.
  Devices without state keys record Check-In (`I`).
* `checktype_mode = "status"`: the behaviour of the old version (fingerprint → `I`, face → `O`).
* `VERIFYCODE` = how the person was verified: 0 password, 1 fingerprint, 2 card, 15 face, etc.

---

## 13. How it works

```
 ZKTeco device memory ──(every 60 s, full log read)──▶ local journal (zkteco_journal.db)
                                                              │  insert + read back
                                                              ▼
                                                        Oracle checkinout
                                                              ▲
                                       hourly audit ──────────┘ (re-sends anything missing)
```

1. **Collect.** Every minute each device is connected. If its record count changed (and at least
   every 10 minutes regardless), the **entire log stored on the device** is read and checked
   ([zk_reader.py](zk_reader.py)). The app does not depend on real-time events, so a missed event
   or a network drop cannot lose a record. The record is still on the device and is read next time.
2. **Save locally.** New records are written to the local journal and flushed to disk
   **before** anything is sent to Oracle ([journal.py](journal.py)).
3. **Send and confirm.** Waiting records are inserted into `checkinout`, then **read back**. A record
   is marked done only when Oracle actually has it. Otherwise it stays waiting and is retried
   (5 s, 10 s, 20 s … up to 1 h). Records already in Oracle are recognised and never duplicated.
4. **Audit.** Every hour all records of the last 31 days in the journal are compared with Oracle,
   and anything missing is sent again.
5. **Reconnect.** Every poll is a new connection. Unreachable devices are marked `OFFLINE` and retried
   forever. When Oracle is down, records wait in the journal. If Oracle is down when the app
   starts, it uses the device list saved from the last run.

### Project files
| File | Purpose |
|---|---|
| `sync_service.py` | The app: device collectors, Oracle sync, audit, supervisor, `--status` |
| `zk_reader.py` | Reads and checks the attendance log stored on a device |
| `journal.py` | Local journal (SQLite) |
| `config.py` | Settings |
| `settings.env` | Database user / password / address |
| `setup_db.sql` | Creates the Oracle tables |
| `zkteco_journal.db` | Created at runtime: local copy of every record. **Keep it, back it up.** |
| `zkteco_sync.log` | Created at runtime: log file (rotated, 10 × 10 MB) |
