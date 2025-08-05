import os
import re
import time
import json
import subprocess
from pathlib import Path
from glob import glob
from typing import List, Dict, Tuple, Optional

import psutil
import pyautogui
import requests
import uiautomation as auto
import ctypes

# ============================== CONFIG =================================
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK = "7"
TASK_DIR = PROJECT_ROOT / "task_logs" / DEFAULT_TASK
JS_REPLAY_ENDPOINT = "http://localhost:8090/replay"

# Executables (adjust if needed)
APP_PATHS = {
    "chrome.exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "calc.exe":   r"C:\Windows\System32\calc.exe",
    "notepad.exe":r"C:\Windows\System32\notepad.exe",
    "cmd.exe":    r"C:\Windows\System32\cmd.exe",
}

# Key normalization
KEYMAP = {
    "return": "enter", "enter": "enter", "key.enter": "enter", "Key.enter": "enter",
    "escape": "esc", "esc": "esc",
    "backspace": "backspace", "key.backspace": "backspace", "Key.backspace": "backspace",
    "space": "space", "key.space": "space", "Key.space": "space",
    "tab": "tab"
}

# PyAutoGUI safety tweaks
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

# Win32 fallback for low-level clicking
_user32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004

# UIA clickable type bias
CLICKABLE_TYPES = {
    "ButtonControl", "HyperlinkControl", "MenuItemControl",
    "ListItemControl", "TreeItemControl", "TabItemControl"
}

# ============================== LOGGING ================================
def dbg(msg: str):
    print(f"[REPLAY] {time.strftime('%H:%M:%S')} {msg}", flush=True)

# ============================ UTILITIES =================================
def ts_to_seconds(ts) -> float:
    try:
        t = float(ts)
        return t/1000.0 if t > 1e11 else t
    except Exception:
        return 0.0

def clean_name(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"\s*\(+\s*$", "", raw).strip()
    return s.replace(" ppinned", " pinned")

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
    return corrections.get(s, s.capitalize() if s else "")

def get_process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""

def is_newtab_title(s: str) -> bool:
    return (s or "").strip().lower() == "new tab - google chrome"

# ========================= GEOMETRY / MOUSE =============================
def uia_rect(ctrl: auto.Control) -> Dict:
    try:
        r = ctrl.BoundingRectangle
        return {"left": int(r.left), "top": int(r.top), "right": int(r.right), "bottom": int(r.bottom)}
    except Exception:
        return {"left":0, "top":0, "right":0, "bottom":0}

def rect_valid(r: Dict) -> bool:
    return (r.get("right", 0) > r.get("left", 0)) and (r.get("bottom", 0) > r.get("top", 0))

def rect_center(r: Dict) -> Tuple[int,int]:
    return ((r["left"] + r["right"]) // 2, (r["top"] + r["bottom"]) // 2)

def _safe_point(x: int, y: int, margin: int = 6) -> Tuple[int,int]:
    sw, sh = pyautogui.size()
    return max(margin, min(sw - margin, int(x))), max(margin, min(sh - margin, int(y)))

def _move_cursor(x: int, y: int):
    x, y = _safe_point(x, y)
    _user32.SetCursorPos(int(x), int(y))

def _win32_click(x: int, y: int):
    _move_cursor(x, y)
    time.sleep(0.02)
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    _user32.mouse_event(MOUSEEVENTF_LEFTUP,   0, 0, 0, 0)

def move_and_click(x: int, y: int, clicks: int = 1):
    x, y = _safe_point(x, y)
    dbg(f"mouse → move_to={x},{y} clicks={clicks}")
    try:
        pyautogui.moveTo(x, y, duration=0.08)
        pyautogui.click(x, y, clicks=clicks)
    except Exception as e:
        dbg(f"[WARN] pyautogui click failed ({e}); falling back to win32")
        _win32_click(x, y)

def clickable_point(ctrl: auto.Control) -> Optional[Tuple[int,int]]:
    try:
        pt = ctrl.GetClickablePoint()
        return int(pt[0]), int(pt[1])
    except Exception:
        return None

# ============================ UIA HELPERS ==============================
def supports_invoke(ctrl: auto.Control) -> bool:
    try:
        ctrl.GetInvokePattern()
        return True
    except Exception:
        return False

def try_scroll_into_view(ctrl: auto.Control):
    for _ in range(4):
        try:
            ctrl.GetScrollItemPattern().ScrollIntoView()
            time.sleep(0.05)
            return
        except Exception:
            try:
                ctrl = ctrl.GetParentControl()
            except Exception:
                break

def elevate_to_clickable(ctrl: auto.Control, expected_type: str, expected_rect: Optional[Dict]) -> auto.Control:
    wanted = (expected_type or "").lower()
    node = ctrl
    try:
        for _ in range(6):
            ct = node.ControlTypeName or ""
            r = uia_rect(node)
            if ct in CLICKABLE_TYPES and rect_valid(r):
                return node
            if ("Text" in ct or "Group" in ct or not rect_valid(r) or
                (wanted in ("button","menuitem","listitem","treeitem","tabitem") and "Text" in ct)):
                node = node.GetParentControl()
                continue
            return node
    except Exception:
        pass
    return ctrl

def descendant_match(root: auto.Control, name: str, ctype: str) -> Optional[auto.Control]:
    try:
        for ch in root.GetChildren():
            nm = ch.Name or ""
            ct = ch.ControlTypeName or ""
            if (not name or name == nm or name in nm) and (not ctype or ctype in ct):
                return ch
            got = descendant_match(ch, name, ctype)
            if got:
                return got
    except Exception:
        pass
    return None

def find_element_by_attrs(parent: auto.Control, name: str, control_type: str,
                          expected_rect: Optional[Dict]=None) -> Optional[auto.Control]:
    """
    Score candidates. Prefer:
     1. Exact name+expected_type match.
     2. Name substring + expected_type.
     3. Clickable types over plain text.
     4. Overlap with expected_rect.
    """
    name_clean = clean_name(name)
    expected_type = normalize_control_type(control_type)
    best, best_score = None, -1

    def score_element(el: auto.Control) -> int:
        nm = el.Name or ""
        ct = el.ControlTypeName or ""
        score = 0
        # exact name + expected type
        if name_clean and nm == name_clean and expected_type and expected_type in ct:
            score += 100
        # substring name match
        if name_clean and name_clean in nm:
            score += 10
        # expected type match
        if expected_type and expected_type in ct:
            score += 30
        # clickable boost
        if ct in CLICKABLE_TYPES:
            score += 8
        # demote if pure TextControl when not expected as text
        if "TextControl" in ct and expected_type.lower() != "text":
            score -= 10
        # expected rect overlap
        if expected_rect and rect_valid(expected_rect):
            try:
                erect = uia_rect(el)
                # compute area overlap heuristic
                ax1, ay1, ax2, ay2 = erect["left"], erect["top"], erect["right"], erect["bottom"]
                bx1, by1, bx2, by2 = expected_rect.get("left",0), expected_rect.get("top",0), expected_rect.get("right",0), expected_rect.get("bottom",0)
                x_overlap = max(0, min(ax2, bx2) - max(ax1, bx1))
                y_overlap = max(0, min(ay2, by2) - max(ay1, by1))
                ov = x_overlap * y_overlap
                if ov > 0:
                    score += int(10 * ov / max(1, (expected_rect.get("right",0)-expected_rect.get("left",0)) * max(1, expected_rect.get("bottom",0)-expected_rect.get("top",0))))
            except Exception:
                pass
        return score

    def dfs(node: auto.Control):
        nonlocal best, best_score
        try:
            sc = score_element(node)
            if sc > best_score:
                best_score, best = sc, node
            for ch in node.GetChildren():
                dfs(ch)
        except Exception:
            pass

    # 1) Try hit-test vicinity first
    if expected_rect and rect_valid(expected_rect):
        x, y = rect_center(expected_rect)
        try:
            hit = auto.ControlFromPoint(x, y)
        except Exception:
            hit = None
        if hit:
            # look under hit
            candidate = descendant_match(hit, name_clean, expected_type)
            if candidate:
                return candidate
            # climb if matching
            walker = hit
            for _ in range(6):
                if not walker: break
                nm = walker.Name or ""; ct = walker.ControlTypeName or ""
                if (not name_clean or name_clean == nm or name_clean in nm) and (not expected_type or expected_type in ct):
                    return walker
                try:
                    walker = walker.GetParentControl()
                except Exception:
                    break

    # 2) Full tree search with scoring
    dfs(parent)
    return best

# ========================== WINDOW MANAGEMENT ==========================
def _root() -> auto.Control:
    return auto.GetRootControl()

def find_window(app: str, title_contains: str) -> Optional[auto.Control]:
    app_l = (app or "").lower()
    ttl = (title_contains or "").lower()
    for w in _root().GetChildren():
        try:
            name_l = (w.Name or "").lower()
            proc = get_process_name(w.ProcessId).lower()
            class_name = getattr(w, "ClassName", lambda: "")() or ""
            # Special case: UWP Settings
            if "settings" in app_l or "systemsettings" in app_l or "applicationframehost" in app_l:
                if "settings" in name_l or "settings" in ttl:
                    if ("applicationframewindow" in class_name.lower() or
                        "applicationframehost.exe" in proc or
                        "settings" in name_l):
                        return w
            # Usual logic:
            if ttl and ttl not in name_l:
                continue
            if app_l and proc != app_l:
                continue
            return w
        except Exception:
            continue
    return None



def bring_to_front(w: Optional[auto.Control]):
    if not w:
        return
    try:
        w.SetActive()
        time.sleep(0.05)
    except Exception:
        pass

def launch_uwp_by_title(title: str):
    t = (title or "").lower()
    if "settings" in t:
        dbg("Launching Settings via ms-settings:")
        subprocess.Popen('cmd /c start ms-settings:', shell=True)
        return
    if "calculator" in t:
        if os.path.exists(APP_PATHS.get("calc.exe","")):
            dbg("Launching Calculator via calc.exe")
            subprocess.Popen(APP_PATHS["calc.exe"])
        else:
            dbg("Launching Calculator via URI")
            subprocess.Popen('cmd /c start calculator:', shell=True)
        return

def ensure_target_window(app: str, title: str, bounds: Optional[Dict], state: Optional[str], launched: set) -> Optional[auto.Control]:
    key = f"{(app or '').lower()}|{(title or '').lower()}"
    w = find_window(app, title)
    if w:
        bring_to_front(w)
        if bounds:
            try:
                if (state or "").lower() == "normal":
                    width = max(100, bounds["right"] - bounds["left"])
                    height = max(100, bounds["bottom"] - bounds["top"])
                    w.MoveTo(bounds["left"], bounds["top"])
                    w.Resize(width, height)
            except Exception:
                pass
        return w

    # UWP: always try to launch if not found
    app_l = (app or "").lower()
    if (app_l == "applicationframehost.exe" or "settings" in app_l or "systemsettings" in app_l or "settings" in title.lower()):
        launch_uwp_by_title(title)
        launched.add(key)
        time.sleep(2.0)  # UWP takes a sec
        w = find_window(app, title)
        if w:
            bring_to_front(w)
            return w
        dbg(f"ensure_target_window: failed to get UWP window for {app}|{title}")
        return None

    # Regular EXEs
    if key not in launched:
        exe = APP_PATHS.get(app_l)
        if exe and os.path.exists(exe):
            dbg(f"Launching {exe}")
            subprocess.Popen(exe)
        launched.add(key)
        time.sleep(1.0)
        w = find_window(app, title)
        if w:
            bring_to_front(w)
            return w
        dbg(f"ensure_target_window: failed to get window for {app}|{title}")
    return None


# ============================ REPLAY PRIMITIVES ==========================
def press_key(key: str):
    k = (key or "").strip()
    mapped = KEYMAP.get(k, KEYMAP.get(k.lower(), k.lower()))
    dbg(f"KEY_PRESS '{k}' -> '{mapped}'")
    try:
        pyautogui.press(mapped)
    except Exception as e:
        dbg(f"[WARN] key press failed: {e}")

def send_to_js_replay(action: Dict):
    try:
        requests.post(JS_REPLAY_ENDPOINT, json=action, timeout=1.5)
        dbg(f"→ JS {action.get('type') or action.get('event')}")
    except Exception as e:
        dbg(f"[WARN] JS replay error: {e}")

def replay_click(app: str, log: Dict):
    if (app or "").lower() == "chrome.exe":
        send_to_js_replay(log)
        return

    element = log.get("element_under_cursor") or log.get("element") or {}
    name = element.get("name") or element.get("text") or ""
    ctype_raw = element.get("control_type", "")
    expected_rect = element.get("bounding_rect") or {}

    wtitle = log.get("window_title","") or ""
    # bring window front
    w = find_window(app, wtitle)
    bring_to_front(w)

    with auto.UIAutomationInitializerInThread():
        root = _root()
        target = None
        # restrict to same process with matching title
        try:
            for win in root.GetChildren():
                try:
                    if get_process_name(win.ProcessId).lower() == app.lower():
                        if wtitle.lower() in (win.Name or "").lower():
                            target = win
                            break
                except Exception:
                    continue
        except Exception:
            pass
        target = target or (w or root)

        match = find_element_by_attrs(target, name, ctype_raw, expected_rect)
        if match:
            nm, ct = match.Name or "", match.ControlTypeName or ""
            dbg(f"UIA match → '{nm}' ({ct}) expected_type='{normalize_control_type(ctype_raw)}'")
            # elevate to clickable
            click_target = elevate_to_clickable(match, normalize_control_type(ctype_raw), expected_rect)
            try_scroll_into_view(click_target)
            r = uia_rect(click_target)
            dbg(f"click_target rect → {r}")

            # ---- ALWAYS TRY .Click() FIRST! ----
            try:
                click_target.Click()
                dbg("UIA Click succeeded")
                return
            except Exception as e:
                dbg(f"[WARN] UIA Click failed: {e}")
            # ---- fallback: clickable point, etc. ----
            pt = clickable_point(click_target)
            if pt:
                dbg(f"click by clickable point {pt}")
                move_and_click(pt[0], pt[1])
                return

            if rect_valid(r):
                cx, cy = rect_center(r)
                dbg(f"click by rect center {(cx, cy)}")
                move_and_click(cx, cy)
                return

            # last UIA attempt: Invoke
            if supports_invoke(click_target):
                try:
                    click_target.Invoke()
                    dbg("UIA Invoke succeeded")
                    return
                except Exception as e:
                    dbg(f"[WARN] UIA Invoke failed: {e}")

    # final fallback: raw coordinates
    if expected_rect and rect_valid(expected_rect):
        cx, cy = rect_center(expected_rect)
        dbg(f"[FALLBACK] screen click at {(cx, cy)} for '{name}' ({ctype_raw})")
        move_and_click(cx, cy)


# ========================= LOG LOADING & MERGE ===========================
def load_from_metadata(task_dir: Path) -> List[Dict]:
    meta_path = task_dir / "metadata.json"
    if not meta_path.exists():
        dbg(f"[ERROR] metadata.json missing in {task_dir}")
        return []
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    web_dir = task_dir / "web_logs"
    web_pages: List[Dict] = []
    first_real_page: Optional[Dict] = None
    first_real_ts = float("inf")

    # A) extension pages to know first real web page
    for wfile in sorted(glob(str(web_dir / "web_tab*.json"))):
        try:
            with open(wfile, "r", encoding="utf-8") as f:
                page = json.load(f)
        except Exception as e:
            dbg(f"[WARN] failed reading {wfile}: {e}")
            continue
        web_pages.append({"file": wfile, "page": page})

        ts_candidates = []
        if "created_at" in page: ts_candidates.append(ts_to_seconds(page["created_at"]))
        inters = page.get("interactions") or []
        if inters:
            ts_candidates.append(min(ts_to_seconds(i.get("timestamp")) for i in inters if "timestamp" in i))
        page_first_ts = min(ts_candidates) if ts_candidates else float("inf")
        if page_first_ts < first_real_ts:
            first_real_ts = page_first_ts
            first_real_page = page

    if first_real_page:
        dbg(f"first real page: '{first_real_page.get('title','')}' at {time.strftime('%H:%M:%S', time.localtime(first_real_ts))}")
    else:
        dbg("no web_tab*.json found; may only replay New-Tab typing")

    # B) local logs (Chrome New-Tab special-casing)
    files_in_order = [e["log_file"] for e in meta["session"]["apps_by_order"]]
    events: List[Dict] = []
    newtab_events: List[Dict] = []

    for fname in files_in_order:
        path = task_dir / fname
        if not path.exists():
            dbg(f"[WARN] missing log file {path}")
            continue
        with open(path, "r", encoding="utf-8") as f:
            arr = json.load(f) or []
        kept = 0
        for ev in arr:
            ev["_ts_s"] = ts_to_seconds(ev.get("timestamp"))
            app = (ev.get("application") or "").lower()
            if app == "chrome.exe":
                if is_newtab_title(ev.get("window_title","")):
                    ev["is_web"] = True
                    ev["newtab"] = True
                    k = (ev.get("key") or "").lower()
                    if k in ("key.enter", "return"):
                        ev["enter"] = True
                    newtab_events.append(ev); kept += 1
            else:
                ev["is_web"] = False
                events.append(ev); kept += 1
        dbg(f"loaded {kept} kept from {fname}")

    if newtab_events:
        t0 = newtab_events[0]["_ts_s"] - 0.01
        events.append({
            "application": "chrome.exe",
            "window_title": "New Tab - Google Chrome",
            "is_web": True,
            "newtab": True,
            "type": "newtab_boot",
            "_ts_s": t0
        })
        if first_real_page:
            dom_url = first_real_page.get("dom_url") or first_real_page.get("dom_file")
            snap_b64 = first_real_page.get("dom_snapshot_base64")
            first_enter = next((e for e in newtab_events if e.get("enter")), None)
            if first_enter:
                if dom_url:
                    first_enter["next_dom_url"] = dom_url if str(dom_url).startswith("/") else f"/{dom_url}"
                if snap_b64:
                    first_enter["next_dom_snapshot_base64"] = snap_b64
        events.extend(newtab_events)

    # === SYNTHETIC PAGELAOD EVENTS FOR WEB TABS WITH NO INTERACTIONS ===
    for item in web_pages:
        page = item["page"]
        inters = page.get("interactions", [])
        title = page.get("title", "")
        url = page.get("url", "")
        dom_url = page.get("dom_url") or page.get("dom_file")
        snap = page.get("dom_snapshot_base64")
        ts = ts_to_seconds(page.get("created_at"))
        if inters and len(inters) > 0:
            continue  # skip tabs with interactions
        if not title or not dom_url:
            continue  # skip synthetic pageloads for blank/unknown tabs
        prev_max_ts = max([e["_ts_s"] for e in events], default=0)
        ts = max(prev_max_ts + 0.001, ts)
        event = {
            "application": "chrome.exe",
            "window_title": title,
            "url": url,
            "is_web": True,
            "type": "page_load",
            "_ts_s": ts,
        }
        if dom_url:
            event["dom_url"] = dom_url if str(dom_url).startswith("/") else f"/{dom_url}"
        if snap:
            event["dom_snapshot_base64"] = snap
        events.append(event)
        dbg(f"added synthetic page_load for tab {title} at {ts}")


    # C) extension web interactions
    for item in web_pages:
        page = item["page"]
        inters = page.get("interactions", []) or []
        title = page.get("title","")
        url = page.get("url")
        snap = page.get("dom_snapshot_base64")
        dom_url = page.get("dom_url") or page.get("dom_file")

        for idx, act in enumerate(inters):
            ev = dict(act)
            ev["application"] = "chrome.exe"
            ev["window_title"] = title
            ev["url"] = url
            ev["is_web"] = True
            ev["_ts_s"] = ts_to_seconds(act.get("timestamp"))
            if idx == 0:
                if snap: ev["dom_snapshot_base64"] = snap
                if dom_url:
                    ev["dom_url"] = dom_url if str(dom_url).startswith("/") else f"/{dom_url}"
            et = (ev.get("type") or ev.get("event") or "")
            if et == "page" and not ev.get("dom_snapshot_base64") and not ev.get("dom_url"):
                continue
            events.append(ev)
        dbg(f"loaded web_tab.json {Path(item['file']).name} interactions={len(inters)}")

    # Final sort
    events = [e for e in events if "_ts_s" in e]
    events.sort(key=lambda e: e["_ts_s"])
    dbg(f"TOTAL events merged:{len(events)}")
    kept_newtab = sum(1 for e in events if e.get("newtab"))
    kept_web    = sum(1 for e in events if e.get("is_web") and not e.get("newtab"))
    kept_local  = len(events) - kept_newtab - kept_web
    dbg(f"SUMMARY newtab(local→web)={kept_newtab}  web(ext)={kept_web}  local(UIA)={kept_local}")
    # print ("events", events)
    return events

# ================================ MAIN =================================
def replay(task_dir: Path):
    logs = load_from_metadata(task_dir)
    if not logs:
        dbg("No logs to replay; exiting.")
        return

    chrome_opened = False
    launched: set[str] = set()
    current_key: Optional[str] = None
    prev_ts: Optional[float] = None

    for i, ev in enumerate(logs):
        ts = ev.get("_ts_s", 0.0)
        if prev_ts is not None:
            time.sleep(max(0.0, min(ts - prev_ts, 1.0)))
        prev_ts = ts

        app = (ev.get("application") or "")
        title = ev.get("window_title","")
        etype = (ev.get("event") or ev.get("type") or "").lower()
        is_web = bool(ev.get("is_web"))
        # keying: keep one Chrome web window, otherwise per-app|title
        if app.lower() == "chrome.exe" and is_web:
            key = "chrome.exe|web"
        else:
            key = f"{app.lower()}|{title.lower()}"

        if key != current_key:
            dbg(f"→ switching to {app} :: '{title}'")
            if app.lower() == "chrome.exe" and is_web and not chrome_opened:
                exe = APP_PATHS.get("chrome.exe")
                if exe and os.path.exists(exe):
                    subprocess.Popen([exe, "--new-window", "http://localhost:8090/"])
                    dbg("Opened Chrome at http://localhost:8090/")
                    time.sleep(2.0)
                    chrome_opened = True
            else:
                ensure_target_window(app, title, ev.get("window_bounds"), ev.get("window_state"), launched)
            current_key = key

        dbg(f"{i+1}/{len(logs)} → {etype} app={app} title='{title}' web={is_web}")

        if app.lower() == "chrome.exe" and is_web:
            send_to_js_replay(ev)
            continue

        if etype == "key_press":
            press_key(ev.get("key",""))
        elif etype in ("mouse_click", "web_click"):
            replay_click(app, ev)

if __name__ == "__main__":
    if not TASK_DIR.exists():
        print(f"[REPLAY][ERROR] Task folder not found: {TASK_DIR}")
        raise SystemExit(1)
    print(f"[REPLAY] Using task folder: {TASK_DIR}")
    print("[REPLAY] Start server separately:  python replay/replay_server.py")
    replay(TASK_DIR)
