# web_logger_server.py
# web_logger_server.py
import threading, json, os, time  # add time
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from base64 import b64decode

import tempfile
import io

TASK_LOG_DIR = "task_logs"
CURRENT_TASK = "latest"
interactions_by_tab = {}
order_counter = 1

# ---- shared metadata lock (used by both processes) ----
META_LOCK = threading.RLock()

def _atomic_write_json(path, obj):
    """Write JSON atomically to avoid torn reads."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _robust_load_json(path: str, retries: int = 10, delay: float = 0.01):
    for _ in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            time.sleep(delay)
        except FileNotFoundError:
            return None
    # last attempt
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _task_folder():
    return os.path.join(TASK_LOG_DIR, str(CURRENT_TASK))

def _web_folder():
    p = os.path.join(_task_folder(), "web_logs")
    os.makedirs(p, exist_ok=True); return p

def _meta_path():
    return os.path.join(_task_folder(), f"metadata_{CURRENT_TASK}.json")

def reset_state_for_task(task_id):
    global CURRENT_TASK, interactions_by_tab, order_counter
    CURRENT_TASK = str(task_id)
    interactions_by_tab = {}
    order_counter = 1
    os.makedirs(_task_folder(), exist_ok=True)
    os.makedirs(_web_folder(), exist_ok=True)
    # ensure metadata exists
    with META_LOCK:
        if not os.path.exists(_meta_path()):
            _atomic_write_json(_meta_path(), {
                "task": {},
                "session": {"started_at": None, "last_event_at": None,
                            "folder": _task_folder().replace("/", "\\"), "apps_by_order": []},
                "apps": {},
                "web": {"tabs_by_order": [], "events": 0, "by_tab": {}},
                "summary": {"apps": 0, "events": 0, "by_type": {}, "by_app": {}}
            })

def _load_meta():
    with META_LOCK:
        try:
            with open(_meta_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            # tiny backoff in case the other side just replaced the file
            time.sleep(0.01)
            try:
                with open(_meta_path(), "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        except Exception:
            return None

def _save_meta(m):
    with META_LOCK:
        _atomic_write_json(_meta_path(), m)


def _tab_key(d):
    sid = d.get("tab_session_id", "")
    url = d.get("url", "")
    return f"{sid}::{url}"

def _tab_json_name(order, created_ms): return f"web_tab{order}_{created_ms}.json"
def _tab_dom_name(order, created_ms):  return f"web_tab{order}_{created_ms}.html"
def _tab_a11y_name(order, created_ms): return f"web_tab{order}_{created_ms}_a11y_tree.json"

class WebLogHandler(BaseHTTPRequestHandler):
    def _set_headers(self, code=200):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers()

    def do_POST(self):
        if self.path != "/log_web":
            self._set_headers(404); return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._set_headers(400); return

        try:
            url    = data.get("url", "") or ""
            title  = data.get("title", "") or ""
            inters = data.get("interactions", []) or []   # DO NOT filter anything
            dom_b64 = data.get("dom_snapshot_base64")
            has_a11y = ("accessibility_tree" in data)
            ts_now = int(data.get("timestamp") or int(datetime.utcnow().timestamp() * 1000))

            # --- per-tab state (no metadata lock needed here) ---
            global order_counter
            key = _tab_key(data)
            if key not in interactions_by_tab:
                interactions_by_tab[key] = {
                    "url": url, "title": title, "order": order_counter,
                    "created_at": ts_now, "interactions": [],
                    "dom_file": None, "dom_url": None,
                    "a11y_file": None, "a11y_url": None
                }
                order_counter += 1

            tab = interactions_by_tab[key]
            if url:   tab["url"] = url
            if title: tab["title"] = title

            web_dir = _web_folder()
            order   = tab["order"]
            created = tab["created_at"]

            # Persist DOM snapshot if provided (no metadata lock)
            if dom_b64:
                dom_name = _tab_dom_name(order, created)
                with open(os.path.join(web_dir, dom_name), "wb") as hf:
                    hf.write(b64decode(dom_b64))
                tab["dom_file"] = os.path.join(TASK_LOG_DIR, CURRENT_TASK, "web_logs", dom_name).replace("\\", "/")
                tab["dom_url"]  = "/" + tab["dom_file"]

            # Persist a11y tree if provided (no metadata lock)
            if has_a11y:
                tree = data.get("accessibility_tree") or {"error": "empty"}
                a11y_name = _tab_a11y_name(order, created)
                with open(os.path.join(web_dir, a11y_name), "w", encoding="utf-8") as jf:
                    json.dump(tree, jf, indent=2)
                tab["a11y_file"] = os.path.join(TASK_LOG_DIR, CURRENT_TASK, "web_logs", a11y_name).replace("\\", "/")
                tab["a11y_url"]  = "/" + tab["a11y_file"]

            # Append all interactions (including "page") to the per-tab buffer
            if inters:
                tab["interactions"].extend(inters)

            # Persist the per-tab JSON (no metadata lock)
            tab_json = _tab_json_name(order, created)
            with open(os.path.join(web_dir, tab_json), "w", encoding="utf-8") as f:
                json.dump({
                    "url": tab["url"], "title": tab["title"],
                    "order": order, "created_at": created,
                    "dom_file": tab["dom_file"], "dom_url": tab["dom_url"],
                    "a11y_file": tab["a11y_file"], "a11y_url": tab["a11y_url"],
                    "interactions": tab["interactions"]
                }, f, indent=2)

            # --- SHORT metadata critical section ---
            with META_LOCK:
                meta = _load_meta() or {}
                web_meta = meta.setdefault("web", {"tabs_by_order": [], "events": 0, "by_tab": {}})
                tabs = web_meta["tabs_by_order"]
                ident = {"order": order, "first_ts": created}
                rel_tab_json = os.path.join("task_logs", CURRENT_TASK, "web_logs", tab_json).replace("\\", "/")

                found = None
                for t in tabs:
                    if t.get("order") == ident["order"] and t.get("first_ts") == ident["first_ts"]:
                        found = t; break

                if not found:
                    tabs.append({
                        **ident,
                        "url": tab["url"], "title": tab["title"],
                        "file": "/" + rel_tab_json,
                        "dom_url": tab["dom_url"], "a11y_url": tab["a11y_url"]
                    })
                else:
                    found.update({
                        "url": tab["url"], "title": tab["title"],
                        "file": "/" + rel_tab_json,
                        "dom_url": tab["dom_url"], "a11y_url": tab["a11y_url"]
                    })

                added = len(inters)  # count everything
                web_meta["events"] = int(web_meta.get("events", 0)) + added
                key2 = f"{order}::{created}"
                web_meta["by_tab"][key2] = int(web_meta["by_tab"].get(key2, 0)) + added

                meta.setdefault("summary", {}).setdefault("events", 0)
                meta["summary"]["events"] = int(meta["summary"]["events"]) + added
                meta.setdefault("session", {})["last_event_at"] = data.get("timestamp") or meta["session"].get("last_event_at")

                _save_meta(meta)

            self._set_headers(200)
        except Exception as e:
            print("[WebLogServer] error:", e)
            self._set_headers(500)



def run_web_logger():
    server = HTTPServer(("localhost", 8765), WebLogHandler)
    print(f"Web logger listening on http://localhost:8765/log_web (task {CURRENT_TASK})")
    server.serve_forever()

def run_web_logger_in_thread():
    threading.Thread(target=run_web_logger, daemon=True).start()
