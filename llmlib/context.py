"""프롬프트 맥락 보강: `@파일` 멘션 첨부 · CLAUDE.md/AGENTS.md 프로젝트 지침 수집."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

MENTION_RE = re.compile(r"(?<![\w/@])@((?:[\w.~-]|/)[^\s`'\"<>]*)")
MENTION_MAX_CHARS = 60_000
MENTION_MAX_FILES = 10
PROJECT_DOC_NAMES = ("CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
PROJECT_DOCS_MAX_CHARS = 24_000
PROJECT_DOCS_ENV = "LLM_PROJECT_DOCS"


def expand_mentions(
    prompt: str,
    root: Path,
    resolve: Callable[[Path, str], Path],
    on_file: Callable[[Path], None] | None = None,
) -> tuple[str, list[str]]:
    """`@path` 로 가리킨 파일/폴더 내용을 프롬프트 뒤에 붙인다.

    resolve 는 워크스페이스 밖·비밀 파일이면 ValueError 를 내는 함수
    (agent.resolve_in_workspace). 해석 못 한 멘션은 그대로 둔다(이메일·데코레이터 등).
    반환: (새 프롬프트, 첨부한 상대 경로 목록)
    """
    attached: list[str] = []
    blocks: list[str] = []
    budget = MENTION_MAX_CHARS
    for raw in MENTION_RE.findall(prompt):
        token = raw.rstrip(".,;:!?)]}")
        if not token or token in attached or len(attached) >= MENTION_MAX_FILES:
            continue
        try:
            path = resolve(root, token)
        except (ValueError, OSError):
            continue
        rel = os.path.relpath(path, root)
        if path.is_dir():
            try:
                names = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
            except OSError:
                continue
            listing = "\n".join(names[:200])
            blocks.append(f'<directory path="{rel}">\n{listing}\n</directory>')
            attached.append(token)
            continue
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            blocks.append(f'<file path="{rel}">(바이너리 파일: {len(data)} bytes)</file>')
            attached.append(token)
            continue
        text = data.decode("utf-8", errors="replace")
        if len(text) > budget:
            text = text[:budget] + "\n... [truncated; read 도구로 offset/limit 지정해 이어 읽기]"
        budget -= len(text)
        blocks.append(f'<file path="{rel}">\n{text}\n</file>')
        attached.append(token)
        if on_file is not None:
            on_file(path)
        if budget <= 0:
            break
    if not blocks:
        return prompt, []
    return prompt + "\n\n[첨부된 파일(@멘션)]\n" + "\n\n".join(blocks), attached


def project_doc_files(root: Path, home: Path | None = None) -> list[Path]:
    """적용할 프로젝트 지침 파일(위→아래 순). 전역(~/.claude) → 상위 폴더 → 작업 폴더."""
    if os.environ.get(PROJECT_DOCS_ENV, "").strip().lower() in {"0", "false", "no", "off"}:
        return []
    home = (home or Path.home()).resolve()
    root = root.resolve()
    found: list[Path] = []
    global_doc = home / ".claude" / "CLAUDE.md"
    if global_doc.is_file():
        found.append(global_doc)
    chain = [root, *root.parents]
    # 홈 아래 작업 폴더면 홈까지만 올라간다(그 위 시스템 폴더는 보지 않는다).
    if root == home or home in root.parents:
        chain = [p for p in chain if p == home or home in p.parents]
    else:
        chain = chain[:1]
    for folder in reversed(chain):
        for name in PROJECT_DOC_NAMES:
            path = folder / name
            if path.is_file() and path.resolve() not in {p.resolve() for p in found}:
                found.append(path)
    return found


def load_project_docs(root: Path, home: Path | None = None) -> list[tuple[Path, str]]:
    docs: list[tuple[Path, str]] = []
    budget = PROJECT_DOCS_MAX_CHARS
    for path in project_doc_files(root, home):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > budget:
            text = text[:budget] + "\n... [truncated]"
        docs.append((path, text))
        budget -= len(text)
        if budget <= 0:
            break
    return docs
