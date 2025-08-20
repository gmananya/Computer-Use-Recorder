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
from tkinter import simpledialog
from tkinter import messagebox, ttk

import math
import winreg
import psutil
import uiautomation as auto
import ffmpeg

import win32api
import win32con
import win32gui
import win32process

from pynput import mouse, keyboard
from pynput.keyboard import Key, KeyCode

import obsws_python as obs
import glob, logging
from obsws_python.error import OBSSDKRequestError
import configparser
from pathlib import Path

import web_logger_server as web_logger_server  # local http server that receives web logs
import accessibility_user_settings as a11y


TASKS_PATH = "tasks_list.json"
COMPLETED_TASKS_FILE = "completed_tasks.txt"



# ---------- small utilities ----------

def _get_ancestor(hwnd, flag):
    try:
        return win32gui.GetAncestor(hwnd, flag)
    except Exception:
        return None

def _normalize_to_frame_hwnd(hwnd):
    """
    Always return the top-level frame window for the point-in-time foreground.
    For UWP this is an 'ApplicationFrameWindow' owned by ApplicationFrameHost.exe.
    """
    if not hwnd:
        return None

    # Try root (parent chain)
    root = _get_ancestor(hwnd, win32con.GA_ROOT) or hwnd
    try:
        cls = win32gui.GetClassName(root) or ""
    except Exception:
        cls = ""

    if cls == "ApplicationFrameWindow":
        return root

    # Try owner chain (covers cases where a child surface is 'foreground')
    owner = _get_ancestor(hwnd, win32con.GA_ROOTOWNER) or root
    try:
        ocls = win32gui.GetClassName(owner) or ""
    except Exception:
        ocls = ""

    if ocls == "ApplicationFrameWindow":
        return owner

    # As a last resort, walk up by parent to find a frame (rare)
    try:
        h = hwnd
        for _ in range(32):
            p = win32gui.GetParent(h)
            if not p:
                break
            try:
                pcl = win32gui.GetClassName(p) or ""
            except Exception:
                pcl = ""
            if pcl == "ApplicationFrameWindow":
                return p
            h = p
    except Exception:
        pass

    return root


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



# obs controller


class ObsController:
    def __init__(self, host="127.0.0.1", port=4455, password="OqOC5wKTGnahpL8L"):
        self.host, self.port, self.password = host, port, password
        self.req = None
        self._record_dir = None
        self._existing_before = set()
        self._started_ts = None

    def connect(self):
        # v5 request client (no event client needed)
        self.req = obs.ReqClient(host=self.host, port=self.port, password=self.password, timeout=5)

    def _safe_get_record_directory(self):
        # v5 typed call (if present), else raw
        try:
            return self.req.get_record_directory().record_directory
        except Exception:
            try:
                resp = self.req.send("GetRecordDirectory", {})
                return getattr(resp, "recordDirectory", None) or resp.get("recordDirectory")
            except Exception:
                return None


    def set_record_dir(self, folder):
        self._record_dir = os.path.abspath(folder)
        os.makedirs(self._record_dir, exist_ok=True)

        # Try to set it, but don't die if OBS refuses (e.g., returns 500)
        try:
            try:
                self.req.set_record_directory(self._record_dir)  # typed API (OBS 30+)
            except AttributeError:
                # raw fallback
                self.req.send("SetRecordDirectory", {"recordDirectory": self._record_dir})
        except OBSSDKRequestError as e:
            # 500 → backend refused (mode, perms, or unsupported); just use existing dir
            if getattr(e, "code", None) in (500, 501):
                print(f"SetRecordDirectory refused ({e}); using existing OBS record directory instead.")
            else:
                # unexpected error – rethrow
                raise
        except Exception as e:
            # any other unexpected failure; continue with current OBS setting
            print(f"SetRecordDirectory failed ({e}); using existing OBS record directory.")

        # Determine the effective directory OBS will actually use
        current = self._safe_get_record_directory()
        if current:
            self._record_dir = os.path.abspath(current)
        print(f"obs record folder (effective): {self._record_dir}")
        return self._record_dir


    def _snapshot_existing(self, rec_dir):
        try:
            files = []
            for ext in ("mkv", "mp4", "mov", "flv"):
                files += glob.glob(os.path.join(rec_dir, f"*.{ext}"))
            self._existing_before = set(map(os.path.abspath, files))
        except Exception:
            self._existing_before = set()

    def start(self, out_dir):
        if self.req is None:
            self.connect()

        rec_dir = self.set_record_dir(out_dir) or out_dir
        self._snapshot_existing(rec_dir)
        self._started_ts = time.time()

        # Start recording (v5)
        self.req.start_record()

        # Poll until active (like your v4 loop)
        for _ in range(50):  # ~5s
            try:
                st = self.req.get_record_status()
                if getattr(st, "output_active", False):
                    break
            except Exception:
                pass
            time.sleep(0.1)

        # Confirm recording really started
        st = self.req.get_record_status()
        if not getattr(st, "output_active", False):
            raise RuntimeError("OBS did not start recording (check Output settings/path).")

        print("obs recording started (v5).")

    def _pick_new_file(self):
        # Prefer new files since start; otherwise newest in dir
        rec_dir = self._record_dir or self._safe_get_record_directory()
        if not rec_dir:
            return None
        candidates = []
        for ext in ("mkv", "mp4", "mov", "flv"):
            candidates += glob.glob(os.path.join(rec_dir, f"*.{ext}"))
        if not candidates:
            return None

        abs_set = set(map(os.path.abspath, candidates))
        new_files = list(abs_set - self._existing_before)
        if new_files:
            return max(new_files, key=os.path.getmtime)
        # Fallback: newest file that’s at/after start
        newest = max(candidates, key=os.path.getmtime)
        if self._started_ts and os.path.getmtime(newest) + 1 < self._started_ts:
            return None
        return newest

    def stop(self):
        # Mirror v4: query status, stop if active, then poll until inactive
        try:
            st = self.req.get_record_status()
            active = getattr(st, "output_active", False)
        except Exception:
            active = True  # if unsure, attempt to stop

        if active:
            try:
                self.req.stop_record()
            except OBSSDKRequestError as e:
                if getattr(e, "code", None) == 501:
                    print("stop_record: 501 (already stopped or never started), continuing…")
                else:
                    raise

            # Poll until not recording (like your old GetRecordingStatus loop)
            for _ in range(40):  # ~4s
                try:
                    st2 = self.req.get_record_status()
                    if not getattr(st2, "output_active", False):
                        break
                except Exception:
                    pass
                time.sleep(0.1)
        else:
            print("stop_record: not recording; skipping StopRecord and trying to locate file…")

        print("obs recording stopped (v5).")

        # v5 doesn’t give filename via GetRecordStatus; pick it from disk (reliable)
        return self._pick_new_file()



# ---------- gui app ----------

class TaskGUI:
    ROLL_THRESHOLD = 300  # seconds of idle after which a new session file is started for the same surface

    def __init__(self, user_id):
        self.root = tk.Tk()
        self.root.title("Computer Use Logger")
        
        self.user_id = user_id

        self.user_logs_root = os.path.join("task_logs", f"User {self.user_id}")
        os.makedirs(self.user_logs_root, exist_ok=True)
        self.completed_tasks_file = os.path.join(self.user_logs_root, "completed_tasks.txt")


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

        self._surface_sig = {}
        self._A11Y_RECAP_COOLDOWN = 15.0 
        self._a11y_mode = "focus_only"         # "focus_only" or "smart"
        self._focused_surface = None

        # apps we never log
        self.accessibility_skiplist = {
            "python.exe", "pythonw.exe", "obs64.exe",
            "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
            "teamviewer.exe", "teamviewer_service.exe", "teamviewer_desktop.exe", "teamviewerqs.exe",
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

    def _existing_task_ids_for_user(self) -> set:
        """
        Return a set of task IDs (ints) that already have a folder under
        task_logs/User {user_id}/.
        """
        try:
            base = getattr(self, "user_logs_root", None)
            if not base or not os.path.isdir(base):
                return set()
            ids = set()
            for name in os.listdir(base):
                p = os.path.join(base, name)
                if not os.path.isdir(p):
                    continue
                m = re.fullmatch(r"\s*(\d+)\s*", name)
                if m:
                    ids.add(int(m.group(1)))
            return ids
        except Exception:
            return set()

    def _task_id_from_row(self, task) -> int | None:
        """Extract Task ID as int from a task row."""
        tid = task.get("Task ID") or task.get("TaskID") or task.get("ID")
        try:
            return int(tid)
        except Exception:
            return None


    def _rand_dropdown(self, event=None):
        col = self.rand_id_var.get()
        if not col:
            return

        def _to_int(v):
            try:
                return int(v)
            except Exception:
                return None

        # keep only tasks that have a numeric rank in this column, sort ascending
        ranked = [(t, _to_int(t.get(col))) for t in self.tasks]
        ordered = [t for t, r in sorted([p for p in ranked if p[1] is not None], key=lambda x: x[1])]

        # --- NEW: filter out tasks that already have a folder for this user ---
        already_done = self._existing_task_ids_for_user()
        filtered = []
        for t in ordered:
            tid = self._task_id_from_row(t)
            # include tasks with missing/invalid ID (defensive), exclude ones that exist
            if tid is None or tid not in already_done:
                filtered.append(t)

        self.current_tasks = filtered

        # dropdown label: T{Task ID} - {Instruction}
        display = []
        for t in self.current_tasks:
            tid = t.get("Task ID") or t.get("TaskID") or t.get("ID")
            instr = t.get("Instruction") or t.get("Task") or ""
            display.append(f"T{tid} - {instr}")

        self.task_number_dropdown["values"] = display
        self.task_number_var.set("")

        # Disable "Next" if no remaining tasks
        try:
            self.continue_button.configure(state=("normal" if display else "disabled"))
        except Exception:
            pass


    def _load_tasks(self):
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    
    def _build_initial_gui(self):
        """Build initial GUI and preselect the Rand ID column based on --user_id."""
        self.root.geometry("900x500")
        self.root.resizable(True, True)

        # Detect available Rand ID columns from the JSON header
        sample = self.tasks[0] if self.tasks else {}
        self.rand_columns = [k for k in sample.keys() if re.match(r"(?i)rand\s*id\s*\d+", k)]
        # also allow a single "Rand ID" (no number) if present
        if "Rand ID" in sample and "Rand ID" not in self.rand_columns:
            self.rand_columns.append("Rand ID")

        # sort by the numeric suffix when possible
        def _rk(k):
            m = re.search(r"(\d+)", k)
            return int(m.group(1)) if m else 0
        self.rand_columns.sort(key=_rk)

        # Helper: find best index for the provided --user_id
        def _find_rand_col_index(columns, user_id):
            if not columns:
                return -1
            # 1) Exact flexible match: "Rand ID {n}" (ignore case/whitespace)
            pat_exact = re.compile(rf"^rand\s*id\s*{re.escape(str(user_id))}\s*$", re.I)
            for i, c in enumerate(columns):
                if pat_exact.match(c.strip()):
                    return i
            # 2) Any column whose trailing digits equal user_id (e.g., "Rand_ID(2)")
            for i, c in enumerate(columns):
                m = re.search(r"(\d+)\s*$", c)
                if m and int(m.group(1)) == int(user_id):
                    return i
            # 3) Fallback: plain "Rand ID"
            for i, c in enumerate(columns):
                if re.match(r"^rand\s*id\s*$", c, re.I):
                    return i
            # 4) Last resort: first column
            return 0

        # Tk variables
        self.rand_id_var = tk.StringVar()
        self.task_number_var = tk.StringVar()

        # Layout
        self.form_frame = tk.Frame(self.root, padx=20, pady=20)
        self.form_frame.pack(fill="both", expand=True)

        tk.Label(self.form_frame, text="User ID:", font=self.base_font)\
            .grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.rand_dropdown = ttk.Combobox(
            self.form_frame, textvariable=self.rand_id_var, state="readonly", width=47
        )
        self.rand_dropdown.configure(font=self.base_font)
        self.rand_dropdown["values"] = self.rand_columns
        self.rand_dropdown.grid(row=0, column=1, padx=5, pady=5)
        self.rand_dropdown.bind("<<ComboboxSelected>>", self._rand_dropdown)

        tk.Label(self.form_frame, text="Task:", font=self.base_font)\
            .grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.task_number_dropdown = ttk.Combobox(
            self.form_frame, textvariable=self.task_number_var, state="readonly", width=47
        )
        self.task_number_dropdown.configure(font=self.base_font)
        self.task_number_dropdown.grid(row=1, column=1, padx=5, pady=5)

        self.continue_button = tk.Button(self.form_frame, text="Next", command=self._task_details, width=20)
        self.continue_button.configure(font=self.base_font)
        self.continue_button.grid(row=2, column=0, columnspan=2, pady=20)

        # Preselect the correct Rand ID column by index (avoids Tk picking a different item)
        if self.rand_columns:
            idx = _find_rand_col_index(self.rand_columns, self.user_id) if self.user_id else 0
            # Clamp just in case
            idx = max(0, min(idx, len(self.rand_columns) - 1))
            self.rand_dropdown.current(idx)
            self.rand_id_var.set(self.rand_columns[idx])  # keep var in sync

            # Populate the task list for this user immediately
            self._rand_dropdown()


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

    # inside class TaskGUI (put near other small helpers)
    def _should_skip_app(self, app_name: str) -> bool:
        nm = (app_name or "").lower()
        # exact-name skiplist still honored
        if nm in self.accessibility_skiplist:
            return True
        # NEW: skip any python*.exe variant (python.exe, pythonw.exe, python3.13.exe, etc.)
        if nm.endswith(".exe") and nm.startswith("python"):
            return True
        return False
    

    def _append_completed_task_record(self):
        """
        Writes: 'Participant <user_id> : Task <task_id>' into
        task_logs/User <user_id>/completed_tasks.txt
        """
        try:
            participant_id = self.user_id
            task_id = self.task_metadata.get("task_id")
            # Ensure the per-user folder exists (defensive)
            os.makedirs(self.user_logs_root, exist_ok=True)
            with self.file_lock:
                with open(self.completed_tasks_file, "a", encoding="utf-8") as f:
                    f.write(f"Participant {participant_id} : Task {task_id}\n")
        except Exception as e:
            print(f"[completed_tasks] append failed: {e}")



    def _task_details(self):
        idx = self.task_number_dropdown.current()
        if idx is None or idx < 0:
            # fallback: resolve by the displayed text (just in case)
            label = self.task_number_var.get()
            values = list(self.task_number_dropdown["values"])
            try:
                idx = values.index(label)
            except ValueError:
                messagebox.showwarning("missing", "Please select a task.")
                return

        if not self.rand_id_var.get():
            messagebox.showwarning("missing", "Please choose User ID.")
            return

        task = self.current_tasks[idx]

        # core ids / labels
        tid = task.get("Task ID") or task.get("TaskID") or task.get("ID")
        try:
            tid = int(tid)
        except Exception:
            pass

        rand_col = self.rand_id_var.get()
        rand_order_raw = task.get(rand_col)
        try:
            rand_order = int(rand_order_raw)
        except Exception:
            rand_order = rand_order_raw  # keep as-is if not numeric

        # human-facing fields from the sheet
        task_title = task.get("Task") or task.get("Title") or ""
        instruction = task.get("Instruction") or task.get("Task Instruction") or ""
        context = task.get("Context", "")

        # OPTIONAL: other useful columns if present
        task_group = task.get("Task Group", "")
        interactions = task.get("Interactions", "")
        applications = task.get("Applications", "")

        # what will get written into meta["task"]
        self.task_metadata = {
            "task_id": tid,
            "task_title": task_title,
            "instruction": instruction,
            "task": instruction,     
            "context": context,
            "rand_id_column": rand_col,
            "rand_order": rand_order,
            "task_group": task_group,
            "interactions": interactions,
            "applications": applications,
        }

        # keep the whole source row too (handy for audits)
        self._selected_task_row = task


        # rebuild panel (same as before)
        for w in self.form_frame.winfo_children():
            w.destroy()

        tk.Label(self.form_frame, text="Task Context", font=self.bold_font).pack(anchor="w", pady=(0, 2))
        tk.Label(self.form_frame, text=context, wraplength=800, justify="left", font=self.base_font).pack(anchor="w", pady=(0, 10))

        tk.Label(self.form_frame, text="Task Instruction", font=self.bold_font).pack(anchor="w", pady=(0, 2))
        tk.Label(self.form_frame, text=instruction, wraplength=800, justify="left", font=self.base_font).pack(anchor="w", pady=(0, 10))

        self.task_familiarity = tk.StringVar()
        self.task_difficulty = tk.StringVar()

        self.task_familiarity.set("High")
        self.task_difficulty.set("Low")

        dropdown_frame = tk.Frame(self.form_frame)
        dropdown_frame.pack(pady=10)

        tk.Label(dropdown_frame, text="How familiar are you with this task?", font=self.base_font)\
            .grid(row=0, column=0, sticky="w", padx=5, pady=2)
        ttk.Combobox(dropdown_frame, textvariable=self.task_familiarity,
                    values=["Low", "Medium", "High"], state="readonly", width=20)\
            .grid(row=0, column=1, padx=5, pady=4, sticky="w")

        tk.Label(dropdown_frame, text="What is the expected difficulty of this task?", font=self.base_font)\
            .grid(row=1, column=0, sticky="w", padx=5, pady=2)
        ttk.Combobox(dropdown_frame, textvariable=self.task_difficulty,
                    values=["Easy", "Medium", "Hard"], state="readonly", width=20)\
            .grid(row=1, column=1, padx=5, pady=4, sticky="w")

        buttons = tk.Frame(self.form_frame)
        buttons.pack(pady=10)

        self.start_button = tk.Button(buttons, text="Start task", command=self.start_task, width=20)
        self.start_button.configure(font=self.base_font)
        self.start_button.pack(side="left", padx=10)

        self.quit_button = tk.Button(buttons, text="Quit", command=self.root.quit, width=20)
        self.quit_button.configure(font=self.base_font)
        self.quit_button.pack(side="left", padx=10)


    # ----- process control -----

    def _pid_has_title_markers(self, pid, markers):
        titles = []
        def cb(hwnd, acc):
            try:
                _, p = win32process.GetWindowThreadProcessId(hwnd)
                if p == pid and win32gui.IsWindowVisible(hwnd):
                    t = (win32gui.GetWindowText(hwnd) or "").lower()
                    if t:
                        acc.append(t)
            except Exception:
                pass
        try:
            win32gui.EnumWindows(cb, titles)
        except Exception:
            return False
        joined = " | ".join(titles)
        return any(m in joined for m in (m.lower() for m in markers))

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
            'osk.exe', 'nvda.exe', 'jfw.exe', 'obs64.exe',
            # removed 'explorer.exe' so File Explorer can be closed
            'startmenuexperiencehost.exe', 'shellexperiencehost.exe', 'searchhost.exe',
            # TeamViewer family — do not close
            'teamviewer.exe', 'teamviewer_service.exe', 'teamviewer_desktop.exe', 'teamviewerqs.exe',
        }

        for proc in psutil.process_iter(['pid', 'name', 'username']):
            try:
                pid = proc.info['pid']
                name = (proc.info['name'] or "").lower()
                username = (proc.info['username'] or "").lower()

                if pid == cur_pid:
                    continue
                if name in whitelist or (name.endswith('.exe') and name.startswith('python')):
                    continue
                if username in ('system', 'local service', 'network service'):
                    continue
                if not self._has_visible_window(pid):
                    continue


                if name == 'explorer.exe':
                    hwnds = []
                    def _collect(hwnd, acc):
                        try:
                            _, p = win32process.GetWindowThreadProcessId(hwnd)
                            if p == pid and win32gui.IsWindowVisible(hwnd):
                                acc.append(hwnd)
                        except Exception:
                            pass
                    win32gui.EnumWindows(_collect, hwnds)

                    closed_any = False
                    for h in hwnds:
                        try:
                            cls = win32gui.GetClassName(h)
                            # Close File Explorer windows only (not taskbar: Shell_TrayWnd)
                            if cls in ('CabinetWClass', 'ExploreWClass'):
                                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
                                closed_any = True
                        except Exception:
                            pass
                    if closed_any:
                        print(f"closed File Explorer windows for pid {pid} (kept shell alive)")
                    else:
                        print("kept explorer.exe running (no File Explorer windows to close)")

                    continue



                print(f"terminating: {name} (pid {pid})")
                proc.terminate()

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue



    # ----- obs -----
    def _start_obs(self, out_dir):
        try:
            self._obs = ObsController(host="localhost", port=4455, password="OqOC5wKTGnahpL8L")
            self._obs.start(out_dir)
            print(f"OBS: recording to {os.path.abspath(out_dir)}")
        except Exception as e:
            print(f"failed to start obs (v5): {e}")

    def _stop_obs(self):
        try:
            if not hasattr(self, "_obs"): 
                return None
            rec_path = self._obs.stop()
            return rec_path  # may be None if nothing was recorded
        except Exception as e:
            print(f"stop_record failed: {e}")
            return None

    # ----- ffmpeg helpers -----
    def _resolve_ff_binaries(self):
        """Return (ffmpeg_bin, ffprobe_bin) or (None, None) if not found."""
        candidates_ffmpeg = [
            os.environ.get("FFMPEG_BINARY"),
            os.environ.get("FFMPEG_PATH"),
            shutil.which("ffmpeg"),
            r"C:\Program Files\ffmpeg\ffmpeg.exe"
        ]
        candidates_ffprobe = [
            os.environ.get("FFPROBE_BINARY"),
            shutil.which("ffprobe"),
            r"C:\Program Files\ffmpeg\ffprobe.exe"
        ]

        ffmpeg_bin = next((p for p in candidates_ffmpeg if p and os.path.exists(p)), None)
        ffprobe_bin = next((p for p in candidates_ffprobe if p and os.path.exists(p)), None)
        return ffmpeg_bin, ffprobe_bin


    def _split_mkv(self, rec_path=None):
        if not rec_path:
            rec_path = os.path.join(self.log_folder_path, "recording.mkv")

        if not os.path.exists(rec_path):
            print(f"recording file not found at {rec_path}")
            return

        ffmpeg_bin, ffprobe_bin = self._resolve_ff_binaries()
        if not ffmpeg_bin or not ffprobe_bin:
            print("FFmpeg/ffprobe not found. Add them to PATH or set FFMPEG_BINARY/FFPROBE_BINARY env vars.")
            return

        # Probe streams to pick the right maps and a safe container for the video copy
        try:
            meta = ffmpeg.probe(rec_path, cmd=ffprobe_bin)
        except ffmpeg.Error as e:
            print("ffprobe error:", e.stderr.decode() if e.stderr else str(e))
            return

        streams = meta.get("streams", [])
        v_streams = [s for s in streams if s.get("codec_type") == "video"]
        a_streams = [s for s in streams if s.get("codec_type") == "audio"]

        # Choose a container for the video copy (mp4 for h264/hevc/av1, else mkv)
        v_codec = (v_streams[0].get("codec_name") if v_streams else "").lower()
        video_ext = "mp4" if v_codec in {"h264", "hevc", "av1"} else "mkv"

        out_screen = os.path.join(self.log_folder_path, f"screen.{video_ext}")
        out_sys = os.path.join(self.log_folder_path, "system_audio.wav")
        out_mic = os.path.join(self.log_folder_path, "mic_audio.wav")

        # ffmpeg's map "0:a:N" uses the N-th audio stream *among audio streams*
        # Build a list in that order so N is correct.
        audio_nths = [s for s in streams if s.get("codec_type") == "audio"]

        # Heuristic: pick stream with more channels as "system", the other as "mic"
        if len(audio_nths) >= 1:
            nths_with_channels = [
                (i, int(s.get("channels", 0)), s.get("tags", {}).get("title", ""))
                for i, s in enumerate(audio_nths)
            ]
            if len(nths_with_channels) >= 2:
                sys_nth = max(nths_with_channels, key=lambda t: t[1])[0]
                # choose the first different from sys_nth
                mic_nth = next(i for i, _, _ in nths_with_channels if i != sys_nth)
            else:
                sys_nth, mic_nth = 0, None
        else:
            sys_nth, mic_nth = None, None

        try:
            # Copy video stream only
            ffmpeg.input(rec_path).output(
                out_screen, map="0:v:0", c="copy"
            ).run(overwrite_output=True, cmd=ffmpeg_bin)

            # Extract "system" audio (or the only audio)
            if sys_nth is not None:
                ffmpeg.input(rec_path).output(
                    out_sys, map=f"0:a:{sys_nth}", acodec="pcm_s16le"
                ).run(overwrite_output=True, cmd=ffmpeg_bin)

            # Extract "mic" audio if present
            if mic_nth is not None:
                ffmpeg.input(rec_path).output(
                    out_mic, map=f"0:a:{mic_nth}", acodec="pcm_s16le"
                ).run(overwrite_output=True, cmd=ffmpeg_bin)

            print(
                "successfully split recording:\n"
                f"  video -> {out_screen}\n"
                f"  system audio -> {out_sys if sys_nth is not None else 'N/A'}\n"
                f"  mic audio -> {out_mic if mic_nth is not None else 'N/A'}"
            )
        except FileNotFoundError as e:
            # happens when ffmpeg.exe isn’t reachable
            print(f"FFmpeg not found: {e}. Ensure PATH is set or binaries are configured.")
        except ffmpeg.Error as e:
            print("ffmpeg error:", e.stderr.decode() if e.stderr else str(e))

    def _bring_obs_file(self, src_path):
        if not src_path:
            print("no recording file provided.")
            return None

        src_path = os.path.abspath(src_path)
        dst_dir  = os.path.abspath(self.log_folder_path)

        # If OBS already recorded into the task folder, nothing to do.
        try:
            if os.path.commonpath([src_path, dst_dir]) == dst_dir:
                print("recording already in task folder.")
                return src_path
        except ValueError:
            # different drives etc.; proceed to copy/move
            pass

        # Wait for OBS to release the file handle (event can arrive a hair early)
        for _ in range(60):  # up to ~30s
            try:
                with open(src_path, "rb"):
                    break
            except PermissionError:
                time.sleep(0.5)

        dst_path = os.path.join(dst_dir, os.path.basename(src_path))

        # Prefer copy (keeps the original in case you want to archive elsewhere)
        try:
            shutil.copy2(src_path, dst_path)
            print(f"copied obs recording to task folder: {dst_path}")
            return dst_path
        except Exception as e:
            print(f"copy failed: {e}; trying move")
            try:
                shutil.move(src_path, dst_path)
                print(f"moved obs recording to task folder: {dst_path}")
                return dst_path
            except Exception as e2:
                print(f"move failed: {e2}")
                return None


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

    def _bucket_for_app(self, app_name, window_title, hwnd):
        app = (app_name or "").lower()
        title = (window_title or "").strip().lower()

        if app != "applicationframehost.exe":
            # key used for files/meta, label shown in UI
            return app_name, app_name

        # Split AFH by hosted app using title heuristics (fallback to hwnd)
        if "calculator" in title:
            suffix, label = "Calculator", "Calculator"
        elif "photos" in title:
            suffix, label = "Photos", "Photos"
        elif "media player" in title or "windows media player" in title:
            suffix, label = "MediaPlayer", "Media Player"
        elif "settings" in title:
            suffix, label = "Settings", "Settings"
        else:
            suffix, label = f"HWND_{hwnd}", f"AFH_{hwnd}"

        key = f"ApplicationFrameHost.exe::{suffix}"  # NOTE: will sanitize when used in filenames
        ui_label = f"ApplicationFrameHost ({label})"
        return key, ui_label


    def _surface_key(self, app_name, window_title, hwnd):
        app = (app_name or "").lower()
        title = (window_title or "").strip().lower()

        if app == "applicationframehost.exe":
            # Distinguish hosted UWP apps by title (fallback to hwnd if title is empty)
            if "calculator" in title:
                tag = "calculator"
            elif "photos" in title:
                tag = "photos"
            elif "media player" in title or "windows media player" in title:
                tag = "mediaplayer"
            elif "settings" in title:
                tag = "settings"
            else:
                tag = title or f"hwnd:{hwnd}"
            return f"{app}|{tag}"

        # Non-AFH windows still keyed by hwnd
        return f"{app}|hwnd:{hwnd}"


    # ----- logging session -----

    def start_task(self):
        self.task_metadata["familiarity"] = self.task_familiarity.get()
        self.task_metadata["difficulty_before"] = self.task_difficulty.get()

        self._close_other_apps()

        final_task_id = self.task_metadata["task_id"]

        self._append_completed_task_record()

    
        folder_path = os.path.join(self.user_logs_root, str(final_task_id))

        os.makedirs(folder_path, exist_ok=True)

        self.log_folder_path = folder_path
        self.metadata_path = os.path.join(folder_path, f"metadata_{final_task_id}.json")


        # (optional one-time cleanup for old runs of this task)
        legacy_meta = os.path.join(folder_path, "metadata.json")
        if os.path.exists(legacy_meta) and legacy_meta != self.metadata_path:
            try:
                os.remove(legacy_meta)
            except Exception:
                pass

        # start the web logger AFTER paths are set; point it to the same metadata file if supported
        try:
            web_logger_server.TASK_LOG_DIR = self.user_logs_root
            web_logger_server.reset_state_for_task(final_task_id)
            web_logger_server.run_web_logger_in_thread()
        except Exception as e:
            print(f"[web_logger_server] could not start: {e}")

        

        baseline = a11y.get_accessibility_state()
        # sr = a11y.collect_screen_reader_settings()

        meta = {
            "task": dict(self.task_metadata),        # the curated fields
            # "task_row": dict(self._selected_task_row) if hasattr(self, "_selected_task_row") else None,  # raw row snapshot
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


        meta["accessibility"] = {
            "baseline": baseline,
            # "last": baseline,
            "changes": [],
            # "screen_reader_settings": sr,
        }

        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        # messagebox.showinfo("logging started", f"started logging:\n\n{self.task_metadata}")
        self._show_task_brief_dialog()

        self.start_button.pack_forget()

        self._last_acc_check_ts = 0.0
        self._acc_check_interval = 1.0

        self.app_name_to_order = {}
        self.app_order_counter = 1
        self.surface_last_event_ts = {}

        self._start_obs(folder_path)

        def _safe_process_name(pid, hwnd):
            # Always classify by the *frame* hwnd for consistency
            try:
                frame = _normalize_to_frame_hwnd(hwnd) or hwnd
                cls = win32gui.GetClassName(frame) or ""
            except Exception:
                frame, cls = hwnd, ""

            # Map any ApplicationFrameWindow to ApplicationFrameHost.exe
            if cls == "ApplicationFrameWindow":
                return "ApplicationFrameHost.exe"

            # 1) psutil (fast path)
            try:
                return psutil.Process(pid).name()
            except Exception:
                pass

            # 2) QueryFullProcessImageNameW
            try:
                h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                try:
                    import ctypes, ctypes.wintypes
                    buf = ctypes.create_unicode_buffer(260)
                    sz = ctypes.wintypes.DWORD(len(buf))
                    if ctypes.windll.kernel32.QueryFullProcessImageNameW(int(h), 0, buf, ctypes.byref(sz)):
                        return os.path.basename(buf.value)
                finally:
                    win32api.CloseHandle(h)
            except Exception:
                pass

            # 3) Last resort: stable bucket
            return f"pid_{pid}"

        def _nearest_meaningful(el):
            """
            Walk up until we find a control with a useful Name or a known actionable type.
            """
            ACTION_TYPES = {
                "ButtonControl", "HyperlinkControl", "MenuItemControl", "TabItemControl",
                "ToggleButtonControl", "ListItemControl", "SliderControl", "SpinnerControl",
                "ThumbControl", "SplitButtonControl", "ComboBoxControl"
            }
            MAX_HOPS = 12
            node = el
            hops = 0
            while node and hops < MAX_HOPS:
                try:
                    ct = node.ControlTypeName or ""
                    nm = (node.Name or "").strip()
                    if nm or ct in ACTION_TYPES:
                        # include AutomationId when available
                        aid = ""
                        try:
                            aid = node.AutomationId or ""
                        except Exception:
                            pass
                        return {
                            "name": nm,
                            "control_type": ct,
                            "automation_id": aid
                        }
                    node = node.GetParentControl()
                    hops += 1
                except Exception:
                    break
            return None


        def _annotate_semantic_target(ev):
            """Fill ev['target'] with nearest actionable element at cursor, if any."""
            if not ev or not isinstance(ev, dict):
                return
            x, y = (ev.get("cursor_position") or (None, None))
            if x is None or y is None:
                return
            try:
                with auto.UIAutomationInitializerInThread():
                    el = auto.ControlFromPoint(x, y)
                    if not el:
                        return
                    target = _nearest_meaningful(el)
                    if target:
                        ev["target"] = target  # {'name','control_type','automation_id'}
            except Exception:
                pass


        def get_current_info():
            # Get the actual frame hwnd first
            raw_hwnd = win32gui.GetForegroundWindow()
            if not raw_hwnd:
                return None
            hwnd = _normalize_to_frame_hwnd(raw_hwnd)
            if not hwnd or not win32gui.IsWindow(hwnd):
                return None

            try:
                cls = win32gui.GetClassName(hwnd) or ""
            except Exception:
                cls = ""

            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if not pid or pid < 0:
                    return None
            except Exception:
                return None

            # Normalize app name (ApplicationFrameWindow → ApplicationFrameHost.exe)
            app_name = _safe_process_name(pid, hwnd)

            # Respect skiplist *after* normalization
            if self._should_skip_app(app_name):
                return None

            window_title = win32gui.GetWindowText(hwnd) or ""

            # Skip browser-based CRD viewers, but only if this frame *is* a browser tab showing CRD
            if (app_name or "").lower() in {"chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe"}:
                if re.search(r"(chrome remote desktop|remotedesktop\.google\.com)", window_title, re.IGNORECASE):
                    return None

            # Cursor + UIA context (optional, never gates logging)
            x, y = win32api.GetCursorPos()
            element_info = {}
            focused_info = {}
            bounds = _get_window_bounds(hwnd)
            state = _get_window_state(hwnd)

            with auto.UIAutomationInitializerInThread():
                try:
                    elem = auto.ControlFromPoint(x, y)
                    element_info = {
                        "name": elem.Name,
                        "control_type": elem.ControlTypeName,
                        "bounding_rect": {
                            "left": elem.BoundingRectangle.left,
                            "top": elem.BoundingRectangle.top,
                            "right": elem.BoundingRectangle.right,
                            "bottom": elem.BoundingRectangle.bottom
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
                "application": app_name,         # e.g., 'ApplicationFrameHost.exe' for Photos/Media Player
                "window_title": window_title,
                "cursor_position": [x, y],
                "focused_element": focused_info,
                "element_under_cursor": element_info,
                "hwnd": hwnd,                    # frame hwnd (stable for bucketing & a11y capture)
                "pid": pid,                      # frame pid
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
                "log_file": f"{order}_{_sanitize_filename(app_name)}.json",  # <— sanitize
                "a11y_tree_files": [],
                "events": 0,
                "by_type": {
                    "mouse_click": 0,
                    "mouse_up": 0,
                    "mouse_move": 0,
                    "drag_drop": 0,
                    "key_press": 0,
                    "hotkey": 0,
                    "scroll": 0
                }
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
            curr = a11y.get_accessibility_state()
            ch = a11y.diff_accessibility(last, curr) if last else None
            if ch:
                acc_changes = acc.setdefault("changes", [])
                acc_changes.append({"ts": now_ts, "diff": ch})
                acc["last"] = curr
                m["accessibility"] = acc
                _save_meta(m)

        
        def _focused_surface_key(ev):
            return self._surface_key(ev.get("application"), ev.get("window_title"), ev.get("hwnd"))

        def _note_focus_transition_if_any(ev):
            key = _focused_surface_key(ev)
            if not key:
                return False
            if self._focused_surface != key:
                self._focused_surface = key
                return True
            return False




        def _maybe_capture_a11y_tree(ev, order=None, on_focus=False):
            # Only capture when the surface *gains* focus if focus-only mode is enabled
            if self._a11y_mode == "focus_only" and not on_focus:
                return

            key = self._surface_key(ev.get("application"), ev.get("window_title"), ev.get("hwnd"))
            hwnd = ev.get("hwnd")
            if not hwnd:
                return

            # cooldown gate
            now = time.time()
            last = self._surface_sig.get(key, {})
            if last and (now - last.get("ts", 0.0) < self._A11Y_RECAP_COOLDOWN):
                return

            try:
                if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                    return
            except Exception:
                return

            try:
                with auto.UIAutomationInitializerInThread():
                    # anchor by handle
                    root = None
                    try:
                        root = auto.ControlFromHandle(hwnd)
                    except Exception:
                        root = None
                    if not root:
                        return

                    # walk up to top-level Window node (smaller, stable subtree)
                    node = root
                    hops = 0
                    try:
                        while node and node.ControlTypeName != "Window" and hops < 64:
                            node = node.GetParentControl()
                            hops += 1
                    except Exception:
                        pass
                    if not node:
                        node = root

                    # build a quick signature of *immediate* children (type+name)
                    sig_parts = []
                    try:
                        for ch in node.GetChildren():
                            nm = (ch.Name or "").strip()
                            ct = ch.ControlTypeName or ""
                            if nm or ct:
                                sig_parts.append((ct, nm[:64]))
                                if len(sig_parts) >= 24:  # cap for speed
                                    break
                    except Exception:
                        pass
                    sig = hash(tuple(sig_parts))

                    # If signature didn't change, skip; otherwise record & dump
                    if last and last.get("sig") == sig:
                        self._surface_sig[key]["ts"] = now  # refresh timestamp
                        return

                    # Serialize a bounded tree (depth 4 is enough)
                    tree = self._serialize_a11y_tree(node, depth=0, max_depth=4)
                    if not tree:
                        return

                    if order is None:
                        order = self.app_name_to_order.get(ev.get("application"), self.app_order_counter)

                    ts_ms = int(round(float(ev.get("timestamp", time.time())) * 1000))
                    fname = f"{order}_{_sanitize_filename(ev.get('application'))}_{ts_ms}_a11y_tree.json"

                    with open(os.path.join(self.log_folder_path, fname), "w", encoding="utf-8") as tf:
                        json.dump(tree, tf, indent=2)

                    m = _load_meta() or {}
                    app_entry = _ensure_app_in_meta(m, ev.get("application"), order)
                    app_entry.setdefault("a11y_tree_files", []).append(fname)
                    _save_meta(m)

                    # remember signature & time
                    self._surface_sig[key] = {"sig": sig, "ts": now}

            except Exception as e:
                print(f"[a11y] capture failed for {ev.get('application')}: {e}")

        
        def log_event(ev):
            raw_app = ev.get("application") or "unknown"
            if self._should_skip_app(raw_app):
                return

            # Determine the per-app bucket (splits ApplicationFrameHost.exe by hosted app)
            bf = getattr(self, "_bucket_for_app", None)
            if callable(bf):
                bucket_key, bucket_label = bf(raw_app, ev.get("window_title"), ev.get("hwnd"))
            else:
                bucket_key, bucket_label = raw_app, raw_app

            # --- SESSION-BOUNDARY / ROLLOVER LOGIC ---
            surface_key = self._surface_key(ev.get("application"), ev.get("window_title"), ev.get("hwnd"))
            last_ts = self.surface_last_event_ts.get(surface_key)
            now_ts = ev.get("timestamp", time.time())

            if last_ts is not None and (now_ts - last_ts) > self.ROLL_THRESHOLD:
                # bump order so a new file is used
                self.app_name_to_order[bucket_key] = self.app_order_counter
                order = self.app_order_counter
                self.app_order_counter += 1
                try:
                    gap = now_ts - last_ts
                except Exception:
                    gap = 0
                print(f"[ROLLOVER] Idle gap {gap:.1f}s for {bucket_key} on {surface_key}, new order {order}")
            else:
                if bucket_key not in self.app_name_to_order:
                    self.app_name_to_order[bucket_key] = self.app_order_counter
                    self.app_order_counter += 1
                order = self.app_name_to_order[bucket_key]

            # update last event time for this surface
            self.surface_last_event_ts[surface_key] = now_ts

            # focus-only a11y recap; capture only when the surface gains focus
            on_focus = _note_focus_transition_if_any(ev)
            _maybe_capture_a11y_tree(ev, order=order, on_focus=on_focus)

            # periodic accessibility settings snapshot/diff
            _maybe_record_accessibility_change(ev.get("timestamp", now_ts))

            # --- METADATA UPDATE (bucketed by bucket_key) ---
            m = _load_meta() or {}
            app_entry = _ensure_app_in_meta(m, bucket_key, order)
            app_entry["display_name"] = bucket_label  # nice label for any UI you build later

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
            by_type = app_entry.setdefault("by_type", {
                "mouse_click": 0, "mouse_up": 0, "mouse_move": 0, "drag_drop": 0,
                "key_press": 0, "hotkey": 0, "scroll": 0
            })
            by_type[ev_type] = int(by_type.get(ev_type, 0)) + 1

            m.setdefault("summary", {"apps": 0, "events": 0, "by_type": {}, "by_app": {}})
            m["summary"]["events"] = int(m["summary"].get("events", 0)) + 1
            m["summary"]["by_app"][bucket_key] = int(m["summary"]["by_app"].get(bucket_key, 0)) + 1
            m["summary"]["by_type"][ev_type] = int(m["summary"]["by_type"].get(ev_type, 0)) + 1
            m["session"]["last_event_at"] = ev.get("timestamp", now_ts)
            _save_meta(m)

            # --- APPEND TO PER-APP FILE (sanitize bucket_key for filenames) ---
            filename = f"{order}_{_sanitize_filename(bucket_key)}.json"
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




         # --- input tracking state/config ---
        MOVE_THROTTLE_SEC = 0.25   # limit mouse_move logging to ~10 Hz
        DRAG_DISTANCE_PX = 6       # minimum pixels to count as drag
        pressed_keys = set()       # normalized names, e.g., {'Ctrl', 'Shift', 'a'}
        pressed_mods = set()       # subset: {'Ctrl','Alt','Shift','Win','Insert','CapsLock'}
        mouse_buttons_down = set() # e.g., {'Button.left'}
        drag_start = None          # {'pos':(x,y), 'ts':..., 'button':..., 'context': ev_at_press}
        dragging = False
        last_move_ts = 0.0
        last_move_pos = None

        # --- key normalization / hotkey helpers ---
        from pynput.keyboard import Key

        MOD_SET = {"Ctrl", "Alt", "Shift", "Win", "Insert", "CapsLock"}
        MOD_ORDER = {"Ctrl": 1, "Alt": 2, "Win": 3, "Shift": 4, "Insert": 5, "CapsLock": 6}

        def _normalize_key_obj(k):
            # chars
            try:
                ch = getattr(k, "char", None)
                if ch:
                    return ch.lower()
            except Exception:
                pass

            # named keys
            s = str(k)  # e.g., 'Key.ctrl_l' -> 'Key.ctrl_l'
            if s.startswith("Key."):
                name = s[4:]
            else:
                name = s

            mapping = {
                "ctrl": "Ctrl", "ctrl_l": "Ctrl", "ctrl_r": "Ctrl",
                "alt": "Alt", "alt_l": "Alt", "alt_r": "Alt",
                "shift": "Shift", "shift_l": "Shift", "shift_r": "Shift",
                "cmd": "Win", "cmd_l": "Win", "cmd_r": "Win",
                "caps_lock": "CapsLock", "insert": "Insert",
                "tab": "Tab", "esc": "Esc", "space": "Space",
                "enter": "Enter", "backspace": "Backspace",
                "delete": "Delete", "home": "Home", "end": "End",
                "page_up": "PageUp", "page_down": "PageDown",
                "up": "Up", "down": "Down", "left": "Left", "right": "Right",
            }
            if name in mapping:
                return mapping[name]

            # F-keys: Key.f1 ... Key.f24
            if name.startswith("f") and name[1:].isdigit():
                return name.upper()

            # fallback: title-case best effort
            return name.title()

        def _is_modifier(norm):
            return norm in MOD_SET

        def _build_combo(mods, main_key):
            ordered_mods = sorted(mods, key=lambda m: MOD_ORDER.get(m, 99))
            tail = main_key.upper() if len(main_key) == 1 else main_key
            return "+".join(ordered_mods + [tail])

        COMMON_INTENTS = {
            ("Ctrl", "C"): "copy",
            ("Ctrl", "V"): "paste",
            ("Ctrl", "X"): "cut",
            ("Ctrl", "Z"): "undo",
            ("Ctrl", "Y"): "redo",
            ("Ctrl", "S"): "save",
            ("Ctrl", "O"): "open",
            ("Ctrl", "N"): "new",
            ("Ctrl", "P"): "print",
            ("Ctrl", "L"): "focus_location_bar",
            ("Ctrl", "T"): "new_tab",
            ("Ctrl", "W"): "close_tab",
            ("Alt", "Tab"): "task_switch",
            ("Alt", "F4"): "close_window",
            ("Win", "D"): "show_desktop",
            ("Win", "Left"): "snap_left",
            ("Win", "Right"): "snap_right",
        }

        def _classify_hotkey(mods, main_key):
            mods_set = set(mods)
            reader = None
            if "Insert" in mods_set:
                reader = "NVDA/JAWS likely"
            if "CapsLock" in mods_set:
                reader = "Narrator likely" if reader is None else f"{reader} or Narrator"

            intent = None
            key_for_map = main_key.upper() if len(main_key) == 1 else main_key
            for mod in ("Ctrl", "Alt", "Win"):
                if mod in mods_set:
                    intent = COMMON_INTENTS.get((mod, key_for_map))
                    if intent:
                        break

            return {"screen_reader_combo": bool(reader), "reader_hint": reader, "intent": intent}

        # --- Slider helpers ---

        def _find_slider_at_point(x, y):
            """Return the UIA element for the nearest SliderControl at screen point (x,y)."""
            try:
                with auto.UIAutomationInitializerInThread():
                    el = auto.ControlFromPoint(x, y)
                    node = el
                    for _ in range(12):
                        if not node:
                            break
                        try:
                            if (node.ControlTypeName or "") == "SliderControl":
                                return node
                            node = node.GetParentControl()
                        except Exception:
                            break
            except Exception:
                pass
            return None

        def _read_range_value(slider_el):
            """Read RangeValue/Value from a slider. Returns dict or None."""
            if not slider_el:
                return None
            # Try RangeValuePattern first
            try:
                p = slider_el.GetRangeValuePattern()
                return {
                    "value": float(p.Value),
                    "min": float(p.Minimum),
                    "max": float(p.Maximum),
                    "small_change": float(getattr(p, "SmallChange", 0) or 0),
                    "large_change": float(getattr(p, "LargeChange", 0) or 0),
                    "is_read_only": bool(getattr(p, "IsReadOnly", False)),
                }
            except Exception:
                pass
            # Fallback to ValuePattern if exposed as text
            try:
                vp = slider_el.GetValuePattern()
                v = vp.Value
                try:
                    v = float(v)
                except Exception:
                    pass
                return {"value": v, "min": None, "max": None}
            except Exception:
                return None

        def _slider_identity(slider_el):
            try:
                return {
                    "name": slider_el.Name,
                    "control_type": slider_el.ControlTypeName,
                    "automation_id": getattr(slider_el, "AutomationId", "") or "",
                }
            except Exception:
                return {"control_type": "SliderControl"}


        def on_click(x, y, button, pressed):
            nonlocal drag_start, dragging
            btn_str = str(button)

            if pressed:
                ev = get_current_info()
                if not ev:
                    return
                _annotate_semantic_target(ev)

                # If press starts on a slider, record its value ("before")
                slider_el = _find_slider_at_point(x, y)
                slider_before = _read_range_value(slider_el) if slider_el else None
                slider_meta = _slider_identity(slider_el) if slider_el else None
                if slider_before:
                    ev.setdefault("slider", dict(slider_meta or {}))
                    ev["slider"]["before"] = slider_before

                ev["event"] = "mouse_click"   # <<< add this so clicks aren’t counted as 'unknown'
                log_event(ev)

                mouse_buttons_down.add(btn_str)
                drag_start = {
                    "pos": (x, y),
                    "ts": time.time(),
                    "button": btn_str,
                    "context": ev,
                    "slider_meta": slider_meta,
                    "slider_before": slider_before,
                }
                dragging = False
                return

            # released
            try:
                mouse_buttons_down.remove(btn_str)
            except KeyError:
                pass

            ev_up = get_current_info()
            if not ev_up:
                # foreground might be filtered/closed; just reset drag state and bail
                dragging = False
                drag_start = None
                return

            _annotate_semantic_target(ev_up)

            # Use event cursor if present; otherwise fall back to callback coords
            x_up, y_up = ev_up.get("cursor_position", (x, y))
            slider_el_up = _find_slider_at_point(x_up, y_up)
            slider_after = _read_range_value(slider_el_up) if slider_el_up else None
            slider_meta_up = _slider_identity(slider_el_up) if slider_el_up else None

            started_on_slider = bool(drag_start and drag_start.get("slider_meta"))
            ended_on_slider   = bool(slider_after)
            was_slider = started_on_slider or ended_on_slider

            ev_up["button"] = btn_str

            if dragging and drag_start:
                dd = {
                    "event": "drag_drop",
                    "timestamp": ev_up["timestamp"],
                    "button": btn_str,
                    "application": ev_up["application"],
                    "window_title": ev_up["window_title"],
                    "cursor_position": ev_up.get("cursor_position", [x, y]),
                    "focused_element": ev_up.get("focused_element"),
                    "element_under_cursor": ev_up.get("element_under_cursor"),
                    "hwnd": ev_up.get("hwnd"),
                    "pid": ev_up.get("pid"),
                    "window_bounds": ev_up.get("window_bounds"),
                    "window_state": ev_up.get("window_state"),
                    "from": {
                        "application": drag_start["context"]["application"],
                        "window_title": drag_start["context"]["window_title"],
                        "cursor_position": drag_start["context"]["cursor_position"],
                        "element_under_cursor": drag_start["context"].get("element_under_cursor"),
                        "focused_element": drag_start["context"].get("focused_element"),
                    },
                    "to": {
                        "application": ev_up["application"],
                        "window_title": ev_up["window_title"],
                        "cursor_position": ev_up.get("cursor_position", [x, y]),
                        "element_under_cursor": ev_up.get("element_under_cursor"),
                        "focused_element": ev_up.get("focused_element"),
                    },
                    "drag_distance": int(math.hypot(
                        x_up - drag_start["context"]["cursor_position"][0],
                        y_up - drag_start["context"]["cursor_position"][1]
                    )),
                    "duration_ms": int((ev_up["timestamp"] - drag_start["ts"]) * 1000),
                }
                if was_slider:
                    dd["slider"] = (drag_start.get("slider_meta") or slider_meta_up or {})
                    dd["slider"]["before"] = drag_start.get("slider_before")
                    dd["slider"]["after"] = slider_after
                log_event(dd)

            elif was_slider and drag_start:
                dur_ms = int((ev_up["timestamp"] - drag_start["ts"]) * 1000)
                dd = {**ev_up}
                dd["event"] = "drag_drop"
                dd["from"] = {
                    "application": drag_start["context"]["application"],
                    "window_title": drag_start["context"]["window_title"],
                    "cursor_position": drag_start["context"]["cursor_position"],
                    "element_under_cursor": drag_start["context"].get("element_under_cursor"),
                    "focused_element": drag_start["context"].get("focused_element"),
                }
                dd["drag_distance"] = int(math.hypot(
                    x_up - drag_start["context"]["cursor_position"][0],
                    y_up - drag_start["context"]["cursor_position"][1]
                ))
                dd["duration_ms"] = dur_ms
                dd["slider"] = (drag_start.get("slider_meta") or slider_meta_up or {})
                dd["slider"]["before"] = drag_start.get("slider_before")
                dd["slider"]["after"] = slider_after
                log_event(dd)

            else:
                ev_up["event"] = "mouse_up"
                log_event(ev_up)

            dragging = False
            drag_start = None




        def on_move(x, y):
            nonlocal last_move_ts, last_move_pos, dragging
            now = time.time()
            if now - last_move_ts < MOVE_THROTTLE_SEC:
                return
            last_move_ts = now

            # compute delta
            if last_move_pos is None:
                dx = dy = 0
            else:
                dx = x - last_move_pos[0]
                dy = y - last_move_pos[1]
            last_move_pos = (x, y)

            # detect drag start if a button is down
            if mouse_buttons_down and drag_start and not dragging:
                dist = math.hypot(x - drag_start["pos"][0], y - drag_start["pos"][1])
                if dist >= DRAG_DISTANCE_PX:
                    dragging = True  # we’ll emit the final drag_drop on mouse up

            ev = get_current_info()
            if not ev:
                return
            ev["event"] = "mouse_move"
            ev["delta"] = [dx, dy]
            # Ensure we report actual cursor pos used for this callback
            ev["cursor_position"] = [x, y]
            log_event(ev)


        def on_scroll(x, y, dx, dy):
            ev = get_current_info()
            if not ev:
                return
            ev["event"] = "scroll"
            ev["delta"] = [dx, dy]
            log_event(ev)


        def on_press(key):
            name = _normalize_key_obj(key)
            pressed_keys.add(name)
            if _is_modifier(name):
                pressed_mods.add(name)

            ev = get_current_info()
            if not ev:
                return
            ev["event"] = "key_press"
            ev["key"] = name
            # add current modifiers to help interpret user intent later
            if pressed_mods:
                ev["modifiers"] = sorted(list(pressed_mods))
            log_event(ev)

            # hotkey detection: log only when a non-modifier key is pressed with any modifier,
            # OR when Insert/CapsLock (screen reader keys) combine with something else
            if not _is_modifier(name):
                mods_now = sorted(list(pressed_mods), key=lambda m: MOD_ORDER.get(m, 99))
                if mods_now:
                    combo = _build_combo(mods_now, name)
                    hk = get_current_info()
                    if not hk:
                        return
                    hk["event"] = "hotkey"
                    hk["combo"] = combo
                    hk["mods"] = mods_now
                    hk["key"] = name
                    hk["classification"] = _classify_hotkey(mods_now, name)
                    log_event(hk)

        def on_release(key):
            name = _normalize_key_obj(key)
            pressed_keys.discard(name)
            pressed_mods.discard(name)

        ml = mouse.Listener(on_click=on_click, on_scroll=on_scroll, on_move=on_move)
        kl = keyboard.Listener(on_press=on_press, on_release=on_release)
        ml.start()
        kl.start()
        self.interaction_loggers = [ml, kl]

        self.quit_button.pack_forget()
        self.stop_button = tk.Button(self.form_frame, text="Stop logging", command=self.stop_task, width=20)
        self.stop_button.configure(font=self.base_font)
        self.stop_button.pack(pady=5)



    def _show_task_brief_dialog(self):
        """Modal pop-up showing the selected task's context and instruction."""
        top = tk.Toplevel(self.root)
        top.title("Task Brief")
        top.transient(self.root)
        top.grab_set()               # modal (user must close before continuing)
        top.resizable(True, True)
        top.attributes("-topmost", True)

        # Size + center
        top.update_idletasks()
        W, H = 850, 500
        x = (top.winfo_screenwidth()  - W) // 2
        y = (top.winfo_screenheight() - H) // 2
        top.geometry(f"{W}x{H}+{x}+{y}")

        # Content
        padx = 20
        tk.Label(top, text="Task Context", font=self.bold_font).pack(anchor="w", padx=padx, pady=(16, 4))
        tk.Message(top,
                text=self.task_metadata.get("context", "no context"),
                width=W - (padx * 2),
                font=self.base_font,
                justify="left").pack(anchor="w", padx=padx)

        tk.Label(top, text="Task Instruction", font=self.bold_font).pack(anchor="w", padx=padx, pady=(12, 4))
        tk.Message(top,
                text=self.task_metadata.get("task", "no task"),
                width=W - (padx * 2),
                font=self.base_font,
                justify="left").pack(anchor="w", padx=padx)

        btns = tk.Frame(top)
        btns.pack(fill="x", padx=padx, pady=(18, 16))
        tk.Button(btns, text="OK — Start logging", width=20, command=top.destroy).pack(side="left")

        # Block until user acknowledges (keeps current behavior like messagebox)
        self.root.wait_window(top)

    def _ask_post_task_outcome_dialog(self):
        """
        Modal dialog shown after logging stops.
        Collects:
        - difficulty_after   (Easy/Medium/Hard)
        - success            (True/False)
        - failure_reason     (if success == False; one selected from dropdown)
        - failure_notes      (optional free text)
        Returns a dict with these keys.
        """
        top = tk.Toplevel(self.root)
        top.title("Task outcome")
        top.transient(self.root)
        top.grab_set()
        top.resizable(False, False)

        # ----- size + center -----
        top.update_idletasks()
        W, H = 800, 500
        x = (top.winfo_screenwidth()  - W) // 2
        y = (top.winfo_screenheight() - H) // 2
        top.geometry(f"{W}x{H}+{x}+{y}")

        # block closing via 'X' (force Save)
        top.protocol("WM_DELETE_WINDOW", lambda: None)

        pad_x = 24

        tk.Label(top, text="Logging stopped", font=self.bold_font)\
            .pack(padx=pad_x, pady=(18, 8), anchor="w")

        # --- Difficulty after ---
        tk.Label(top, text="How difficult was the task to complete?", font=self.base_font)\
            .pack(padx=pad_x, pady=(2, 6), anchor="w")

        default_after = self.task_metadata.get("difficulty_before") or "Medium"
        diff_var = tk.StringVar(value=default_after)
        diff_cb = ttk.Combobox(top, textvariable=diff_var,
                            values=["Easy", "Medium", "Hard"],
                            state="readonly", width=24)
        diff_cb.pack(padx=pad_x, pady=(0, 10), anchor="w")

        # --- Success / failure ---
        tk.Label(top, text="Was the task completed successfully?", font=self.base_font)\
            .pack(padx=pad_x, pady=(10, 4), anchor="w")

        success_var = tk.StringVar(value="yes")  # "yes" or "no"
        r_frame = tk.Frame(top)
        r_frame.pack(padx=pad_x, pady=(0, 8), anchor="w")
        tk.Radiobutton(r_frame, text="Yes", variable=success_var, value="yes", font=self.base_font)\
            .pack(side="left", padx=(0, 12))
        tk.Radiobutton(r_frame, text="No", variable=success_var, value="no", font=self.base_font)\
            .pack(side="left")

        # --- Failure details (conditional) ---
        fail_frame = tk.LabelFrame(top, text="If not successful, why?", padx=10, pady=8)
        fail_frame.pack(fill="x", padx=pad_x, pady=(6, 10))

        tk.Label(fail_frame, text="Reason (choose one):", font=self.base_font)\
            .grid(row=0, column=0, sticky="w")

        # DROPDOWN list of reasons
        reasons = [
            "Ran out of time",
            "Couldn't find the required info",
            "App/website inaccessible",
            "App crashed/froze",
            "Instructions unclear",
            "Permission/account issues",
            "Other (specify)"
        ]
        reason_var = tk.StringVar(value="")
        reason_cb = ttk.Combobox(
            fail_frame, textvariable=reason_var, values=reasons, state="disabled", width=40
        )
        reason_cb.grid(row=0, column=1, padx=(8, 0), pady=(0, 6), sticky="w")

        tk.Label(fail_frame, text="Notes (optional):", font=self.base_font)\
            .grid(row=1, column=0, sticky="nw", pady=(2, 0))
        notes_txt = tk.Text(fail_frame, width=56, height=4, state="disabled")
        notes_txt.grid(row=1, column=1, padx=(8, 0), pady=(2, 0), sticky="w")

        def _toggle_failure_fields(*_):
            is_fail = (success_var.get() == "no")
            reason_cb.configure(state="readonly" if is_fail else "disabled")
            notes_txt.configure(state="normal" if is_fail else "disabled")
            if not is_fail:
                reason_var.set("")
                notes_txt.delete("1.0", "end")

        success_var.trace_add("write", _toggle_failure_fields)
        _toggle_failure_fields()

        # --- Buttons ---
        result = {
            "difficulty_after": default_after,
            "success": True,
            "failure_reason": None,
            "failure_notes": None
        }

        def on_save():
            # collect difficulty
            result["difficulty_after"] = diff_var.get() or default_after
            # collect success + failures
            ok = True
            if success_var.get() == "yes":
                result["success"] = True
                result["failure_reason"] = None
                result["failure_notes"] = None
            else:
                result["success"] = False
                reason = reason_var.get().strip()
                if not reason:
                    messagebox.showwarning("Missing reason", "Please select a reason for failure.")
                    ok = False
                result["failure_reason"] = reason or None
                if notes_txt["state"] == "normal":
                    notes = notes_txt.get("1.0", "end").strip()
                    result["failure_notes"] = notes or None
            if ok:
                top.destroy()

        btns = tk.Frame(top)
        btns.pack(fill="x", padx=pad_x, pady=(8, 16))
        tk.Button(btns, text="Save", command=on_save, width=14).pack(side="left")

        self.root.wait_window(top)
        return result


    def stop_task(self):
        for l in self.interaction_loggers:
            try: l.stop()
            except Exception: pass
        self.interaction_loggers = []

        # NEW combined dialog
        outcome = self._ask_post_task_outcome_dialog()
        difficulty_after = outcome["difficulty_after"]
        success = outcome["success"]
        failure_reason = outcome.get("failure_reason")
        failure_notes = outcome.get("failure_notes")

        try:
            with open(self.metadata_path, "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            m = {}

        m.setdefault("task", {})
        if "difficulty_before" not in m["task"]:
            prev = self.task_metadata.get("difficulty_before") or self.task_metadata.get("difficulty")
            if prev is not None:
                m["task"]["difficulty_before"] = prev
            m["task"].pop("difficulty", None)

        m["task"]["difficulty_after"] = difficulty_after

        # --- NEW: success / failure fields ---
        m["task"]["success"] = bool(success)
        if not success:
            m["task"]["failure_reason"] = failure_reason or ""
            if failure_notes:
                m["task"]["failure_notes"] = failure_notes
            else:
                m["task"].pop("failure_notes", None)
        else:
            m["task"].pop("failure_reason", None)
            m["task"].pop("failure_notes", None)

        m.setdefault("session", {})
        m["session"]["ended_at"] = time.time()

        try:
            with open(self.metadata_path, "w", encoding="utf-8") as f:
                json.dump(m, f, indent=2)
        except Exception as e:
            print(f"[meta] write failed in stop_task: {e}")

        rec_src = self._stop_obs()
        final_path = self._bring_obs_file(rec_src)
        self._split_mkv(final_path)
        self.root.quit()


    def run(self):
        self.root.mainloop()
import argparse
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user_id", type=int, default = 0)
    args = parser.parse_args()
    app = TaskGUI(args.user_id)
    app.run()
