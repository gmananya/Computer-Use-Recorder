// background.js

function shouldInject(tab) {
  const url = tab?.url || "";

  // Only inject on real web pages (http/https)
  if (!/^https?:\/\//i.test(url)) return false;

  // Never inject into your local replayer UI
  if (/^http:\/\/(localhost|127\.0\.0\.1):8090/i.test(url)) return false;

  return true;
}

function inject(tabId) {
  chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"]
  }).catch(err => console.warn("[ext] inject failed", tabId, err));
}

chrome.runtime.onInstalled.addListener(() => {
  console.log("Extension installed.");
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs) {
      if (tab.id && shouldInject(tab)) inject(tab.id);
    }
  });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && shouldInject(tab)) {
    inject(tabId);
  }
});

// Proxy fetch to localhost on behalf of content scripts.
// Background service workers use the extension origin (chrome-extension://...)
// and are exempt from Chrome's Private Network Access restrictions.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type !== "log_web") return false;
  fetch("http://localhost:8765/log_web", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(message.data)
  }).then(() => sendResponse({ ok: true }))
    .catch(() => sendResponse({ ok: false }));
  return true; // keep channel open for async response
});
