// replay/replay_web.js
const iframe = document.getElementById("replayFrame");
const logDiv  = document.getElementById("log");

let omniboxBuffer = "";
let isOnNewTab = false;
let lastLoadedDomURL = null;
let lastLoadedDomBase64 = null;
let currentReplayId = 0;

const wait = (ms) => new Promise(r => setTimeout(r, ms));

async function queryWithRetry(doc, selector, timeout = 1500, interval = 100) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const el = doc.querySelector(selector);
      if (el) return el;
    } catch {}
    await wait(interval);
  }
  return null;
}

function highlightBox(targetDoc, x, y) {
  const highlight = targetDoc.createElement("div");
  highlight.style.position = "absolute";
  highlight.style.top = `${y - 20}px`;
  highlight.style.left = `${x - 20}px`;
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

function sanitizeDOM(html) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, "text/html");
  doc.querySelectorAll("script, meta[http-equiv], link[rel=preload], link[rel=modulepreload], iframe")
    .forEach(el => el.remove());
  for (const el of doc.querySelectorAll("*")) {
    for (const { name } of Array.from(el.attributes)) {
      if (name.toLowerCase().startsWith("on")) el.removeAttribute(name);
    }
  }
  const neuter = (el, attr) => {
    const v = el.getAttribute(attr);
    if (!v) return;
    if (v.startsWith("/")) {
      if (attr === "src" && el.tagName === "IMG") {
        el.setAttribute("src","data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==");
      } else {
        el.setAttribute(attr, "about:blank#blocked");
      }
    }
  };
  doc.querySelectorAll("img[src], link[href], a[href], form[action], source[srcset]").forEach(el => {
    if (el.hasAttribute("src"))    neuter(el, "src");
    if (el.hasAttribute("href"))   neuter(el, "href");
    if (el.hasAttribute("action")) neuter(el, "action");
    if (el.hasAttribute("srcset")) el.setAttribute("srcset", "");
  });
  return doc.documentElement.outerHTML;
}

function ensureGhost(win) {
  try {
    if (!win || win.__ghostInstalled) return;
    win.__ghostInstalled = true;
    const d = win.document;
    const cur = d.createElement("div");
    cur.id = "replay-ghost-cursor";
    Object.assign(cur.style, {
      position: "fixed", width: "14px", height: "14px", borderRadius: "50%",
      border: "2px solid rgba(0,0,0,.7)", background: "rgba(255,255,255,.85)",
      boxShadow: "0 0 6px rgba(0,0,0,.25)",
      pointerEvents: "none", zIndex: 2147483647,
      left: `${ghostX}px`,
      top: `${ghostY}px`,
      transform: "translate(-7px,-7px)", transition: "left 120ms ease, top 120ms ease"
    });
    d.documentElement.appendChild(cur);
    win.__ghostMove = (x, y) => { 
      ghostX = x; ghostY = y;
      cur.style.left = `${x}px`; cur.style.top = `${y}px`; 
    };
    win.__ghostClick = () => {
      const ring = d.createElement("div");
      Object.assign(ring.style, {
        position: "fixed", width: "24px", height: "24px", borderRadius: "50%",
        border: "2px solid rgba(0,0,0,.45)", pointerEvents: "none",
        zIndex: 2147483647, left: cur.style.left, top: cur.style.top,
        transform: "translate(-12px,-12px)", opacity: 1, transition: "opacity 400ms ease, transform 400ms ease"
      });
      d.documentElement.appendChild(ring);
      requestAnimationFrame(() => {
        ring.style.opacity = 0;
        ring.style.transform = "translate(-12px,-12px) scale(1.7)";
      });
      setTimeout(() => ring.remove(), 420);
    };
  } catch {}
}


function ghostToElement(win, el) {
  try {
    const r = el.getBoundingClientRect();
    // Compute midpoint of the element
    ghostX = Math.max(3, Math.min(win.innerWidth - 3, r.left + r.width / 2));
    ghostY = Math.max(3, Math.min(win.innerHeight - 3, r.top  + r.height / 2));
    win.__ghostMove?.(ghostX, ghostY);
  } catch {}
}


function installGuards(win) {
  try {
    if (!win || win.__guardsInstalled) return;
    win.__guardsInstalled = true;
    const doc = win.document;
    try { win.open = function(){ console.debug("[WEB] window.open blocked"); return null; }; } catch {}
    let base = doc.querySelector("base");
    if (!base) { base = doc.createElement("base"); doc.head?.appendChild(base); }
    base.setAttribute("target", "_self");
    doc.addEventListener("click", (e) => {
      const a = e.target.closest?.("a[href]");
      if (!a) return;
      if (win.__allowNextAnchorNav) {
        delete win.__allowNextAnchorNav;
        return;
      }
      e.preventDefault(); e.stopPropagation();
      console.debug("[WEB] blocked anchor nav:", a.getAttribute("href"));
    }, true);
    doc.addEventListener("submit", (e) => {
      e.preventDefault(); e.stopPropagation();
      console.debug("[WEB] blocked form submit");
    }, true);
    ensureGhost(win);
  } catch (e) {
    console.warn("[WEB] installGuards error:", e);
  }
}

// === DOM/Page Loader ===
async function loadDOM(dom_snapshot_base64, dom_url, navLabel) {
  return new Promise((resolve) => {
    iframe.onload = () => {
      installGuards(iframe.contentWindow);
      resolve();
    };
    if (dom_snapshot_base64 && dom_snapshot_base64 !== lastLoadedDomBase64) {
      console.log("[REPLAY] Loading base64 snapshot", navLabel||"");
      lastLoadedDomBase64 = dom_snapshot_base64;
      lastLoadedDomURL = null;
      const raw = decodeURIComponent(escape(atob(dom_snapshot_base64)));
      const sanitized = sanitizeDOM(raw);
      const blobURL = URL.createObjectURL(new Blob([sanitized], { type: "text/html" }));
      iframe.src = blobURL;
    } else if (dom_url && dom_url !== lastLoadedDomURL) {
      console.log("[REPLAY] Loading dom_url", dom_url, navLabel||"");
      lastLoadedDomURL = dom_url;
      lastLoadedDomBase64 = null;
      iframe.src = dom_url;
    } else if (!dom_snapshot_base64 && !dom_url) {
      console.log("[REPLAY] Loading newtab.html (fallback)", navLabel||"");
      lastLoadedDomBase64 = null;
      lastLoadedDomURL = null;
      iframe.src = "newtab.html";
    } else {
      // Already loaded, just resolve.
      resolve();
    }
  });
}

// === Omnibox Helper ===
function setOmnibox(val) {
  try {
    const w = iframe.contentWindow;
    const d = iframe.contentDocument || w?.document;
    const set = w?.setOmnibox;
    if (typeof set === "function") { set(val); return; }
    const inp = d?.getElementById("omnibox") || d?.querySelector("input");
    if (inp) {
      inp.value = val;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    }
  } catch {}
}

// === Main Replay Function ===
async function replayInteractions(log, replayId) {
  logDiv.innerText = `Replaying: ${log.title || log.__filename || ""}`;
  const actions = log.interactions || [];
  let t0 = actions[0]?.timestamp || 0;
  let isFirstNewTab = false;

  for (const act of actions) {
    if (replayId !== currentReplayId) return; // bail if another replay started

    const now = act.timestamp || 0;
    const delay = Math.max(0, now - t0);
    t0 = now;
    await wait(delay + 100);

    const type = (act.type || act.event || "").toLowerCase();
    console.log(`[REPLAY] Event:`, act);

    // === DOM/PAGE SNAPSHOT NAVIGATION ===
    let pageNav = false;
    if (type === "newtab_boot") {
      await loadDOM(null, null, "newtab_boot");
      isOnNewTab = true;
      isFirstNewTab = true;
      pageNav = true;
    } else if (act.dom_snapshot_base64 || act.dom_url || act.next_dom_snapshot_base64 || act.next_dom_url) {
      let toLoadBase64 = act.dom_snapshot_base64 || act.next_dom_snapshot_base64;
      let toLoadUrl = act.dom_url || act.next_dom_url;
      await loadDOM(toLoadBase64, toLoadUrl, type);
      isOnNewTab = false;
      pageNav = true;
    }

    // Always update reference to iframe context after possible nav
    const w = iframe.contentWindow;
    const d = iframe.contentDocument || w?.document;

    // === NEWTAB TYPING ===
    if (isOnNewTab && type === "key_press") {
      const raw = act.key || "";
      const k = raw.toLowerCase();
      if (k.includes("backspace")) omniboxBuffer = omniboxBuffer.slice(0, -1);
      else if (k === " " || k.includes("space")) omniboxBuffer += " ";
      else if (k.includes("enter") || act.enter) {
        // Loading handled above, reset buffer
        isOnNewTab = false;
      } else if (raw.length === 1) {
        omniboxBuffer += raw;
      }
      setOmnibox(omniboxBuffer);
      continue;
    }

    try {
      switch (type) {
        case "scroll":
          w?.scrollTo(act.scrollLeft || 0, act.scrollTop || 0);
          break;

        case "mouse_click": {
          let el = null;
          if (act.selector) {
            el = await queryWithRetry(d, act.selector);
          }
          if (!el) {
            const textHint = (act.element?.text || act.title || "").trim();
            if (textHint) {
              const candidates = Array.from(d.querySelectorAll("a, button, [role='button'], [role='link']"));
              el = candidates.find(c => c.textContent && c.textContent.trim().toLowerCase().includes(textHint.toLowerCase()));
            }
          }
          if (!el && act.x != null && act.y != null) {
            el = d.elementFromPoint(act.x, act.y);
            if (el) {
              el.focus?.();
              el.click?.();
              highlightBox(d, act.x, act.y);
              break;
            }
          }
          if (el) {
            ghostToElement(w, el);
            w.__ghostClick?.();
            highlightBox(d, el.getBoundingClientRect().left + el.getBoundingClientRect().width / 2, el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2);
            if (el.tagName === "A" && el.getAttribute("href")) {
              w.__allowNextAnchorNav = true;
              try { el.click(); } catch {}
              await wait(200);
              const href = el.getAttribute("href");
              if (href && w.location.href === window.location.href) {
                // still same → force navigation
              }
            } else {
              try { el.focus?.(); } catch {}
              try { el.click?.(); } catch {}
            }
          }
          break;
        }

        case "input":
          if (act.selector && act.element?.value !== undefined) {
            const elInput = await queryWithRetry(d, act.selector);
            if (elInput) {
              const proto = elInput.tagName === "TEXTAREA"
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
              if (setter) setter.call(elInput, act.element.value);
              elInput.dispatchEvent(new Event("input", { bubbles: true }));
              elInput.dispatchEvent(new Event("change", { bubbles: true }));
            }
          }
          break;

        case "focus":
        case "blur":
        case "key_press":
        case "page":
        default:
          // Unhandled or handled elsewhere
          break;
      }
    } catch (e) {
      console.error(`Error replaying ${type}:`, e);
    }
  }
  logDiv.innerText += " ✔";
}

// === Poller ===
async function pollAndReplay() {
  while (true) {
    try {
      const res = await fetch("/next");
      if (!res.ok) { await wait(300); continue; }
      const data = await res.json();
      if (data.status === "empty" || data.status === "not_owner") { await wait(200); continue; }
      currentReplayId++;
      const replayData = { interactions: [data], title: data.window_title || data.title || "" };
      if (data.dom_snapshot_base64) replayData.dom_snapshot_base64 = data.dom_snapshot_base64;
      if (data.dom_url)             replayData.dom_url             = data.dom_url;
      await replayInteractions(replayData, currentReplayId);
      await wait(160);
    } catch (e) {
      console.error("Polling error:", e);
      await wait(700);
    }
  }
}

pollAndReplay();
