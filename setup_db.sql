-- =============================================================================
-- Oracle setup script for ZKTeco Attendance Sync
-- Run as DBA / SYSDBA to create the schema user, then as attendance_user
-- to create the objects.
--
-- Step 1 (as SYSDBA / DBA):
--   sqlplus sys/SYS_PASSWORD@//localhost:1521/XEPDB1 as sysdba @setup_db.sql
-- =============================================================================

-- ── 1. Create the schema user (run as SYSDBA) ─────────────────────────────────
-- Uncomment and adjust if the user does not exist yet.
/*
CREATE USER attendance_user IDENTIFIED BY "your_password"
    DEFAULT TABLESPACE users
    TEMPORARY TABLESPACE temp
    QUOTA UNLIMITED ON users;

GRANT CREATE SESSION,
      CREATE TABLE,
      CREATE SEQUENCE,
      CREATE TRIGGER,
      CREATE VIEW
  TO attendance_user;
*/

-- ── From here, connect as attendance_user ─────────────────────────────────────
-- sqlplus attendance_user/your_password@//localhost:1521/XEPDB1 @setup_db.sql


-- ── 2. devices ────────────────────────────────────────────────────────────────
-- One row per physical ZKTeco unit.
-- device_status: 'ONLINE' | 'OFFLINE' | 'UNKNOWN'
-- The extra columns store the connection parameters for each device.
CREATE TABLE devices (
    device_id     NUMBER        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_ip     VARCHAR2(45)  NOT NULL,
    device_name   VARCHAR2(100),
    device_status VARCHAR2(20)  DEFAULT 'UNKNOWN'
                                CONSTRAINT chk_status
                                CHECK (device_status IN ('ONLINE','OFFLINE','UNKNOWN')),
    last_seen     TIMESTAMP,
    -- Connection parameters (with sensible defaults)
    port          NUMBER        DEFAULT 4370,
    timeout       NUMBER        DEFAULT 10,
    password      NUMBER        DEFAULT 0,
    force_udp     CHAR(1)       DEFAULT 'N' CHECK (force_udp IN ('Y','N')),
    ommit_ping    CHAR(1)       DEFAULT 'N' CHECK (ommit_ping IN ('Y','N')),
    CONSTRAINT uq_device_ip UNIQUE (device_ip)
);


-- ── 3. user_info ──────────────────────────────────────────────────────────────
-- Users pulled from ZKTeco device memory.
-- user_id_from_device is the numeric ID stored on the ZK device.
CREATE TABLE user_info (
    id                   NUMBER        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id_from_device  VARCHAR2(50)  NOT NULL,
    user_name            VARCHAR2(100),
    CONSTRAINT uq_user_device UNIQUE (user_id_from_device)
);


-- ── 4. checkinout ─────────────────────────────────────────────────────────────
-- Every attendance punch: check-in or check-out.
CREATE TABLE checkinout (
    id                   NUMBER     GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id_from_device  VARCHAR2(50)  NOT NULL,
    punch_time           TIMESTAMP     NOT NULL,
    device_id            NUMBER        NOT NULL,
    CONSTRAINT fk_checkinout_device
        FOREIGN KEY (device_id) REFERENCES devices (device_id),
    CONSTRAINT fk_checkinout_user
        FOREIGN KEY (user_id_from_device) REFERENCES user_info (user_id_from_device),
    -- Prevent exact duplicate punches from the same device
    CONSTRAINT uq_punch UNIQUE (user_id_from_device, punch_time, device_id)
);


-- ── 5. Useful views ───────────────────────────────────────────────────────────

-- Today's first and last punch per user
CREATE OR REPLACE VIEW v_today_attendance AS
SELECT
    u.user_id_from_device,
    u.user_name,
    MIN(c.punch_time)  AS first_seen,
    MAX(c.punch_time)  AS last_seen,
    d.device_name,
    d.device_ip
FROM checkinout c
JOIN user_info  u ON u.user_id_from_device = c.user_id_from_device
JOIN devices    d ON d.device_id           = c.device_id
WHERE TRUNC(c.punch_time) = TRUNC(SYSDATE)
GROUP BY u.user_id_from_device, u.user_name, d.device_name, d.device_ip;


-- Per-device punch counts for today
CREATE OR REPLACE VIEW v_device_activity_today AS
SELECT
    d.device_id,
    d.device_name,
    d.device_ip,
    d.device_status,
    COUNT(c.id)              AS total_punches,
    COUNT(DISTINCT c.user_id_from_device) AS unique_users,
    MAX(c.punch_time)        AS last_punch_at
FROM devices   d
LEFT JOIN checkinout c
       ON c.device_id = d.device_id
      AND TRUNC(c.punch_time) = TRUNC(SYSDATE)
GROUP BY d.device_id, d.device_name, d.device_ip, d.device_status;