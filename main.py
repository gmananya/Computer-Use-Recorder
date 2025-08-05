import os
import re
import json
import time
import shutil
import ctypes
import platform
import threading
import tkinter as tk
import tkinter.font as tkFont
from tkinter import messagebox, ttk

import winreg
import psutil
import uiautomation as auto
import ffmpeg

import win32api
import win32con
import win32gui
import win32process

from pynput import mouse, keyboard
from obswebsocket import obsws, requests

import web_logger_server as web_logger_server  # local http server that receives web logs

TASKS_PATH = "tasks_list.json"


# ---------- small utilities ----------

def _read_reg(root, path, name):
    try:
        with winreg.OpenKey(root, path) as key:
            val, _ = winreg.QueryValueEx(key, name)
            return val
    except Exception:
        return None


def _proc_running(fragment):
    frag = (fragment or "").lower()
    for p in psutil.process_iter(["name"]):
        try:
            nm = (p.info["name"] or "").lower()
            if frag and frag in nm:
                return True
        except Exception:
            pass
    return False


def _get_window_bounds(hwnd):
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return {"left": l, "top": t, "right": r, "bottom": b}
    except Exception:
        return None


def _get_window_state(hwnd):
    # 1 = normal, 2 = minimized, 3 = maximized
    try:
        _, show_cmd, _, _, _ = win32gui.GetWindowPlacement(hwnd)
        if show_cmd == 3:
            return "maximized"
        if show_cmd == 2:
            return "minimized"
        return "normal"
    except Exception:
        return "unknown"


def _sanitize_filename(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "")


# ---------- high contrast via spi (safe fallback if it fails) ----------

class HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint),
                ("dwFlags", ctypes.c_uint),
                ("lpszDefaultScheme", ctypes.c_wchar_p)]


def _get_high_contrast_enabled():
    # spi_gethighcontrast = 0x0042, hcf_highcontraston = 0x0001
    SPI_GETHIGHCONTRAST = 0x0042
    HCF_HIGHCONTRASTON = 0x0001
    try:
        hc = HIGHCONTRASTW()
        hc.cbSize = ctypes.sizeof(HIGHCONTRASTW)
        res = ctypes.windll.user32.SystemParametersInfoW(SPI_GETHIGHCONTRAST, hc.cbSize, ctypes.byref(hc), 0)
        if res:
            return bool(hc.dwFlags & HCF_HIGHCONTRASTON)
    except Exception:
        pass
    return None


# ---------- accessibility snapshot & diff ----------

def get_accessibility_state():
    narrator_running = _proc_running("narrator.exe")
    narrator_startup = bool(_read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Narrator\NoRoam", "WinEnterLaunchEnabled"))

    mag_zoom = _read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ScreenMagnifier", "Magnification")
    magnifier_running = _proc_running("magnify.exe")

    cf_active = _read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ColorFiltering", "Active")
    cf_type_raw = _read_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ColorFiltering", "FilterType")
    FILTER_TYPES = {0: "none", 1: "inverted", 2: "grayscale", 3: "red-green", 4: "green-red", 5: "blue-yellow"}
    try:
        cf_type = FILTER_TYPES.get(int(cf_type_raw), "unknown")
    except Exception:
        cf_type = "unknown"
    cf_enabled = bool(int(cf_active)) if str(cf_active).isdigit() else False

    hc_enabled = _get_high_contrast_enabled()
    hc_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\HighContrast", "Flags")

    sk_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\StickyKeys", "Flags")
    tk_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\ToggleKeys", "Flags")
    fk_flags = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\Keyboard Response", "Flags")

    scaling = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", "LogPixels")
    font_smoothing = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", "FontSmoothing")
    arrow = _read_reg(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors", "Arrow")
    if isinstance(arrow, str) and "aero" in arrow.lower():
        cursor_scheme = "windows aero"
    elif isinstance(arrow, str) and "windows black" in arrow.lower():
        cursor_scheme = "windows black"
    else:
        cursor_scheme = os.path.basename(arrow) if isinstance(arrow, str) else "unavailable"

    def _nz(x):
        try:
            return int(x) != 0
        except Exception:
            return False

    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "narrator": {"enabled": narrator_running, "startup_enabled": narrator_startup},
        "magnifier": {"enabled": magnifier_running, "zoom": int(mag_zoom) if str(mag_zoom).isdigit() else mag_zoom},
        "color_filter": {"enabled": cf_enabled, "type": cf_type},
        "high_contrast": {"enabled": hc_enabled, "flags": str(hc_flags) if hc_flags is not None else "unavailable"},
        "sticky_keys": {"flags": str(sk_flags) if sk_flags is not None else "unavailable", "maybe_enabled": _nz(sk_flags)},
        "toggle_keys": {"flags": str(tk_flags) if tk_flags is not None else "unavailable", "maybe_enabled": _nz(tk_flags)},
        "filter_keys": {"flags": str(fk_flags) if fk_flags is not None else "unavailable", "maybe_enabled": _nz(fk_flags)},
        "font_smoothing": font_smoothing,
        "display_scaling": scaling,
        "mouse_cursor_scheme": cursor_scheme,
    }


def diff_accessibility(prev, curr):
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return None

    def pick(d, path, default=None):
        try:
            for k in path:
                d = d[k]
            return d
        except Exception:
            return default

    checks = [
        (("narrator", "enabled"),),
        (("narrator", "startup_enabled"),),
        (("magnifier", "enabled"),),
        (("magnifier", "zoom"),),
        (("color_filter", "enabled"),),
        (("color_filter", "type"),),
        (("high_contrast", "enabled"),),
        (("sticky_keys", "maybe_enabled"),),
        (("toggle_keys", "maybe_enabled"),),
        (("filter_keys", "maybe_enabled"),),
    ]

    changes = {}
    for (p,) in checks:
        old = pick(prev, p)
        new = pick(curr, p)
        if old != new:
            changes["/".join(p)] = {"old": old, "new": new}

    return changes or None


# ---------- gui app ----------

class TaskGUI:
    ROLL_THRESHOLD = 300  # seconds of idle after which a new session file is started for the same surface

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("computer use logger")

        self.tasks = self._load_tasks()
        self.task_metadata = {}
        self.current_tasks = []
        self.interaction_loggers = []
        self.obs_client = None

        # per-app ordering for filenames
        self.app_name_to_order = {}
        self.app_order_counter = 1

        # track last event timestamp per surface to detect idle-gap rollover
        self.surface_last_event_ts = {}

        # apps we never log
        self.accessibility_skiplist = {
            "explorer.exe", "python.exe", "pythonw.exe", "obs64.exe",
            "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe"
        }

        # locks for safe writes and one-time a11y capture
        self.file_lock = threading.Lock()
        self.meta_lock = threading.Lock()
        self.a11y_capture_lock = threading.Lock()
        self.captured_surfaces = set()

        # fonts
        self.base_font = tkFont.Font(family="segoe ui", size=10)
        self.bold_font = tkFont.Font(family="segoe ui", size=12, weight="bold")

        self._build_initial_gui()

    # ----- gui + tasks -----

    def _load_tasks(self):
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_initial_gui(self):
        self.root.geometry("1000x700")
        self.root.resizable(True, True)

        self.task_set_var = tk.StringVar()
        self.task_number_var = tk.StringVar()

        self.form_frame = tk.Frame(self.root, padx=20, pady=20)
        self.form_frame.pack(fill="both", expand=True)

        tk.Label(self.form_frame, text="task set #:", font=self.base_font).grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.task_set_dropdown = ttk.Combobox(self.form_frame, textvariable=self.task_set_var, state="readonly", width=47)
        self.task_set_dropdown.configure(font=self.base_font)
        self.task_set_dropdown["values"] = [f"Set {i}" for i in range(1, 8)]
        self.task_set_dropdown.grid(row=0, column=1, padx=5, pady=5)
        self.task_set_dropdown.bind("<<ComboboxSelected>>", self._task_dropdown)

        tk.Label(self.form_frame, text="task #:", font=self.base_font).grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.task_number_dropdown = ttk.Combobox(self.form_frame, textvariable=self.task_number_var, state="readonly", width=47)
        self.task_number_dropdown.configure(font=self.base_font)
        self.task_number_dropdown.grid(row=1, column=1, padx=5, pady=5)

        self.continue_button = tk.Button(self.form_frame, text="next", command=self._task_details, width=20)
        self.continue_button.configure(font=self.base_font)
        self.continue_button.grid(row=2, column=0, columnspan=2, pady=20)

    def _task_dropdown(self, event=None):
        s = self.task_set_var.get()
        if not s:
            return
        set_index = int(s.split()[1])
        start_idx = 0 if set_index == 1 else 10 + (set_index - 2) * 15
        count = 10 if set_index == 1 else 15
        self.current_tasks = self.tasks[start_idx:start_idx + count]

        display_values = []
        for i, task in enumerate(self.current_tasks):
            example = task.get("Example Instruction", "").strip()
            label = f"Task {i+1} - {example}" if example else f"Task {i+1}"
            display_values.append(label)

        self.task_number_dropdown["values"] = display_values
        self.task_number_var.set("")

    def _task_details(self):
        if not self.task_set_var.get() or not self.task_number_var.get():
            messagebox.showwarning("missing", "please select both a task set and task number.")
            return

        selected_label = self.task_number_var.get()
        try:
            task_index = int(selected_label.split()[1]) - 1
        except Exception:
            messagebox.showerror("error", "failed to parse task number.")
            return

        task = self.current_tasks[task_index]
        context = task.get("Context", "no context")
        instruction = task.get("Task", "no task")

        self.task_metadata = {
            "task_set": self.task_set_var.get(),
            "task_number": self.task_number_var.get(),
            "context": context,
            "task": instruction
        }

        for w in self.form_frame.winfo_children():
            w.destroy()

        tk.Label(self.form_frame, text="task context", font=self.bold_font).pack(anchor="w", pady=(0, 2))
        tk.Label(self.form_frame, text=context, wraplength=800, justify="left", font=self.base_font).pack(anchor="w", pady=(0, 10))

        tk.Label(self.form_frame, text="task instruction", font=self.bold_font).pack(anchor="w", pady=(0, 2))
        tk.Label(self.form_frame, text=instruction, wraplength=800, justify="left", font=self.base_font).pack(anchor="w", pady=(0, 10))

        self.task_familiarity = tk.StringVar()
        self.task_difficulty = tk.StringVar()

        dropdown_frame = tk.Frame(self.form_frame)
        dropdown_frame.pack(pady=10)

        tk.Label(dropdown_frame, text="familiarity", font=self.base_font).grid(row=0, column=0, sticky="w", padx=5, pady=2)
        ttk.Combobox(dropdown_frame, textvariable=self.task_familiarity,
                     values=["Low", "Medium", "High"], state="readonly", width=20).grid(row=0, column=1, padx=5, pady=4, sticky="w")

        tk.Label(dropdown_frame, text="difficulty", font=self.base_font).grid(row=1, column=0, sticky="w", padx=5, pady=2)
        ttk.Combobox(dropdown_frame, textvariable=self.task_difficulty,
                     values=["Easy", "Medium", "Hard"], state="readonly", width=20).grid(row=1, column=1, padx=5, pady=4, sticky="w")

        buttons = tk.Frame(self.form_frame)
        buttons.pack(pady=10)

        self.start_button = tk.Button(buttons, text="start logging", command=self.start_task, width=20)
        self.start_button.configure(font=self.base_font)
        self.start_button.pack(side="left", padx=10)

        self.quit_button = tk.Button(buttons, text="quit", command=self.root.quit, width=20)
        self.quit_button.configure(font=self.base_font)
        self.quit_button.pack(side="left", padx=10)

    # ----- process control -----

    def _has_visible_window(self, pid):
        flags = []
        def cb(hwnd, acc):
            _, p = win32process.GetWindowThreadProcessId(hwnd)
            if p == pid and win32gui.IsWindowVisible(hwnd):
                acc.append(True)
        win32gui.EnumWindows(cb, flags)
        return bool(flags)

    def _close_other_apps(self):
        cur_pid = os.getpid()
        whitelist = {
            'code.exe', 'python.exe', 'pythonw.exe', 'magnify.exe', 'narrator.exe',
            'osk.exe', 'nvda.exe', 'jfw.exe', 'obs64.exe', 'explorer.exe',
            'startmenuexperiencehost.exe', 'shellexperiencehost.exe', 'searchhost.exe'
        }
        for proc in psutil.process_iter(['pid', 'name', 'username']):
            try:
                pid = proc.info['pid']
                name = (proc.info['name'] or "").lower()
                username = (proc.info['username'] or "").lower()
                if pid == cur_pid or name in whitelist:
                    continue
                if username in ('system', 'local service', 'network service'):
                    continue
                if not self._has_visible_window(pid):
                    continue
                print(f"terminating: {proc.info['name']} (pid {pid})")
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    # ----- obs -----

    def _start_obs(self, out_dir):
        try:
            self.obs_client = obsws("localhost", 4444, "")
            self.obs_client.connect()
            time.sleep(0.5)
            try:
                self.obs_client.call(requests.SetRecordingFolder(out_dir))
                print(f"obs folder set: {out_dir}")
            except Exception:
                pass
            self.obs_client.call(requests.StartRecording())
            print("obs recording started.")
        except Exception as e:
            print(f"failed to start obs: {e}")

    def _stop_obs(self):
        try:
            if not self.obs_client:
                return None
            pre = None
            try:
                status = self.obs_client.call(requests.GetRecordingStatus())
                if hasattr(status, "getRecordingFilename"):
                    pre = status.getRecordingFilename()
            except Exception:
                pass
            try:
                self.obs_client.call(requests.StopRecording())
            except Exception as e:
                print(f"stop recording failed: {e}")
            for _ in range(40):
                try:
                    st = self.obs_client.call(requests.GetRecordingStatus())
                    if hasattr(st, "getIsRecording") and not st.getIsRecording():
                        break
                except Exception:
                    pass
                time.sleep(0.25)
            try:
                self.obs_client.disconnect()
            except Exception:
                pass
            print("obs recording stopped.")
            return pre
        except Exception as e:
            print(f"failed to stop obs: {e}")
            return None

    def _bring_obs_file(self, src_path):
        if not src_path:
            print("no recording file provided.")
            return None
        dst = os.path.join(self.log_folder_path, os.path.basename(src_path))
        for _ in range(20):
            try:
                with open(src_path, "rb"):
                    break
            except PermissionError:
                time.sleep(0.5)
        try:
            shutil.copy2(src_path, dst)
            print(f"copied obs recording to task folder: {dst}")
            return dst
        except Exception as e:
            print(f"error moving obs recording: {e}")
            return None

    def _split_mkv(self, rec_path=None):
        if not rec_path:
            rec_path = os.path.join(self.log_folder_path, "recording.mkv")
        out_screen = os.path.join(self.log_folder_path, "screen.mp4")
        out_sys = os.path.join(self.log_folder_path, "system_audio.wav")
        out_mic = os.path.join(self.log_folder_path, "mic_audio.wav")
        if not os.path.exists(rec_path):
            print(f"recording file not found at {rec_path}")
            return
        try:
            ffmpeg.input(rec_path).output(out_screen, map="0:v", c="copy").run(overwrite_output=True)
            ffmpeg.input(rec_path).output(out_sys, map="0:a:0", acodec="pcm_s16le").run(overwrite_output=True)
            ffmpeg.input(rec_path).output(out_mic, map="0:a:1", acodec="pcm_s16le").run(overwrite_output=True)
            print("successfully split .mkv into separate streams.")
        except ffmpeg.Error as e:
            print("ffmpeg error:", e.stderr.decode() if e.stderr else str(e))

    # ----- a11y tree -----

    def _serialize_a11y_tree(self, element, depth=0, max_depth=4):
        if element is None or depth > max_depth:
            return None
        try:
            kids = []
            for ch in element.GetChildren():
                q = self._serialize_a11y_tree(ch, depth + 1, max_depth)
                if q:
                    kids.append(q)
            return {
                "name": element.Name,
                "control_type": element.ControlTypeName,
                "automation_id": element.AutomationId,
                "bounding_rectangle": {
                    "left": element.BoundingRectangle.left,
                    "top": element.BoundingRectangle.top,
                    "right": element.BoundingRectangle.right,
                    "bottom": element.BoundingRectangle.bottom
                },
                "children": kids
            }
        except Exception as e:
            return {"error": str(e)}

    # ----- surface key (prevents duplicate trees) -----

    def _surface_key(self, app_name, window_title, hwnd):
        app = (app_name or "").lower()
        title = (window_title or "").lower()
        if app == "applicationframehost.exe":
            if "settings" in title:
                tag = "settings"
            elif "calculator" in title:
                tag = "calculator"
            else:
                tag = f"hwnd:{hwnd}"
            return f"{app}|{tag}"
        return f"{app}|hwnd:{hwnd}"

    # ----- logging session -----

    def start_task(self):
        self.task_metadata["familiarity"] = self.task_familiarity.get()
        self.task_metadata["difficulty"] = self.task_difficulty.get()

        self._close_other_apps()

        set_number = int(self.task_metadata["task_set"].split()[1])
        task_number = int(self.task_metadata["task_number"].split()[1])
        final_task_id = task_number if set_number == 1 else 10 + (set_number - 2) * 15 + task_number

        try:
            if hasattr(web_logger_server, "reset_state_for_task"):
                web_logger_server.reset_state_for_task(final_task_id)
            web_logger_server.CURRENT_TASK = str(final_task_id)
            web_logger_server.run_web_logger_in_thread()
        except Exception as e:
            print(f"[web_logger_server] could not start: {e}")

        folder_name = str(final_task_id)
        folder_path = os.path.join("task_logs", folder_name)
        os.makedirs(folder_path, exist_ok=True)
        self.log_folder_path = folder_path
        self.metadata_path = os.path.join(folder_path, "metadata.json")

        baseline = get_accessibility_state()

        meta = {
            "task": dict(self.task_metadata),
            "session": {
                "started_at": time.time(),
                "last_event_at": None,
                "folder": folder_path.replace("/", "\\"),
                "apps_by_order": []
            },
            "apps": {},
            "web": {"tabs_by_order": [], "events": 0, "by_tab": {}},
            "summary": {"apps": 0, "events": 0, "by_type": {}, "by_app": {}},
            "accessibility": {
                "baseline": baseline,
                "last": baseline,
                "changes": []
            }
        }
        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        messagebox.showinfo("logging started", f"started logging:\n\n{self.task_metadata}")
        self.start_button.pack_forget()

        self._last_acc_check_ts = 0.0
        self._acc_check_interval = 1.0

        self.app_name_to_order = {}
        self.app_order_counter = 1
        self.surface_last_event_ts = {}

        self._start_obs(folder_path)

        def get_current_info():
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid or pid < 0:
                return None
            try:
                proc = psutil.Process(pid)
                app_name = proc.name()
            except Exception:
                return None
            if (app_name or "").lower() in self.accessibility_skiplist:
                return None

            window_title = win32gui.GetWindowText(hwnd)
            x, y = win32api.GetCursorPos()
            element_info = {}
            focused_info = {}
            bounds = _get_window_bounds(hwnd)
            state = _get_window_state(hwnd)

            with auto.UIAutomationInitializerInThread():
                try:
                    element = auto.ControlFromPoint(x, y)
                    element_info = {
                        "name": element.Name,
                        "control_type": element.ControlTypeName,
                        "bounding_rect": {
                            "left": element.BoundingRectangle.left,
                            "top": element.BoundingRectangle.top,
                            "right": element.BoundingRectangle.right,
                            "bottom": element.BoundingRectangle.bottom
                        }
                    }
                except Exception as e:
                    element_info = {"error": str(e)}
                try:
                    focused = auto.GetFocusedControl()
                    focused_info = {"name": focused.Name, "control_type": focused.ControlTypeName}
                except Exception as e:
                    focused_info = {"error": str(e)}

            return {
                "timestamp": time.time(),
                "application": app_name,
                "window_title": window_title,
                "cursor_position": [x, y],
                "focused_element": focused_info,
                "element_under_cursor": element_info,
                "hwnd": hwnd,
                "pid": pid,
                "window_bounds": bounds,
                "window_state": state
            }

        def _load_meta():
            with self.meta_lock:
                try:
                    with open(self.metadata_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    return None

        def _save_meta(m):
            with self.meta_lock:
                try:
                    with open(self.metadata_path, "w", encoding="utf-8") as f:
                        json.dump(m, f, indent=2)
                except Exception as e:
                    print(f"[meta] write failed: {e}")

        def _ensure_app_in_meta(m, app_name, order):
            m.setdefault("summary", {"apps": 0, "events": 0, "by_type": {}, "by_app": {}})
            m.setdefault("session", {
                "started_at": time.time(),
                "last_event_at": None,
                "folder": self.log_folder_path.replace("/", "\\"),
                "apps_by_order": []
            })
            apps = m.setdefault("apps", {})
            app_entry = apps.setdefault(app_name, {
                "order": order,
                "log_file": f"{order}_{app_name}.json",
                "a11y_tree_files": [],
                "events": 0,
                "by_type": {"mouse_click": 0, "key_press": 0, "scroll": 0}
            })
            listed = m["session"].setdefault("apps_by_order", [])
            if not any(a.get("app") == app_name and a.get("order") == order for a in listed):
                listed.append({"order": order, "app": app_name, "log_file": app_entry["log_file"]})
                m["summary"]["apps"] = len(listed)
            print(f"[META] Ensuring {app_name} (order={order}) in apps_by_order")
            return app_entry

        def _maybe_record_accessibility_change(now_ts):
            if now_ts - self._last_acc_check_ts < self._acc_check_interval:
                return
            self._last_acc_check_ts = now_ts

            m = _load_meta() or {}
            acc = m.get("accessibility") or {}
            last = acc.get("last")
            curr = get_accessibility_state()
            ch = diff_accessibility(last, curr) if last else None
            if ch:
                acc_changes = acc.setdefault("changes", [])
                acc_changes.append({"ts": now_ts, "diff": ch})
                acc["last"] = curr
                m["accessibility"] = acc
                _save_meta(m)

        def _capture_a11y_tree_once(ev):
            key = self._surface_key(ev.get("application"), ev.get("window_title"), ev.get("hwnd"))
            with self.a11y_capture_lock:
                if key in self.captured_surfaces:
                    return
                def _pid_of(name):
                    for p in psutil.process_iter(["pid", "name"]):
                        if (p.info["name"] or "").lower() == (name or "").lower():
                            return p.info["pid"]
                    return None
                try:
                    pid = ev.get("pid") or _pid_of(ev.get("application"))
                    if pid is None:
                        return
                    with auto.UIAutomationInitializerInThread():
                        root = auto.GetRootControl()
                        for ch in root.GetChildren():
                            if ch.ProcessId == pid:
                                tree = self._serialize_a11y_tree(ch)
                                if tree:
                                    order = self.app_name_to_order[ev.get("application")]
                                    ts_ms = int(round(float(ev.get("timestamp", time.time())) * 1000))
                                    fname = f"{order}_{_sanitize_filename(ev.get('application'))}_{ts_ms}_a11y_tree.json"
                                    with open(os.path.join(self.log_folder_path, fname), "w", encoding="utf-8") as tf:
                                        json.dump(tree, tf, indent=2)
                                    m = _load_meta() or {}
                                    app_entry = _ensure_app_in_meta(m, ev.get("application"), order)
                                    app_entry.setdefault("a11y_tree_files", []).append(fname)
                                    _save_meta(m)
                                break
                    self.captured_surfaces.add(key)
                except Exception as e:
                    print(f"[a11y] capture failed for {ev.get('application')}: {e}")

        def log_event(ev):
            app_name = ev.get("application") or "unknown"
            app_lower = app_name.lower()
            if app_lower in self.accessibility_skiplist:
                return

            # SESSION-BOUNDARY / ROLLOVER LOGIC
            surface_key = self._surface_key(ev.get("application"), ev.get("window_title"), ev.get("hwnd"))
            last_ts = self.surface_last_event_ts.get(surface_key)
            now_ts = ev.get("timestamp", time.time())

            # Determine if we need a new order (rollover) due to idle gap
            if last_ts is not None and (now_ts - last_ts) > self.ROLL_THRESHOLD:
                # bump order so a new file is used
                self.app_name_to_order[app_name] = self.app_order_counter
                order = self.app_order_counter
                self.app_order_counter += 1
                print(f"[ROLLOVER] Idle gap {now_ts - last_ts:.1f}s for {app_name} on {surface_key}, new order {order}")
            else:
                if app_name not in self.app_name_to_order:
                    self.app_name_to_order[app_name] = self.app_order_counter
                    self.app_order_counter += 1
                order = self.app_name_to_order[app_name]

            # update last event time for this surface
            self.surface_last_event_ts[surface_key] = now_ts

            # capture a11y tree once per surface
            _capture_a11y_tree_once(ev)

            # check accessibility changes
            _maybe_record_accessibility_change(ev["timestamp"])

            # update metadata
            m = _load_meta() or {}
            app_entry = _ensure_app_in_meta(m, app_name, order)

            cur_window = {
                "title": ev.get("window_title"),
                "bounds": ev.get("window_bounds"),
                "state": ev.get("window_state"),
                "hwnd": ev.get("hwnd"),
                "pid": ev.get("pid"),
                "ts": ev.get("timestamp"),
            }
            prev_window = app_entry.get("last_window") or {}
            if prev_window.get("hwnd") != cur_window["hwnd"] or prev_window.get("title") != cur_window["title"]:
                app_entry.setdefault("windows_seen", []).append(cur_window)
                if len(app_entry["windows_seen"]) > 10:
                    app_entry["windows_seen"] = app_entry["windows_seen"][-10:]
            app_entry["last_window"] = cur_window

            ev_type = ev.get("event", "unknown")
            app_entry["events"] = int(app_entry.get("events", 0)) + 1
            by_type = app_entry.setdefault("by_type", {"mouse_click": 0, "key_press": 0, "scroll": 0})
            by_type[ev_type] = int(by_type.get(ev_type, 0)) + 1

            m.setdefault("summary", {"apps": 0, "events": 0, "by_type": {}, "by_app": {}})
            m["summary"]["events"] = int(m["summary"].get("events", 0)) + 1
            m["summary"]["by_app"] = m["summary"].get("by_app", {})
            m["summary"]["by_app"][app_name] = int(m["summary"]["by_app"].get(app_name, 0)) + 1
            m["summary"]["by_type"] = m["summary"].get("by_type", {})
            m["summary"]["by_type"][ev_type] = int(m["summary"]["by_type"].get(ev_type, 0)) + 1
            m["session"]["last_event_at"] = ev["timestamp"]
            _save_meta(m)

            # append to per-app file
            filename = f"{order}_{app_name}.json"
            path = os.path.join(self.log_folder_path, filename)
            with self.file_lock:
                if not os.path.exists(path):
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump([], f)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                    if not isinstance(logs, list):
                        logs = []
                except Exception as e:
                    print(f"[log_event] corrupt log; resetting {path}: {e}")
                    try:
                        os.replace(path, path + ".bad")
                    except Exception:
                        pass
                    logs = []
                logs.append(ev)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(logs, f, indent=2)

        def on_click(x, y, button, pressed):
            if not pressed:
                return
            ev = get_current_info()
            if not ev:
                return
            ev["event"] = "mouse_click"
            ev["button"] = str(button)
            log_event(ev)

        def on_scroll(x, y, dx, dy):
            ev = get_current_info()
            if not ev:
                return
            ev["event"] = "scroll"
            ev["delta"] = [dx, dy]
            log_event(ev)

        def on_press(key):
            ev = get_current_info()
            if not ev:
                return
            try:
                key_str = key.char
            except Exception:
                key_str = str(key)
            ev["event"] = "key_press"
            ev["key"] = key_str
            log_event(ev)

        ml = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
        kl = keyboard.Listener(on_press=on_press)
        ml.start()
        kl.start()
        self.interaction_loggers = [ml, kl]

        self.quit_button.pack_forget()
        self.stop_button = tk.Button(self.form_frame, text="stop logging", command=self.stop_task, width=20)
        self.stop_button.configure(font=self.base_font)
        self.stop_button.pack(pady=5)

    def stop_task(self):
        messagebox.showinfo("stopped", "logging stopped. data saved.")
        for l in self.interaction_loggers:
            try:
                l.stop()
            except Exception:
                pass
        self.interaction_loggers = []

        rec_src = self._stop_obs()
        final_path = self._bring_obs_file(rec_src)
        self._split_mkv(final_path)

        self.root.quit()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = TaskGUI()
    app.run()
