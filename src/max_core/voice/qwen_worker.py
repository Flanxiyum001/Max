"""A single bounded, cancellable subprocess for local desktop speech."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import BinaryIO

_MAX_TEXT = 1500
_MAX_LINE = 12_100


def read_request(stream: BinaryIO) -> str | None:
    # The parent always writes complete UTF-8 JSON records, with no raw newlines.
    raw = stream.readline(_MAX_LINE)
    if not raw:
        return None
    if len(raw) >= _MAX_LINE or not raw.endswith(b"\n"):
        raise ValueError("invalid_speech_request")
    text = json.loads(raw)
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT:
        raise ValueError("invalid_speech_request")
    return text


def _read_status(process: subprocess.Popen[bytes], events: queue.SimpleQueue[str]) -> None:
    try:
        if process.stdout is not None:
            while line := process.stdout.readline(256):
                if line in (b"MAX_AUDIO_READY\n", b"MAX_DONE\n", b"MAX_FAILED\n"):
                    events.put(line.decode("ascii").strip())
    finally:
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        events.put("MAX_EXIT")


def _write_request(
    process: subprocess.Popen[bytes], record: bytes, events: queue.SimpleQueue[str]
) -> None:
    try:
        stream = process.stdin
        if stream is None:
            raise OSError("missing_speech_pipe")
        while record:
            written = stream.write(record)
            if not written:
                raise OSError("closed_speech_pipe")
            record = record[written:]
    except (OSError, ValueError):
        events.put("MAX_FAILED")


class QwenWorker:
    def __init__(self, *, timeout: float = 90.0) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._events: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._timeout = timeout
        self._deadline: float | None = None
        self.audio_ready = False

    def _start(self) -> subprocess.Popen[bytes]:
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        flags = 0x00004000 if os.name == "nt" and env.get("MAX_LOCAL_GAMING") == "yes" else 0
        process = subprocess.Popen(
            [sys.executable, "-m", "max_core.voice.local_runtime", "worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=flags,
        )
        self._events = queue.SimpleQueue()
        self._process = process
        threading.Thread(target=_read_status, args=(process, self._events), daemon=True).start()
        return process

    def start(self, text: str) -> bool:
        text = text.strip()[:_MAX_TEXT]
        if not text:
            return False
        if self._deadline is not None:
            self.cancel()
        process = self._process
        try:
            if process is None or process.poll() is not None:
                process = self._start()
            if process.stdin is None:
                self.cancel()
                return False
            record = json.dumps(text, ensure_ascii=False).encode("utf-8") + b"\n"
        except (OSError, UnicodeError):
            self.cancel()
            return False
        self.audio_ready = False
        self._deadline = time.monotonic() + self._timeout
        threading.Thread(
            target=_write_request, args=(process, record, self._events), daemon=True
        ).start()
        return True

    def status(self) -> str:
        deadline = self._deadline
        if deadline is None:
            return "idle"
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event == "MAX_AUDIO_READY":
                self.audio_ready = True
                deadline = time.monotonic() + self._timeout
                self._deadline = deadline
            elif event == "MAX_DONE":
                self._deadline = None
                return "done"
            else:
                self.cancel()
                return "failed"
        if time.monotonic() >= deadline:
            self.cancel()
            return "failed"
        return "busy"

    def cancel(self) -> None:
        process, self._process = self._process, None
        self._deadline = None
        if process is None:
            return
        # The reader thread reaps the process; never wait for inference in the UI.
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
