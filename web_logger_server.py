import threading
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

TASK_LOG_DIR = "task_logs"
CURRENT_TASK = "latest"
MAX_ACCESSIBILITY_TREES = 15
interactions_by_tab = {}
order_counter = 1

def get_tab_key(data):
    return f"{data.get('url')}::{data.get('title')}"

def get_tab_filename(tab_key, order):
    safe_key = f"web_tab{order}.json"
    return safe_key

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
            self._set_headers(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body.decode("utf-8"))
            url = data.get("url")
            title = data.get("title")
            tab_key = get_tab_key(data)
            has_tree = "accessibility_tree" in data
            has_dom = "dom_snapshot_base64" in data
            has_interactions = "interactions" in data

            folder = os.path.join(TASK_LOG_DIR, str(CURRENT_TASK), "web_logs")
            os.makedirs(folder, exist_ok=True)

            global order_counter

            if tab_key not in interactions_by_tab:
                interactions_by_tab[tab_key] = {
                    "url": url,
                    "title": title,
                    "accessibility_tree": data.get("accessibility_tree") if has_tree else None,
                    "dom_snapshot_base64": data.get("dom_snapshot_base64") if has_dom else None,
                    "order": order_counter,
                    "interactions": []
                }
                order_counter += 1

            if has_interactions:
                interactions_by_tab[tab_key]["interactions"].extend(data["interactions"])

            # Write or overwrite the file each time
            tab_data = interactions_by_tab[tab_key]
            file_path = os.path.join(folder, get_tab_filename(tab_key, tab_data["order"]))
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(tab_data, f, ensure_ascii=False, indent=2)

            self._set_headers(200)

        except Exception as e:
            print(f"[WebLogServer] Error: {e}")
            self._set_headers(500)

def run_web_logger():
    server = HTTPServer(('localhost', 8765), WebLogHandler)
    print("Web logger listening on http://localhost:8765/log_web")
    server.serve_forever()

def run_web_logger_in_thread():
    threading.Thread(target=run_web_logger, daemon=True).start()
