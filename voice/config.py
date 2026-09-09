"""
Environment loading for the voice listener. Deliberately independent of
bot/config.py: this runs as its own process (own venv — see
requirements.txt), often not even started from this repo's checkout root,
so it reads the same .env file directly rather than importing bot.*.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — add it to .env and restart voice-listener.")
    return value


# Must match bot/config.py's VOICE_SERVER_SECRET/VOICE_SERVER_PORT exactly —
# this is the same shared secret authenticating this listener to the bot's
# webhook, not a per-user credential.
VOICE_SERVER_SECRET = _required("VOICE_SERVER_SECRET")
VOICE_SERVER_PORT = int(os.environ.get("VOICE_SERVER_PORT", "8794"))
VOICE_SERVER_URL = os.environ.get("VOICE_SERVER_URL", f"http://127.0.0.1:{VOICE_SERVER_PORT}/voice/command")

# Cloud Whisper transcription for the short post-wake-word clip only — see
# the plan doc for why this isn't OpenRouter (no STT endpoint) and why Groq
# specifically (fast hosted Whisper, matters for the "make it quicker" goal).
GROQ_API_KEY = _required("GROQ_API_KEY")
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

# Where the trained custom wake-word model lives — see wakeword/train.md.
# Not committed to the repo (personal voice-trigger asset, not code).
WAKE_WORD_MODEL_PATH = os.environ.get("WAKE_WORD_MODEL_PATH", str(Path(__file__).resolve().parent / "wakeword" / "jian_yang.onnx"))
WAKE_WORD_THRESHOLD = float(os.environ.get("WAKE_WORD_THRESHOLD", "0.5"))

# Your enrolled voiceprint — see enroll.py. Not committed; it's derived
# biometric data, like profiles.json's secrets are credentials.
ENROLLMENT_PATH = os.environ.get("VOICE_ENROLLMENT_PATH", str(Path(__file__).resolve().parent / "enrollment.npy"))
# Cosine similarity below this rejects the speaker. Start here and tune by
# watching the logged score for your own voice vs. a housemate's/the TV's —
# see the README section on calibration.
SPEAKER_THRESHOLD = float(os.environ.get("VOICE_SPEAKER_THRESHOLD", "0.75"))

# Audio capture. 16kHz mono 16-bit PCM is what both openWakeWord and Whisper
# expect, so everything downstream stays in this format — no resampling.
SAMPLE_RATE = 16000
FRAME_MS = 80  # openWakeWord's expected chunk size (1280 samples at 16kHz).
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
INPUT_DEVICE = os.environ.get("VOICE_INPUT_DEVICE")  # sounddevice name/index; None = system default
OUTPUT_DEVICE = os.environ.get("VOICE_OUTPUT_DEVICE")  # None = system default

# How long to keep recording a command after the wake word fires before
# giving up on silence ever coming (safety cap, not the common case).
MAX_COMMAND_SECONDS = float(os.environ.get("VOICE_MAX_COMMAND_SECONDS", "8"))
# Stop recording after this much continuous near-silence, once at least one
# frame of real speech has been seen.
SILENCE_MS = int(os.environ.get("VOICE_SILENCE_MS", "800"))
# RMS (of normalized [-1, 1] samples) below this counts as silence for
# endpointing. Needs calibration against your mic/room noise floor.
SILENCE_RMS_THRESHOLD = float(os.environ.get("VOICE_SILENCE_RMS_THRESHOLD", "0.01"))

PIPER_MODEL_PATH = os.environ.get("PIPER_MODEL_PATH")  # e.g. .../en_US-lessac-medium.onnx; TTS is skipped if unset
