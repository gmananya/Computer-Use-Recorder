let currentURL = window.location.href;
let accessibilityTreeSent = false;
window.currentURL = currentURL;
let accessibilityTreeMap = {};

function getElementInfo(element) {
  if (!element) return null;
  return {
    tag: element.tagName,
    id: element.id,
    class: element.className,
    role: element.getAttribute("role"),
    ariaLabel: element.getAttribute("aria-label"),
    name: element.getAttribute("name"),
    value: element.value,
    text: element.innerText?.slice(0, 100),
  };
}

function getSelectorFromElement(element) {
  if (element.id) return `#${element.id}`;
  if (element.name) return `[name='${element.name}']`;
  if (element.className) return `.${element.className.split(' ').join('.')}`;
  return null;
}


function getAccessibilityTree(element) {
  const elementInfo = getElementInfo(element);
  const children = element.children || [];
  const treeNode = {
    ...elementInfo,
    children: [],
  };
  for (let child of children) {
    treeNode.children.push(getAccessibilityTree(child));
  }
  return treeNode;
}

function getCompressedDOM() {
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll("script").forEach(el => el.remove()); // Only remove scripts
  clone.querySelectorAll("link[rel='preload'], meta[http-equiv], iframe").forEach(el => el.remove()); // Avoid preload/meta that can crash the replay

  // Keep <style> and <link rel="stylesheet"> for CSS
  return btoa(unescape(encodeURIComponent(clone.outerHTML)));
}


function sendToLogger(interaction) {
  const now = Date.now();
  const url = window.location.href;
  const title = document.title;

  const data = {
    timestamp: now,
    url,
    title,
    interactions: [interaction],
  };

  if (!accessibilityTreeSent) {
    data.accessibility_tree = getAccessibilityTree(document.body);
    data.dom_snapshot_base64 = getCompressedDOM();
    accessibilityTreeSent = true;
  }

  fetch("http://localhost:8765/log_web", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }).catch((e) => console.error("Failed to send log:", e));
}

function handleUrlChange() {
  if (window.location.href !== window.currentURL) {
    window.currentURL = window.location.href;
    accessibilityTreeSent = false;
  }
}
window.addEventListener("popstate", handleUrlChange);
window.addEventListener("hashchange", handleUrlChange);

document.addEventListener("click", (event) => {
  sendToLogger({
    timestamp: Date.now(),
    type: "mouse_click",
    x: event.clientX,
    y: event.clientY,
    element: getElementInfo(event.target),
    selector: getSelectorFromElement(event.target)
  });
});

window.addEventListener("keydown", (event) => {
  sendToLogger({
    timestamp: Date.now(),
    type: "key_press",
    key: event.key
  });
}, true);

window.addEventListener("scroll", () => {
  sendToLogger({
    type: "scroll",
    timestamp: Date.now(),
    scrollTop: document.documentElement.scrollTop,
    scrollLeft: document.documentElement.scrollLeft,
    windowHeight: window.innerHeight,
    documentHeight: document.documentElement.scrollHeight,
  });
});

document.addEventListener("focus", (event) => {
  sendToLogger({
    type: "focus",
    timestamp: Date.now(),
    selector: getSelectorFromElement(event.target),
    element: getElementInfo(event.target),
  });
}, true);

document.addEventListener("blur", (event) => {
  sendToLogger({
    type: "blur",
    selector: getSelectorFromElement(event.target),
    element: getElementInfo(event.target),
  });
}, true);
