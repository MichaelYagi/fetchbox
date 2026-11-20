"""Phase 0 gate: a job survives `kill -9` of the process running it and resumes on restart."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

from fetchbox import migrations
from fetchbox.core.db import Database
from fetchbox.core.events import EventBus
from fetchbox.core.jobs import Job, JobContext, JobRunner

CHILD = textwrap.dedent(
    """
    import asyncio, sys
    from fetchbox.core.db import Database
    from fetchbox.core.events import EventBus
    from fetchbox.core.jobs import JobRunner

    async def main(url):
        db = Database(url)
        r = JobRunner(db, EventBus(), pools={"io": 1}, poll_interval=0.05)

        @r.handler("import")
        async def imp(ctx):
            print("RUNNING", ctx.job_id, flush=True)
            await asyncio.sleep(3600)

        await r.enqueue("import", {"file": "x.mkv"})
        await r.start()
        await asyncio.sleep(3600)

    asyncio.run(main(sys.argv[1]))
    """
)


async def test_job_survives_sigkill(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'crash.db').as_posix()}"
    await asyncio.to_thread(migrations.upgrade, url)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CHILD, url, stdout=asyncio.subprocess.PIPE
    )
    try:
        assert proc.stdout is not None
        line = (await asyncio.wait_for(proc.stdout.readline(), 30)).decode()
        assert line.startswith("RUNNING"), line
        job_id = line.split()[1]
    finally:
        proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup runs
        await proc.wait()

    db = Database(url)
    r = JobRunner(db, EventBus(), pools={"io": 1}, poll_interval=0.05)
    attempts: list[int] = []

    @r.handler("import")
    async def imp(ctx: JobContext) -> dict[str, str]:
        attempts.append(ctx.attempt)
        return {"file": ctx.payload["file"]}

    before = await r.get(job_id)
    assert before is not None and before.status == "running"
    job: Job | None = None
    await r.start()
    try:
        for _ in range(200):
            job = await r.get(job_id)
            if job is not None and job.status == "succeeded":
                break
            await asyncio.sleep(0.02)
    finally:
        await r.stop()
        await db.dispose()
    assert job is not None and job.status == "succeeded"
    assert job.result == {"file": "x.mkv"}
    assert attempts == [2]
