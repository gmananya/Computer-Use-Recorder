// content.js
(() => {
  // ---- one-time install guard (prevents double listeners / var re-declare) ----
  if (window.__CU_INSTALLED__) {
    console.debug("[cu] content script already installed");
    return;
  }
  window.__CU_INSTALLED__ = true;

  // ---- do not run in the replay UI or on non-http(s) pages ----
  const here = location.href;
  const isHttp = /^https?:\/\//i.test(here);
  const isReplayUI = /^https?:\/\/(localhost|127\.0\.0\.1):8090/i.test(location.origin);
  if (!isHttp || isReplayUI) {
    console.debug("[cu] skipping on this page:", here);
    return;
  }

  // ---- config / state ----
  const LOG_URL = "http://localhost:8765/log_web"; // unchanged
  let accessibilityTreeSent = false;
  let lastURL = location.href;
  let lastScrollPayload = null;
  let scrollTimer = null;

  // stable tab session id
  const TAB_SESSION_ID =
    (globalThis.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : String(Date.now() + Math.random());

  // ---- helpers ----
  function wait(ms) { return new Promise(r => setTimeout(r, ms)); }

  function getElementInfo(el) {
    if (!el) return null;
    return {
      tag: el.tagName,
      id: el.id || "",
      class: (el.className && typeof el.className === "string") ? el.className : "",
      role: el.getAttribute && el.getAttribute("role"),
      ariaLabel: el.getAttribute && el.getAttribute("aria-label"),
      name: el.getAttribute && el.getAttribute("name"),
      href: el.getAttribute && el.getAttribute("href"),
      value: ("value" in el) ? el.value : undefined,
      text: (el.innerText || "").slice(0, 200)
    };
  }

  // Build a robust selector
  function generateSelector(el) {
    if (!el || el.nodeType !== 1) return null;
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.getAttribute && el.getAttribute("name")) {
      return `[name="${CSS.escape(el.getAttribute("name"))}"]`;
    }
    if (el.classList && el.classList.length) {
      return el.tagName.toLowerCase() + "." + Array.from(el.classList).map(c => CSS.escape(c)).join(".");
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) break;
      const siblings = Array.from(parent.children).filter(n => n.tagName === node.tagName);
      const idx = siblings.indexOf(node) + 1;
      parts.unshift(`${tag}:nth-of-type(${idx})`);
      if (parent.id) { parts.unshift(`#${CSS.escape(parent.id)}`); break; }
      node = parent;
      if (parts.length > 6) break;
    }
    return parts.length ? parts.join(" > ") : null;
  }

  function getAccessibilityTree(element) {
    if (!element) return null;
    const info = getElementInfo(element);
    const node = { ...info, children: [] };
    const kids = element.children || [];
    for (const child of kids) node.children.push(getAccessibilityTree(child));
    return node;
  }

  function getCompressedDOM() {
    const clone = document.documentElement.cloneNode(true);
    // Strip risky/irrelevant elements; keep CSS so the snapshot looks right
    clone.querySelectorAll(
      "script, iframe, link[rel='preload'], link[rel='modulepreload'], meta[http-equiv]"
    ).forEach(el => el.remove());
    const html = clone.outerHTML;
    return btoa(unescape(encodeURIComponent(html)));
  }

  function basePayload() {
    return {
      timestamp: Date.now(),
      url: location.href,
      title: document.title,
      tab_session_id: TAB_SESSION_ID
    };
  }

  function sendToLogger(interaction) {
    const data = { ...basePayload(), interactions: [interaction] };

    // Include DOM snapshot only once per URL
    if (!accessibilityTreeSent) {
      try { data.accessibility_tree = getAccessibilityTree(document.body); } catch {}
      try { data.dom_snapshot_base64 = getCompressedDOM(); } catch {}
      accessibilityTreeSent = true;
    }

    // Be quiet if the logger isn't running
    try {
      fetch(LOG_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
        // Short timeout via AbortController to reduce console noise on refused
        signal: (() => {
          const ctl = new AbortController();
          setTimeout(() => ctl.abort(), 800);
          return ctl.signal;
        })()
      }).catch(() => {/* swallow errors to avoid console spam */});
    } catch {/* swallow */}
  }

  // ---- URL change handling ----
  async function onUrlChanged() {
    if (location.href === lastURL) return;
    lastURL = location.href;
    accessibilityTreeSent = false;
    sendToLogger({ type: "page", timestamp: Date.now() });
  }

  window.addEventListener("popstate", onUrlChanged, true);
  window.addEventListener("hashchange", onUrlChanged, true);

  // Wrap history API
  (function wrapHistory() {
    const _push = history.pushState;
    const _replace = history.replaceState;
    history.pushState = function (...args) { const r = _push.apply(this, args); onUrlChanged(); return r; };
    history.replaceState = function (...args) { const r = _replace.apply(this, args); onUrlChanged(); return r; };
  })();

  // ---- event capture ----
  document.addEventListener("click", (event) => {
    const el = event.target;
    sendToLogger({
      type: "mouse_click",
      timestamp: Date.now(),
      x: event.clientX,
      y: event.clientY,
      selector: generateSelector(el),
      element: getElementInfo(el)
    });
  }, true);

  window.addEventListener("keydown", (event) => {
    if (["Shift", "Alt", "Control", "Meta"].includes(event.key)) return;
    const active = document.activeElement;
    sendToLogger({
      type: "key_press",
      timestamp: Date.now(),
      key: event.key,
      selector: generateSelector(active),
      element: getElementInfo(active)
    });
  }, true);

  document.addEventListener("input", (event) => {
    const el = event.target;
    if (!el || !("value" in el)) return;
    sendToLogger({
      type: "input",
      timestamp: Date.now(),
      selector: generateSelector(el),
      element: getElementInfo(el)
    });
  }, true);

  document.addEventListener("focus", (event) => {
    const el = event.target;
    sendToLogger({
      type: "focus",
      timestamp: Date.now(),
      selector: generateSelector(el),
      element: getElementInfo(el)
    });
  }, true);

  document.addEventListener("blur", (event) => {
    const el = event.target;
    sendToLogger({
      type: "blur",
      timestamp: Date.now(),
      selector: generateSelector(el),
      element: getElementInfo(el)
    });
  }, true);

  window.addEventListener("scroll", () => {
    const payload = {
      type: "scroll",
      timestamp: Date.now(),
      scrollTop: document.documentElement.scrollTop || document.body.scrollTop || 0,
      scrollLeft: document.documentElement.scrollLeft || document.body.scrollLeft || 0,
      windowHeight: window.innerHeight,
      documentHeight: document.documentElement.scrollHeight
    };
    lastScrollPayload = payload;
    if (scrollTimer) return;
    scrollTimer = setTimeout(() => {
      scrollTimer = null;
      if (lastScrollPayload) { sendToLogger(lastScrollPayload); lastScrollPayload = null; }
    }, 150);
  }, { passive: true });

  // initial page marker
  sendToLogger({ type: "page", timestamp: Date.now() });
})();
