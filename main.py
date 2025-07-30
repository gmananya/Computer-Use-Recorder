import platform
import shutil
import tkinter as tk
from tkinter import messagebox, ttk
import tkinter.font as tkFont
import json
import ctypes
import win32process
import win32gui
import psutil
import ffmpeg
from ffmpeg._run import Error as FFmpegError
import os
import winreg
import uiautomation as auto
from pynput import mouse, keyboard
from datetime import datetime
import time
import threading
import win32api
import win32con
from obswebsocket import obsws, requests
import subprocess
import web_logger_server


TASKS_PATH = "tasks_list.json"

class TaskGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Computer Use Logger")
        self.tasks = self.load_tasks()
        self.task_metadata = {}
        self.current_tasks = []
        self.logging_active = False
        self.interaction_loggers = []
        self.current_log_path = None
        self.app_name_to_order = {} 
        self.app_order_counter = 1
        self.accessibility_trees = {}
        self.accessibility_tree_logged_apps = set()
        self.accessibility_skiplist = {"explorer.exe","python.exe","pythonw.exe","obs64.exe","SearchHost.exe","StartMenuExperienceHost.exe","ShellExperienceHost.exe"}



        # font styling
        self.base_font = tkFont.Font(family="Segoe UI", size=10)
        self.bold_font = tkFont.Font(family="Segoe UI", size=12, weight="bold")

        self.build_initial_gui()

    def load_tasks(self):
        with open(TASKS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def build_initial_gui(self):
        self.root.geometry("1000x700")
        self.root.resizable(True, True)

        self.task_set_var = tk.StringVar()
        self.task_number_var = tk.StringVar()

        self.form_frame = tk.Frame(self.root, padx=20, pady=20)
        self.form_frame.pack(fill="both", expand=True)

        # task set
        tk.Label(self.form_frame, text="Task Set #:", font=self.base_font).grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.task_set_dropdown = ttk.Combobox(self.form_frame, textvariable=self.task_set_var, state="readonly", width=47)
        self.task_set_dropdown.configure(font=self.base_font)
        self.task_set_dropdown['values'] = [f"Set {i}" for i in range(1, 8)]
        self.task_set_dropdown.grid(row=0, column=1, padx=5, pady=5)
        self.task_set_dropdown.bind("<<ComboboxSelected>>", self.task_dropdown)

        # task number
        tk.Label(self.form_frame, text="Task #:", font=self.base_font).grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.task_number_dropdown = ttk.Combobox(self.form_frame, textvariable=self.task_number_var, state="readonly", width=47)
        self.task_number_dropdown.configure(font=self.base_font)
        self.task_number_dropdown.grid(row=1, column=1, padx=5, pady=5)

        self.continue_button = tk.Button(self.form_frame, text="Next", command=self.task_details, width=20)
        self.continue_button.configure(font=self.base_font)
        self.continue_button.grid(row=2, column=0, columnspan=2, pady=20)

    def task_dropdown(self, event=None):
        selected_set = self.task_set_var.get()
        if not selected_set:
            return

        set_index = int(selected_set.split()[1])
        start_idx = 0 if set_index == 1 else 10 + (set_index - 2) * 15
        count = 10 if set_index == 1 else 15
        self.current_tasks = self.tasks[start_idx:start_idx + count]

        display_values = []
        for i, task in enumerate(self.current_tasks):
            example = task.get("Example Instruction", "").strip()
            label = f"Task {i+1} - {example}" if example else f"Task {i+1}"
            display_values.append(label)

        self.task_number_dropdown['values'] = display_values
        self.task_number_var.set("")

    def task_details(self):
        if not self.task_set_var.get() or not self.task_number_var.get():
            messagebox.showwarning("Missing", "Please select both a task set and task number.")
            return

        selected_label = self.task_number_var.get()
        try:
            task_index = int(selected_label.split()[1]) - 1
        except Exception:
            messagebox.showerror("Error", "Failed to parse task number.")
            return

        task = self.current_tasks[task_index]
        context = task.get("Context", "No context")
        instruction = task.get("Task", "No task")

        self.task_metadata = {
            "task_set": self.task_set_var.get(),
            "task_number": self.task_number_var.get(),
            "context": context,
            "task": instruction
        }

        for widget in self.form_frame.winfo_children():
            widget.destroy()

        # show task context and instruction
        tk.Label(self.form_frame, text="Task Context", font=self.bold_font).pack(anchor="w", pady=(0, 2))
        tk.Label(self.form_frame, text=context, wraplength=800, justify="left", font=self.base_font).pack(anchor="w", pady=(0, 10))

        tk.Label(self.form_frame, text="Task Instruction", font=self.bold_font).pack(anchor="w", pady=(0, 2))
        tk.Label(self.form_frame, text=instruction, wraplength=800, justify="left", font=self.base_font).pack(anchor="w", pady=(0, 10))

        # familiarity and difficulty
        self.task_familiarity = tk.StringVar()
        self.task_difficulty = tk.StringVar()

        dropdown_frame = tk.Frame(self.form_frame)
        dropdown_frame.pack(pady=10)

        tk.Label(dropdown_frame, text="Familiarity", font=self.base_font).grid(row=0, column=0, sticky="w", padx=5, pady=2)
        familiarity_combo = ttk.Combobox(dropdown_frame, textvariable=self.task_familiarity,
                                         values=["Low", "Medium", "High"], state="readonly", width=20)
        familiarity_combo.configure(font=self.base_font)
        familiarity_combo.grid(row=0, column=1, padx=5, pady=4, sticky="w")

        tk.Label(dropdown_frame, text="Difficulty", font=self.base_font).grid(row=1, column=0, sticky="w", padx=5, pady=2)
        difficulty_combo = ttk.Combobox(dropdown_frame, textvariable=self.task_difficulty,
                                        values=["Easy", "Medium", "Hard"], state="readonly", width=20)
        difficulty_combo.configure(font=self.base_font)
        difficulty_combo.grid(row=1, column=1, padx=5, pady=4, sticky="w")

        # start and quit logging
        button_frame = tk.Frame(self.form_frame)
        button_frame.pack(pady=10)

        self.start_button = tk.Button(button_frame, text="Start Logging", command=self.start_task, width=20)
        self.start_button.configure(font=self.base_font)
        self.start_button.pack(side="left", padx=10)

        self.quit_button = tk.Button(button_frame, text="Quit", command=self.root.quit, width=20)
        self.quit_button.configure(font=self.base_font)
        self.quit_button.pack(side="left", padx=10)

    def close_other_apps(self):
        current_pid = os.getpid()
        whitelist = ['code.exe', 'python.exe', 'pythonw.exe', 'magnify.exe', 'narrator.exe', 'osk.exe', 'nvda.exe', 'jfw.exe', 'obs64.exe', 'explorer.exe']

        for proc in psutil.process_iter(['pid', 'name', 'username']):
            try:
                pid = proc.info['pid']
                name = proc.info['name']
                username = proc.info['username']

                # skip current app or if app is in whitelist
                if pid == current_pid or name.lower() in whitelist:
                    continue

                # skip system processes
                if username is None or username.lower() in ['system', 'local service', 'network service']:
                    continue

                # skip processes without a visible window (background tasks)
                if not self.has_visible_window(pid):
                    continue

                print(f"Terminating: {name} (PID {pid})")
                proc.terminate()

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    
    def has_visible_window(self, pid):
        def callback(hwnd, pid_list):
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
            if found_pid == pid and win32gui.IsWindowVisible(hwnd):
                pid_list.append(True)
        results = []
        win32gui.EnumWindows(callback, results)
        return bool(results)
    
    # retrieve accessibility settings from the windows registry
    def get_accessibility_settings(self):
        def read_registry(root, path, key):
            try:
                with winreg.OpenKey(root, path) as reg_key:
                    value, _ = winreg.QueryValueEx(reg_key, key)
                    return value
            except Exception:
                return "unavailable"

        def is_narrator_running():
            for proc in psutil.process_iter(['name']):
                if proc.info['name'] and 'narrator' in proc.info['name'].lower():
                    return True
            return False

        
        def get_cursor_theme():
            arrow = read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors", "Arrow")
            if isinstance(arrow, str) and "aero" in arrow.lower():
                return "Windows Aero"
            elif isinstance(arrow, str) and "windows black" in arrow.lower():
                return "Windows Black"
            elif arrow:
                return arrow.split("\\")[-1]  # fallback to filename
            else:
                return "unavailable"

        # define color filter types
        FILTER_TYPES = {
            0: "None", 1: "Inverted", 2: "Grayscale",
            3: "Red-Green", 4: "Green-Red", 5: "Blue-Yellow"
        }

        raw_filter_type = read_registry(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ColorFiltering", "FilterType")
        readable_filter_type = FILTER_TYPES.get(int(raw_filter_type), "Unknown") if isinstance(raw_filter_type, int) or str(raw_filter_type).isdigit() else "unavailable"

        return {
            "os": platform.system(),
            "os_version": platform.version(),
            "python_version": platform.python_version(),
            "high_contrast": read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\HighContrast", "Flags"),
            "sticky_keys": read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\StickyKeys", "Flags"),
            "toggle_keys": read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\ToggleKeys", "Flags"),
            "filter_keys": read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\Keyboard Response", "Flags"),
            "font_smoothing": read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", "FontSmoothing"),
            "display_scaling": read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", "LogPixels"),
            "narrator": read_registry(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Narrator\NoRoam", "WinEnterLaunchEnabled") or is_narrator_running(),
            "magnifier": read_registry(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ScreenMagnifier", "Magnification"),
            "color_filter": read_registry(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\ColorFiltering", "Active"),
            # text_cursor_thickness": read_registry(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility", "CursorWidth"),
            "color_filter_type": readable_filter_type,
            "mouse_cursor_scheme": get_cursor_theme(),
        }
    
    def start_obs_recording(self, output_path):
        try:
            self.obs_client = obsws("localhost", 4444, "") 
            self.obs_client.connect()
            time.sleep(2)
            self.obs_client.call(requests.StartRecording())
            print("OBS recording started.")
        except Exception as e:
            print(f"Failed to start OBS recording: {e}")

    def stop_obs_recording(self):
        try:
            if self.obs_client:
                status = self.obs_client.call(requests.GetRecordingStatus())
                recording_file = status.getRecordingFilename()

                self.obs_client.call(requests.StopRecording())
                time.sleep(5)  # Ensure recording is finalized
                self.obs_client.disconnect()
                print("OBS recording stopped.")
                return recording_file
        except Exception as e:
            print(f"Failed to stop OBS recording: {e}")
        return None

    def split_mkv_streams(self):

        ffmpeg_path = r"C:\ffmpeg\bin\ffmpeg.exe"
        
        recording_path = os.path.join(self.log_folder_path, "recording.mkv")
        output_screen = os.path.join(self.log_folder_path, "screen.mp4")
        output_system_audio = os.path.join(self.log_folder_path, "system_audio.wav")
        output_mic_audio = os.path.join(self.log_folder_path, "mic_audio.wav")

        if not os.path.exists(recording_path):
            print(f"Recording file not found at {recording_path}")
            return

        try:
            # Extract screen video
            ffmpeg.input(recording_path).output(output_screen, map='0:v', c='copy').run(overwrite_output=True)

            # Extract system audio (first audio track)
            ffmpeg.input(recording_path).output(output_system_audio, map='0:a:0', acodec='pcm_s16le').run(overwrite_output=True)

            # Extract mic audio (second audio track)
            ffmpeg.input(recording_path).output(output_mic_audio, map='0:a:1', acodec='pcm_s16le').run(overwrite_output=True)

            print("Successfully split .mkv into separate streams.")
        except ffmpeg.Error as e:
            print("FFmpeg error:", e.stderr.decode() if e.stderr else str(e))

    def move_obs_recording_to_task_folder(self, recording_file):
        try:
            if not recording_file:
                print("No recording file provided.")
                return

            # Wait for OBS to release the file
            for _ in range(20):  # try for up to ~5 seconds
                try:
                    with open(recording_file, 'rb'):
                        break  # file is accessible
                except PermissionError:
                    print("Waiting for OBS to release the file...")
                    time.sleep(0.5)
            else:
                print("Timeout: OBS is still using the file.")
                return

            if os.path.exists(recording_file):
                dest_path = os.path.join(self.log_folder_path, "recording.mkv")
                os.rename(recording_file, dest_path)
                print(f"Moved OBS recording to task folder: {dest_path}")
            else:
                print(f"Recording file not found at {recording_file}")
        except Exception as e:
            print(f"Error moving OBS recording: {e}")

    def serialize_accessibility_tree(self, element, depth=0, max_depth=4):
        if element is None or depth > max_depth:
            return None
        try:
            children = []
            for child in element.GetChildren():
                serialized_child = self.serialize_accessibility_tree(child, depth + 1, max_depth)
                if serialized_child:
                    children.append(serialized_child)
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
                "children": children
            }
        except Exception as e:
            return {"error": str(e)}


    def start_task(self):
        self.task_metadata["familiarity"] = self.task_familiarity.get()
        self.task_metadata["difficulty"] = self.task_difficulty.get()
        self.task_metadata["accessibility_settings"] = self.get_accessibility_settings()

        self.close_other_apps()

        set_number = int(self.task_metadata["task_set"].split()[1])
        task_number = int(self.task_metadata["task_number"].split()[1])

        if set_number == 1:
            final_task_id = task_number
        else:
            final_task_id = 10 + (set_number - 2) * 15 + task_number

        web_logger_server.CURRENT_TASK = str(final_task_id)
        web_logger_server.run_web_logger_in_thread()
        
        folder_name = str(final_task_id)
        folder_path = os.path.join("task_logs", folder_name)
        os.makedirs(folder_path, exist_ok=True)
        with open(os.path.join(folder_path, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(self.task_metadata, f, indent=2)

        messagebox.showinfo("Logging started", f"Started logging:\n\n{self.task_metadata}")

        self.start_button.pack_forget()
        def interaction_logger():
            def log_event(event_data):
                app_name = event_data.get("application", "unknown")
                # Log accessibility tree once per application
                if app_name not in self.accessibility_tree_logged_apps:
                    def get_pid_by_name(name):
                        for proc in psutil.process_iter(['pid', 'name']):
                            if proc.info['name'] and proc.info['name'].lower() == name.lower():
                                return proc.info['pid']
                        return None

                    try:
                        pid = get_pid_by_name(app_name)
                        if pid is not None:
                            with auto.UIAutomationInitializerInThread():
                                root_element = auto.GetRootControl()
                                for child in root_element.GetChildren():
                                    if child.ProcessId == pid:
                                        tree = self.serialize_accessibility_tree(child)
                                        if tree:
                                            self.accessibility_trees[app_name] = tree
                                            self.accessibility_tree_logged_apps.add(app_name)
                                            # Save to file immediately
                                            with open(os.path.join(self.log_folder_path, "local_apps_accessibility_trees.json"), "w", encoding="utf-8") as f:
                                                json.dump(self.accessibility_trees, f, indent=2)
                                        break
                    except Exception as e:
                        print(f"Failed to get accessibility tree for {app_name}: {e}")

                timestamp = event_data["timestamp"]
                readable_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")

                if app_name not in self.app_name_to_order:
                    self.app_name_to_order[app_name] = self.app_order_counter
                    self.app_order_counter += 1

                order = self.app_name_to_order[app_name]
                filename = f"{order}_{app_name}.json"
                filepath = os.path.join(self.log_folder_path, filename)

                if not os.path.exists(filepath):
                    with open(filepath, "w", encoding="utf-8") as f:
                        json.dump([], f)

                with open(filepath, "r", encoding="utf-8") as f:
                    logs = json.load(f)

                logs.append(event_data)

                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(logs, f, indent=2)

            def get_current_info():
                hwnd = win32gui.GetForegroundWindow()
                tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid is None or pid < 0:
                    return None 
                process = psutil.Process(pid)
                app_name = process.name()
                if app_name.lower() in self.accessibility_skiplist:
                    return None
                window_title = win32gui.GetWindowText(hwnd)
                x, y = win32api.GetCursorPos()
                element_info = {}
                focused_info = {}
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
                        focused_info = {
                            "name": focused.Name,
                            "control_type": focused.ControlTypeName
                        }
                    except Exception as e:
                        focused_info = {"error": str(e)}

                return {
                    "timestamp": time.time(),
                    "application": app_name,
                    "window_title": window_title,
                    "cursor_position": [x, y],
                    "focused_element": focused_info,
                    "element_under_cursor": element_info
                }

            def on_click(x, y, button, pressed):
                event = get_current_info()
                if event is None:
                    return
                if pressed:
                    event = get_current_info()
                    event["event"] = "mouse_click"
                    event["button"] = str(button)
                    log_event(event)

            def on_scroll(x, y, dx, dy):
                event = get_current_info()
                if event is None:
                    return
                event = get_current_info()
                event["event"] = "scroll"
                event["delta"] = [dx, dy]
                log_event(event)

            def on_press(key):
                event = get_current_info()
                if event is None:
                    return
                try:
                    key_str = key.char
                except:
                    key_str = str(key)
                event = get_current_info()
                event["event"] = "key_press"
                event["key"] = key_str
                log_event(event)

            mouse_listener = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
            keyboard_listener = keyboard.Listener(on_press=on_press)
            mouse_listener.start()
            keyboard_listener.start()

            self.interaction_loggers = [mouse_listener, keyboard_listener]

        # Store the folder path for access
        self.log_folder_path = folder_path
        self.start_obs_recording(folder_path)
        interaction_logger()


        self.quit_button.pack_forget()

        self.stop_button = tk.Button(self.form_frame, text="Stop Logging", command=self.stop_task, width=20)
        self.stop_button.configure(font=self.base_font)
        self.stop_button.pack(pady=5)


    def stop_task(self):
        messagebox.showinfo("Stopped", "Logging stopped. Data saved.")
        for logger in self.interaction_loggers:
            logger.stop()
        self.interaction_loggers = []

        recording_file = self.stop_obs_recording()
        self.move_obs_recording_to_task_folder(recording_file)
        self.split_mkv_streams()
        self.root.quit()


    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = TaskGUI()
    app.run()
