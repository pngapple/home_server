"""
Speaker verification: is this command clip actually you?

This runs entirely locally, on the same short clip that's about to be sent
out for transcription, and gates whether that send ever happens — a clip
that doesn't match the enrolled voice never leaves the device at all (no
network call, no bot involvement), so a housemate or a TV saying the wake
word can't trigger anything even before considering that they don't know
the wake word matters.

Resemblyzer over a heavier option like SpeechBrain's ECAPA-TDNN: smaller
model, no large checkpoint download, fast enough on a Pi's CPU for a few
seconds of audio — accuracy is good enough for "reject everyone but one
enrolled person," which is a much easier bar than open-set speaker ID.
"""

import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav

from . import config

_encoder: VoiceEncoder | None = None


def _get_encoder() -> VoiceEncoder:
    # Loaded lazily, once — construction loads the model weights, which
    # should happen after the process is up and logging, not at import time.
    global _encoder
    if _encoder is None:
        _encoder = VoiceEncoder()
    return _encoder


def embed(samples: np.ndarray, sample_rate: int = config.SAMPLE_RATE) -> np.ndarray:
    """`samples` is float32 in [-1, 1] (see listen.py's capture format)."""
    wav = preprocess_wav(samples, source_sr=sample_rate)
    return _get_encoder().embed_utterance(wav)


def enrolled() -> np.ndarray | None:
    try:
        return np.load(config.ENROLLMENT_PATH)
    except FileNotFoundError:
        return None


def save_enrollment(embedding: np.ndarray) -> None:
    np.save(config.ENROLLMENT_PATH, embedding)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def is_enrolled_speaker(samples: np.ndarray, sample_rate: int = config.SAMPLE_RATE) -> tuple[bool, float]:
    """`(matched, score)` — score is returned even on rejection so the
    caller can log it; that's what calibrating SPEAKER_THRESHOLD is done
    against (see config.py)."""
    reference = enrolled()
    if reference is None:
        raise RuntimeError(f"No enrollment found at {config.ENROLLMENT_PATH} — run `python -m voice.enroll` first.")
    score = similarity(embed(samples, sample_rate), reference)
    return score >= config.SPEAKER_THRESHOLD, score
