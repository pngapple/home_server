"""
Spoken replies via piper (fast local neural TTS — sub-second synthesis on a
Pi, no cloud round trip for the reply). This is the ephemeral half of the
reply; bot/voice_server.py also DMs the same text to you regardless, so a
missing speaker or a piper failure here never loses the reply entirely —
see this repo's plan doc for why both exist.

A no-op (logged, not raised) if PIPER_MODEL_PATH isn't configured, so
running without a speaker set up yet doesn't crash the listener.
"""

import logging
import subprocess
import sys
from pathlib import Path

from . import config

log = logging.getLogger("voice.tts")

# Resolved next to the running interpreter (voice/venv/bin/piper, installed
# there by `pip install piper-tts`) rather than trusting bare "piper" on
# $PATH — this runs under systemd (see voice-listener.service), whose
# default PATH doesn't include the venv's bin/ at all, so the bare name
# would silently fail there even after working in an interactive shell.
_PIPER_BIN = str(Path(sys.executable).parent / "piper")


def speak(text: str) -> None:
    if not config.PIPER_MODEL_PATH:
        log.info("PIPER_MODEL_PATH not set; skipping spoken reply (DM still sent).")
        return
    try:
        piper = subprocess.Popen(
            [_PIPER_BIN, "--model", config.PIPER_MODEL_PATH, "--output-raw"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        aplay_cmd = ["aplay", "-q", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"]
        if config.OUTPUT_DEVICE:
            aplay_cmd[1:1] = ["-D", config.OUTPUT_DEVICE]
        aplay = subprocess.Popen(aplay_cmd, stdin=piper.stdout)
        piper.stdin.write(text.encode("utf-8"))
        piper.stdin.close()
        piper.stdout.close()
        aplay.wait()
        piper.wait()
    except FileNotFoundError as exc:
        log.warning("TTS unavailable (%s not found) — is piper-tts installed in this venv?", exc.filename)
    except Exception:
        log.exception("TTS playback failed")


def synthesize(text: str) -> bytes | None:
    """WAV bytes for `text`, or None if unavailable. Used instead of
    speak() to play replies through the /voice/ dashboard in the browser
    rather than through whatever's plugged into the Pi itself — see
    listen.py, which uploads this to bot/voice_status_server.py."""
    if not config.PIPER_MODEL_PATH:
        log.info("PIPER_MODEL_PATH not set; skipping TTS synthesis.")
        return None
    try:
        result = subprocess.run(
            [_PIPER_BIN, "--model", config.PIPER_MODEL_PATH, "-f", "-"],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("piper exited %d: %s", result.returncode, result.stderr.decode(errors="replace"))
            return None
        return result.stdout
    except FileNotFoundError as exc:
        log.warning("TTS unavailable (%s not found) — is piper-tts installed in this venv?", exc.filename)
        return None
    except Exception:
        log.exception("TTS synthesis failed")
        return None
