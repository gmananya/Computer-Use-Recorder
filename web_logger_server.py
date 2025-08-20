# web_logger_server.py
import threading, json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime
from base64 import b64decode

TASK_LOG_DIR = "task_logs"
CURRENT_TASK = "latest"
interactions_by_tab = {}
order_counter = 1

def _task_folder():
    return os.path.join(TASK_LOG_DIR, str(CURRENT_TASK))

def _meta_path():
    return os.path.join(_task_folder(), f"metadata_{CURRENT_TASK}.json")

def _web_folder():
    p = os.path.join(_task_folder(), "web_logs")
    os.makedirs(p, exist_ok=True); return p

def reset_state_for_task(task_id):
    global CURRENT_TASK, interactions_by_tab, order_counter
    CURRENT_TASK = str(task_id)
    interactions_by_tab = {}
    order_counter = 1
    os.makedirs(_task_folder(), exist_ok=True)
    os.makedirs(_web_folder(), exist_ok=True)
    # ensure metadata exists
    if not os.path.exists(_meta_path()):
        with open(_meta_path(), "w", encoding="utf-8") as f:
            json.dump({
                "task": {},
                "session": {"started_at": None, "last_event_at": None,
                            "folder": _task_folder().replace("/", "\\"), "apps_by_order": []},
                "apps": {},
                "web": {"tabs_by_order": [], "events": 0, "by_tab": {}},
                "summary": {"apps": 0, "events": 0, "by_type": {}, "by_app": {}}
            }, f, indent=2)

def _load_meta():
    try:
        with open(_meta_path(), "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return None

def _save_meta(m):
    try:
        with open(_meta_path(), "w", encoding="utf-8") as f: json.dump(m, f, indent=2)
    except Exception as e:
        print("[WebLogServer] metadata write failed:", e)

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
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._set_headers(400); return

        try:
            url = data.get("url", "")
            title = data.get("title", "")
            inters = data.get("interactions", []) or []
            real_inters = [ev for ev in inters if ev.get("type") != "page"]
            dom_b64 = data.get("dom_snapshot_base64")
            a11y_tree = data.get("accessibility_tree")
            ts_now = int(data.get("timestamp") or (datetime.utcnow().timestamp() * 1000))

            global order_counter
            key = _tab_key(data)
            # Always set up or update tab info
            if key not in interactions_by_tab:
                interactions_by_tab[key] = {
                    "url": url, "title": title, "order": order_counter,
                    "created_at": ts_now, "interactions": [],
                    "dom_file": None, "dom_url": None,
                    "a11y_file": None, "a11y_url": None
                }
                order_counter += 1

            tab = interactions_by_tab[key]
            web_dir = _web_folder()
            order = tab["order"]
            created = tab["created_at"]

            # Always (re)write HTML and a11y if provided
            if dom_b64:
                dom_name = _tab_dom_name(order, created)
                with open(os.path.join(web_dir, dom_name), "wb") as hf:
                    hf.write(b64decode(dom_b64))
                tab["dom_file"]  = os.path.join(TASK_LOG_DIR, CURRENT_TASK, "web_logs", dom_name).replace("\\", "/")
                tab["dom_url"] = "/" + tab["dom_file"]

            if "accessibility_tree" in data:
                tree = data["accessibility_tree"] or {"error": "empty"}
                a11y_name = _tab_a11y_name(order, created)
                with open(os.path.join(web_dir, a11y_name), "w", encoding="utf-8") as jf:
                    json.dump(tree, jf, indent=2)
                tab["a11y_file"] = os.path.join(TASK_LOG_DIR, CURRENT_TASK, "web_logs", a11y_name).replace("\\", "/")
                tab["a11y_url"] = "/" + tab["a11y_file"]



            tab = interactions_by_tab[key]
            # tab["interactions"].extend(inters)
            tab["interactions"].extend(real_inters)

            # Persist per-tab JSON (links to files)
            web_dir = _web_folder()
            tab_json = _tab_json_name(tab["order"], tab["created_at"])
            with open(os.path.join(web_dir, tab_json), "w", encoding="utf-8") as f:
                json.dump({
                    "url": tab["url"], "title": tab["title"],
                    "order": tab["order"], "created_at": tab["created_at"],
                    "dom_file": tab["dom_file"], "dom_url": tab["dom_url"],
                    "a11y_file": tab["a11y_file"], "a11y_url": tab["a11y_url"],
                    "interactions": tab["interactions"]
                }, f, indent=2)

            # Update metadata (no replay_timeline)
            meta = _load_meta() or {}
            web_meta = meta.setdefault("web", {"tabs_by_order": [], "events": 0, "by_tab": {}})
            tabs = web_meta["tabs_by_order"]
            ident = {"order": tab["order"], "first_ts": tab["created_at"]}
            found = None
            for t in tabs:
                if t.get("order") == ident["order"] and t.get("first_ts") == ident["first_ts"]:
                    found = t; break
            rel_tab_json = os.path.join("task_logs", CURRENT_TASK, "web_logs", tab_json).replace("\\", "/")
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
            added = len(inters)
            web_meta["events"] = int(web_meta.get("events", 0)) + added
            key2 = f"{tab['order']}::{tab['created_at']}"
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
