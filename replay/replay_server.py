# replay_server.py

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import queue
import os

app = Flask(__name__, static_folder=".", static_url_path="")  # Serve current folder
CORS(app)

interaction_queue = queue.Queue()

@app.route("/replay", methods=["POST"])
def receive_interaction():
    data = request.get_json()
    print("📥 Received from Python:", data)
    interaction_queue.put(data)
    return jsonify({"status": "ok"})

@app.route("/next", methods=["GET"])
def send_next():
    if interaction_queue.empty():
        return jsonify({"status": "empty"})
    data = interaction_queue.get()
    print("🚀 Sent to Chrome:", data)
    return jsonify(data)

@app.route("/")
def serve_index():
    return send_from_directory(".", "replay_web.html") 

if __name__ == "__main__":
    app.run(port=8090)
