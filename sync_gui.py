"""
ZKTeco Attendance Sync - desktop UI
===================================

    ZKTecoSync.exe              -> opens this window
    ZKTecoSync.exe --service    -> runs the collector without a window (Task Scheduler)
    ZKTecoSync.exe --status     -> prints the journal status (console builds only)

Tabs:
  Database  - Oracle connection settings (test + save to settings.env)
  Devices   - devices from device_machines with their status and record counts
  Records   - every record collected (local journal), search + export to CSV
  Logs      - live view of zkteco_sync.log
"""

from __future__ import annotations

import csv
import os
import queue
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta


def _cli():
    args = sys.argv[1:]
    if "--service" in args:
        import sync_service
        sync_service.run_headless()
        return True
    if "--status" in args:
        import sync_service
        sync_service.print_status()
        return True
    return False


# ── UI ─────────────────────────────────────────────────────────────────────────
import tkinter as tk                                   # noqa: E402
from tkinter import ttk, messagebox, filedialog        # noqa: E402

import oracledb                                        # noqa: E402

import config                                          # noqa: E402
import sync_service as svc                             # noqa: E402
from journal import Journal                            # noqa: E402

APP_TITLE = "ZKTeco Attendance Sync"
VERIFY_NAMES = {0: "Password", 1: "Fingerprint", 4: "Card", 15: "Face"}
MAX_ROWS_SHOWN = 2000
LOG_INITIAL_BYTES = 512 * 1024
LOG_MAX_LINES = 5000


def parse_dsn(dsn: str) -> dict:
    """Split a DSN into host / port / name / mode ('service' or 'sid')."""
    d = (dsn or "").strip()
    if d.upper().startswith("(DESCRIPTION"):
        def grab(k, default=""):
            m = re.search(rf"\({k}\s*=\s*([^)]+)\)", d, re.I)
            return m.group(1).strip() if m else default
        sid = grab("SID")
        return {"host": grab("HOST"), "port": grab("PORT", "1521"),
                "name": sid or grab("SERVICE_NAME"), "mode": "sid" if sid else "service"}
    if d.startswith("//"):
        d = d[2:]
    m = re.match(r"^([^:/]+)(?::(\d+))?([/:])(.+)$", d)
    if m:
        return {"host": m.group(1), "port": m.group(2) or "1521", "name": m.group(4),
                "mode": "service" if m.group(3) == "/" else "sid"}
    return {"host": d, "port": "1521", "name": "", "mode": "service"}


def build_dsn(host: str, port: str, name: str, mode: str) -> str:
    if mode == "sid":
        return oracledb.makedsn(host, int(port), sid=name)
    return f"{host}:{port}/{name}"


class App:
    def __init__(self):
        svc.setup_logging()
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1150x720")
        self.root.minsize(900, 560)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")

        self.pool = ThreadPoolExecutor(max_workers=3)
        self.ui_q: queue.Queue = queue.Queue()
        self.journal_path = svc._app_path(svc._cfg("journal_path", "zkteco_journal.db"))
        self.journal = Journal(self.journal_path)
        self.log_path = svc._app_path(svc._cfg("log_file", "zkteco_sync.log"))

        self.sup = None
        self.sup_thread = None
        self.lock = None
        self.stopping = False
        self.closing = False

        self._build_header()
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.nb = nb
        self._build_db_tab(nb)
        self._build_devices_tab(nb)
        self._build_records_tab(nb)
        self._build_logs_tab(nb)
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._drain_ui_queue)
        self.root.after(200, self._tick_status)
        self.root.after(300, self._tick_logs)

        if os.getenv("AUTOSTART_SERVICE", "0") == "1":
            self.root.after(500, self.start_service)

        # If the .exe launcher is killed, stop the service cleanly and close.
        svc.watch_launcher(lambda: self.ui_q.put((self._launcher_gone, ())))

    def _launcher_gone(self):
        self.stop_service()
        self.closing = True
        self._wait_then_close()

    # ── helpers ───────────────────────────────────────────────────────────────
    def bg(self, fn, done=None, fail=None):
        """Run fn in a worker thread; deliver result to done() on the UI thread."""
        def task():
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001
                self.ui_q.put((fail or self._show_error, (exc,)))
                return
            if done:
                self.ui_q.put((done, (res,)))
        self.pool.submit(task)

    def _drain_ui_queue(self):
        try:
            while True:
                fn, args = self.ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception as exc:  # noqa: BLE001
                    print("UI callback error:", exc)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def _show_error(self, exc):
        messagebox.showerror(APP_TITLE, str(exc))

    @staticmethod
    def _tree(parent, columns, widths):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col, w in zip(columns, widths):
            tree.heading(col, text=col)
            tree.column(col, width=w, anchor="w", stretch=True)
        ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree.tag_configure("bad", foreground="#b00020")
        tree.tag_configure("wait", foreground="#a15c00")
        tree.tag_configure("ok", foreground="#1b5e20")
        return frame, tree

    # ── header: service status + start/stop ───────────────────────────────────
    def _build_header(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="Service:").pack(side="left")
        self.status_lbl = tk.Label(bar, text="...", font=("Segoe UI", 11, "bold"))
        self.status_lbl.pack(side="left", padx=(6, 14))
        self.start_btn = ttk.Button(bar, text="Start", command=self.start_service)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop_service)
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.totals_lbl = ttk.Label(bar, text="")
        self.totals_lbl.pack(side="right")

    def _service_state(self) -> str:
        if self.sup_thread is not None and self.sup_thread.is_alive():
            return "stopping" if self.stopping else "here"
        return "elsewhere" if svc.service_running_elsewhere() else "stopped"

    def _tick_status(self):
        state = self._service_state()
        if state == "here":
            self.status_lbl.config(text="Running", fg="#1b5e20")
        elif state == "stopping":
            self.status_lbl.config(text="Stopping...", fg="#a15c00")
        elif state == "elsewhere":
            self.status_lbl.config(text="Running in background (another process)", fg="#1b5e20")
        else:
            self.status_lbl.config(text="Stopped - records are NOT being collected", fg="#b00020")
        self.start_btn.state(["!disabled"] if state == "stopped" else ["disabled"])
        self.stop_btn.state(["!disabled"] if state == "here" else ["disabled"])
        if self.stopping and state != "stopping":
            self.stopping = False

        def totals():
            stats = self.journal.stats()
            total = sum(s["total"] for s in stats.values())
            pending = sum(s["pending"] for s in stats.values())
            failing = sum(s["failing"] for s in stats.values())
            ids = list(stats) + [d["device_id"] for d in self.journal.cached_devices()]
            return total, pending, failing, ids

        def show(t):
            total, pending, failing, ids = t
            self._fill_device_filter(ids)
            text = f"Records collected: {total:,}   |   Waiting for Oracle: {pending:,}"
            if failing:
                text += f"   |   Failing: {failing:,}"
            self.totals_lbl.config(text=text, foreground="#b00020" if failing else "")

        self.bg(totals, show, lambda e: None)
        self.root.after(3000, self._tick_status)

    def start_service(self):
        if self._service_state() != "stopped":
            return
        lock = svc.InstanceLock()
        if not lock.acquire():
            messagebox.showinfo(APP_TITLE, "The service is already running in another process "
                                           "(for example the startup task).")
            return
        self.lock = lock
        self.sup = svc.Supervisor()
        self.sup_thread = threading.Thread(target=self._run_supervisor, name="Supervisor", daemon=True)
        self.sup_thread.start()
        self._tick_status_once()

    def _run_supervisor(self):
        try:
            self.sup.run(install_signals=False)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("main").exception("Service crashed")
        finally:
            if self.lock:
                self.lock.release()
                self.lock = None

    def stop_service(self):
        if self.sup is not None and self.sup_thread and self.sup_thread.is_alive():
            self.stopping = True
            self.sup.stop.set()
            self._tick_status_once()

    def _tick_status_once(self):
        state = self._service_state()
        self.start_btn.state(["!disabled"] if state == "stopped" else ["disabled"])
        self.stop_btn.state(["!disabled"] if state == "here" else ["disabled"])

    def on_close(self):
        if self.sup_thread is not None and self.sup_thread.is_alive():
            if not messagebox.askyesno(
                    APP_TITLE,
                    "The service is running in this window.\n\n"
                    "Closing stops collecting until the program is opened again. No record is "
                    "lost: records stay on the devices and in the journal and are collected "
                    "next time.\n\nClose and stop the service?"):
                return
            self.stop_service()
            self.closing = True
            self.status_lbl.config(text="Stopping, please wait...", fg="#a15c00")
            self._wait_then_close()
        else:
            self._destroy()

    def _wait_then_close(self):
        if self.sup_thread is not None and self.sup_thread.is_alive():
            self.root.after(300, self._wait_then_close)
        else:
            self._destroy()

    def _destroy(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    # ── Database tab ──────────────────────────────────────────────────────────
    def _build_db_tab(self, nb):
        tab = ttk.Frame(nb, padding=16)
        nb.add(tab, text="  Database  ")
        p = parse_dsn(config.DB_CONFIG["dsn"])

        self.v_host = tk.StringVar(value=p["host"])
        self.v_port = tk.StringVar(value=p["port"])
        self.v_name = tk.StringVar(value=p["name"])
        self.v_mode = tk.StringVar(value=p["mode"])
        self.v_user = tk.StringVar(value=config.DB_CONFIG["user"])
        self.v_pass = tk.StringVar(value=config.DB_CONFIG["password"])
        self.v_show = tk.BooleanVar(value=False)
        self.v_auto = tk.BooleanVar(value=os.getenv("AUTOSTART_SERVICE", "0") == "1")

        form = ttk.LabelFrame(tab, text="Oracle connection", padding=14)
        form.pack(anchor="nw", fill="x")
        rows = [("Server (host / IP):", self.v_host, 36),
                ("Port:", self.v_port, 10)]
        r = 0
        for label, var, width in rows:
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=var, width=width).grid(row=r, column=1, sticky="w", pady=4)
            r += 1

        ttk.Label(form, text="Database:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(form)
        f.grid(row=r, column=1, sticky="w")
        ttk.Entry(f, textvariable=self.v_name, width=24).pack(side="left")
        ttk.Radiobutton(f, text="Service name", value="service", variable=self.v_mode).pack(side="left", padx=(10, 0))
        ttk.Radiobutton(f, text="SID", value="sid", variable=self.v_mode).pack(side="left", padx=(6, 0))
        r += 1

        ttk.Label(form, text="User:").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.v_user, width=24).grid(row=r, column=1, sticky="w", pady=4)
        r += 1
        ttk.Label(form, text="Password:").grid(row=r, column=0, sticky="w", pady=4)
        f = ttk.Frame(form)
        f.grid(row=r, column=1, sticky="w")
        self.pass_entry = ttk.Entry(f, textvariable=self.v_pass, width=24, show="*")
        self.pass_entry.pack(side="left")
        ttk.Checkbutton(f, text="Show", variable=self.v_show,
                        command=lambda: self.pass_entry.config(show="" if self.v_show.get() else "*")
                        ).pack(side="left", padx=(8, 0))
        r += 1
        form.columnconfigure(0, minsize=150)

        opt = ttk.LabelFrame(tab, text="Startup", padding=14)
        opt.pack(anchor="nw", fill="x", pady=(12, 0))
        ttk.Checkbutton(opt, text="Start the service automatically when this program opens",
                        variable=self.v_auto).pack(anchor="w")

        btns = ttk.Frame(tab)
        btns.pack(anchor="nw", pady=(14, 0))
        ttk.Button(btns, text="Test connection", command=self.test_connection).pack(side="left")
        ttk.Button(btns, text="Save", command=self.save_settings).pack(side="left", padx=(8, 0))

        self.db_result = tk.Label(tab, text="", justify="left", anchor="w", wraplength=900,
                                  font=("Segoe UI", 10))
        self.db_result.pack(anchor="nw", fill="x", pady=(12, 0))
        ttk.Label(tab, text=f"Settings file: {config.ENV_PATH}", style="Hint.TLabel").pack(
            anchor="sw", side="bottom")

    def _form_values(self):
        host, port, name = self.v_host.get().strip(), self.v_port.get().strip(), self.v_name.get().strip()
        user, pw = self.v_user.get().strip(), self.v_pass.get()
        if not host or not name or not user:
            raise ValueError("Server, Database and User are required.")
        if not port.isdigit():
            raise ValueError("Port must be a number (usually 1521).")
        if "\n" in pw or "\r" in pw:
            raise ValueError("Password cannot contain line breaks.")
        return user, pw, build_dsn(host, port, name, self.v_mode.get())

    def test_connection(self):
        try:
            user, pw, dsn = self._form_values()
        except ValueError as exc:
            self.db_result.config(text=str(exc), fg="#b00020")
            return
        self.db_result.config(text="Connecting...", fg="#333333")

        def work():
            conn = oracledb.connect(user=user, password=pw, dsn=dsn, tcp_connect_timeout=8)
            try:
                cur = conn.cursor()
                lines = [f"Connected to Oracle {conn.version}."]
                for table in ("device_machines", "checkinout"):
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        lines.append(f"Table {table}: {cur.fetchone()[0]:,} row(s).")
                    except oracledb.DatabaseError as exc:
                        lines.append(f"Table {table}: NOT accessible - {str(exc).splitlines()[0]}")
                return "\n".join(lines)
            finally:
                conn.close()

        self.bg(work,
                lambda msg: self.db_result.config(text=msg, fg="#b00020" if "NOT" in msg else "#1b5e20"),
                lambda exc: self.db_result.config(text=f"Connection failed:\n{exc}", fg="#b00020"))

    def save_settings(self):
        try:
            user, pw, dsn = self._form_values()
        except ValueError as exc:
            self.db_result.config(text=str(exc), fg="#b00020")
            return
        try:
            path = config.save_env({"DB_USER": user, "DB_PASSWORD": pw, "DB_DSN": dsn,
                                    "AUTOSTART_SERVICE": "1" if self.v_auto.get() else "0"})
        except OSError as exc:
            self.db_result.config(text=f"Could not save settings: {exc}", fg="#b00020")
            return
        config.apply_db_config(user, pw, dsn)
        self.db_result.config(text=f"Saved to {path}.", fg="#1b5e20")

        state = self._service_state()
        if state == "here":
            if messagebox.askyesno(APP_TITLE, "Settings saved.\n\nRestart the service now to use them?"):
                self._restart_service()
        elif state == "elsewhere":
            messagebox.showinfo(APP_TITLE, "Settings saved.\n\nThe service running in the background "
                                           "uses the new settings after it is restarted.")

    def _restart_service(self):
        self.stop_service()

        def wait_and_start():
            if self.sup_thread is not None and self.sup_thread.is_alive():
                self.root.after(300, wait_and_start)
            else:
                self.start_service()
        wait_and_start()

    # ── Devices tab ───────────────────────────────────────────────────────────
    def _build_devices_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  Devices  ")
        self.devices_tab = tab
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Button(top, text="Refresh", command=self.refresh_devices).pack(side="left")
        self.dev_msg = ttk.Label(top, text="", style="Hint.TLabel")
        self.dev_msg.pack(side="left", padx=10)
        cols = ("ID", "IP address", "Port", "Status", "Status time", "Records collected",
                "Waiting for Oracle", "Failing", "Latest punch")
        frame, self.dev_tree = self._tree(tab, cols, (50, 130, 60, 90, 150, 120, 120, 70, 150))
        frame.pack(fill="both", expand=True)
        ttk.Label(tab, text="Devices are managed in the Oracle table device_machines; changes are picked "
                            "up by the service within 5 minutes.", style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

    def refresh_devices(self):
        self.dev_msg.config(text="Loading...")

        def work():
            stats = self.journal.stats()
            latest = dict(self.journal._conn().execute(
                "SELECT device_id, MAX(checktime) FROM punches GROUP BY device_id").fetchall())
            err = None
            try:
                conn = oracledb.connect(**config.DB_CONFIG, tcp_connect_timeout=8)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT ID, IP, PORT, STATUS, STATUS_TIME FROM device_machines ORDER BY ID")
                    devices = [(int(r[0]), r[1], r[2], r[3] or "", r[4]) for r in cur.fetchall()]
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                err = str(exc).splitlines()[0]
                devices = [(d["device_id"], d["ip"], d["port"], "?", None)
                           for d in self.journal.cached_devices()]
            return devices, stats, latest, err

        def show(res):
            devices, stats, latest, err = res
            self.dev_tree.delete(*self.dev_tree.get_children())
            for dev_id, ip, port, status, st_time in devices:
                s = stats.get(dev_id, {"total": 0, "pending": 0, "failing": 0})
                tag = "ok" if status == "ONLINE" else "bad" if status == "OFFLINE" else ""
                if s["failing"]:
                    tag = "bad"
                self.dev_tree.insert("", "end", tags=(tag,), values=(
                    dev_id, ip, port, status,
                    st_time.strftime("%Y-%m-%d %H:%M:%S") if st_time else "",
                    f"{s['total']:,}", f"{s['pending']:,}", f"{s['failing']:,}", latest.get(dev_id, "")))
            if err:
                self.dev_msg.config(text=f"Oracle not reachable ({err}). Showing the cached device list.")
            else:
                self.dev_msg.config(text=f"{len(devices)} device(s). Updated {datetime.now():%H:%M:%S}.")
            self._fill_device_filter([d[0] for d in devices] + list(stats))

        self.bg(work, show, lambda e: self.dev_msg.config(text=f"Error: {e}"))

    # ── Records tab ───────────────────────────────────────────────────────────
    def _build_records_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  Records  ")
        self.records_tab = tab
        f = ttk.Frame(tab)
        f.pack(fill="x", pady=(0, 8))

        self.v_dev = tk.StringVar(value="All")
        self.v_uid = tk.StringVar()
        self.v_from = tk.StringVar(value=(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
        self.v_to = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.v_state = tk.StringVar(value="All")

        ttk.Label(f, text="Device:").pack(side="left")
        self.dev_combo = ttk.Combobox(f, textvariable=self.v_dev, width=8, state="readonly", values=["All"])
        self.dev_combo.pack(side="left", padx=(4, 12))
        ttk.Label(f, text="User ID:").pack(side="left")
        ttk.Entry(f, textvariable=self.v_uid, width=12).pack(side="left", padx=(4, 12))
        ttk.Label(f, text="From:").pack(side="left")
        ttk.Entry(f, textvariable=self.v_from, width=11).pack(side="left", padx=(4, 6))
        ttk.Label(f, text="To:").pack(side="left")
        ttk.Entry(f, textvariable=self.v_to, width=11).pack(side="left", padx=(4, 12))
        ttk.Label(f, text="State:").pack(side="left")
        ttk.Combobox(f, textvariable=self.v_state, width=12, state="readonly",
                     values=["All", "In Oracle", "Waiting", "Failing"]).pack(side="left", padx=(4, 12))
        ttk.Button(f, text="Search", command=self.search_records).pack(side="left")
        ttk.Button(f, text="Export CSV...", command=self.export_records).pack(side="left", padx=(6, 0))

        cols = ("Device", "User ID", "Date / time", "Type", "Verify", "State", "Attempts", "Error")
        frame, self.rec_tree = self._tree(tab, cols, (60, 90, 150, 60, 100, 110, 70, 420))
        frame.pack(fill="both", expand=True)
        self.rec_tree.bind("<Double-1>", self._record_details)
        self.rec_msg = ttk.Label(tab, text="Dates are YYYY-MM-DD (leave empty for no limit). "
                                           "Records come from the local journal: every record ever collected.",
                                 style="Hint.TLabel")
        self.rec_msg.pack(anchor="w", pady=(6, 0))

    def _fill_device_filter(self, ids):
        values = ["All"] + [str(i) for i in sorted(set(ids))]
        if list(self.dev_combo.cget("values")) != values:
            self.dev_combo.config(values=values)

    def _records_query(self):
        where, args = [], []
        dev = self.v_dev.get()
        if dev and dev != "All":
            where.append("device_id = ?")
            args.append(int(dev))
        uid = self.v_uid.get().strip()
        if uid:
            where.append("user_id = ?")
            args.append(uid)
        for var, op, add_day in ((self.v_from, ">=", False), (self.v_to, "<", True)):
            text = var.get().strip()
            if text:
                try:
                    d = datetime.strptime(text, "%Y-%m-%d")
                except ValueError:
                    raise ValueError(f"Date '{text}' must be YYYY-MM-DD") from None
                if add_day:
                    d += timedelta(days=1)
                where.append(f"checktime {op} ?")
                args.append(d.strftime("%Y-%m-%d %H:%M:%S"))
        state = self.v_state.get()
        if state == "In Oracle":
            where.append("synced = 1")
        elif state == "Waiting":
            where.append("synced = 0 AND attempts = 0")
        elif state == "Failing":
            where.append("synced = 0 AND attempts > 0")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return clause, args

    @staticmethod
    def _row_values(row):
        dev, uid, ct, status, punch, synced, attempts, err = row
        checktype = svc.checktype_for(status, punch)
        verify = VERIFY_NAMES.get(status, "")
        verify = f"{verify} ({status})" if verify else str(status)
        if synced:
            state, tag = "In Oracle", "ok"
        elif attempts:
            state, tag = "Failing (retrying)", "bad"
        else:
            state, tag = "Waiting", "wait"
        return (dev, uid, ct, "IN" if checktype == "I" else "OUT", verify, state, attempts,
                err or ""), tag

    def search_records(self):
        try:
            clause, args = self._records_query()
        except ValueError as exc:
            self.rec_msg.config(text=str(exc))
            return
        self.rec_msg.config(text="Searching...")

        def work():
            c = self.journal._conn()
            total = c.execute("SELECT COUNT(*) FROM punches" + clause, args).fetchone()[0]
            rows = c.execute(
                "SELECT device_id, user_id, checktime, status, punch, synced, attempts, last_error "
                "FROM punches" + clause + " ORDER BY checktime DESC LIMIT ?",
                args + [MAX_ROWS_SHOWN]).fetchall()
            return total, rows

        def show(res):
            total, rows = res
            self.rec_tree.delete(*self.rec_tree.get_children())
            for row in rows:
                values, tag = self._row_values(row)
                self.rec_tree.insert("", "end", values=values, tags=(tag,))
            if total > len(rows):
                self.rec_msg.config(text=f"Showing the newest {len(rows):,} of {total:,} matching records. "
                                         "Narrow the search or use Export CSV for all of them.")
            else:
                self.rec_msg.config(text=f"{total:,} matching record(s).")

        self.bg(work, show, lambda e: self.rec_msg.config(text=f"Error: {e}"))

    def _record_details(self, _event):
        sel = self.rec_tree.selection()
        if not sel:
            return
        v = self.rec_tree.item(sel[0], "values")
        cols = self.rec_tree["columns"]
        messagebox.showinfo("Record", "\n".join(f"{c}: {x}" for c, x in zip(cols, v)))

    def export_records(self):
        try:
            clause, args = self._records_query()
        except ValueError as exc:
            self.rec_msg.config(text=str(exc))
            return
        path = filedialog.asksaveasfilename(
            title="Export records", defaultextension=".csv",
            initialfile=f"attendance_{datetime.now():%Y%m%d_%H%M}.csv",
            filetypes=[("CSV (Excel)", "*.csv")])
        if not path:
            return
        self.rec_msg.config(text="Exporting...")
        header = list(self.rec_tree["columns"])   # read Tk state on the UI thread

        def work():
            c = self.journal._conn()
            cur = c.execute(
                "SELECT device_id, user_id, checktime, status, punch, synced, attempts, last_error "
                "FROM punches" + clause + " ORDER BY checktime", args)
            n = 0
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(header)
                for row in cur:
                    w.writerow(self._row_values(row)[0])
                    n += 1
            return n

        self.bg(work, lambda n: self.rec_msg.config(text=f"Exported {n:,} record(s) to {path}"),
                lambda e: self.rec_msg.config(text=f"Export failed: {e}"))

    # ── Logs tab ──────────────────────────────────────────────────────────────
    def _build_logs_tab(self, nb):
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  Logs  ")
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))
        self.v_level = tk.StringVar(value="All")
        self.v_follow = tk.BooleanVar(value=True)
        ttk.Label(top, text="Show:").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.v_level, width=20, state="readonly",
                          values=["All", "Warnings and errors", "Errors only", "Summary only"])
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._reload_logs())
        ttk.Checkbutton(top, text="Auto-scroll", variable=self.v_follow).pack(side="left")
        ttk.Button(top, text="Reload", command=self._reload_logs).pack(side="left", padx=(12, 0))
        ttk.Button(top, text="Open log folder", command=lambda: os.startfile(os.path.dirname(self.log_path))
                   ).pack(side="left", padx=(6, 0))
        ttk.Label(top, text=self.log_path, style="Hint.TLabel").pack(side="right")

        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled",
                                background="#fbfbfb")
        ys = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.log_text.tag_configure("ERROR", foreground="#b00020")
        self.log_text.tag_configure("CRITICAL", foreground="#ffffff", background="#b00020")
        self.log_text.tag_configure("WARNING", foreground="#a15c00")
        self.log_text.tag_configure("SUMMARY", foreground="#0d47a1")
        self._log_offset = None

    def _log_line_ok(self, line: str) -> bool:
        level = self.v_level.get()
        if level == "Warnings and errors":
            return any(k in line for k in ("[WARNING]", "[ERROR]", "[CRITICAL]"))
        if level == "Errors only":
            return "[ERROR]" in line or "[CRITICAL]" in line
        if level == "Summary only":
            return "[summary]" in line
        return True

    def _append_log(self, text: str):
        lines = [ln for ln in text.splitlines() if ln.strip() and self._log_line_ok(ln)]
        if not lines:
            return
        t = self.log_text
        t.config(state="normal")
        for ln in lines:
            tag = ("CRITICAL" if "[CRITICAL]" in ln else "ERROR" if "[ERROR]" in ln
                   else "WARNING" if "[WARNING]" in ln else "SUMMARY" if "[summary]" in ln else "")
            t.insert("end", ln + "\n", tag)
        extra = int(t.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if extra > 0:
            t.delete("1.0", f"{extra + 1}.0")
        t.config(state="disabled")
        if self.v_follow.get():
            t.see("end")

    def _reload_logs(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._log_offset = None
        self._read_new_log()

    def _read_new_log(self):
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return
        if self._log_offset is None:
            self._log_offset = max(0, size - LOG_INITIAL_BYTES)
            skip_partial = self._log_offset > 0
        else:
            skip_partial = False
            if size < self._log_offset:  # log was rotated
                self._log_offset = 0
        if size == self._log_offset:
            return
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._log_offset)
                data = f.read(size - self._log_offset)
        except OSError:
            return
        # only consume complete lines
        cut = data.rfind(b"\n")
        if cut < 0:
            return
        data = data[:cut + 1]
        self._log_offset += len(data)
        text = data.decode("utf-8", errors="replace")
        if skip_partial and "\n" in text:
            text = text.split("\n", 1)[1]
        self._append_log(text)

    def _tick_logs(self):
        self._read_new_log()
        self.root.after(1000, self._tick_logs)

    # ── misc ──────────────────────────────────────────────────────────────────
    def _on_tab_changed(self, _event):
        current = self.nb.nametowidget(self.nb.select())
        if current is self.devices_tab:
            self.refresh_devices()
        elif current is self.records_tab and not self.rec_tree.get_children():
            self.search_records()

    def run(self):
        self.root.mainloop()


def main():
    if _cli():
        return
    App().run()


if __name__ == "__main__":
    main()
