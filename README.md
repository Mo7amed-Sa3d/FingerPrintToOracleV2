# ZKTeco → MySQL Real-Time Attendance Sync

Streams attendance punches from a ZKTeco device (TCP/IP) into MySQL as they happen.

---

## Quick Start

### 1. Install dependencies
```bash
python3 -m venv venv
venv\Scripts\activate.bat
pip install -r requirements.txt
```

### 2. Set up the database
```bash
sqlplus -u root -p < setup_db.sql
```

### 3. Edit config.py
Update `DB_CONFIG` with your MySQL credentials.

### 4. Run
```bash
python sync_service.py
```
pip install pyinstaller

python -m PyInstaller --onefile --add-data "settings.env;." --name zkteco_sync ^
  --hidden-import oracledb ^
  --hidden-import zk ^
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
  --add-data "config.py;." ^
  sync_service.py
---

set DB_USER=my_user
set DB_PASSWORD=my_pass
set DB_DSN=//myhost:1521/mydb
sync_service.exe

## Running as a systemd service (Linux)

```bash
# Edit the service file first
nano zkteco-sync.service   # set User= and WorkingDirectory=

sudo cp zkteco-sync.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable zkteco-sync
sudo systemctl start zkteco-sync

# Check status / logs
sudo systemctl status zkteco-sync
journalctl -u zkteco-sync -f
```

---

## How it works (zero-loss design)

The device's own memory is the source of truth. The app never relies on
real-time events, so a missed event can never lose a record.

| Step | What happens |
|---|---|
| **Poll** | Every `poll_interval_seconds` (60) each device is connected, its record count checked, and if it changed (or every `full_read_interval_seconds`) the **full stored log** is read with a validated parser ([zk_reader.py](zk_reader.py)). |
| **Journal** | Every record not seen before is written to the local SQLite file `zkteco_journal.db` (fsync'd) **before** anything is sent to Oracle. Records are never deleted from it. |
| **Oracle sync** | Pending journal records are inserted into `checkinout`, committed, then **read back**. Only records found in Oracle are marked synced; anything else stays pending and is retried with back-off (5s up to 1h). |
| **Audit** | Every `audit_interval_seconds` (1h) all synced records of the last `audit_lookback_days` (31) are compared with Oracle; anything missing is re-sent. |
| **Reconnect** | Every poll is a fresh connection. Any device error -> `OFFLINE`, retry forever with back-off 5s..60s. Oracle outages: records wait in the journal. If Oracle is down at startup, the cached device list is used. |
| **Devices** | `device_machines` is re-read every 5 min; added/removed/changed devices are picked up without restart. |

Delay from punch to Oracle is normally under ~70 seconds.

### Files created next to the app
* `zkteco_journal.db` (+ `-wal`, `-shm`): the local journal. **Do not delete it** and back it up with the app.
  * table `punches`: every record ever read; `synced=0` rows are still waiting for Oracle (`last_error` says why).
  * table `bad_records`: device records that could not be decoded (kept, never silently dropped).
* `zkteco_sync.log`: rotating log (10 x 10 MB). A `[summary]` block every 10 minutes shows per device:
  last successful read, records on device, records in journal, pending and failing counts.

### Check type
`checktype_mode = "punch"` (default) maps the device punch state: 0/3/4 -> `I`, 1/2/5 -> `O`.
`"status"` restores the old behaviour (`status==1 -> I`), which actually used the verify method
(1 = finger, 15 = face). `VERIFYCODE` stores the verify method.

### Notes
* A record can only be lost if it is deleted from the device **before** the app ever read it
  (e.g. the log is cleared while the app is stopped). The log warns when a device log is 90% full.
* If another program (ZKTime/ZKBioTime) also connects to the devices, polls may fail while it is
  connected; they are retried automatically.
* If you delete rows from `checkinout` on purpose, the audit will re-insert them if they are within
  `audit_lookback_days`.
