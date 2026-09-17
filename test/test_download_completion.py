"""Download completion stays a task-step result when a click outlives its timer."""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_download_completion_and_timeout_are_handled_before_click_returns():
    module = pathlib.Path(__file__).resolve().parents[1] / "runner/scripts/lib/download_completion.js"
    script = r"""
        const assert = require('node:assert/strict');
        const {EventEmitter} = require('node:events');
        const {waitForDownloadProgress} = require(process.argv[1]);

        async function main() {
          const success = new EventEmitter();
          const guid = await waitForDownloadProgress(success, async () => {
            setTimeout(() => success.emit('Browser.downloadProgress', {
              state: 'completed', guid: 'saved-file'
            }), 5);
          }, 50);
          assert.equal(guid, 'saved-file');
          assert.equal(success.listenerCount('Browser.downloadProgress'), 0);

          const lateClick = new EventEmitter();
          await assert.rejects(
            waitForDownloadProgress(lateClick, async () => {
              await new Promise(resolve => setTimeout(resolve, 40));
            }, 5),
            /download did not complete before the timeout/
          );
          assert.equal(lateClick.listenerCount('Browser.downloadProgress'), 0);

          const canceled = new EventEmitter();
          await assert.rejects(
            waitForDownloadProgress(canceled, async () => {
              canceled.emit('Browser.downloadProgress', {state: 'canceled'});
            }, 50),
            /download was canceled/
          );
          assert.equal(canceled.listenerCount('Browser.downloadProgress'), 0);
        }
        main().catch(error => { console.error(error); process.exitCode = 1; });
    """
    result = subprocess.run(
        ["node", "--unhandled-rejections=strict", "-e", script, str(module)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
