#!/usr/bin/env python3
"""Stella helper: the laptop's ears and mouth for the top-bar extension. Listens on 127.0.0.1 only.

POST /listen {max_s}      record the mic until ~0.8 s of silence (max 15 s), transcribe with Whisper.
             {hold, id}   push to talk: record until POST /stop {id} (max 30 s), then transcribe.
                          Music is turned down while listening. Returns {text, speech}.
POST /stop   {id}         end a push-to-talk recording.
POST /cancel              stop a running /listen and throw it away, stop speaking.
POST /speak  {text, wait} say it with Kokoro (falls back to spd-say). Returns at once, or when finished if wait.
POST /hush                stop speaking.
GET  /health

Settings come from the environment: STELLA_PORT (8098), STELLA_WHISPER (small.en), STELLA_VOICE (bf_isabella),
STELLA_SPEED (1.0), STELLA_KOKORO_DIR (~/.local/share/stella/kokoro).
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

PORT = int(os.environ.get("STELLA_PORT", "8098"))
WHISPER = os.environ.get("STELLA_WHISPER", "small.en")
VOICE = os.environ.get("STELLA_VOICE", "bf_isabella")
SPEED = float(os.environ.get("STELLA_SPEED", "1.0"))
KOKORO_DIR = pathlib.Path(os.environ.get("STELLA_KOKORO_DIR", "~/.local/share/stella/kokoro")).expanduser()

RATE = 16000
FRAME = 480                 # 30 ms of 16 kHz mono s16
WAKE = re.compile(r"^\W*(?:hey|hi|okay|ok)?[\s,]*stella\b[\s,.!?]*", re.I)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Gate:
    """Energy voice gate that learns the room's noise floor from the first few frames."""

    def __init__(self):
        self.floor = None

    def reset(self):
        self.floor = None

    def speech(self, x):
        rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) + 1.0
        if self.floor is None:
            self.floor = rms
        elif rms < self.floor * 2:
            self.floor = 0.95 * self.floor + 0.05 * rms
        return rms > max(self.floor * 3.0, 300.0)


def clean(text):
    """Join Whisper segments and drop a leading wake word, which is not needed here."""
    return WAKE.sub("", " ".join(str(text).split())).strip()


class Ears:
    def __init__(self):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(WHISPER, device="cpu", compute_type="int8")
        self.gate = Gate()
        self.lock, self.proc, self.cancelled = threading.Lock(), None, False
        self.stopped = set()      # push-to-talk ids already released (a /stop can beat its /listen)

    def _duck(self, on, saved=[None]):
        if not shutil.which("wpctl"):
            return
        try:
            if on:
                out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                                     capture_output=True, text=True, timeout=3).stdout
                saved[0] = float(out.split()[1])
                subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{saved[0] * 0.3:.2f}"], timeout=3)
            elif saved[0] is not None:
                subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{saved[0]:.2f}"], timeout=3)
                saved[0] = None
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass

    def _record(self):
        cmd = (["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"] if shutil.which("pw-record")
               else ["arecord", "-q", "-r", str(RATE), "-c", "1", "-f", "S16_LE", "-t", "raw"])
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def listen(self, max_s=12.0, wait_s=6.0, hold=None):
        if not self.lock.acquire(blocking=False):
            return {"error": "busy"}
        self.cancelled = False
        self._duck(True)
        frames, started = [], False
        try:
            self.proc = self._record()
            self.gate.reset()
            silence, t0 = 0, time.time()
            while not self.cancelled:
                b = self.proc.stdout.read(FRAME * 2)
                if len(b) < FRAME * 2:
                    break
                x = np.frombuffer(b, np.int16)
                loud = self.gate.speech(x)
                if hold is not None:                      # push to talk: keep everything until released
                    frames.append(x)
                    started = started or loud
                    if hold in self.stopped or len(frames) * 0.03 > 30:
                        break
                    continue
                if not started:
                    frames = (frames + [x])[-12:]         # keep ~0.36 s before speech
                    if loud:
                        started = True
                    elif time.time() - t0 > wait_s:
                        break
                    continue
                frames.append(x)
                silence = silence + 1 if not loud else 0
                if silence > 26 or len(frames) * 0.03 > max_s:
                    break
        finally:
            if self.proc:
                self.proc.kill()
                self.proc = None
            self._duck(False)
            self.stopped.discard(hold)
            self.lock.release()
        if self.cancelled:
            return {"text": "", "speech": False, "cancelled": True}
        if not started or not frames:
            return {"text": "", "speech": False}
        audio = np.concatenate(frames).astype(np.float32) / 32768
        segs, _ = self.model.transcribe(audio, language="en", beam_size=5, without_timestamps=True,
                                        condition_on_previous_text=False, vad_filter=True)
        text = clean(" ".join(s.text for s in segs))
        log("heard:", text)
        return {"text": text, "speech": True}

    def stop(self, hold):
        self.stopped.add(hold)
        if len(self.stopped) > 50:
            self.stopped.clear()

    def cancel(self):
        self.cancelled = True


class Mouth:
    def __init__(self):
        self.k, self.proc, self.lock, self.hushed = None, None, threading.Lock(), 0.0

    def _kokoro(self):
        if self.k is None:
            from kokoro_onnx import Kokoro
            self.k = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
        return self.k

    def _play(self, path):
        player = ["pw-play", path] if shutil.which("pw-play") else ["aplay", "-q", path]
        self.proc = subprocess.Popen(player)
        self.proc.wait()
        self.proc = None

    def speak(self, text):
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:700]
        if not text:
            return
        with self.lock:
            t0 = time.time()
            try:
                import soundfile as sf
                samples, sr = self._kokoro().create(text, voice=VOICE, speed=SPEED, lang="en-gb")
            except Exception as e:                       # no Kokoro model: use the system voice
                log("kokoro unavailable, using spd-say:", repr(e)[:120])
                if shutil.which("spd-say"):
                    self.proc = subprocess.Popen(["spd-say", "-w", text])
                    self.proc.wait()
                    self.proc = None
                return
            if self.hushed > t0:
                return
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                sf.write(f.name, samples, sr)
            try:
                if self.hushed <= t0:
                    self._play(f.name)
            finally:
                os.unlink(f.name)

    def hush(self):
        self.hushed = time.time()
        if self.proc:
            self.proc.terminate()


class Handler(BaseHTTPRequestHandler):
    ears = mouth = None

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        ears, mouth = self.ears, self.mouth
        if self.path == "/listen":
            hold = str(body["id"]) if body.get("hold") and body.get("id") else None
            return self._send(200, ears.listen(min(float(body.get("max_s", 12)), 15.0), hold=hold))
        if self.path == "/stop":
            ears.stop(str(body.get("id", "")))
            return self._send(200, {"ok": True})
        if self.path == "/cancel":
            ears.cancel()
            mouth.hush()
            return self._send(200, {"ok": True})
        if self.path == "/speak":
            if body.get("wait"):
                mouth.speak(body.get("text", ""))
            else:
                threading.Thread(target=mouth.speak, args=(body.get("text", ""),), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/hush":
            mouth.hush()
            return self._send(200, {"ok": True})
        self._send(404, {})


def main():
    log(f"loading Whisper {WHISPER}")
    Handler.ears, Handler.mouth = Ears(), Mouth()
    log(f"ready on 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
