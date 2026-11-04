// Only registered on the platform origin the user explicitly paired with.
// The page can wake the collector; it cannot read its token or execute commands.
if (!globalThis.sciencemateCnkiWakeInstalled) {
  globalThis.sciencemateCnkiWakeInstalled = true;
  window.addEventListener("message", event => {
    if (event.source === window && event.origin === location.origin && event.data?.type === "sciencemate-cnki-wake") {
      chrome.runtime.sendMessage({type: "wake"}).catch(() => {});
    }
  });
}
