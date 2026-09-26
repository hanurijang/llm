"""편집 안전장치: 읽은 파일 추적 · 외부 변경 감지 · diff · 되돌리기(undo) 저장소.

- `FileTracker`: 에이전트가 `read` 한 파일의 mtime/크기를 기억한다. 읽지 않은
  기존 파일을 고치거나, 읽은 뒤 바깥에서 바뀐 파일을 덮어쓰려 하면 거부한다.
- `unified_diff`/`diff_stat`: 편집 전후 차이를 사람이 보는 형태로 만든다.
- `UndoStore`: 턴마다 편집 전 원본을 저장해 `/undo`(`llm undo`)로 되돌린다.
  셸로 바꾼 파일은 추적하지 않는다(edit 도구 변경만 대상).
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

UNDO_KEEP_TURNS = 20
UNDO_MAX_FILE_BYTES = 2_000_000


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class FileTracker:
    """읽은 파일 상태 기록(프로세스 동안 유지: REPL 여러 턴에 걸쳐 쓴다)."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def mark(self, path: Path) -> None:
        stamp = _stamp(path)
        if stamp is None:
            return
        with self._lock:
            self._seen[str(path.resolve())] = stamp

    def forget(self, path: Path) -> None:
        with self._lock:
            self._seen.pop(str(path.resolve()), None)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def check(self, path: Path) -> str | None:
        """편집해도 되면 None, 아니면 모델에게 돌려줄 오류 문구."""
        if not path.exists():
            return None  # 새 파일 생성은 읽을 것이 없다
        with self._lock:
            seen = self._seen.get(str(path.resolve()))
        if seen is None:
            return "먼저 read 로 파일을 읽은 뒤 편집하세요(읽지 않은 기존 파일은 고칠 수 없습니다)."
        if _stamp(path) != seen:
            return "파일이 마지막으로 읽은 뒤 바깥에서 변경되었습니다. 다시 read 한 뒤 편집하세요."
        return None


def unified_diff(before: str, after: str, rel: str, context: int = 3) -> list[str]:
    return list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            n=context,
            lineterm="",
        )
    )


def diff_stat(diff: list[str]) -> tuple[int, int]:
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


class UndoStore:
    """턴 단위 편집 기록. 파일: <dir>/undo_<session>.json

    각 턴 = {"id", "at", "prompt", "files": [{path, before(b64|None), after_hash}]}.
    같은 턴에서 같은 파일을 여러 번 고치면 **처음 원본**만 남기고 after 는 갱신한다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.turn: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, turns: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(turns[-UNDO_KEEP_TURNS:], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def begin_turn(self, prompt: str) -> None:
        self.turn = {
            "id": str(time.time_ns()),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": prompt[:120],
            "files": [],
        }

    def record(self, path: Path, rel: str, before: bytes | None, after: bytes | None) -> bool:
        """편집 한 번을 기록한다. 원본이 너무 크면 False(되돌릴 수 없음)."""
        if self.turn is None:
            self.begin_turn("")
        assert self.turn is not None
        with self._lock:
            files: list[dict[str, Any]] = self.turn["files"]
            existing = next((f for f in files if f["path"] == str(path)), None)
            if existing is not None:
                existing["after"] = _digest(after)
            else:
                if before is not None and len(before) > UNDO_MAX_FILE_BYTES:
                    return False
                files.append(
                    {
                        "path": str(path),
                        "rel": rel,
                        "before": None if before is None else base64.b64encode(before).decode("ascii"),
                        "after": _digest(after),
                    }
                )
            turns = self._load()
            if turns and turns[-1].get("id") == self.turn["id"]:
                turns[-1] = self.turn
            else:
                turns.append(self.turn)
            self._save(turns)
        return True

    def last(self) -> dict[str, Any] | None:
        turns = self._load()
        return turns[-1] if turns else None

    def conflicts(self, turn: dict[str, Any]) -> list[str]:
        """마지막 편집 뒤 다시 바뀐 파일(되돌리면 그 변경도 사라진다)."""
        changed = []
        for item in turn.get("files", []):
            path = Path(item["path"])
            current = path.read_bytes() if path.is_file() else None
            if _digest(current) != item.get("after"):
                changed.append(item.get("rel") or item["path"])
        return changed

    def undo_last(self) -> tuple[dict[str, Any] | None, list[str]]:
        """마지막 턴을 되돌린다. 반환: (턴, 처리 결과 줄 목록)."""
        with self._lock:
            turns = self._load()
            if not turns:
                return None, []
            turn = turns.pop()
            results: list[str] = []
            for item in reversed(turn.get("files", [])):
                path = Path(item["path"])
                rel = item.get("rel") or item["path"]
                before = item.get("before")
                if before is None:
                    if path.exists():
                        path.unlink()
                        results.append(f"삭제(새로 만든 파일): {rel}")
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(before))
                results.append(f"복원: {rel}")
            self._save(turns)
            if self.turn is not None and self.turn.get("id") == turn.get("id"):
                self.turn = None
            return turn, results
