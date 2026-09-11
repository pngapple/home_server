"""
Main loop: `python -m voice.listen`.

Wake word (openWakeWord, continuous, local, Pi mic only — see below) ->
record the command until a short silence -> speaker verification (local; a
non-match is dropped right here, before any network call) -> transcribe the
clip (Groq Whisper, the one unavoidable cloud hop) -> POST the transcript to
the bot's /voice/command webhook -> synthesize the reply (piper, local) and
ship it to the /voice/ dashboard (bot/voice_status_server.py) to play
in-browser.

Everything before the POST runs on this device only. See the plan doc
(voice commands) for the full latency reasoning and the division of labor
between this process and bot/voice_server.py.

Wake-word detection only runs against the Pi's own mic (the Listener class
below). A device with the /voice/ dashboard open instead gets push-to-talk
(voice/browser_mic.py): pressing the mic button there already signals
intent the way a wake word exists to substitute for, so running wake-word
detection on a browser's mic too would just be redundant — continuous
always-listening browser mics were tried and deliberately dropped in favor
of this. Both paths converge on _handle_command_audio() once they each have
a finished clip, so speaker verification, transcription, and everything
after is identical either way.
"""

import base64
import collections
import logging
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
    status POST rather than a separate upload. `source` says where this
    came from (the Pi's mic, or which push-to-talk connection)."""
    body = {"secret": config.VOICE_SERVER_SECRET, "phase": phase, "detail": {**(detail or {}), "source": source}}
    if audio:
        body["audio_b64"] = base64.b64encode(audio).decode("ascii")
    try:
        requests.post(config.VOICE_STATUS_URL, json=body, timeout=(5 if audio else 3))
    except Exception:
        pass


def _handle_command_audio(source: str, command_audio: np.ndarray) -> None:
    """Everything from "I have a finished recording" onward: speaker
    verification, transcription, sending it to the bot, and speaking the
    reply back. Shared by the Pi's wake-word Listener (which gets here via
    silence-based endpointing after a trigger) and push-to-talk (which gets
    here as soon as the button is released) — neither cares how the other
    decided a recording was complete."""
    matched, sim_score = speaker.is_enrolled_speaker(command_audio)
    log.info(
        "[%s] Speaker check: score=%.3f threshold=%.2f -> %s",
        source,
        sim_score,
        config.SPEAKER_THRESHOLD,
        "match" if matched else "REJECTED",
    )
    if not matched:
        _report_status(source, "rejected", {"reason": "speaker mismatch", "score": round(float(sim_score), 3)})
        return

    _report_status(source, "transcribing")
    result = stt.transcribe(command_audio)
    if not result.text:
        log.info("[%s] Empty transcript, ignoring", source)
        _report_status(source, "rejected", {"reason": "empty transcript"})
        return
    log.info("[%s] Transcript: %r (audio=%.1fs, cost=$%.5f)", source, result.text, result.duration_s, result.cost_usd)

    _report_status(source, "sending", {"transcript": result.text})
    reply = _send_command(result)
    if reply is not None:
        log.info("[%s] Reply: %s", source, reply)
        reply_detail = {"transcript": result.text, "reply": reply[:400]}
        # Text first, unconditionally — piper synthesis (tts.synthesize)
        # can occasionally take several seconds under CPU contention from
        # wake-word inference, and blocking the reply itself on that isn't
        # worth it just to attach audio at the same time. The dashboard
        # sees the reply immediately; a second update with the same phase
        # follows once/if audio is ready, picked up via the bumped
        # audio_id rather than a second "replied" (bot/voice_status_server
        # only counts the first as a real history entry, matching this).
        _report_status(source, "replied", reply_detail)
        audio = tts.synthesize(reply)
        if audio:
            _report_status(source, "replied", reply_detail, audio=audio)
    else:
        _report_status(source, "error", {"reason": "webhook unreachable", "transcript": result.text})


class Listener:
    """Wake-word detection against the Pi's own mic. There is exactly one
    of these — unlike the old design, browser connections no longer get one
    of their own (see push-to-talk in browser_mic.py / _on_ptt_clip below)."""

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

            _handle_command_audio(self.source, command_audio)
            last_heartbeat = time.monotonic()


def _on_ptt_clip(remote: str, command_audio: np.ndarray) -> None:
    """Called from browser_mic.py once a push-to-talk recording is
    complete (the connection closed) — skips straight to verification,
    no wake word involved."""
    source = f"browser_ptt:{remote}"
    _report_status(source, "recording")  # dashboard visibility only; the recording is already done by this point
    _handle_command_audio(source, command_audio)


# Below this, too little audio to trust an embedding from at all — a
# half-second blip isn't "a bad sample," it's almost certainly a dropped
# connection or a release before any real speech started.
_MIN_ENROLL_SAMPLE_S = 2.5


def _on_enroll_clip(remote: str, samples: np.ndarray) -> dict:
    """Called synchronously (from browser_mic.py's asyncio.to_thread, with
    the WebSocket still open waiting on the result) once someone's read a
    passage into the dashboard's "improve voice match" flow. Folds the
    sample into the existing enrollment via speaker.add_enrollment_sample —
    see that function's docstring for why this is an average, not a
    replacement."""
    duration_s = len(samples) / config.SAMPLE_RATE
    if duration_s < _MIN_ENROLL_SAMPLE_S:
        return {"ok": False, "message": f"Only {duration_s:.1f}s captured — hold the button longer and read the whole passage."}
    result = speaker.add_enrollment_sample(samples)
    log.info(
        "[enroll:%s] Added a %.1fs sample (voiceprint shifted to %.3f similarity with the previous one)",
        remote,
        duration_s,
        result["similarity_to_previous"],
    )
    return {
        "ok": True,
        "message": f"Added a {duration_s:.1f}s sample from this device's mic to your voiceprint.",
    }


def run() -> None:
    log.info("Loading wake-word model from %s", config.WAKE_WORD_MODEL_PATH)
    browser_mic.on_clip(_on_ptt_clip)
    browser_mic.on_enroll_clip(_on_enroll_clip)
    browser_mic.start_background()
    Listener("pi_mic", pi_frame_stream()).run()


if __name__ == "__main__":
    run()
