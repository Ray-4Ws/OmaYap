"""Exercise the QML state reducer with real worker-shaped events."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is needed for the QML JavaScript reducer test")
class AgentIndicatorTests(unittest.TestCase):
    def test_indicator_follows_audio_source_and_cancellation(self):
        qml = (ROOT / "Service.qml").read_text()
        match = re.search(r"  function handleWorkerLine\(raw\) \{.*?^  \}", qml, re.M | re.S)
        self.assertIsNotNone(match)
        script = """
const assert = require('node:assert/strict');
const root = {
  workerOutputSuppressed: false, expectedWorkerExit: false,
  stopPending: false, agentAudioStarted: false,
  validWorkerEvent: () => true, clampSpeed: Number,
  armIdleEviction: () => {}, notify: () => {}, setupHint: () => {}
};
const workerIdleTimer = {stop: () => {}};
""" + match.group(0) + """
function event(status, source, audioStarted) {
  handleWorkerLine(JSON.stringify({event:'state', status, source, audioStarted,
    speed:1, characters:14}));
}
event('loading', 'agent'); assert.equal(root.agentAudioStarted, false);
event('speaking', 'agent'); assert.equal(root.agentAudioStarted, false);
event('speaking', 'agent', true); assert.equal(root.agentAudioStarted, true);
event('speaking', 'agent'); assert.equal(root.agentAudioStarted, true);
event('idle', 'agent'); assert.equal(root.agentAudioStarted, false);
event('speaking', 'selection', true); assert.equal(root.agentAudioStarted, false);
event('speaking', 'agent', true);
event('error', 'agent'); assert.equal(root.agentAudioStarted, false);
root.stopPending = true; root.agentAudioStarted = false;
event('speaking', 'agent', true); assert.equal(root.agentAudioStarted, false);
assert.equal(root.status, 'stopping');
event('idle', null); assert.equal(root.stopPending, false);
root.workerOutputSuppressed = true;
event('speaking', 'agent', true); assert.equal(root.agentAudioStarted, false);
"""
        result = subprocess.run(["node"], input=script, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
