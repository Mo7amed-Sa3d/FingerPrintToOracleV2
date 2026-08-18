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

## How it works

| Phase | What happens |
|---|---|
| **Connect** | Opens persistent TCP connection to device on port 4370 |
| **Back-fill** | Pulls all stored logs from device memory into MySQL (skips duplicates) |
| **Live capture** | Keeps connection open; each punch is inserted within ~1 second |
| **Reconnect** | On any error, waits `retry_delay_seconds` then reconnects automatically |

---
