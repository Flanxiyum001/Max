from __future__ import annotations

import io
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from max_core.voice.qwen_worker import QwenWorker, read_request

_FAKE_CHILD = """
import json
import sys
import time
for record in sys.stdin.buffer:
    text = json.loads(record)
    if text == 'fail':
        print('MAX_FAILED', flush=True)
        break
    if text == 'stall':
        time.sleep(30)
        continue
    print('MAX_AUDIO_READY', flush=True)
    if text == 'long':
        time.sleep(30)
    print('MAX_DONE', flush=True)
"""


class SpeechWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = QwenWorker(timeout=0.8)
        self.addCleanup(self.worker.cancel)
        real = subprocess.Popen
        self.processes: list[subprocess.Popen[bytes]] = []

        def popen(*args, **kwargs):  # type: ignore[no-untyped-def]
            del args
            process = real([sys.executable, "-u", "-c", _FAKE_CHILD], **kwargs)
            self.processes.append(process)
            return process

        stub = patch("max_core.voice.qwen_worker.subprocess.Popen", side_effect=popen)
        self.addCleanup(stub.stop)
        stub.start()

    def wait_for(self, check) -> None:  # type: ignore[no-untyped-def]
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if check():
                return
            time.sleep(0.01)
        self.fail("speech state not reached")

    def test_warm_worker_is_reused(self) -> None:
        for text in ("Hello", "Again"):
            self.assertTrue(self.worker.start(text))
            self.wait_for(lambda: self.worker.status() == "done")
            self.assertTrue(self.worker.audio_ready)
        self.assertEqual(len(self.processes), 1)

    def test_cancel_then_recover(self) -> None:
        self.assertTrue(self.worker.start("long"))

        def audio_started() -> bool:
            return self.worker.status() == "busy" and self.worker.audio_ready

        self.wait_for(audio_started)
        self.worker.cancel()
        self.assertEqual(self.worker.status(), "idle")
        self.wait_for(lambda: self.processes[0].poll() is not None)
        self.assertTrue(self.worker.start("Hello again"))
        self.wait_for(lambda: self.worker.status() == "done")
        self.assertNotEqual(self.processes[0].pid, self.processes[1].pid)

    def test_failure_before_playback_and_stall(self) -> None:
        for text in ("fail", "stall"):
            self.assertTrue(self.worker.start(text))
            self.wait_for(lambda: self.worker.status() == "failed")
            self.assertFalse(self.worker.audio_ready)

    def test_rejects_unbounded_or_invalid_record(self) -> None:
        self.assertEqual(read_request(io.BytesIO(b'"first\\nsecond"\n')), "first\nsecond")
        for record in (b"[]" + b"\n", b'"' + b"a" * 1501 + b'"\n', b'"partial"'):
            with self.assertRaises(ValueError):
                read_request(io.BytesIO(record))
