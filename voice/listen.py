"""
Main loop: `python -m voice.listen`.

Wake word (openWakeWord, continuous, local) -> record the command until a
short silence -> speaker verification (local; a non-match is dropped right
here, before any network call) -> transcribe the clip (Groq Whisper, the one
unavoidable cloud hop) -> POST the transcript to the bot's /voice/command
webhook -> speak the reply back (piper, local).

Everything before the POST runs on this device only. See the plan doc
(voice commands) for the full latency reasoning and the division of labor
between this process and bot/voice_server.py.
"""

import collections
import logging

import numpy as np
import requests
from openwakeword.model import Model

from . import config, speaker, stt, tts
from .audio import frame_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice.listen")

# Frames of pre-roll kept around at all times so the recorded command isn't
# missing whatever you said in the same breath as the wake word trailing
# off — without this, "jian yang, turn on the desk lights" can end up
# recorded as "urn on the desk lights."
_PREROLL_FRAMES = 5  # ~400ms at FRAME_MS=80


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


def _send_command(transcript: str) -> str | None:
    try:
        resp = requests.post(
            config.VOICE_SERVER_URL,
            json={"secret": config.VOICE_SERVER_SECRET, "transcript": transcript},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["reply"]
    except Exception:
        log.exception("Failed to reach the bot's voice webhook at %s", config.VOICE_SERVER_URL)
        return None


def run() -> None:
    log.info("Loading wake-word model from %s", config.WAKE_WORD_MODEL_PATH)
    oww = Model(wakeword_models=[config.WAKE_WORD_MODEL_PATH], inference_framework="onnx")
    wake_word_name = next(iter(oww.models.keys()))
    log.info("Listening for wake word %r (threshold=%.2f)", wake_word_name, config.WAKE_WORD_THRESHOLD)

    preroll: collections.deque = collections.deque(maxlen=_PREROLL_FRAMES)
    frames = frame_stream()

    for frame in frames:
        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
        prediction = oww.predict(pcm16)
        score = prediction.get(wake_word_name, 0.0)
        preroll.append(frame)

        if score < config.WAKE_WORD_THRESHOLD:
            continue

        log.info("Wake word detected (score=%.2f) — recording command", score)
        oww.reset()
        command_audio = _record_command(frames, preroll)
        preroll.clear()

        matched, sim_score = speaker.is_enrolled_speaker(command_audio)
        log.info("Speaker check: score=%.3f threshold=%.2f -> %s", sim_score, config.SPEAKER_THRESHOLD, "match" if matched else "REJECTED")
        if not matched:
            continue

        transcript = stt.transcribe(command_audio)
        if not transcript:
            log.info("Empty transcript, ignoring")
            continue
        log.info("Transcript: %r", transcript)

        reply = _send_command(transcript)
        if reply is not None:
            log.info("Reply: %s", reply)
            tts.speak(reply)


if __name__ == "__main__":
    run()
