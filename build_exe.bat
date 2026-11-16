@echo off
REM Builds dist\ZKTecoSync.exe (UI + background service in one file).
REM Run from the project folder with the virtual environment present:
REM     .venv\Scripts\python.exe -m pip install pyinstaller
REM     build_exe.bat
cd /d "%~dp0"

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name ZKTecoSync ^
  --hidden-import oracledb ^
  --hidden-import zk ^
  --hidden-import sqlite3 ^
  --hidden-import journal ^
  --hidden-import zk_reader ^
  --hidden-import sync_service ^
  --hidden-import getpass ^
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
  --hidden-import cryptography.hazmat.bindings ^
  --hidden-import cryptography.hazmat.bindings._rust ^
  sync_gui.py

if errorlevel 1 (
  echo.
  echo BUILD FAILED
  exit /b 1
)
echo.
echo Built: %~dp0dist\ZKTecoSync.exe
