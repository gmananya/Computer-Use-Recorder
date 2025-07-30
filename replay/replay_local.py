import json
import os
import time
import subprocess
import uiautomation as auto
import psutil
from typing import List, Dict, Tuple
from glob import glob
import pyautogui
import requests
import re

key_mapping = {
    "Return": "enter", 
    "Escape": "esc",
    "Backspace": "backspace",
    "Space": "space",
    "Tab": "tab",
    "key.enter": "enter"
}

app_paths = {
    "calc.exe": r"C:\Windows\System32\calc.exe",
    "notepad.exe": r"C:\Windows\System32\notepad.exe",
    "chrome.exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "cmd.exe": r"C:\Windows\System32\cmd.exe",
    "Code.exe": r"C:\Users\Ananya\AppData\Local\Programs\Microsoft VS Code\Code.exe",
}

JS_REPLAY_ENDPOINT = "http://localhost:8090/replay"

# =============================
# Helpers
# =============================

def ts_to_seconds(ts) -> float:
    """Normalize unix timestamps to seconds (float)."""
    if ts is None:
        return 0.0
    try:
        # int or float
        t = float(ts)
    except Exception:
        return 0.0
    # Heuristic: ms if > 1e11 (1973 in seconds), or if integer with 13 digits
    return t / 1000.0 if t > 1e11 else t

def normalize_control_type(raw: str) -> str:
    s = (raw or "").replace("Control", "").strip().lower()
    corrections = {
        "groupp": "Group",
        "buttoon": "Button",
        "listitem": "ListItem",
        "pane": "Pane",
        "button": "Button",
        "edit": "Edit",
        "checkbox": "CheckBox",
        "hyperlink": "Hyperlink",
        "text": "Text",
    }
    return corrections.get(s, s.capitalize() if s else s)

def clean_name(raw: str) -> str:
    if not raw:
        return ""
    # Remove dangling parentheses (e.g., "Accessibility (")
    name = re.sub(r"\s*\(+\s*$", "", raw).strip()
    # Fix frequent logger typo
    name = name.replace(" ppinned", " pinned")
    return name

def get_process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""

def find_element_by_attrs(parent: auto.Control, name: str, control_type: str) -> auto.Control:
    name = clean_name(name)
    ctype = normalize_control_type(control_type)
    try:
        for elem in parent.GetChildren():
            # Exact match first
            if elem.Name == name and ctype in elem.ControlTypeName:
                return elem
            # Fuzzy fallback
            if name and name in elem.Name and ctype.lower() in elem.ControlTypeName.lower():
                return elem
            child = find_element_by_attrs(elem, name, ctype)
            if child:
                return child
    except Exception:
        pass
    return None

def activate_window_by_title(title_contains: str) -> auto.Control | None:
    if not title_contains:
        return None
    for w in auto.GetRootControl().GetChildren():
        try:
            if title_contains.lower() in (w.Name or "").lower():
                try:
                    w.SetActive()
                except Exception:
                    pass
                return w
        except Exception:
            continue
    return None

def target_from_window_title(window_title: str) -> str:
    t = (window_title or "").lower()
    if "calculator" in t:
        return "uwp:calculator"
    if "settings" in t:
        return "uwp:settings"
    return "uwp:unknown"

def launch_uwp_target(window_title: str):
    target = target_from_window_title(window_title)
    if target == "uwp:calculator":
        exe = app_paths.get("calc.exe")
        if exe and os.path.exists(exe):
            subprocess.Popen(exe)
            print("✅ Launched Calculator")
        else:
            print("❌ Calculator path not found")
    elif target == "uwp:settings":
        try:
            subprocess.Popen('cmd /c start ms-settings:', shell=True)
            print("✅ Launched Settings (ms-settings:)")
        except Exception as e:
            print(f"❌ Failed to launch Settings: {e}")
    else:
        print(f"⚠️ Unknown UWP target for ApplicationFrameHost: '{window_title}'")

# =============================
# Launchers
# =============================

def launch_application(app_name: str, window_title: str = ""):
    if app_name == "SearchHost.exe":
        print("🔍 Opening Windows Search…")
        pyautogui.press("win")
        time.sleep(1)
        return

    if app_name == "ApplicationFrameHost.exe":
        launch_uwp_target(window_title)
        time.sleep(2)
        activate_window_by_title(window_title)
        return

    exe_path = app_paths.get(app_name)
    if exe_path and os.path.exists(exe_path):
        subprocess.Popen(exe_path)
        print(f"✅ Launched: {exe_path}")
        return

    print(f"⚠️ Cannot launch {app_name}. Path not found or not provided.")

# =============================
# Replay primitives
# =============================

def press_key(key: str):
    normalized = key_mapping.get(key, (key or "").lower())
    try:
        pyautogui.press(normalized)
        print(f"⌨️ Pressed key: {normalized}")
    except Exception as e:
        print(f"❌ Error pressing key: {e}")

def send_to_js_replay(action: Dict):
    """Send only web actions to JS server."""
    if not action.get("is_web"):
        return
    try:
        resp = requests.post(JS_REPLAY_ENDPOINT, json=action)
        if resp.status_code == 200:
            print("📡 Sent to JS replay server")
        else:
            print(f"⚠️ JS replay failed with status {resp.status_code}")
    except Exception as e:
        print(f"❌ JS replay error: {e}")

def replay_click(app: str, log: Dict):
    # Chrome DOM events are handled in the browser
    if app == "chrome.exe":
        send_to_js_replay(log)
        return

    element = log.get("element_under_cursor", log.get("element", {}))
    name = clean_name(element.get("name") or element.get("text"))
    control_type = normalize_control_type(element.get("control_type", ""))
    if not name or not control_type:
        print("⚠️ Missing element name or control_type; skipping.")
        return

    # Bring the correct window forward if we have the title
    win_title = log.get("window_title")
    activate_window_by_title(win_title or "")

    for window in auto.GetRootControl().GetChildren():
        try:
            proc_name = get_process_name(window.ProcessId)
            if proc_name.lower() == (app or "").lower():
                match = find_element_by_attrs(window, name, control_type)
                if match:
                    print(f"🎯 Found and clicking: {name} ({control_type})")
                    try:
                        match.Click()
                    except Exception:
                        match.Invoke()
                    return
        except Exception:
            continue

    print(f"⚠️ Could not find element {name} ({control_type}) in any window for {app}")

# =============================
# Log loading
# =============================

def compute_local_time_window(local_events: List[Dict], pad_seconds: int = 600) -> Tuple[float, float]:
    """Return (min_ts_s, max_ts_s) for local events; pad with ±pad_seconds."""
    if not local_events:
        return (0.0, float("inf"))
    ts_s = [ts_to_seconds(e.get("timestamp")) for e in local_events if "timestamp" in e]
    mn, mx = min(ts_s), max(ts_s)
    return (mn - pad_seconds, mx + pad_seconds)

def load_all_logs(folder: str) -> List[Dict]:
    """
    Load local app logs and web logs.
    - Local: all *_*.json EXCEPT web_tab*.json and *chrome.exe.json
    - Web:   web_logs/web_tab*.json (preferred) or top-level web_tab*.json
    Mark web events with is_web=True. For each page, only the FIRST
    interaction carries dom_snapshot_base64 so the DOM loads once.
    Normalize timestamps to seconds for ordering & pacing.
    """
    local_events: List[Dict] = []
    web_events: List[Dict] = []

    # 1) Local app logs (skip chrome.exe local logs)
    for file in glob(os.path.join(folder, "*_*.json")):
        base = os.path.basename(file)
        if base.startswith("web_tab"):
            continue
        if base.lower().endswith("chrome.exe.json"):
            continue

        with open(file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            for ev in data:
                if isinstance(ev, dict):
                    ev["is_web"] = False
                    ev["_ts_s"] = ts_to_seconds(ev.get("timestamp"))
                    local_events.append(ev)
        elif isinstance(data, dict):
            data["is_web"] = False
            data["_ts_s"] = ts_to_seconds(data.get("timestamp"))
            local_events.append(data)

    # 2) Web logs (prefer web_logs/ subfolder; fallback to top-level)
    web_files = set()
    web_files.update(glob(os.path.join(folder, "web_logs", "web_tab*.json")))
    web_files.update(glob(os.path.join(folder, "web_tab*.json")))

    for file in sorted(web_files):
        with open(file, "r", encoding="utf-8") as f:
            page = json.load(f)

        inters = page.get("interactions", []) or []
        url = page.get("url")
        title = page.get("title", "")
        snapshot_b64 = page.get("dom_snapshot_base64")

        for idx, action in enumerate(inters):
            if not isinstance(action, dict):
                continue
            ev = dict(action)
            ev["application"] = "chrome.exe"
            ev["url"] = url
            ev["window_title"] = title
            ev["is_web"] = True
            ev["_ts_s"] = ts_to_seconds(action.get("timestamp"))
            ev["timestamp"] = action.get("timestamp")
            if idx == 0 and snapshot_b64:
                ev["dom_snapshot_base64"] = snapshot_b64
            web_events.append(ev)

    # Filter web events to the local time window (if any locals exist)
    mn, mx = compute_local_time_window(local_events, pad_seconds=600)
    if local_events:
        before = len(web_events)
        web_events = [e for e in web_events if mn <= e["_ts_s"] <= mx]
        after = len(web_events)
        if before != after:
            print(f"🧹 Filtered {before - after} web events outside local session window "
                  f"({time.strftime('%H:%M:%S', time.localtime(mn))}–{time.strftime('%H:%M:%S', time.localtime(mx))})")

    # Merge & order by normalized seconds
    logs = local_events + web_events
    logs = [e for e in logs if "_ts_s" in e]
    logs.sort(key=lambda x: x["_ts_s"])
    return logs

# =============================
# Main replay loop
# =============================

def make_launch_key(app: str, window_title: str, is_web: bool) -> str:
    if app == "ApplicationFrameHost.exe":
        tgt = target_from_window_title(window_title)
        return f"{app}|{tgt}"
    if app == "chrome.exe" and is_web:
        return "chrome.exe|web"
    return app or ""

def replay_interactions(logs: List[Dict], folder: str):
    launched: set[str] = set()
    previous_ts_s = None
    print("🌀 Starting Full Replay (chronological)…")

    for i, log in enumerate(logs):
        ts_s = log.get("_ts_s", 0.0)
        if previous_ts_s is not None:
            # pace gently (avoid very long waits if real gap is hours/days)
            delay = max(0.0, min(ts_s - previous_ts_s, 1.0))
            time.sleep(delay)
        previous_ts_s = ts_s

        app = log.get("application")
        window_title = log.get("window_title", "")
        is_web = bool(log.get("is_web"))
        key = make_launch_key(app, window_title, is_web)

        # Launch when first encountered per key
        if key and key not in launched:
            if app == "chrome.exe" and is_web:
                subprocess.Popen([app_paths["chrome.exe"], "--new-window", "http://localhost:8090"])
                time.sleep(3)
                print("🌐 Opened Chrome for JS replay (http://localhost:8090)")
            else:
                launch_application(app, window_title)
                time.sleep(2)
            launched.add(key)

        kind = log.get("type") or log.get("event") or "(web)"
        print(f"▶ Replaying {i+1}/{len(logs)}: {kind} for {app} — {window_title}")

        # Route web actions to JS only
        if app == "chrome.exe" and is_web:
            send_to_js_replay(log)
            continue

        # Local app actions
        if log.get("event") == "key_press":
            press_key(log.get("key", ""))
        elif log.get("event") in ["mouse_click", "web_click"]:
            replay_click(app, log)

if __name__ == "__main__":
    folder = "./../task_logs/1"
    if not os.path.exists(folder):
        print(f"❌ Folder not found: {folder}")
        raise SystemExit(1)

    logs = load_all_logs(folder)
    web_count = sum(1 for x in logs if x.get("is_web"))
    local_count = len(logs) - web_count
    print(f"✅ Loaded {len(logs)} events. (local={local_count}, web={web_count})")
    replay_interactions(logs, folder)
