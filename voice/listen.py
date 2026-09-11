"""
Main loop: `python -m voice.listen`.

Wake word (openWakeWord, continuous, local) -> record the command until a
short silence -> speaker verification (local; a non-match is dropped right
here, before any network call) -> transcribe the clip (Groq Whisper, the one
unavoidable cloud hop) -> POST the transcript to the bot's /voice/command
webhook -> synthesize the reply (piper, local) and ship it to the /voice/
dashboard (bot/voice_status_server.py) to play in-browser.

Everything before the POST runs on this device only. See the plan doc
(voice commands) for the full latency reasoning and the division of labor
between this process and bot/voice_server.py.

This same pipeline runs once per audio source, not once total: the Pi's own
mic always gets one, and browser_mic.py spawns another per browser tab that
has its dashboard "use this mic too" toggle on — see Listener below and
run()'s wiring. Whichever source's Listener actually hears the wake word
first is the one that acts; there's no explicit "which mic is closest"
comparison because none is needed.
"""

import base64
import collections
import logging
import queue
import threading
import time

import numpy as np
import requests
from openwakeword.model import Model

from . import browser_mic, config, speaker, stt, tts
from .audio import frame_stream as pi_frame_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice.listen")

# Frames of pre-roll kept around at all times so the recorded command isn't
# missing whatever you said in the same breath as the wake word trailing
# off — without this, "jian yang, turn on the desk lights" can end up
# recorded as "urn on the desk lights."
_PREROLL_FRAMES = 5  # ~400ms at FRAME_MS=80

# Floor for the near-trigger log line below — low enough to catch "it heard
# something wake-word-shaped but not enough," high enough that background
# noise/normal speech doesn't spam the log every frame.
_NEAR_TRIGGER_LOG_THRESHOLD = 0.15

# How often to push a "still listening" ping to the dashboard
# (bot/voice_status_server.py) while idle — frequent enough that "offline"
# (see that module's _STALE_S) shows up reasonably fast if this process
# dies, infrequent enough not to matter as network chatter.
_HEARTBEAT_INTERVAL_S = 20.0


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(frame))))


def _record_command(frames, preroll: collections.deque) -> np.ndarray:
    """Consumes from `frames` until a silence gap or the hard cap, and
    returns the accumulated command audio (preroll included)."""
    collected = list(preroll)
    silence_frame_budget = config.SILENCE_MS // config.FRAME_MS
    max_frames = int(config.MAX_COMMAND_SECONDS * 1000) // config.FRAME_MS
    silent_run = 0
    heard_speech = False

    for frame in frames:
        collected.append(frame)
        loud = _rms(frame) >= config.SILENCE_RMS_THRESHOLD
        if loud:
            heard_speech = True
            silent_run = 0
        else:
            silent_run += 1
        if heard_speech and silent_run >= silence_frame_budget:
            break
        if len(collected) >= max_frames:
            log.info("Command recording hit the %.1fs cap without a clear silence", config.MAX_COMMAND_SECONDS)
            break

    return np.concatenate(collected)


def _send_command(result: stt.Transcription) -> str | None:
    try:
        resp = requests.post(
            config.VOICE_SERVER_URL,
            json={
                "secret": config.VOICE_SERVER_SECRET,
                "transcript": result.text,
                # So the bot can log this as a metrics.py `calls` row
                # (source="groq") the same way it already does OpenRouter
                # spend — see bot/voice_server.py.
                "stt_model": config.GROQ_STT_MODEL,
                "stt_duration_s": result.duration_s,
                "stt_cost_usd": result.cost_usd,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["reply"]
    except Exception:
        log.exception("Failed to reach the bot's voice webhook at %s", config.VOICE_SERVER_URL)
        return None


def _report_status(source: str, phase: str, detail: dict | None = None, audio: bytes | None = None) -> None:
    """Best-effort push to the dashboard. Short timeout, swallows every
    error — unlike _send_command's failure (which drops a real command),
    a missed status ping is just a stale dashboard, never worth stalling
    wake-word detection over. `audio`, when given, is a WAV clip (see
    tts.synthesize) the dashboard plays in-browser instead of this device
    speaking it locally — base64 because it rides along in the same JSON
    status POST rather than a separate upload. `source` says which
    Listener this came from (the Pi's mic, or which browser connection) —
    with several possibly running at once, the dashboard needs that to
    show anything meaningful about which one actually heard you."""
    body = {"secret": config.VOICE_SERVER_SECRET, "phase": phase, "detail": {**(detail or {}), "source": source}}
    if audio:
        body["audio_b64"] = base64.b64encode(audio).decode("ascii")
    try:
        requests.post(config.VOICE_STATUS_URL, json=body, timeout=(5 if audio else 3))
    except Exception:
        pass


class Listener:
    """One instance per audio source (the Pi's own mic, or one per browser
    tab currently streaming via browser_mic.py) — each owns its own
    openWakeWord model instance and buffers, so two sources running at once
    never share mutable state that would corrupt each other's streaming
    predictions."""

    def __init__(self, source: str, frames):
        self.source = source
        self.frames = frames
        self.oww = Model(wakeword_models=[config.WAKE_WORD_MODEL_PATH], inference_framework="onnx")
        self.wake_word_name = next(iter(self.oww.models.keys()))
        self.preroll: collections.deque = collections.deque(maxlen=_PREROLL_FRAMES)

    def run(self) -> None:
        log.info(
            "[%s] Listening for wake word %r (threshold=%.2f)", self.source, self.wake_word_name, config.WAKE_WORD_THRESHOLD
        )
        _report_status(self.source, "listening")
        last_heartbeat = time.monotonic()

        for frame in self.frames:
            # A browser connection with nothing currently arriving (idle,
            # or just disconnected) ticks this loop with IDLE_FRAME rather
            # than blocking — see browser_mic.frame_stream(). Running
            # wake-word inference on that would be pure waste, and for the
            # Pi's own mic this branch is simply never taken (its frames
            # are never that exact object).
            if frame is browser_mic.IDLE_FRAME:
                now = time.monotonic()
                if now - last_heartbeat >= _HEARTBEAT_INTERVAL_S:
                    _report_status(self.source, "listening")
                    last_heartbeat = now
                continue

            pcm16 = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
            prediction = self.oww.predict(pcm16)
            score = prediction.get(self.wake_word_name, 0.0)
            self.preroll.append(frame)

            # A near-miss is exactly what calibration (see train.md) needs
            # to see — logging only real triggers left no way to tell "it's
            # not hearing me at all" apart from "it heard me, scored 0.3,
            # and the threshold is just a bit too high."
            if score >= _NEAR_TRIGGER_LOG_THRESHOLD:
                log.info("[%s] Near-trigger score=%.2f (threshold=%.2f)", self.source, score, config.WAKE_WORD_THRESHOLD)

            if score < config.WAKE_WORD_THRESHOLD:
                now = time.monotonic()
                if now - last_heartbeat >= _HEARTBEAT_INTERVAL_S:
                    _report_status(self.source, "listening")
                    last_heartbeat = now
                continue

            log.info("[%s] Wake word detected (score=%.2f) — recording command", self.source, score)
            _report_status(self.source, "awoken", {"score": round(float(score), 3)})
            self.oww.reset()
            _report_status(self.source, "recording")
            command_audio = _record_command(self.frames, self.preroll)
            self.preroll.clear()

            matched, sim_score = speaker.is_enrolled_speaker(command_audio)
            log.info(
                "[%s] Speaker check: score=%.3f threshold=%.2f -> %s",
                self.source,
                sim_score,
                config.SPEAKER_THRESHOLD,
                "match" if matched else "REJECTED",
            )
            if not matched:
                _report_status(self.source, "rejected", {"reason": "speaker mismatch", "score": round(float(sim_score), 3)})
                last_heartbeat = time.monotonic()
                continue

            _report_status(self.source, "transcribing")
            result = stt.transcribe(command_audio)
            if not result.text:
                log.info("[%s] Empty transcript, ignoring", self.source)
                _report_status(self.source, "rejected", {"reason": "empty transcript"})
                last_heartbeat = time.monotonic()
                continue
            log.info("[%s] Transcript: %r (audio=%.1fs, cost=$%.5f)", self.source, result.text, result.duration_s, result.cost_usd)

            _report_status(self.source, "sending", {"transcript": result.text})
            reply = _send_command(result)
            if reply is not None:
                log.info("[%s] Reply: %s", self.source, reply)
                # Spoken through the /voice/ dashboard in the browser, not
                # this device's own speaker — see tts.synthesize's docstring.
                _report_status(
                    self.source, "replied", {"transcript": result.text, "reply": reply[:400]}, audio=tts.synthesize(reply)
                )
            else:
                _report_status(self.source, "error", {"reason": "webhook unreachable", "transcript": result.text})
            last_heartbeat = time.monotonic()


def _on_browser_connection(remote: str, q: "queue.Queue") -> None:
    """Called from browser_mic's WebSocket thread for each new connection —
    spins up a dedicated Listener + thread for it, which winds itself down
    once that connection's frame_stream sees the disconnect sentinel."""
    listener = Listener(f"browser:{remote}", browser_mic.frame_stream(q))
    threading.Thread(target=listener.run, name=f"listener-browser-{remote}", daemon=True).start()


def run() -> None:
    log.info("Loading wake-word model from %s", config.WAKE_WORD_MODEL_PATH)
    browser_mic.on_connection(_on_browser_connection)
    browser_mic.start_background()
    Listener("pi_mic", pi_frame_stream()).run()


if __name__ == "__main__":
    run()
