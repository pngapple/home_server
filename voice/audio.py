"""
Mic capture, shared between the live listener and the one-time enrollment
script so both record in the exact same format (16kHz mono float32) that
openWakeWord, Resemblyzer, and Whisper all expect.
"""

import numpy as np
import sounddevice as sd

from . import config


def record_seconds(seconds: float) -> np.ndarray:
    """Blocking: record `seconds` of audio and return it as float32 in
    [-1, 1]. Used by enroll.py, where there's no wake word to wait for."""
    frames = int(seconds * config.SAMPLE_RATE)
    audio = sd.rec(frames, samplerate=config.SAMPLE_RATE, channels=1, dtype="float32", device=config.INPUT_DEVICE)
    sd.wait()
    return audio[:, 0]


def frame_stream():
    """Yields FRAME_SAMPLES-sized float32 chunks forever, for the wake-word
    loop's streaming inference. A generator (not a callback) so listen.py's
    control flow — wait for wake word, then switch to command-recording
    mode — reads top to bottom instead of being split across callbacks."""
    with sd.InputStream(
        samplerate=config.SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=config.FRAME_SAMPLES,
        device=config.INPUT_DEVICE,
    ) as stream:
        while True:
            chunk, _overflowed = stream.read(config.FRAME_SAMPLES)
            yield chunk[:, 0]
