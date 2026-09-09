"""
Transcribes one short command clip via Groq's hosted Whisper API.

Not OpenRouter: OpenRouter (already used for chat, see bot/config.py) has no
speech-to-text endpoint. Groq specifically because it's one of the fastest
hosted Whisper endpoints available — the one cloud round trip this pipeline
can't avoid, so it's worth picking for latency over defaulting to whichever
provider happens to already be configured elsewhere in this repo.
"""

import io
import wave

import numpy as np
import requests

from . import config

_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


def transcribe(samples: np.ndarray, sample_rate: int = config.SAMPLE_RATE) -> str:
    resp = requests.post(
        _URL,
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        files={"file": ("command.wav", _wav_bytes(samples, sample_rate), "audio/wav")},
        data={"model": config.GROQ_STT_MODEL},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["text"].strip()
