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
9. [Build the .exe](#9-build-the-exe)
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

> Easiest offline option: build `ZKTecoSync.exe` once on a PC with internet ([section 9](#9-build-the-exe))
> and copy only that file to the server. Python is then not needed there.

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
  (Comm Key / port). If a Comm Key is set, add `DEVICE_PASSWORD=<key>` to `settings.env` ([section 12](#12-settings-reference)).
  It is the same key for all devices.

---

## 5. Configure the connection

### With the program window (recommended)
1. Open `ZKTecoSync.exe` (or `python sync_gui.py`).
2. On the **Database** tab enter the Oracle **Server** (host name or IP), **Port** (usually 1521),
   **Database** name (tick *Service name* or *SID*), **User** and **Password**.
3. Click **Test connection**. You should see `Connected to Oracle …` and the row counts of
   `device_machines` and `checkinout`. If a table is not accessible, the reason is shown in red.
4. Click **Save**. The settings are written to `settings.env` next to the program. If the service is
   running in the window, you are asked to restart it so it uses the new settings.

### Or by editing `settings.env`
```ini
DB_USER=attendance_user
DB_PASSWORD=your_password
DB_DSN=192.168.1.10:1521/ORCL
AUTOSTART_SERVICE=0
```
`DB_DSN` is `host:port/service_name`. (For a SID the window writes a full Oracle connect descriptor.)

> `settings.env` contains the database password in plain text. Keep the app folder accessible to
> administrators only.

All other settings (poll interval, device Comm Key, retry times …) have sensible defaults and can
be overridden in `settings.env`. See [section 12](#12-settings-reference).

---

## 6. Run the app

### a) With the window
Double-click **`ZKTecoSync.exe`** and click **Start** (top left).

```
 Service: Running   [Start] [Stop]          Records collected: 15,230 | Waiting for Oracle: 0
 ┌──────────┬─────────┬─────────┬──────┐
 │ Database │ Devices │ Records │ Logs │
```

| Part | What it shows / does |
|---|---|
| **Service** (top) | `Running`, `Running in background (another process)` (see section 8), or `Stopped - records are NOT being collected` in red |
| **Start / Stop** | Starts or stops collecting inside this window |
| **Totals** (top right) | Records collected, records still waiting for Oracle, and failing records (red) |
| **Database** tab | Oracle connection settings: test and save ([section 5](#5-configure-the-connection)) |
| **Devices** tab | Every device in `device_machines`: IP, port, ONLINE/OFFLINE, status time, records collected, waiting for Oracle, failing, latest punch, and **Log deleted** (when the device's attendance log was last deleted; the row turns red for 7 days, double-click for details). Click **Refresh** to update. |
| **Records** tab | Every record ever collected. Filter by device, user ID, date range (`YYYY-MM-DD`) and state (*In Oracle / Waiting / Failing*), then **Search**. Shows the newest 2,000; **Export CSV…** saves all matching records in a file Excel can open. Double-click a row for details. |
| **Logs** tab | Live view of `zkteco_sync.log`. Filter *All / Warnings and errors / Errors only / Summary only*. Errors are red and warnings orange. **Open log folder** opens the folder in Explorer. |

Tick **Start the service automatically when this program opens** on the Database tab (then **Save**)
to start collecting as soon as the window opens.

**Closing the window stops collecting** (you are asked first). Nothing is lost: records stay on the
devices and in the journal and are collected next time. For a server that must collect 24/7,
run the background service instead ([section 8](#8-run-automatically-at-windows-startup)).

### b) Without a window (background service)
```bat
ZKTecoSync.exe --service
```
Collects exactly like the window but without one. It logs to `zkteco_sync.log` and is meant for
Task Scheduler ([section 8](#8-run-automatically-at-windows-startup)). You can still open
`ZKTecoSync.exe` normally at the same time to watch devices, records and logs.

### c) From Python (without the .exe)
```bat
cd C:\ZKTecoSync
.venv\Scripts\activate.bat
python sync_gui.py
```
or `python sync_service.py` for the console version (stop it with `Ctrl+C`).

### Only one collector at a time
Only one copy can collect at a time (window **or** background service). A second copy refuses to
start (`Another copy of the sync service is already running`), and the window shows
`Running in background` with Start disabled.

### What happens on the first run
The app reads every record stored on every device and checks each one against Oracle:
```
[INFO] Device-1: Read 15230 record(s) from device (layout 40 bytes); 15230 NEW saved to journal.
[INFO] OracleSync: Oracle sync: 500 sent, 12 inserted, 488 already present, 500 verified, 0 failed.
```
Records already in Oracle are counted as `already present` and are never duplicated. For large
devices this can take a few minutes. After that, only new records are sent.

---

## 7. Check that it is working

### a) In the window
* Top: **Service: Running** and **Waiting for Oracle** at 0 (or small for a few seconds).
* **Devices** tab: every device **ONLINE**, with *Latest punch* matching the newest punch on the device.
* **Records** tab: search for the current date to see the punches of the day.
  State **In Oracle** = confirmed in Oracle; **Waiting** = not sent yet; **Failing** = Oracle
  rejected it (the *Error* column says why, and it is retried automatically).

### b) The log
Every 10 minutes the log writes a `[summary]` line per device (Logs tab → *Summary only*):
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
| `ATTENDANCE LOG ON THIS DEVICE WAS DELETED` (CRITICAL) | The device's record count went **down**, so something deleted its log. Punches made between the time shown and the deletion may be lost. See [Other attendance software](#other-attendance-software). |

### c) Console status (Python only)
`python sync_service.py --status` prints the same per-device counts in a console window.

### d) In Oracle
```sql
-- device status (updated every minute)
SELECT ID, IP, STATUS, STATUS_TIME FROM device_machines;

-- records of the current day per device
SELECT DEVICEID, COUNT(*) FROM checkinout
 WHERE CHECKTIME >= TRUNC(SYSDATE) GROUP BY DEVICEID;
```

---

## 8. Run automatically at Windows startup

Recommended for production: the background service starts with Windows, before anyone logs on,
and restarts itself if it stops.

1. Open **Task Scheduler** → **Create Task…**
2. **General**: Name `ZKTeco Sync`; select **Run whether user is logged on or not** and
   **Run with highest privileges**.
3. **Triggers** → New → **At startup**.
4. **Actions** → New → Start a program:
   * Program: `C:\ZKTecoSync\ZKTecoSync.exe`
   * Arguments: `--service`
   * Start in: `C:\ZKTecoSync`
5. **Settings**: tick **If the task fails, restart every 1 minute**, attempts `999`;
   untick **Stop the task if it runs longer than…**
6. OK, then right-click the task → **Run**.

Then open `ZKTecoSync.exe` normally whenever you want to look. It shows
**Running in background (another process)**, and the Devices, Records and Logs tabs work as usual.

**After changing the database settings** in the window, restart the task: right-click → **End**,
then **Run**.

*Simpler alternative (only while a user is logged on):* tick **Start the service automatically
when this program opens**, then put a shortcut to `ZKTecoSync.exe` in the Startup folder
(`Win+R` → `shell:startup`).

---

## 9. Build the .exe

On a PC with internet (to install PyInstaller), in the project folder:
```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
build_exe.bat
```
The result is **`dist\ZKTecoSync.exe`** (about 18 MB). It contains Python, the Oracle driver and the
device library, so nothing else needs to be installed on the server.

**Deploy:** create `C:\ZKTecoSync`, copy `ZKTecoSync.exe` into it, open it and configure the
Database tab. `settings.env`, `zkteco_journal.db` and `zkteco_sync.log` are created **next to the
.exe**, whatever folder it is started from.

Notes:
* The password is **not** built into the .exe. It lives in `settings.env` next to it.
* The .exe unpacks itself to `%TEMP%` at start (normal for one-file programs). If antivirus blocks
  it, add an exclusion for `C:\ZKTecoSync\ZKTecoSync.exe`.
* When updating to a new version, stop the window/task, replace only `ZKTecoSync.exe`, and start
  again. Keep `zkteco_journal.db` and `settings.env`.

---

## 10. Daily operation

### Clearing a device's attendance log safely
ZKTeco devices have limited memory. Before clearing a device's log (on the device, or with ZKTime):
1. Wait at least 2 minutes after the last punch.
2. Open `ZKTecoSync.exe` → **Devices** tab → **Refresh**. That device must show **Waiting 0** and
   **Failing 0**, and **Latest punch** must match the latest punch on the device.
3. Now clear the log.

Afterwards the app logs `ATTENDANCE LOG ON THIS DEVICE WAS DELETED` and shows the date in the
**Log deleted** column. That is expected when you cleared it yourself after these checks; nothing was lost.

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
the Devices tab as above), then stop the app, delete the rows in Oracle, and delete the same rows
from the journal table `punches` (or delete `zkteco_journal.db` entirely; it is rebuilt from what
is still on the devices). Then start the app again.

### Other attendance software
Best: let this app be the **only** program that talks to the devices. If ZKTime / ZKBioTime /
ZKAccess must also connect:

| Situation | Effect | What to do |
|---|---|---|
| The other software **deletes or clears the log after downloading** (or on a schedule) | **Can lose records.** Punches made since this app's last read are deleted before it sees them. | Turn off every "delete/clear attendance after download" option. The app detects every deletion and logs `ATTENDANCE LOG ON THIS DEVICE WAS DELETED` with the time window at risk. |
| The device allows only one connection and the other software is downloading | Reads fail and are retried (5–60 s). Records are delayed, not lost. | Nothing needed |
| The other software keeps a **permanent connection** (real-time monitoring window) | This app may never get in. Records wait on the device; the device shows OFFLINE and the log warns `has not been read for X min`. | Close the real-time monitoring window |
| A Comm Key is set on the device | `Unauthenticated`; records wait on the device | Add `DEVICE_PASSWORD=<key>` to `settings.env` |

Reading the log never removes anything, so both programs reading at the same time is not a problem.
Whether a device allows one or several connections depends on its firmware. Test with both
programs running and watch the Devices tab for a day.

---

## 11. Troubleshooting

| Problem | Cause / fix |
|---|---|
| `Cannot read device_machines from Oracle and no cached list yet` | Oracle connection settings are wrong or Oracle is down. Check `settings.env`. The app keeps retrying every 30 s. |
| `DPY-6001: Service "..." is not registered with the listener` | Wrong service name in `DB_DSN`, or the database instance is not started. |
| `ORA-01017: invalid username/password` | Fix `DB_USER` / `DB_PASSWORD` in `settings.env`. |
| `Device poll failed: ... timed out` | Wrong IP/port in `device_machines`, device off, cable/switch issue, or firewall. Test with `ping 192.168.1.201` and check that the device's IP is reachable. |
| `Unauthenticated` | The device has a Comm Key. Add `DEVICE_PASSWORD=<key>` to `settings.env` and restart. |
| `ORA-00001 … this punch is NOT in checkinout: the ID primary key/identity probably collided` | `checkinout.ID` values were inserted manually and the identity counter is behind. Fix: `ALTER TABLE checkinout MODIFY ID GENERATED BY DEFAULT AS IDENTITY (START WITH LIMIT VALUE);`. The waiting records are then inserted automatically. |
| `ORA-02291 integrity constraint … FK_CHECKINOUT_DEVICE` | A device ID was removed from `device_machines` while it still had records. Re-add it. |
| `Inconsistent log read (attempt 1/3)` (occasionally) | Someone punched during the read. The read is retried. This is normal. |
| `Undecodable record kept in journal.bad_records` | The device has a corrupt record (bad date). It is kept in the journal for inspection, and all other records continue normally. |
| Pending count keeps growing | Oracle is unreachable or rejecting records. See the `NOT synced` / `Oracle sync failed` lines in the log. |

---

## 12. Settings reference

Database connection: `settings.env` (`DB_USER`, `DB_PASSWORD`, `DB_DSN`, `AUTOSTART_SERVICE`).
Everything else has a default in `SYNC_CONFIG` in `config.py`. **Any of them can be overridden in
`settings.env` with its name in UPPERCASE**. This is the way to change them for the `.exe`:
```ini
DEVICE_PASSWORD=12345
POLL_INTERVAL_SECONDS=30
```
Restart the service after changing `settings.env`.

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
| `ZKTecoSync.exe` | The built program: window, or background service with `--service` |
| `sync_gui.py` | The window (Database / Devices / Records / Logs) and the .exe entry point |
| `sync_service.py` | The collector: device readers, Oracle sync, audit, supervisor, `--status` |
| `zk_reader.py` | Reads and checks the attendance log stored on a device |
| `journal.py` | Local journal (SQLite) |
| `config.py` | Settings |
| `settings.env` | Database user / password / address |
| `setup_db.sql` | Creates the Oracle tables |
| `build_exe.bat` | Builds `dist\ZKTecoSync.exe` |
| `zkteco_journal.db` | Created at runtime: local copy of every record. **Keep it, back it up.** |
| `zkteco_sync.log` | Created at runtime: log file (rotated, 10 × 10 MB) |
