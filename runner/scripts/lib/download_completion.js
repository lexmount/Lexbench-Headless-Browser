"use strict";

async function waitForDownloadProgress(cdp, click, timeoutMs) {
  let timer;
  let onProgress;
  const completed = new Promise((resolve, reject) => {
    timer = setTimeout(
      () => reject(new Error("download did not complete before the timeout")),
      timeoutMs
    );
    onProgress = (event) => {
      if (event.state === "completed") {
        clearTimeout(timer);
        resolve(event.guid);
      } else if (event.state === "canceled") {
        clearTimeout(timer);
        reject(new Error("download was canceled"));
      }
    };
    cdp.on("Browser.downloadProgress", onProgress);
  });
  // Attach a rejection handler before the click: some framework clicks wait
  // longer than the download timer, and a bare Promise would crash Node.
  const observed = completed.then(
    (guid) => ({ guid }),
    (error) => ({ error })
  );
  try {
    await click();
    const outcome = await observed;
    if (outcome.error) throw outcome.error;
    return outcome.guid;
  } finally {
    clearTimeout(timer);
    cdp.off("Browser.downloadProgress", onProgress);
  }
}

module.exports = { waitForDownloadProgress };
