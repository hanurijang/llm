"""셸 허용/실행 로그(세션별 JSON Lines).

확인 프롬프트에서 허용한 명령(`a` 자동 허용 저장 / `all` 이번 실행 생략 /
`y` 이번만)과, 자동 허용 목록 덕분에 **프롬프트 없이 실행된** 명령(`auto`),
거부한 명령(`n`)을 한 줄씩 남긴다. `llm shell-log`(REPL `/log`)로 최근 기록을
본다. 기록 파일은 기기 로컬 정보라 대화 기록 위치와 무관하게 홈에 둔다.

파일 쓰기가 실패해도 셸 실행 자체를 막지 않는다(로그는 부가 기능).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 로그 종류와 표시 이름(표시 순서도 이 사전 순서를 따른다).
KIND_LABELS = {
    "auto": "자동 허용 실행",
    "a": "자동 허용 추가",
    "all": "이번 실행 생략",
    "y": "이번만 허용",
    "n": "거부",
}
# `llm shell-log` 기본 표시 개수
DEFAULT_VIEW_LINES = 20
# 파일이 무한정 커지지 않도록 최근 이 개수만 남긴다.
MAX_ENTRIES = 500
# 명령 문자열이 너무 길면 잘라 남긴다(로그 한 줄 = 한 명령).
MAX_CMD_CHARS = 500


def sanitize(name: str) -> str:
    """세션 이름을 파일명으로 쓸 수 있게 정리한다."""
    safe = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "_" for ch in (name or "").strip()
    )
    return safe.strip("._") or "default"


def log_path(base: Path, session: str) -> Path:
    """로그 파일 경로: `<base>/shell_log_<세션>.jsonl`."""
    return Path(base) / f"shell_log_{sanitize(session)}.jsonl"


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


class ShellLog:
    """셸 허용/실행 기록기(JSON Lines, append)."""

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled

    def record(self, kind: str, command: str, *, mode: str = "", note: str = "") -> None:
        """한 건 남긴다. 빈 명령·알 수 없는 종류·비활성 상태면 아무것도 하지 않는다."""
        kind = (kind or "").strip()
        command = " ".join((command or "").split())
        if not self.enabled or kind not in KIND_LABELS or not command:
            return
        entry: dict[str, str] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "cmd": command[:MAX_CMD_CHARS],
        }
        if mode:
            entry["mode"] = mode
        if note:
            entry["note"] = note
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.trim()
        except OSError:
            pass

    def entries(self) -> list[dict]:
        """남은 기록을 오래된 것부터 돌려준다(깨진 줄은 건너뛴다)."""
        if not self.path.is_file():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and str(item.get("cmd") or "").strip():
                out.append(item)
        return out

    def count(self) -> int:
        return len(self.entries())

    def clear(self) -> bool:
        """파일을 지운다. 지울 게 없으면 False."""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def trim(self) -> None:
        """최근 MAX_ENTRIES 개만 남긴다."""
        items = self.entries()
        if len(items) <= MAX_ENTRIES:
            return
        try:
            self.path.write_text(
                "".join(
                    json.dumps(entry, ensure_ascii=False) + "\n"
                    for entry in items[-MAX_ENTRIES:]
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


def rows(
    entries: list[dict], limit: int = DEFAULT_VIEW_LINES
) -> list[tuple[str, str, str, str]]:
    """최근 것부터 (시각, 종류, 명령, 비고) 로 돌려준다."""
    picked = entries[-limit:] if limit and limit > 0 else list(entries)
    out: list[tuple[str, str, str, str]] = []
    for item in reversed(picked):
        at = str(item.get("at") or "")
        stamp = f"{at[5:10]} {at[11:16]}".strip() if len(at) >= 16 else at
        out.append(
            (
                stamp,
                str(item.get("kind") or ""),
                str(item.get("cmd") or ""),
                str(item.get("note") or ""),
            )
        )
    return out


def tally(entries: list[dict]) -> str:
    """종류별 개수를 `자동 허용 실행 3 · 거부 1` 처럼 요약한다."""
    counts: dict[str, int] = {}
    for item in entries:
        kind = str(item.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    parts = [
        f"{kind_label(kind)} {counts[kind]}"
        for kind in KIND_LABELS
        if counts.get(kind)
    ]
    return " · ".join(parts)
