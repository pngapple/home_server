"""
Transcribes one short command clip via Groq's hosted Whisper API.

Not OpenRouter: OpenRouter (already used for chat, see bot/config.py) has no
speech-to-text endpoint. Groq specifically because it's one of the fastest
hosted Whisper endpoints available — the one cloud round trip this pipeline
can't avoid, so it's worth picking for latency over defaulting to whichever
provider happens to already be configured elsewhere in this repo.
"""

import io
import logging
import wave
from dataclasses import dataclass

import numpy as np
import requests

from . import config

log = logging.getLogger("voice.stt")

_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# $/hour of audio. Groq bills transcription by audio duration, not tokens,
# and doesn't return a cost figure in the response (unlike OpenRouter's
# `usage.cost` — see bot/llm.py) — computed locally against published
# pricing instead. https://groq.com/pricing, checked 2026-09.
_PRICE_PER_HOUR_USD = {
    "whisper-large-v3-turbo": 0.04,
    "whisper-large-v3": 0.111,
    "distil-whisper-large-v3-en": 0.02,
}


@dataclass
class Transcription:
    text: str
    duration_s: float
    cost_usd: float


def _wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


def transcribe(samples: np.ndarray, sample_rate: int = config.SAMPLE_RATE) -> Transcription:
    resp = requests.post(
        _URL,
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        files={"file": ("command.wav", _wav_bytes(samples, sample_rate), "audio/wav")},
        data={"model": config.GROQ_STT_MODEL},
        timeout=15,
    )
    resp.raise_for_status()
    duration_s = len(samples) / sample_rate
    price_per_hour = _PRICE_PER_HOUR_USD.get(config.GROQ_STT_MODEL)
    if price_per_hour is None:
        log.warning("No known Groq price for model %r; recording its cost as $0", config.GROQ_STT_MODEL)
    return Transcription(
        text=resp.json()["text"].strip(),
        duration_s=duration_s,
        cost_usd=duration_s / 3600 * (price_per_hour or 0.0),
    )
