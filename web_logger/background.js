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
