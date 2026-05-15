# Computer Use Recorder

[![Paper](https://img.shields.io/badge/Paper-arXiv-B31B1B?style=flat-square&logo=arxiv&logoColor=fff)](https://arxiv.org/abs/2602.09310)
[![A11y-CUA Dataset](https://img.shields.io/badge/A11y--CUA_Dataset-HuggingFace-FFD21E?style=flat-square&logo=huggingface&logoColor=000)](https://huggingface.co/datasets/berkeley-hci/A11y-CUA)
[![Reduced Dataset](https://img.shields.io/badge/Reduced_Dataset-HuggingFace-FFD21E?style=flat-square&logo=huggingface&logoColor=000)](https://huggingface.co/datasets/berkeley-hci/Reduced-A11y-CUA)
[![Project Page](https://img.shields.io/badge/Project_Page-blue?style=flat-square&logo=googlechrome&logoColor=fff)](https://ananyagm.com/a11y-cua/a11y-cua.html)
[![Data Explorer](https://img.shields.io/badge/Data_Explorer-blue?style=flat-square&logo=googlechrome&logoColor=fff)](https://ananyagm.com/a11y-cua/dataset-explorer.html)

A desktop recorder for collecting interaction trace data of users doing computer tasks. For every task session, it captures every keyboard and mouse event along with action context, browser interactions (via a Chrome extension), screen video, audio, and a snapshot of the participant's accessibility settings. The tool is designed for assistive-technology research: it works with screen readers (NVDA, JAWS) and records the full Windows UI Automation accessibility tree for every application the user uses.

---

## Prerequisites

| Tool | Version / Notes |
|------|----------------|
| Python | 3.10+ (64-bit) |
| Git | Any recent version |
| ffmpeg + ffprobe | Must be on your `PATH` |
| OBS Studio | With **obs-websocket v5** enabled (port `4455`, password `add your password`) |
| Google Chrome | With the bundled extension loaded (see below) |

### Python dependencies

```
pip install -r requirement.txt
```

---

## OBS Setup

1. Open OBS → **Tools → WebSocket Server Settings**.
2. Enable the WebSocket server, set port to **4455** and add your password.
3. Make sure OBS is open and recording-ready before you start the recorder.

---

## Chrome Extension Setup

1. Open Chrome → `chrome://extensions/` → enable **Developer mode**.
2. Click **Load unpacked** and select the `web_logger/` folder inside this repository.
3. Pin the extension and ensure it is enabled on all sites.

The extension sends browser interactions to the local HTTP server (`localhost:8765`) that `main.py` starts automatically.

---

## Running the Recorder

```bash
# Run VS Code as Administrator
python main.py
```

- A window opens. Choose an available **Task** from the dropdown.
- Click **Next**, review the task context and difficulty, then click **Start task**.
- A brief pop-up shows the task instruction — click **OK** to begin logging.
- When the participant finishes, click **Stop logging**.
- A post-task dialog collects perceived difficulty and success/failure.
- OBS recording is stopped and split into `screen.mp4` + `system_audio.wav` automatically.

---

## Output Structure

```
task_logs/
  User <N>/
    completed_tasks.txt          # one line per recorded task
    <task_id>/
      metadata_<task_id>.json   # task info, session times, app list, a11y baseline
      <order>_<app>.json        # per-app interaction events
      <order>_<app>_<ts>_a11y_tree.json   # UIA accessibility tree snapshots
      screen.mp4                # video-only stream extracted from OBS recording
      system_audio.wav          # system audio stream
      mic_audio.wav             # microphone stream (if present)
      web_logs/
        web_tab<n>_<ts>.json    # interactions per browser tab
        web_tab<n>_<ts>.html    # DOM snapshot
        web_tab<n>_<ts>_a11y_tree.json   # browser accessibility tree
```

