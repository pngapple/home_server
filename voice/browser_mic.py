"""
WebSocket server the /voice/ dashboard streams raw mic audio to when its
"use this mic too" toggle is on — a second (third, fourth...) independent
audio source for the exact same wake-word/recording/speaker-verification/
transcription pipeline in listen.py runs against the Pi's own mic, not a
separate reimplementation of it. Whichever source actually hears the wake
word first is the one that acts; that's the entire "closest mic wins"
behavior, with nothing to explicitly compare or arbitrate.

Multiple devices can have the dashboard open at once, each independently
toggled on — every WebSocket connection gets its own queue and its own
Listener thread (spawned via the callback registered through
on_connection()), never a shared one. Mixing two live audio streams into
one queue would interleave unrelated chunks into nonsense audio, which is
exactly what per-connection state avoids.

Deliberately not authenticated with VOICE_SERVER_SECRET — see
config.BROWSER_MIC_WS_PORT's comment. A stream connected here still has to
clear speaker verification (is_enrolled_speaker) before any of it can reach
the bot or cost a Groq call, same as the Pi's mic; it only needs the same
Tailscale-level access already required to load the dashboard page in the
first place.

Runs its own asyncio event loop in a background thread so listen.py's main
script stays the plain synchronous script it already is.
"""

import asyncio
import logging
import queue
import threading
from collections.abc import Callable

import numpy as np
import websockets

from . import config

log = logging.getLogger("voice.browser_mic")

_FRAME_BYTES = config.FRAME_SAMPLES * 2  # int16 PCM

# A single reused placeholder (not a fresh zeros() each time) so listen.py
# can cheaply tell "real audio" from "nothing's actually streaming right
# now" with an `is` check and skip wake-word inference on it entirely.
IDLE_FRAME = np.zeros(config.FRAME_SAMPLES, dtype=np.float32)

# Set by listen.py via on_connection(); called (from the asyncio thread)
# once per new WebSocket connection with a frame_stream()-compatible
# generator for that connection alone. listen.py is responsible for
# spawning the actual Listener thread — this module only owns the
# WebSocket/queue plumbing, not the wake-word pipeline.
_new_connection_handler: Callable[[str, "queue.Queue"], None] | None = None


def on_connection(handler: Callable[[str, "queue.Queue"], None]) -> None:
    global _new_connection_handler
    _new_connection_handler = handler


def frame_stream(q: "queue.Queue"):
    """Yields FRAME_SAMPLES-sized float32 chunks for one connection's queue
    until that connection closes (a None sentinel), then stops — same
    generator interface as audio.frame_stream() otherwise. Yields
    IDLE_FRAME on a brief gap rather than blocking indefinitely: a live mic
    never simply stops producing frames, and _record_command's
    silence-based endpointing already knows what to do with quiet frames."""
    while True:
        try:
            frame = q.get(timeout=config.FRAME_MS / 1000)
        except queue.Empty:
            yield IDLE_FRAME
            continue
        if frame is None:  # disconnect sentinel
            return
        yield frame


async def _handle(websocket) -> None:
    remote = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}" if websocket.remote_address else "unknown"
    log.info("Browser mic connected (%s)", remote)
    q: "queue.Queue" = queue.Queue(maxsize=200)
    if _new_connection_handler is not None:
        _new_connection_handler(remote, q)
    buf = bytearray()
    try:
        async for message in websocket:
            if not isinstance(message, (bytes, bytearray)):
                continue  # ignore any text control messages, not audio
            buf.extend(message)
            while len(buf) >= _FRAME_BYTES:
                chunk = bytes(buf[:_FRAME_BYTES])
                del buf[:_FRAME_BYTES]
                frame = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    try:
                        q.get_nowait()  # drop the oldest rather than block or drop the newest
                    except queue.Empty:
                        pass
                    q.put_nowait(frame)
    except Exception:
        pass
    finally:
        log.info("Browser mic disconnected (%s)", remote)
        try:
            q.put_nowait(None)  # tells that connection's Listener thread to stop
        except queue.Full:
            q.get_nowait()
            q.put_nowait(None)


async def _serve() -> None:
    async with websockets.serve(_handle, "127.0.0.1", config.BROWSER_MIC_WS_PORT, max_size=None):
        log.info("Browser mic WebSocket listening on 127.0.0.1:%d", config.BROWSER_MIC_WS_PORT)
        await asyncio.Future()  # run forever


def start_background() -> None:
    """Call once at startup, after on_connection() has been set. The
    websockets server runs in its own thread with its own event loop — the
    rest of this module (frame_stream, per-connection queues) is plain
    synchronous code so listen.py doesn't need to become async just to
    gain more audio sources."""

    def _run():
        asyncio.run(_serve())

    threading.Thread(target=_run, name="browser-mic-ws", daemon=True).start()
