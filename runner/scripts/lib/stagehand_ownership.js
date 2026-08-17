"use strict";

// The Kitesurf driver probe opens a fresh, connection-local remote namespace
// for every attempt, so pages first exposed by Stagehand.init belong to that
// attempt. Formal local engines instead reuse a runner-owned browser process
// with pre-existing pages; those pages are borrowed and must never be closed
// by the adapter. Explicit context.newPage() calls are tracked separately.
function selectStagehandInitOwnedPages(contextPages, remoteCdp) {
  const visible = Array.isArray(contextPages) ? contextPages : [];
  return remoteCdp === true ? [...visible] : [];
}

module.exports = { selectStagehandInitOwnedPages };
