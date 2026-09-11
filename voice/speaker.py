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
import torch
from resemblyzer import VoiceEncoder, preprocess_wav

from . import config

# Unset, torch defaults to one intra-op thread per CPU core for its own
# ops — on this Pi's 4 cores, that's real contention against openWakeWord's
# ONNX session (already pinned to 1 thread — see listen.py's Model()) and
# piper's synthesis, both of which can be running around the same time as
# a speaker check. This is also almost certainly part of why piper hit its
# 30s TTS timeout once under load (see tts.py) — capping this narrows that
# kind of contention without giving up meaningfully anything: a few-second
# clip's embedding is fast enough on 2 threads that stealing the other 2
# from openWakeWord/piper was never buying much.
torch.set_num_threads(2)

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


def add_enrollment_sample(samples: np.ndarray, sample_rate: int = config.SAMPLE_RATE) -> dict:
    """Blends one more utterance into the existing enrollment by averaging
    unit-normalized embedding *directions* and renormalizing, rather than
    redoing full enrollment from scratch. This is the practical way to fold
    in a sample from a different microphone: enroll.py's original 5 phrases
    on the Pi mic weren't kept as raw audio (only the resulting embedding
    was saved — see save_enrollment), so there's no way to hand a fresh
    embed_speaker() call the old samples alongside new ones. Averaging two
    already-computed unit vectors is a reasonable stand-in for that: the
    result sits toward both, rather than snapping the whole voiceprint over
    to whichever mic recorded most recently. See listen.py's
    _on_enroll_clip for why this matters: cross-mic score drift is real
    (enroll.py's own docstring already calls out re-enrolling after
    changing microphones), not something a threshold tweak alone fixes
    well."""
    new_embedding = embed(samples, sample_rate)
    new_unit = new_embedding / np.linalg.norm(new_embedding)
    existing = enrolled()
    if existing is not None:
        existing_unit = existing / np.linalg.norm(existing)
        combined = existing_unit + new_unit
        combined = combined / np.linalg.norm(combined)
        shift = similarity(existing_unit, combined)
    else:
        combined = new_unit
        shift = 1.0
    save_enrollment(combined)
    return {"had_existing": existing is not None, "similarity_to_previous": round(shift, 3)}
