"""백그라운드 셸: 서버·watch 같은 오래 걸리는 명령을 띄우고 나중에 출력을 읽는다.

shell(command=..., background=true) → `bg_1` 같은 id 를 돌려주고,
shell(bg_id="bg_1") 로 새 출력·상태를 확인, shell(bg_id=..., kill=true) 로 종료한다.
llm 프로세스가 끝나면 띄운 작업도 함께 정리한다(atexit).
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

TAIL_CHARS = 20_000


@dataclass
class BgJob:
    id: str
    command: str
    proc: subprocess.Popen
    log: Path
    offset: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class BgManager:
    def __init__(self) -> None:
        self.jobs: dict[str, BgJob] = {}
        self.counter = 0
        self.dir = Path(tempfile.mkdtemp(prefix="llm-bg-"))
        atexit.register(self.kill_all)

    def start(self, command: str, cwd: Path, env: dict[str, str]) -> BgJob:
        self.counter += 1
        job_id = f"bg_{self.counter}"
        log = self.dir / f"{job_id}.log"
        handle = open(log, "wb")
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                executable="/bin/bash",
                start_new_session=True,  # Ctrl+C 가 백그라운드 작업까지 죽이지 않게
            )
        finally:
            handle.close()
        job = BgJob(job_id, command, proc, log)
        self.jobs[job_id] = job
        return job

    def status_text(self, job: BgJob) -> str:
        code = job.proc.poll()
        return "running" if code is None else f"exited {code}"

    def read_new(self, job_id: str) -> str:
        job = self.jobs.get(job_id)
        if job is None:
            known = ", ".join(self.jobs) or "없음"
            return f"ERROR: 알 수 없는 bg_id: {job_id} (실행 중: {known})"
        with job.lock:
            try:
                data = job.log.read_bytes()
            except OSError:
                data = b""
            new = data[job.offset:]
            job.offset = len(data)
        text = new.decode("utf-8", errors="replace")
        if len(text) > TAIL_CHARS:
            text = f"... [앞 {len(text) - TAIL_CHARS}자 생략]\n" + text[-TAIL_CHARS:]
        body = text if text else "(새 출력 없음)"
        return f"{job_id} [{self.status_text(job)}] {job.command}\n--- 새 출력 ---\n{body}"

    def kill(self, job_id: str) -> str:
        job = self.jobs.get(job_id)
        if job is None:
            return f"ERROR: 알 수 없는 bg_id: {job_id}"
        self._terminate(job)
        return f"{job_id} 종료 요청 → [{self.status_text(job)}]"

    def _terminate(self, job: BgJob) -> None:
        if job.proc.poll() is not None:
            return
        try:
            os.killpg(job.proc.pid, signal.SIGTERM)
            job.proc.wait(timeout=3)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(job.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def kill_all(self) -> None:
        for job in list(self.jobs.values()):
            self._terminate(job)

    def summary(self) -> str:
        if not self.jobs:
            return "(백그라운드 작업 없음)"
        return "\n".join(f"{j.id} [{self.status_text(j)}] {j.command}" for j in self.jobs.values())
