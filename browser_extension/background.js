async function enableActionSidePanel() {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

chrome.runtime.onInstalled.addListener(() => {
  enableActionSidePanel().catch((error) => console.error(error));
});

chrome.runtime.onStartup.addListener(() => {
  enableActionSidePanel().catch((error) => console.error(error));
});

enableActionSidePanel().catch((error) => console.error(error));
