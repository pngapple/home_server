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

Runs its own asyncio event loop in a background thread so listen.py's main
script stays the plain synchronous script it already is.
"""

import asyncio
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

# Set by listen.py via on_clip(); called (from a new thread, not the
# asyncio one) once per finished push-to-talk recording with the full
# concatenated audio and a label identifying which connection it was.
_clip_handler: Callable[[str, np.ndarray], None] | None = None


def on_clip(handler: Callable[[str, np.ndarray], None]) -> None:
    global _clip_handler
    _clip_handler = handler


async def _handle(websocket) -> None:
    remote = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}" if websocket.remote_address else "unknown"
    log.info("Push-to-talk connected (%s)", remote)
    buf = bytearray()
    frames: list[np.ndarray] = []
    try:
        async for message in websocket:
            if not isinstance(message, (bytes, bytearray)):
                continue  # ignore any text control messages, not audio
            buf.extend(message)
            while len(buf) >= _FRAME_BYTES:
                chunk = bytes(buf[:_FRAME_BYTES])
                del buf[:_FRAME_BYTES]
                frames.append(np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0)
            if len(frames) >= _MAX_FRAMES:
                log.info("Push-to-talk (%s) hit the %.1fs cap, closing", remote, config.MAX_COMMAND_SECONDS)
                await websocket.close()
                break
    except Exception:
        pass
    finally:
        log.info("Push-to-talk disconnected (%s), %d frame(s) captured", remote, len(frames))
        if frames and _clip_handler is not None:
            clip = np.concatenate(frames)
            threading.Thread(target=_clip_handler, args=(remote, clip), name=f"ptt-{remote}", daemon=True).start()


async def _serve() -> None:
    async with websockets.serve(_handle, "127.0.0.1", config.BROWSER_MIC_WS_PORT, max_size=None):
        log.info("Push-to-talk WebSocket listening on 127.0.0.1:%d", config.BROWSER_MIC_WS_PORT)
        await asyncio.Future()  # run forever


def start_background() -> None:
    """Call once at startup, after on_clip() has been set. The websockets
    server runs in its own thread with its own event loop so listen.py's
    main script doesn't need to become async just to gain this."""

    def _run():
        asyncio.run(_serve())

    threading.Thread(target=_run, name="browser-mic-ws", daemon=True).start()
