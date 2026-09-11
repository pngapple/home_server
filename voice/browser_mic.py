"""
WebSocket server the /voice/ dashboard streams raw mic audio to for
push-to-talk: press-and-hold the mic button, the connection is open and
streaming; release it, the connection closes and that's the recording.
There's no wake-word detection on this path at all — the button press
already is the "I mean to say a command now" signal a wake word exists to
substitute for, so running one here would be redundant. (Continuous
always-listening browser mics were tried and deliberately dropped in favor
of this — wake-word detection only runs against the Pi's own mic now, see
listen.py.)

Multiple devices can have the dashboard open and push-to-talk it at once;
every connection is independent (own buffer, own thread once it closes),
so concurrent sessions never interfere with each other.

Deliberately not authenticated with VOICE_SERVER_SECRET — see
config.BROWSER_MIC_WS_PORT's comment. A finished clip still has to clear
speaker verification (is_enrolled_speaker) before any of it can reach the
bot or cost a Groq call, same as the Pi's mic; it only needs the same
Tailscale-level access already required to load the dashboard page in the
first place.

A connection to a path containing "enroll" (see nginx's /voice/enroll-ws,
proxied here without stripping the path so the distinction survives — see
the comment on _handle's routing) is treated differently: rather than
firing off to the command pipeline once it disconnects, it's processed
synchronously while still open and the result is sent back over the same
connection as one JSON text message before closing. Enrollment isn't
latency-sensitive the way a voice command is (no reason to let the
WebSocket linger through a Whisper+LLM round trip), and giving the person
who just read a phrase immediate feedback ("added" / "too short, try
again") is worth blocking on here.

Runs its own asyncio event loop in a background thread so listen.py's main
script stays the plain synchronous script it already is.
"""

import asyncio
import json
import logging
import threading
from collections.abc import Callable

import numpy as np
import websockets

from . import config

log = logging.getLogger("voice.browser_mic")

_FRAME_BYTES = config.FRAME_SAMPLES * 2  # int16 PCM

# Safety cap on one push-to-talk recording, same bound as the Pi's own
# silence-triggered recordings (config.MAX_COMMAND_SECONDS) — a client that
# never releases the button (bug, or a dropped release event) shouldn't be
# able to buffer an unbounded clip in memory or run up an unbounded Groq
# bill; the connection is simply closed once this much audio has arrived.
_MAX_FRAMES = int(config.MAX_COMMAND_SECONDS * 1000) // config.FRAME_MS
# Enrollment samples are read-a-passage length, not a quick command — give
# them noticeably more room before the safety cap kicks in.
_MAX_ENROLL_FRAMES = int(30 * 1000) // config.FRAME_MS

# Set by listen.py via on_clip()/on_enroll_clip(); called once per finished
# recording with the full concatenated audio and a label identifying which
# connection it was. on_clip's handler runs in a new thread (fire-and-
# forget — see module docstring); on_enroll_clip's handler runs via
# asyncio.to_thread from within the still-open connection and must return a
# small JSON-serializable dict describing what happened.
_clip_handler: Callable[[str, np.ndarray], None] | None = None
_enroll_handler: Callable[[str, np.ndarray], dict] | None = None


def on_clip(handler: Callable[[str, np.ndarray], None]) -> None:
    global _clip_handler
    _clip_handler = handler


def on_enroll_clip(handler: Callable[[str, np.ndarray], dict]) -> None:
    global _enroll_handler
    _enroll_handler = handler


async def _collect(websocket, remote: str, max_frames: int, stop_on_text: bool = False) -> np.ndarray:
    """`stop_on_text`: for enrollment, the connection has to stay open
    *past* the end of recording (so a result can be sent back over it —
    see _handle), so disconnect can't be the "done" signal there the way
    it is for push-to-talk. The browser sends one text message instead;
    any text message ends collection, its content doesn't matter."""
    buf = bytearray()
    frames: list[np.ndarray] = []
    async for message in websocket:
        if not isinstance(message, (bytes, bytearray)):
            if stop_on_text:
                break
            continue  # ignore any text control messages, not audio
        buf.extend(message)
        while len(buf) >= _FRAME_BYTES:
            chunk = bytes(buf[:_FRAME_BYTES])
            del buf[:_FRAME_BYTES]
            frames.append(np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0)
        if len(frames) >= max_frames:
            log.info("Recording (%s) hit its frame cap, closing", remote)
            break
    return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)


async def _handle(websocket) -> None:
    remote = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}" if websocket.remote_address else "unknown"
    is_enroll = "enroll" in (getattr(websocket.request, "path", "") or "")

    if is_enroll:
        log.info("Enrollment connected (%s)", remote)
        try:
            clip = await _collect(websocket, remote, _MAX_ENROLL_FRAMES, stop_on_text=True)
        except Exception:
            clip = np.zeros(0, dtype=np.float32)
        log.info("Enrollment recording (%s) done, %.1fs captured", remote, len(clip) / config.SAMPLE_RATE)
        result = {"ok": False, "message": "No audio captured."}
        if len(clip) > 0 and _enroll_handler is not None:
            try:
                result = await asyncio.to_thread(_enroll_handler, remote, clip)
            except Exception:
                log.exception("Enrollment handler failed (%s)", remote)
                result = {"ok": False, "message": "Something went wrong processing that — check the logs."}
        try:
            await websocket.send(json.dumps(result))
        except Exception:
            pass
        return

    log.info("Push-to-talk connected (%s)", remote)
    try:
        clip = await _collect(websocket, remote, _MAX_FRAMES)
    except Exception:
        clip = np.zeros(0, dtype=np.float32)
    log.info("Push-to-talk disconnected (%s), %.1fs captured", remote, len(clip) / config.SAMPLE_RATE)
    if len(clip) > 0 and _clip_handler is not None:
        threading.Thread(target=_clip_handler, args=(remote, clip), name=f"ptt-{remote}", daemon=True).start()


async def _serve() -> None:
    async with websockets.serve(_handle, "127.0.0.1", config.BROWSER_MIC_WS_PORT, max_size=None):
        log.info("Push-to-talk WebSocket listening on 127.0.0.1:%d", config.BROWSER_MIC_WS_PORT)
        await asyncio.Future()  # run forever


def start_background() -> None:
    """Call once at startup, after on_clip()/on_enroll_clip() have been
    set. The websockets server runs in its own thread with its own event
    loop so listen.py's main script doesn't need to become async just to
    gain this."""

    def _run():
        asyncio.run(_serve())

    threading.Thread(target=_run, name="browser-mic-ws", daemon=True).start()
