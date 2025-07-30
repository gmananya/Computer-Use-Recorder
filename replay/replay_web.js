// replay_web.js

const iframe = document.getElementById("replayFrame");
const logDiv = document.getElementById("log");
const possibleFiles = Array.from({ length: 8 }, (_, i) => `web_tab${i + 1}.json`); // Edit upper limit as needed
let currentFileIndex = 0;
let lastFocusedElement = null;

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function highlightBox(targetDoc, x, y) {
  const highlight = targetDoc.createElement("div");
  highlight.style.position = "absolute";
  highlight.style.top = `${y}px`;
  highlight.style.left = `${x}px`;
  highlight.style.width = "40px";
  highlight.style.height = "40px";
  highlight.style.border = "3px solid red";
  highlight.style.background = "rgba(255,0,0,0.1)";
  highlight.style.zIndex = "99999";
  highlight.style.pointerEvents = "none";
  const container = targetDoc.body || targetDoc.documentElement;
  container.appendChild(highlight);
  setTimeout(() => highlight.remove(), 1000);
}

function simulateKey(targetWin, key) {
  const event = new KeyboardEvent("keydown", { key });
  targetWin.document.dispatchEvent(event);
  if (lastFocusedElement && lastFocusedElement.tagName === 'INPUT') {
    lastFocusedElement.value += key;
    lastFocusedElement.dispatchEvent(new Event('input', { bubbles: true }));
    lastFocusedElement.dispatchEvent(new Event('change', { bubbles: true }));
  }
}

function simulateInput(targetDoc, selector, value) {
  const inputEl = targetDoc.querySelector(selector);
  if (inputEl) {
    lastFocusedElement = inputEl;
    inputEl.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(inputEl, value);
    inputEl.dispatchEvent(new Event('input', { bubbles: true }));
    inputEl.dispatchEvent(new Event('change', { bubbles: true }));
    const rect = inputEl.getBoundingClientRect();
    highlightBox(targetDoc, rect.left, rect.top);
  } else {
    console.warn("simulateInput: No element found for selector", selector);
  }
}

async function fetchLog(filename) {
  try {
    const response = await fetch(filename);
    if (!response.ok) throw new Error("Failed to load log: " + filename);
    const json = await response.json();
    json.__filename = filename;
    return json;
  } catch (err) {
    console.error("Error fetching log:", err);
    throw err;
  }
}

function sanitizeDOM(html) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  doc.querySelectorAll('script, meta[http-equiv], link[rel=preload], link[rel=modulepreload], iframe').forEach(el => el.remove());
  return doc.documentElement.outerHTML;
}

async function replayInteractions(log) {
  logDiv.innerText = `Replaying: ${log.title || log.__filename || ''}`;

  // Load DOM snapshot only if provided
  if (log.dom_snapshot_base64) {
    const rawHTML = decodeURIComponent(escape(atob(log.dom_snapshot_base64)));
    const sanitizedHTML = sanitizeDOM(rawHTML);
    const blobURL = URL.createObjectURL(new Blob([sanitizedHTML], { type: 'text/html' }));
    iframe.src = blobURL;
    await new Promise(resolve => { iframe.onload = () => resolve(); });
  }

  const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
  lastFocusedElement = null;
  const interactions = log.interactions || [];
  let startTime = interactions[0]?.timestamp || 0;

  for (const action of interactions) {
    const now = action.timestamp || 0;
    const delay = Math.max(0, now - startTime);
    startTime = now;

    await wait(delay + 400);

    try {
      const t = action.type || action.event; // support both
      switch (t) {
        case "scroll":
          iframe.contentWindow.scrollTo(action.scrollLeft, action.scrollTop);
          break;
        case "mouse_click":
          if (action.selector) {
            const el = iframeDoc.querySelector(action.selector);
            if (el) {
              el.click();
              el.focus();
              const rect = el.getBoundingClientRect();
              highlightBox(iframeDoc, rect.left, rect.top);
              break;
            }
          }
          if (action.x != null && action.y != null) {
            const element = iframeDoc.elementFromPoint(action.x, action.y);
            if (element) {
              element.focus();
            }
            highlightBox(iframeDoc, action.x, action.y);
          }
          break;
        case "key_press":
          if (action.key) simulateKey(iframe.contentWindow, action.key);
          break;
        case "blur":
          if (action.element?.value !== undefined && action.element?.id) {
            simulateInput(iframeDoc, `#${action.element.id}`, action.element.value);
          }
          break;
        case "input":
          if (action.selector && action.element?.value !== undefined) {
            simulateInput(iframeDoc, action.selector, action.element.value);
          }
          break;
        case "focus":
          if (action.selector) {
            const el = iframeDoc.querySelector(action.selector);
            if (el) {
              el.focus();
              lastFocusedElement = el;
            }
          }
          break;
        default:
          console.log("⚠️ Unhandled action type:", t);
      }

      console.log(`Replayed: ${action.type}`, action);
    } catch (e) {
      console.error(`Error replaying ${action.type}:`, e);
    }
  }

  logDiv.innerText += ` ✔`;
}

// async function playNextLog() {
//   if (currentFileIndex >= possibleFiles.length) {
//     logDiv.innerText = "✅ All replays finished.";
//     return;
//   }
//   const filename = possibleFiles[currentFileIndex++];
//   try {
//     const log = await fetchLog(filename);
//     await replayInteractions(log);
//     await wait(1500); // pause between tabs
//     playNextLog();
//   } catch (err) {
//     console.warn("Skipping missing or invalid log:", filename);
//     playNextLog();
//   }
// }

// playNextLog();

async function pollAndReplay() {
  while (true) {
    try {
      const res = await fetch("http://localhost:8090/next");
      const data = await res.json();
      if (data.status === "empty") { await wait(300); continue; }
      console.log("📦 Received from server:", data);

      if (data.status === "empty") {
        await wait(500); // wait and retry
        continue;
      }

      // Reconstruct base64 if snapshot is present
      const interactions = [data];
      const replayData = { interactions: [data] };
      if (data.dom_snapshot_base64) replayData.dom_snapshot_base64 = data.dom_snapshot_base64;

      await replayInteractions(replayData);
      await wait(500); // pause between actions
    } catch (e) {
      console.error("Polling error:", e);
      await wait(1000);
    }
  }
}

pollAndReplay(); // 🔁 start polling

