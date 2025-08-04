# replay/replay_server.py
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pathlib import Path
import queue
import time

REPLAY_DIR = Path(__file__).resolve().parent           # .../replay
PROJECT_ROOT = REPLAY_DIR.parent
TASK_LOGS_DIR = PROJECT_ROOT / "task_logs"

app = Flask(__name__, static_folder=str(REPLAY_DIR), static_url_path="")
CORS(app)

interaction_queue = queue.Queue()

def log(msg: str):
    print(f"[SERVER] {time.strftime('%H:%M:%S')} {msg}", flush=True)

# ---------- API from Python replayer ----------
@app.post("/replay")
def receive_interaction():
    data = request.get_json(force=True, silent=True) or {}
    log(f"POST /replay  type={data.get('type') or data.get('event')} title={data.get('window_title','')}")
    interaction_queue.put(data)
    return jsonify({"status": "ok"})

@app.get("/next")
def next_interaction():
    if interaction_queue.empty():
        # comment out the log below if it gets too chatty
        # log("GET /next  (empty)")
        return jsonify({"status": "empty"})
    data = interaction_queue.get()
    log(f"GET /next  → {data.get('type') or data.get('event')}")
    return jsonify(data)

# ---------- pages & static ----------
@app.get("/")
def serve_index():
    p = REPLAY_DIR / "replay_web.html"
    log("GET /")
    if not p.exists():
        return (f"replay_web.html not found at:<br>{p}<br>"
                f"REPLAY_DIR contents: {', '.join([x.name for x in REPLAY_DIR.iterdir()])}"), 500
    return send_from_directory(REPLAY_DIR, "replay_web.html")

@app.get("/newtab.html")
def serve_newtab():
    log("GET /newtab.html")
    return send_from_directory(REPLAY_DIR, "newtab.html")

@app.get("/favicon.ico")
def favicon():
    return ("", 204)

# Allow iframe to load saved DOM files under /task_logs/...
@app.get("/task_logs/<path:subpath>")
def serve_task_logs(subpath):
    log(f"GET /task_logs/{subpath}")
    return send_from_directory(TASK_LOGS_DIR, subpath)

# absorb common ping/redirect endpoints referenced by snapshots
@app.route("/url", methods=["GET","POST"])
def absorb_url_redirect():
    return ("", 204)

@app.route("/gen_204", methods=["GET","POST"])
def absorb_gen204():
    return ("", 204)

@app.get("/healthz")
def healthz():
    return "ok"

if __name__ == "__main__":
    print(f"[SERVER] Serving UI from {REPLAY_DIR}")
    print(f"[SERVER] Serving task logs from {TASK_LOGS_DIR}")
    app.run(host="127.0.0.1", port=8090, debug=False)
