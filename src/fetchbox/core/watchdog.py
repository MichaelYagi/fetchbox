"""Debug aid (FETCHBOX_DB_GUARD=1): report what blocks the event loop.

A thread watches a heartbeat the loop updates every 50 ms. When the loop stalls longer
than ``threshold`` seconds, the main thread's stack at that moment is reported: the
synchronous code holding up every coroutine (and, with SQLite, every open write
transaction, so others wait on busy_timeout and fail with "database is locked").
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

from fetchbox.core.db import _report

TICK = 0.05


def start(loop: asyncio.AbstractEventLoop, *, threshold: float = 0.5) -> threading.Event:
    beat = [time.monotonic()]
    stop = threading.Event()
    loop_thread = threading.get_ident()

    async def heartbeat() -> None:
        try:
            while not stop.is_set():
                beat[0] = time.monotonic()
                await asyncio.sleep(TICK)
        finally:
            stop.set()  # the loop is going away: so is the watch

    loop.create_task(heartbeat(), name="loop-watchdog")

    def watch() -> None:
        reported = 0.0
        while not stop.wait(TICK):
            stalled = time.monotonic() - beat[0]
            if stalled < threshold or beat[0] == reported:
                continue
            frame = sys._current_frames().get(loop_thread)
            frames = []
            while frame is not None:
                frames.append(frame)
                frame = frame.f_back
            names = [f.f_code.co_filename.replace("\\", "/") for f in frames]
            if not any("src/fetchbox/" in n or "/tests/" in n for n in names):
                continue  # the loop isn't running our code (e.g. between tests)
            lines = []
            for k, (f, n) in enumerate(zip(frames, names, strict=True)):
                ours = "src/fetchbox/" in n or "/tests/" in n
                if ours or k < 4:
                    short = (
                        n.split("src/fetchbox/")[-1] if "src/fetchbox/" in n else n.split("/")[-1]
                    )
                    lines.append(f"  {short}:{f.f_lineno} {f.f_code.co_name}")
            _report(f"LOOP BLOCKED {stalled:.1f}s so far, in\n" + "\n".join(reversed(lines)))
            reported = beat[0]

    threading.Thread(target=watch, name="loop-watchdog", daemon=True).start()
    return stop
