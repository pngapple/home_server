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

from . import config

log = logging.getLogger("voice.tts")


def speak(text: str) -> None:
    if not config.PIPER_MODEL_PATH:
        log.info("PIPER_MODEL_PATH not set; skipping spoken reply (DM still sent).")
        return
    try:
        piper = subprocess.Popen(
            ["piper", "--model", config.PIPER_MODEL_PATH, "--output-raw"],
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
        log.warning("TTS unavailable (%s not found) — is piper installed and on PATH?", exc.filename)
    except Exception:
        log.exception("TTS playback failed")
