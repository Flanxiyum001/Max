"""MAX Desktop — animated dark orb, desktop actions, chat and push-to-talk.

Tkinter is imported only when the local UI is launched, never on Railway.
The canvas animation runs in the UI thread; provider/voice work is off-thread.
No always-on microphone or desktop input hooks.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

from .control import PCController, greeting
from .voice import mic_wav, say, transcribe

BG = "#080D18"
PANEL = "#111B2C"
EDGE = "#24354A"
INK = "#EBF7FF"
MUTED = "#8AA2B8"
CYAN = "#44E1FF"
VIOLET = "#9B8CFF"


class MaxDesktop:
    def __init__(self, root, controller: PCController):
        import tkinter as tk
        from tkinter import scrolledtext

        self.root = root
        self.controller = controller
        self.tk = tk
        self.working = False
        self.recording = False
        self.speaking = False
        self.voice_enabled = tk.BooleanVar(value=True)
        self.phase = 0.0
        root.title("MAX | PERSONAL AI")
        root.geometry("850x760")
        root.minsize(620, 650)
        root.configure(bg=BG)

        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=28, pady=(20, 0))
        tk.Label(header, text="M A X", bg=BG, fg=INK, font=("Segoe UI", 21, "bold")).pack(side="left")
        tk.Label(header, text="  /  WINDOWS ASSISTANT", bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(side="left", pady=8)
        self.status = tk.Label(header, text="● STANDBY", bg=BG, fg=MUTED, font=("Segoe UI", 10, "bold"))
        self.status.pack(side="right")

        visual = tk.Frame(root, bg=BG)
        visual.pack(fill="x", pady=(10, 0))
        self.canvas = tk.Canvas(visual, width=550, height=320, bg=BG, bd=0, highlightthickness=0)
        self.canvas.pack(anchor="center")
        self.canvas.create_text(275, 292, text="INTELLIGENCE  /  UNDER YOUR CONTROL",
                                fill=MUTED, font=("Consolas", 10), tags="subtitle")
        self.canvas.bind("<Button-1>", lambda _event: self._quick_wake())

        body = tk.Frame(root, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        body.pack(fill="both", expand=True, padx=28, pady=(7, 14))
        self.chat = scrolledtext.ScrolledText(
            body, wrap="word", bg=PANEL, fg=INK, insertbackground=INK,
            font=("Segoe UI", 11), relief="flat", padx=14, pady=14,
            state="disabled", selectbackground="#294F6C",
        )
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("you", foreground=CYAN, font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("max", foreground=VIOLET, font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("hint", foreground=MUTED)

        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill="x", padx=28, pady=(0, 18))
        self.entry = tk.Entry(
            bottom, bg=PANEL, fg=INK, insertbackground=INK, bd=0, relief="flat",
            highlightbackground=EDGE, highlightcolor=CYAN, highlightthickness=1,
            font=("Segoe UI", 12),
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=13)
        self.entry.bind("<Return>", self._send)
        tk.Button(bottom, text="SEND", command=self._send, bg="#133F57", fg=INK,
                  activebackground="#1A5B76", relief="flat", padx=15, pady=11).pack(side="left", padx=(8, 4))
        self.mic_button = tk.Button(
            bottom, text="🎙  TALK", command=self._record, bg="#263052", fg=INK,
            activebackground="#343E67", relief="flat", padx=12, pady=11,
        )
        self.mic_button.pack(side="left", padx=4)
        tk.Checkbutton(bottom, text="VOICE", variable=self.voice_enabled,
                       selectcolor=PANEL, bg=BG, fg=MUTED, activebackground=BG,
                       activeforeground=INK).pack(side="left", padx=4)
        self._write("MAX", greeting(), "max")
        self._write("SYSTEM", "Type 'hi max' to activate. Click TALK for push-to-talk (optional).", "hint")
        self.entry.focus_set()
        self._animate()

    def _write(self, speaker, text, tag):
        self.chat.configure(state="normal")
        self.chat.insert("end", speaker + "   ", tag)
        self.chat.insert("end", str(text) + "\n\n")
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def _quick_wake(self):
        if not self.controller.awake:
            self._dispatch("hi max")
        else:
            self.entry.focus_set()

    def _send(self, _event=None):
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self._dispatch(text)

    def _dispatch(self, text):
        if self.working:
            self._write("SYSTEM", "MAX is busy with the previous request.", "hint")
            return
        self._write("YOU", text, "you")
        self.working = True
        self._status()
        def task():
            try:
                response = self.controller.handle(text)
                result = response.text
            except Exception as exc:
                result = f"MAX couldn't finish that request ({type(exc).__name__})."
            self.root.after(0, lambda: self._finish(result))
        threading.Thread(target=task, daemon=True).start()

    def _finish(self, result):
        self.working = False
        self._write("MAX", result, "max")
        self._status()
        if self.voice_enabled.get():
            def speak_task():
                self.speaking = True
                try:
                    say(result)
                except Exception:
                    pass
                finally:
                    self.speaking = False
            threading.Thread(target=speak_task, daemon=True).start()

    def _record(self):
        if self.working or self.recording:
            return
        self.recording = True
        self.mic_button.configure(text="● LISTENING")
        self._status()
        def task():
            try:
                text = transcribe(mic_wav(seconds=4.5))
                outcome = ("ok", text) if text else ("error", "I didn't hear any speech.")
            except Exception as exc:
                outcome = ("error", f"Microphone or transcription unavailable: {type(exc).__name__}. "
                           "Install the pc and stt extras and configure a local Whisper model.")
            self.root.after(0, lambda: self._record_finished(outcome))
        threading.Thread(target=task, daemon=True).start()

    def _record_finished(self, outcome):
        self.recording = False
        self.mic_button.configure(text="🎙  TALK")
        self._status()
        if outcome[0] == "ok":
            self._dispatch(outcome[1])
        else:
            self._write("SYSTEM", outcome[1], "hint")

    def _status(self):
        if self.recording:
            state, color = "● LISTENING", CYAN
        elif self.working or self.speaking:
            state, color = "● THINKING", VIOLET
        elif self.controller.awake:
            state, color = "● ONLINE", CYAN
        else:
            state, color = "● STANDBY", MUTED
        self.status.configure(text=state, fg=color)

    def _animate(self):
        self.phase += 0.065
        self.canvas.delete("orb")
        center_x, center_y = 275, 151
        active = self.controller.awake or self.working or self.recording or self.speaking
        rate = 1.8 if (self.speaking or self.recording) else 1.0
        pulse = math.sin(self.phase * rate)
        # Glow layers: concentric circles avoid expensive alpha compositing.
        for i in range(23, 0, -1):
            radius = 62 + i * 3.4 + pulse * (4 if active else 1)
            blue = min(120, 23 + (24 - i) * 3)
            hue = f"#{11:02x}{blue:02x}{min(170,blue+52):02x}"
            self.canvas.create_oval(center_x-radius, center_y-radius,
                                    center_x+radius, center_y+radius,
                                    fill=hue, outline="", tags="orb")
        for j in range(7):
            r = 65 + j * 7.2
            wobble = 8 if active else 3
            points = []
            for t in range(90):
                theta = t * math.tau / 89
                warp = math.sin(theta*3 + self.phase*(1.7+j*.1)+j*.7)*wobble
                rx = (r + warp) * (1 + 0.12 * math.sin(self.phase*.5))
                ry = (r * .68 + warp) * (1 + 0.08 * math.cos(self.phase*.65))
                points.extend((center_x + math.cos(theta)*rx,
                               center_y + math.sin(theta)*ry))
            self.canvas.create_line(*points, fill=CYAN if j % 2 else VIOLET,
                                    width=1.2 if active else .8, smooth=True, tags="orb")
        # Small nucleus and two orbiting satellites.
        r = 22 + pulse*2
        self.canvas.create_oval(center_x-r, center_y-r,
                                center_x+r, center_y+r, fill="#C7F8FF",
                                outline="#6BC4F1", width=2, tags="orb")
        for j in range(2):
            t = self.phase*(.65+j*.2)+j*math.pi
            px = center_x+math.cos(t)*145
            py = center_y+math.sin(t)*77
            self.canvas.create_oval(px-3,py-3,px+3,py+3,fill=CYAN,outline="",tags="orb")
        self._status()
        self.root.after(38, self._animate)


def run(controller: PCController) -> None:
    if sys.platform != "win32":
        raise RuntimeError("MAX PC app currently targets Windows 10/11")
    try:
        import tkinter as tk
    except ImportError as exc:
        raise RuntimeError("Tkinter is unavailable; install Python with Tcl/Tk support") from exc
    root = tk.Tk()
    MaxDesktop(root, controller)
    root.mainloop()
