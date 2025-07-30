// background.js

chrome.runtime.onInstalled.addListener(() => {
  console.log("Extension installed.");
  chrome.tabs.query({}, function(tabs) {
    for (let tab of tabs) {
      if (tab.id && tab.url.startsWith("http")) {
        chrome.scripting.executeScript({
          target: { tabId: tab.id },
          files: ["content.js"]
        });
      }
    }
  });
});

// re-inject content script on tab updates
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url.startsWith("http")) {
    chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"]
    });
  }
});
