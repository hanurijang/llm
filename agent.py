#!/usr/bin/env python3
"""DeepSeek CLI agent — 파일/검색/셸 도구 루프."""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import fnmatch
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from llmlib import guard, mdrender, shelllog, web
from llmlib.bgshell import BgManager
from llmlib.context import expand_mentions, load_project_docs
from llmlib.conversation import clear_old_tool_results, finalize_interrupted
from llmlib.filestate import FileTracker, UndoStore, diff_stat, unified_diff
from llmlib.netretry import urlopen_with_retry

# 셸 안전 분류(분리 모듈)
from llmlib.shell_policy import (
    SHELL_ASK_LABELS,
    SHELL_ASK_SOURCES,
    SHELL_CHECK_WRITE_FLAGS,
    SHELL_DENY,
    SHELL_FLAG_SENSITIVE_HEADS,
    SHELL_META_PATTERN,
    SHELL_NO_PREFIX_HEADS,
    SHELL_NO_PREFIX_SUBCOMMANDS,
    SHELL_SCOPE_BLOCK_DIRS,
    SHELL_SCOPE_HEADS,
    SHELL_SCOPE_MIN_PARTS,
    SHELL_SUBCOMMAND_PROGRAMS,
    SHELL_WRITE_ARG_PATTERN,
    env_assignments_unsafe,
    intent_summary,
    is_safe_command,
    is_workspace_command,
    needs_confirm_always,
    scan_text,
    shell_words,
    split_shell_commands,
    strip_env_assignments,
)

# 실행 배너에 표시할 버전. 사용자에게 보이는 동작이 바뀌면 함께 올린다.
VERSION = "0.8.0"

# 환경변수 파일(.env) 조회 위치. 앞이 우선이고, 시스템 공용 파일은 마지막 폴백이다.
# (`LLM_ENV_FILE`을 지정하면 그 경로가 가장 먼저 읽힌다.)
LOCAL_ENV_FILE = Path(__file__).resolve().parent / ".env"
SYSTEM_ENV_FILE = Path("/etc/env/.env")

API_URL = "https://api.deepseek.com/chat/completions"
BALANCE_URL = "https://api.deepseek.com/user/balance"
HISTORY_DIR = Path.home() / ".local/share/llm"
# 대화 기록(history·summaries)을 어디에 둘지 정하는 환경변수와 기본 위치.
# 기본은 "세션 폴더 안"(`<작업폴더>/.llm/`)이라 프로젝트 저장소와 함께 push/pull 되어
# 다른 PC에서도 같은 대화를 이어갈 수 있다. `LLM_HISTORY=home`이면 예전처럼 홈에만 둔다.
# (기기 로컬 정보인 bindings.json·rates.json·shell_allow_*.json·shell_log_*.jsonl은
#  항상 홈에 남는다.)
HISTORY_SCOPE_ENV = "LLM_HISTORY"
REPO_HISTORY_DIR_NAME = ".llm"
HISTORY_SCOPE_HOME = "home"
# 실행이 끝나면 대화 기록을 자동으로 커밋·push 할지 정하는 환경변수(기본 꺼짐).
# `LLM_AUTO_SYNC=1`(true/yes/on)이면 켠다. 수동은 `llm --sync-history`.
AUTO_SYNC_ENV = "LLM_AUTO_SYNC"
# 질의 전에 원격의 최신 기록을 자동으로 받아올지 정하는 환경변수(기본 켜짐).
# `LLM_AUTO_PULL=0`(false/no/off) 또는 `--no-pull` 로 끈다(실패해도 로컬로 계속).
AUTO_PULL_ENV = "LLM_AUTO_PULL"
# 같은 폴더에서 연달아 실행할 때 매번 원격을 확인하지 않도록 두는 최소 간격(초).
AUTO_PULL_TTL_ENV = "LLM_AUTO_PULL_TTL"
AUTO_PULL_TTL_DEFAULT = 120
# 마지막으로 원격을 확인한 시각을 적어 두는 기기 로컬 파일(커밋·동기화 대상 아님).
AUTO_PULL_STAMP_FILE = HISTORY_DIR / "autopull.json"
# 실행 시 `set_history_base()`가 정한다. 기본값은 홈(폴더를 알기 전 폴백).
HISTORY_BASE = HISTORY_DIR
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
# 에이전트 루프 기본 최대 단계. 한도에 도달하면 이어서 진행할지 묻고,
# y면 같은 값만큼 한도를 늘려 계속한다.
DEFAULT_MAX_STEPS = 100
MAX_READ_BYTES = 200_000
MAX_TOOL_CHARS = 80_000
MAX_GREP_HITS = 80
MAX_RULES_CHARS = 24_000
RULES_REL = Path(".llm") / "rules.md"

# 대화 자동 요약/백업 설정
# 트리거는 "압축 대상"(최근 SUMMARY_KEEP_RECENT개를 뺀 앞부분)만 보고 판단한다.
# 최근 메시지는 어차피 남기므로 대용량 도구 출력 몇 개로 요약이 걸리지 않게 한다.
SUMMARY_TRIGGER_CHARS = 120_000
SUMMARY_TRIGGER_MESSAGES = 160
SUMMARY_KEEP_RECENT = 24
# 요약 직후에는 새 메시지가 이만큼 쌓이기 전까지 다시 요약하지 않는다(쿨다운).
SUMMARY_MIN_NEW_MESSAGES = 40
SUMMARY_MAX_CHARS = 4_000
# 실제 입력 토큰(API usage.prompt_tokens)이 이 값을 넘으면 문자 수와 무관하게 압축한다.
COMPACT_TOKENS_ENV = "LLM_COMPACT_TOKENS"
COMPACT_TOKENS_DEFAULT = 90_000
SUMMARY_DIR_NAME = "summaries"
SUMMARY_MARKER = "[이전 대화 요약]"
# 요약 시점의 메시지 수를 요약본 본문에 흔적 없이 남겨 쿨다운 판단에 쓴다.
SUMMARY_STAMP_RE = re.compile(r"<!--\s*llm-summary-at:(\d+)\s*-->")

# 다른 세션 참조(--ref): 이번 세션 대화에 1회 주입할 맥락의 상한.
REF_MARKER = "[참고: 다른 세션의 맥락]"
REF_MAX_CHARS = 24_000

# 에이전트 작업 계획(todo): 표시 이름과 화면에 그리는 항목 수 상한(초과분은 접는다).
TODO_LABEL = "📋 작업 계획"
TODO_MAX_ITEMS = 50
# 계획 상단에 보여줄 목표/조치 요약 길이와, 요약문에서 복원할 때의 안전 상한.
TODO_GOAL_MAX_CHARS = 200
TODO_RESTORE_MAX_ITEMS = 200
TODO_STATUSES = ("pending", "in_progress", "done")
# 항목 아래에 남기는 하위 작업 로그(도구 실행 등)의 화면/보관 상한과 블록 전체 줄 수.
# 화면에는 항목마다 최근 TODO_LOG_SHOW 줄만 남기고, 넘치면 오래된 로그부터 접는다.
TODO_LOG_SHOW = 6
TODO_LOG_KEEP = 40
TODO_BLOCK_MAX = 24
# 계획 블록 들여쓰기: 항목 줄과 그 항목의 하위 로그 줄.
TODO_ITEM_INDENT = "   "
TODO_LOG_INDENT = "        "
# 여러 도구를 쓰는데 계획이 없을 때 1회만 안내(차단하지 않음).
TODO_REMINDER = (
    "여러 단계 작업이면 `todo`로 goal과 짧은 계획을 보여 주면 진행이 더 잘 보입니다. "
    "한 줄짜리 질문이면 생략해도 됩니다."
)
# 작업 계획의 목표/조치 요약(계획 상단에 표시). 요약(압축)을 넘어 유지된다.
CURRENT_TODO_GOAL = ""
# 현재 실행 중인 에이전트의 작업 계획(런마다 초기화). 요약 시 스냅샷으로 보존한다.
CURRENT_TODO: list[dict[str, str]] = []
# 항목별 하위 작업 로그(항목 index → 화면에 찍은 줄). 화면 표시 전용이라 모델에는
# 보내지 않는다. 항목 내용이 같으면 계획이 갱신돼도 로그를 그대로 이어 붙인다.
TODO_LOGS: list[list[str]] = []
# 지금 그리는 단계 번호(계획 블록 머리말에 표시). 에이전트 루프가 갱신한다.
AGENT_STEP = 0
# 요약문에 실어 보내는 작업 계획 스냅샷(압축·다음 실행에서 같은 계획을 이어가기 위함).
TODO_STAMP_RE = re.compile(r"<!--\s*llm-todo:(.*?)\s*-->", re.S)
# 요약문에서 사람이 읽는 계획 블록의 머리말(이 줄 뒤로 목표·항목이 이어진다).
TODO_PLAN_HEADER = "[진행 중인 작업 계획 — 새로 만들지 말고 이 계획을 이어서 갱신하세요]"

# 폴더 이동(mv) 감지: 사라진 폴더의 세션 중 이어받을 후보를 고를 때의 상한/기준.
MOVE_CANDIDATE_LIMIT = 5
MOVE_SIMILARITY_MIN = 0.3  # 부모 경로만 같아도(+0.3) 후보로 올린다.

# 잔액 환산에 쓰는 환율.
# 계정 잔액은 화폐별로 존재할 수 있고(예: CNY, USD), 환산해 보여주는 화폐는
# 원화(KRW) 하나다. 즉 아래 표는 "1 단위 화폐 = 몇 원"인지이며,
# **실시간 환율을 못 가져왔을 때만** 쓰는 가정(고정) 환율이다.
KRW_PER_CNY = 220.0
KRW_PER_USD = 1350.0
CURRENCY_KRW_RATE = {"CNY": KRW_PER_CNY, "USD": KRW_PER_USD}
CURRENCY_LABELS = {"CNY": "위안화", "USD": "미국달러"}
# 환산해 함께 보여줄 표시 화폐(원화)의 이름/코드.
DISPLAY_CURRENCY_LABEL = "원화"
DISPLAY_CURRENCY = "KRW"
# 실시간 환율(참고용): 공개 환율 API 에서 1 USD 기준 전 화폐 시세를 받아
# "1 화폐 = 몇 원"으로 바꾼다. 실패하거나 `--no-rates`/`LLM_RATES=0` 이면
# 위 가정 환율로 되돌아간다. 결과는 RATE_CACHE_TTL 초 동안 파일로 캐시한다.
RATE_URL = "https://open.er-api.com/v6/latest/USD"
RATE_SOURCE = "open.er-api.com (exchangerate-api)"
RATE_CACHE_FILE = HISTORY_DIR / "rates.json"
RATE_CACHE_TTL = 12 * 3600
RATE_CACHE_VERSION = 1
IGNORE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".cursor",
    ".mypy_cache",
    ".pytest_cache",
}
SECRET_NAMES = guard.SECRET_NAMES  # 비밀 파일 이름(llmlib/guard.py)


def color_enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


class C:
    reset = dim = cyan = green = yellow = red = bold = magenta = ""


def init_color() -> None:
    if not color_enabled():
        return
    C.reset = "\033[0m"
    C.dim = "\033[2m"
    C.cyan = "\033[36m"
    C.green = "\033[32m"
    C.yellow = "\033[33m"
    C.red = "\033[31m"
    C.bold = "\033[1m"
    C.magenta = "\033[35m"


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def terminal_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except OSError:
        return 80


def terminal_height() -> int:
    try:
        return shutil.get_terminal_size().lines
    except OSError:
        return 24


RULE_WIDTH = 60


def char_width(ch: str) -> int:
    """터미널 표시 폭: 동아시아 wide/전각 문자는 2, 그 외는 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


ANSI_SEQ_RE = re.compile(r"\033\[[0-9;]*m")


def visible_width(text: str) -> int:
    """색 코드를 뺀 표시 폭."""
    return display_width(ANSI_SEQ_RE.sub("", text))


def take_width(text: str, width: int) -> str:
    """표시 폭이 width 를 넘지 않는 최대 앞부분."""
    used = 0
    for i, ch in enumerate(text):
        w = char_width(ch)
        if used + w > width:
            return text[:i]
        used += w
    return text


def clip_ansi(text: str, width: int, ellipsis: str = "…") -> str:
    """색 코드는 남기고 표시 폭 기준으로 자른다(한 논리 줄 = 한 물리 줄)."""
    if visible_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    idx = 0
    while idx < len(text):
        seq = ANSI_SEQ_RE.match(text, idx)
        if seq:  # 색 코드는 폭을 차지하지 않는다
            out.append(seq.group())
            idx = seq.end()
            continue
        ch = text[idx]
        w = char_width(ch)
        if used + w > max(0, width - display_width(ellipsis)):
            out.append(ellipsis)
            break
        out.append(ch)
        used += w
        idx += 1
    if out and ANSI_SEQ_RE.search(text):
        out.append(C.reset)
    return "".join(out)


def physical_rows(text: str) -> int:
    """터미널에서 이 줄이 차지하는 물리 줄 수(자동 줄바꿈 포함)."""
    cols = max(1, terminal_width())
    return max(1, (visible_width(text) + cols - 1) // cols)


def wrap_text(text: str, width: int) -> list[str]:
    """공백 기준으로 표시 폭에 맞춰 줄바꿈한다(긴 단어는 잘라서 넘긴다)."""
    width = max(8, width)
    lines: list[str] = []
    for para in str(text or "").split("\n"):
        cur = ""
        for word in para.split():
            if cur and display_width(cur) + 1 + display_width(word) > width:
                lines.append(cur)
                cur = ""
            cur = f"{cur} {word}".strip()
            while display_width(cur) > width:
                head = take_width(cur, width)
                lines.append(head)
                cur = cur[len(head):]
        lines.append(cur)
    return lines or [""]


def pad(text: str, width: int) -> str:
    """표시 폭 기준으로 오른쪽을 공백으로 채운다."""
    return text + " " * max(0, width - display_width(text))


def duration_text(seconds: float) -> str:
    """경과 시간을 `45s` / `1m 05s` / `1h 02m`처럼 짧게 적는다."""
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest:02d}m"


def rule(char: str = "─", label: str | None = None) -> str:
    """가로 구분선. label 이 있으면 중앙에 표시한다."""
    width = max(40, min(terminal_width(), 100))
    if not label:
        return char * width
    text = f" {label} "
    text_w = display_width(text)
    side = max(0, (width - text_w) // 2)
    right = max(0, width - text_w - side)
    return char * side + text + char * right


class StepPanel:
    """진행 상황(작업 계획 블록 + 도구 줄)을 **같은 자리에서** 갱신해 보여주는 패널.

    작업 계획이 있으면 `block`(목표 + 번호 매긴 항목 + 항목별 하위 작업 로그)을
    그리고, 도구 줄은 진행 중인 항목의 하위 로그로 쌓인다. 그릴 때마다 이전 그림을
    지우고 다시 그리므로 화면이 쌓이지 않고, 계획이 위에서 아래로 차례로 진행되는
    모습이 한자리에서 보인다.

    TTY 가 아니거나 `LLM_RAW` 가 설정되면 비활성화되고, 블록에서 **바뀐 줄만**
    새 줄로 출력해 로그로 남긴다(순서대로 내려가는 기록).
    """

    def __init__(self) -> None:
        self.enabled = bool(sys.stdout.isatty() and not os.environ.get("LLM_RAW"))
        self.header = ""             # 단계 머리말(블록이 없고 도구 줄이 있을 때만)
        self.block: list[str] = []   # 항상 그리는 여러 줄 블록(작업 계획 등)
        self.lines: list[str] = []   # 블록 아래에 붙는 도구 줄(블록이 없을 때)
        self.drawn: list[str] = []   # 화면에 실제로 그려 둔 줄
        self.printed: list[str] = []  # 마지막으로 출력한 블록(바뀐 줄만 찍기 위한 기록)
        self.static = False          # 화면보다 큰 그림: 자리 갱신 대신 바뀐 줄만 찍는다

    def begin(self, header: str = "") -> None:
        """새 단계 시작: 단계 도구 줄을 비운다(계획 블록은 그대로 유지)."""
        self.suspend()
        self.header = header
        self.lines = []

    def suspend(self) -> None:
        """다른 출력(프롬프트/안내)이 끼어들기 전에 그림을 지운다."""
        if not self.drawn:
            return
        self._erase()
        self.drawn = []

    def close(self) -> None:
        """에이전트 종료 시 그림을 지운다(계획 블록도 화면에서 사라진다)."""
        self.suspend()

    def freeze(self) -> None:
        """그림을 화면에 남긴 채 관리에서 뺀다(최종 답변 직전 계획을 보존).

        이미 화면에 그려져 있으므로 다시 그리지 않는다(다시 그리면 아래에 복사된다).
        """
        self.drawn = []
        self.block = []
        self.lines = []

    def set_block(self, lines: list[str]) -> None:
        """여러 줄 블록(작업 계획)을 설정하고 다시 그린다."""
        if not self.enabled:
            self._print_diff(lines)
            return
        if lines == self.block and self.drawn:
            return
        self.block = list(lines)
        self._flush()

    def redraw(self) -> None:
        """지워진 그림(블록/도구 줄)을 현재 커서 위치에 다시 그린다."""
        if self.enabled and (self.block or self.lines):
            self._flush()

    def clear_block(self) -> None:
        """블록을 비운다(계획이 없어지거나 종료할 때)."""
        if self.block:
            self.block = []
            self._flush()

    def tool_open(self, text: str) -> int:
        """도구 호출 줄을 추가한다(실행 전). 반환: 이번 단계에서의 줄 인덱스."""
        idx = len(self.lines)
        if self.enabled:
            self.lines.append(text)
            self._flush()
        else:
            print(text, flush=True)
        return idx

    def tool_done(self, idx: int, text: str) -> None:
        """실행 결과(✓/✗)로 도구 줄을 갱신한다."""
        if self.enabled:
            if 0 <= idx < len(self.lines):
                self.lines[idx] = f"{self.lines[idx]}  {text}"
            self._flush()
        else:
            print(f"      {text}", flush=True)

    def _erase(self) -> None:
        rows = self._rows(self.drawn)
        if rows <= 0:
            return
        sys.stdout.write(f"\033[{rows}A\033[J")
        sys.stdout.flush()

    def _write(self, lines: list[str]) -> None:
        clipped = [clip_ansi(line, terminal_width()) for line in lines]
        sys.stdout.write("\n".join(clipped) + "\n")
        sys.stdout.flush()

    def _flush(self) -> None:
        drawn = list(self.block)
        if not self.block and self.header and self.lines:
            drawn.append(self.header)
        drawn += self.lines
        if not self.static and drawn == self.drawn:
            return
        if not self.static and self._rows(drawn) >= max(4, terminal_height() - 2):
            # 화면보다 큰 그림은 자리 갱신(위로 올려 지우기)이 어긋날 수 있다.
            # 이때부터는 바뀐 줄만 새 줄로 찍어 기록으로 남긴다.
            self.static = True
            self.drawn = []
            self.printed = []
        if self.static:
            self.drawn = []
            self._print_diff(drawn)
            return
        if self.drawn:
            self._erase()
        self.drawn = drawn
        if drawn:
            self._write(drawn)

    def _rows(self, lines: list[str]) -> int:
        """물리 줄 수(터미널 폭을 넘는 줄은 자동 줄바꿈으로 한 줄 더 차지)."""
        return sum(physical_rows(line) for line in lines) if lines else 0

    def _print_diff(self, lines: list[str]) -> None:
        """이전 블록과 달라진 줄(새로 생긴/바뀐 줄)만 새 줄로 출력한다.

        TTY 가 아니거나 그림이 화면보다 커져 자리 갱신을 포기한 경우에 쓴다.
        """
        if not self.printed:
            new_lines = list(lines)
        else:
            matcher = difflib.SequenceMatcher(None, self.printed, lines, autojunk=False)
            new_lines = [
                line
                for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
                if tag in ("insert", "replace")
                for line in lines[j1:j2]
            ]
        self.printed = list(lines)
        for line in new_lines:
            print(line, flush=True)


PANEL = StepPanel()


def suspend_panel() -> None:
    """패널이 떠 있으면 지운다(다른 출력이 끼어들 때 호출)."""
    PANEL.suspend()


def resume_panel() -> None:
    """`suspend_panel` 로 지운 계획 블록을 새 출력 아래에 다시 그린다."""
    if PANEL.block or PANEL.lines:
        PANEL.redraw()


def print_title(label: str) -> None:
    suspend_panel()
    line = rule("─", label)
    print(f"{C.bold}{C.cyan}{line}{C.reset}")
    resume_panel()


def print_section(label: str) -> None:
    suspend_panel()
    print()
    print(f"{C.bold}{label}{C.reset}")
    print(f"{C.dim}{rule()}{C.reset}")
    resume_panel()


def print_info(message: str) -> None:
    suspend_panel()
    print(f"{C.cyan}ℹ️  {message}{C.reset}")
    resume_panel()


def print_ok(message: str) -> None:
    suspend_panel()
    print(f"{C.green}✅ {message}{C.reset}")
    resume_panel()


def print_warn(message: str, stream: bool = False) -> None:
    suspend_panel()
    out = eprint if stream else print
    out(f"{C.yellow}⚠️  {message}{C.reset}")
    resume_panel()


def print_error(message: str, stream: bool = True) -> None:
    suspend_panel()
    out = eprint if stream else print
    out(f"{C.red}❌ {message}{C.reset}")
    resume_panel()


def render_markdown(text: str, raw: bool = False) -> None:
    """TTY에서는 내장 렌더러(llmlib.mdrender)로, 아니면 원문 그대로 출력한다.

    외부 도구(glow) 없이 헤딩·목록·코드블록·인용·표·인라인 강조를 정리해
    보여준다. 파이프/리다이렉트(`LLM_RAW` 포함)면 손대지 않은 원문을 출력해
    로그·자동화에서 결과가 왜곡되지 않게 한다.
    """
    if not text:
        return

    if raw or not sys.stdout.isatty() or os.environ.get("LLM_RAW"):
        print(text)
        return

    width = max(40, min(terminal_width(), 120))
    print(mdrender.render(text, width=width, color=color_enabled()))


def clear_status() -> None:
    if sys.stderr.isatty():
        eprint("\r\033[K", end="")


def make_stream_progress(show_thinking: bool = False) -> Callable[[str, str], None]:
    """스트리밍 진행 상황을 stderr에 표시한다.

    종류(kind)는 `reasoning` | `content` | `end`.
    - `reasoning`: 사고 과정(reasoning) 진행 상황. show_thinking 이면 원문을
      회색으로 실시간 출력하고, 기본값(False)에서는 글자 수만 한 줄로 보여준다.
    - `content`: 최종 답변은 완료 후 한 번만 렌더링하므로 글자 수만 표시.
    - `end`: 열려 있는 사고 블록/상태 줄을 정리한다.
    """
    width = max(24, terminal_width() - 1)
    indent = "   "
    state: dict[str, Any] = {
        "reasoning": 0,
        "content": 0,
        "col": 0,         # 현재 줄의 표시 폭
        "status": False,  # 상태(카운터) 한 줄이 떠 있는지
        "dim": False,     # 회색 사고 블록이 열려 있는지
        "header": False,  # 사고 헤더(🧠)를 이미 출력했는지
    }

    def end_line() -> None:
        eprint("", flush=True)
        state["col"] = 0

    def close_status() -> None:
        if state["status"]:
            eprint("\r\033[K", end="", flush=True)
            state["status"] = False

    def close_thinking() -> None:
        if state["col"]:
            end_line()
        if state["dim"]:
            eprint(C.reset, end="", flush=True)
            state["dim"] = False

    def write_thinking(text: str) -> None:
        buf: list[str] = []

        def spill() -> None:
            if buf:
                eprint("".join(buf), end="")
                buf.clear()

        for ch in text:
            if ch == "\n":
                spill()
                end_line()
                continue
            w = char_width(ch)
            if state["col"] + w > width:
                spill()
                end_line()
            if state["col"] == 0:
                if state["header"]:
                    buf.append(indent)
                    state["col"] = len(indent)
                else:
                    state["header"] = True
                    state["dim"] = True
                    buf.append(f"{C.dim}🧠 ")
                    state["col"] = display_width("🧠 ")
            buf.append(ch)
            state["col"] += w
        spill()
        eprint("", end="", flush=True)

    def report(kind: str, text: str) -> None:
        if not sys.stderr.isatty():
            return
        if kind == "end":
            close_thinking()
            close_status()
            return
        if kind == "content":
            state["content"] += len(text)
            close_thinking()
            eprint(
                f"\r\033[K{C.dim}✍️  작성 중… {state['content']}자{C.reset}",
                end="",
                flush=True,
            )
            state["status"] = True
            return
        state["reasoning"] += len(text)
        if show_thinking:
            close_status()
            write_thinking(text)
            return
        close_thinking()
        eprint(
            f"\r\033[K{C.dim}🧠 사고 중… {state['reasoning']}자{C.reset}",
            end="",
            flush=True,
        )
        state["status"] = True

    return report


def load_dotenv(*paths: Path) -> list[Path]:
    """환경변수 파일(.env)을 앞에서부터 순서대로 읽는다.

    조회 순서: `LLM_ENV_FILE`(지정 경로) → 저장소 폴더의 `.env` →
    시스템 공용 `/etc/env/.env`. 이미 설정된 환경변수는 덮어쓰지 않으므로
    **앞의 파일이 우선**한다(같은 키가 뒤 파일에 있어도 앞 값 유지).
    실제로 읽은 파일 목록을 돌려준다(경로만, 값은 반환하지 않음).
    """
    candidates: list[Path] = []
    override = os.environ.get("LLM_ENV_FILE", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend(paths or (LOCAL_ENV_FILE, SYSTEM_ENV_FILE))

    loaded: list[Path] = []
    seen: set[Path] = set()
    for cand in candidates:
        try:
            path = cand.resolve()
        except OSError:
            path = cand
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val
        loaded.append(path)
    return loaded


def env_flag(name: str) -> bool:
    """환경변수를 불리언으로 읽는다(1/true/yes/on/y 만 참)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on", "y"}


def api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        eprint(
            "DEEPSEEK_API_KEY가 없습니다. .env(저장소 폴더), /etc/env/.env "
            "또는 환경변수를 확인하세요."
        )
        sys.exit(1)
    return key


def history_scope_dir(root: Path) -> Path:
    """대화 기록(history·summaries)을 둘 폴더를 정한다.

    기본은 세션 폴더 안(`<작업폴더>/.llm`) — 프로젝트 저장소와 함께 push/pull 되어
    다른 PC에서도 대화를 이어갈 수 있다. `LLM_HISTORY=home`이면 홈(예전 방식).
    """
    mode = os.environ.get(HISTORY_SCOPE_ENV, "").strip().lower()
    if mode in {HISTORY_SCOPE_HOME, "local"}:
        return HISTORY_DIR
    return root / REPO_HISTORY_DIR_NAME


def set_history_base(path: Path) -> None:
    """이번 실행에서 쓸 기록 폴더를 확정한다(main 에서 폴더가 정해진 뒤 호출)."""
    global HISTORY_BASE
    HISTORY_BASE = path
    HISTORY_BASE.mkdir(parents=True, exist_ok=True)


def history_scope_is_repo() -> bool:
    return HISTORY_BASE != HISTORY_DIR


def _history_len(path: Path) -> int:
    """기록 파일의 메시지 수(읽을 수 없으면 -1)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return -1
    return len(data) if isinstance(data, list) else -1


def migrate_legacy_session(session: str) -> Path | None:
    """예전 홈 기록을 저장소 폴더로 1회 옮긴다(복사 후 홈 파일은 `.migrated`로 개명).

    이미 옮긴 세션은 다시 복사하지 않는다(지운 기록이 되살아나지 않도록).
    다만 옮기기 전에 시작해 둔 실행이 마지막 턴을 예전 경로에 저장했을 수 있으므로,
    예전 파일이 더 길면 그 내용을 반영한다(기록은 덧붙이기만 하므로 긴 쪽이 최신).
    """
    if not history_scope_is_repo():
        return None
    src = HISTORY_DIR / f"history_{session}.json"
    if not src.is_file():
        return None
    dst = HISTORY_BASE / f"history_{session}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy2(src, dst)
        # 홈 요약 백업도 함께 복사(있으면).
        src_sum = HISTORY_DIR / SUMMARY_DIR_NAME / sanitize_session_name(session)
        dst_sum = HISTORY_BASE / SUMMARY_DIR_NAME / sanitize_session_name(session)
        if src_sum.is_dir() and not dst_sum.exists():
            shutil.copytree(src_sum, dst_sum)
        print_info(
            f"[{session}] 대화 기록을 저장소 폴더로 옮겼습니다(복사): {dst}"
        )
        print_info(
            "이제 이 폴더를 커밋·push 하면 다른 PC에서도 대화가 이어집니다"
            " (llm --sync-history)."
        )
    elif _history_len(src) > _history_len(dst):
        shutil.copy2(src, dst)
        print_info(f"[{session}] 옮기기 전 실행이 남긴 최신 기록을 반영했습니다: {dst}")
    try:
        src.rename(src.with_name(src.name + ".migrated"))
    except OSError:
        pass
    return dst


def history_file_for(session: str) -> Path:
    if not history_scope_is_repo():
        # LLM_HISTORY=home 폴백: .llm 으로 옮기며 `*.migrated` 로 남긴 기록을 되살린다.
        home = HISTORY_DIR / f"history_{session}.json"
        legacy = home.with_name(home.name + ".migrated")
        if not home.exists() and legacy.is_file():
            try:
                shutil.copy2(legacy, home)
                print_info(f"[{session}] 옮겨 두었던 예전 홈 기록을 다시 꺼내 왔습니다: {home}")
            except OSError:
                pass
    HISTORY_BASE.mkdir(parents=True, exist_ok=True)
    migrate_legacy_session(session)
    return HISTORY_BASE / f"history_{session}.json"


def history_file_of_folder(folder: Path | str, session: str) -> Path:
    """다른 세션(다른 폴더에 묶인)의 기록 파일 경로를 추정한다(--ref 등)."""
    mode = os.environ.get(HISTORY_SCOPE_ENV, "").strip().lower()
    if mode in {HISTORY_SCOPE_HOME, "local"}:
        return HISTORY_DIR / f"history_{session}.json"
    return Path(folder) / REPO_HISTORY_DIR_NAME / f"history_{session}.json"


def find_history_file(session: str, folders: dict[str, str] | None = None) -> Path:
    """세션 기록 파일을 찾는다: 현재 기록 폴더 → 소유 폴더 → 홈(예전 기록).

    남의 세션을 찾을 때는 복사(마이그레이션)를 하지 않는다.
    """
    if folders is None:
        folders = load_bindings()
    candidates: list[Path] = []
    if history_scope_is_repo():
        candidates.append(HISTORY_BASE / f"history_{session}.json")
    owner = folder_for_session(folders, session)
    if owner:
        candidates.append(history_file_of_folder(owner, session))
    candidates.append(HISTORY_DIR / f"history_{session}.json")
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return candidates[0]


def canonical_folder(path: Path | str) -> str:
    """폴더 경로를 비교용 정규 키로 만든다(심볼릭 링크 해소·구분자/대소문자 정리).

    세션 바인딩의 진실은 이 키 하나뿐이다. 원본 문자열은 표시용으로만 쓴다.
    """
    p = os.path.realpath(os.fspath(path))
    if len(p) > 1:
        p = p.rstrip("/\\") or os.sep
    if sys.platform == "darwin" or os.name == "nt":
        p = p.lower()
    if os.name == "nt" and len(p) > 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def bindings_file() -> Path:
    return HISTORY_DIR / "bindings.json"


def load_bindings() -> dict[str, str]:
    # 폴더가 사라진(고아) 바인딩은 제외한다. 예: 프로젝트 폴더를 mv/rm 한 뒤.
    # mv 로 옮긴 경우에는 orphan_bindings() 로 후보를 찾아 이어받을 수 있고,
    # 매핑을 저장할 때 파일에서도 함께 정리된다.
    return {
        path: session
        for path, session in read_bindings_raw().items()
        if os.path.isdir(path)
    }


def read_bindings_raw() -> dict[str, str]:
    """bindings.json 의 폴더→세션 매핑(존재 여부 필터 없음, 경로 정규화만)."""
    path = bindings_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    folders = data.get("folders")
    if not isinstance(folders, dict):
        return {}
    raw: dict[str, str] = {}
    for path_str, session in folders.items():
        if not path_str or not session:
            continue
        raw[canonical_folder(str(path_str))] = str(session)
    return raw


def orphan_bindings() -> dict[str, str]:
    """폴더가 사라진(고아) 매핑. mv 로 옮긴 폴더의 세션 후보를 찾는 데 쓴다."""
    return {
        path: session
        for path, session in read_bindings_raw().items()
        if not os.path.isdir(path)
    }


def save_bindings(folders: dict[str, str]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    normalized = {canonical_folder(path): session for path, session in folders.items()}
    path = bindings_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"folders": normalized}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def sanitize_session_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-가-힣]", "_", name.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("._") or "workspace"
    return cleaned[:80]


def auto_session_name(root: Path, folders: dict[str, str]) -> str:
    root_s = canonical_folder(root)
    parts = [p for p in Path(root_s).parts if p not in {"/", ""}]
    slugs: list[str] = []
    for i in range(1, min(4, len(parts) + 1)):
        slug = sanitize_session_name("_".join(parts[-i:]))
        if slug and slug not in slugs:
            slugs.append(slug)
    if not slugs:
        slugs = ["workspace"]
    for name in slugs:
        owner = folder_for_session(folders, name)
        if owner is None or owner == root_s:
            return name
    digest = hashlib.sha1(root_s.encode("utf-8")).hexdigest()[:6]
    return f"{slugs[0]}_{digest}"


def ensure_binding(
    folders: dict[str, str], root: Path, session: str
) -> tuple[dict[str, str], str | None, str | None]:
    """이 폴더를 세션에 바인딩한다(이 폴더의 기존 매핑은 덮어쓴다).

    - 이미 다른 폴더가 소유한 세션은 끌어오지 않는다(남의 소유를 존중).
    - 이 폴더가 다른 세션에 묶여 있던 경우에는 조용히 갈아탄다. 이전 세션의
      대화 기록은 그대로 남고, 매핑만 풀린다.

    반환: (folders, error, detail)
      - error="session_bound_elsewhere" → detail=세션을 소유한 폴더 경로
    """
    folder_s = canonical_folder(root)
    owner = folder_for_session(folders, session)  # 세션을 소유한 폴더
    if owner is not None and owner != folder_s:
        return folders, "session_bound_elsewhere", owner
    folders = {p: s for p, s in folders.items() if s != session and p != folder_s}
    folders[folder_s] = session
    save_bindings(folders)
    return folders, None, None


def unbind_session(folders: dict[str, str], session: str) -> dict[str, str]:
    folders = {canonical_folder(path): sess for path, sess in folders.items() if sess != session}
    save_bindings(folders)
    return folders


def folder_for_session(folders: dict[str, str], session: str) -> str | None:
    return next((path for path, sess in folders.items() if sess == session), None)


def move_candidate_score(root: Path, old_path: str) -> float:
    """'이 폴더가 old_path 에서 mv 로 왔을 가능성' 점수(0~1 이상).

    폴더 이름 유사도가 기본이고, 이름/부모 경로가 같으면 가산한다.
    """
    root_s = canonical_folder(root)
    root_base = os.path.basename(root_s)
    old_base = os.path.basename(old_path)
    score = difflib.SequenceMatcher(None, root_base, old_base).ratio()
    if root_base and root_base == old_base:
        score += 0.5
    if os.path.dirname(root_s) == os.path.dirname(old_path):
        score += 0.3
    return score


def find_move_candidates(root: Path) -> list[tuple[str, str]]:
    """mv 로 옮겨온 것으로 보이는 (세션, 이전 경로) 후보를 점수순으로 돌려준다."""
    scored: list[tuple[float, str, str]] = []
    for old_path, session in orphan_bindings().items():
        score = move_candidate_score(root, old_path)
        if score >= MOVE_SIMILARITY_MIN:
            scored.append((score, session, old_path))
    scored.sort(key=lambda t: (-t[0], t[2]))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for _, session, old_path in scored:
        if session in seen:
            continue
        seen.add(session)
        out.append((session, old_path))
    return out[:MOVE_CANDIDATE_LIMIT]


def print_move_candidates(candidates: list[tuple[str, str]], root: Path) -> None:
    """mv 이어받기 후보 `(세션, 이전 경로)`를 번호 목록으로 보여준다."""
    root_base = os.path.basename(canonical_folder(root))
    for idx, (session, old_path) in enumerate(candidates, 1):
        hint = f" {C.dim}(이름 일치){C.reset}" if os.path.basename(old_path) == root_base else ""
        print(f"   {C.bold}{idx}){C.reset} {C.cyan}{session}{C.reset}  {C.dim}← {old_path}{C.reset}{hint}")


def hint_move_candidates(root: Path) -> int:
    """mv 로 옮겨온 것으로 보이는 세션 후보를 **안내만** 한다(묻지 않음).

    TTY 여부와 무관하게 쓸 수 있다. 후보가 없으면 아무것도 하지 않고 0.
    반환: 보여준 후보 개수.
    """
    candidates = find_move_candidates(root)
    if not candidates:
        return 0
    print()
    print_info("사라진 폴더에 남은 세션 후보입니다 (폴더를 옮기셨나요?):")
    print_move_candidates(candidates, root)
    names = ", ".join(session for session, _ in candidates)
    print_info(f"이어받으려면  llm -s <이름>  {C.dim}(예: {names}){C.reset}")
    return len(candidates)


def apply_move_binding(
    folders: dict[str, str], root: Path, session: str
) -> tuple[dict[str, str], bool]:
    """이어받기로 고른 세션을 이 폴더에 바인딩하고 이후 안내를 출력한다.

    반환: (folders, 이어받았는지). 실패하면 폴더는 그대로이고 False.
    """
    folders, err, _ = ensure_binding(folders, root, session)
    if err:
        print_warn(f"이전 세션 '{session}'을(를) 이어받지 못했습니다: {err}")
        return folders, False
    print_ok(f"이전 세션 '{session}'을(를) 이어받았습니다.")
    print_info(
        f"이후 이 폴더는 세션 '{session}'(으)로 이어집니다"
        f" {C.dim}(다시 지정하려면  llm -s <이름>){C.reset}"
    )
    return folders, True


def prompt_move_binding(root: Path, new_session: str | None = None) -> str | None:
    """폴더 이동(mv)으로 보이는 세션 후보를 보여주고 이어받을지 확인한다.

    반환: 이어받을 세션 이름, 또는 None(이어받지 않음/묻지 못함).
    - `new_session` 이 주어지면 Enter = 새 세션 '<이름>'(대화를 시작하는 실행).
    - None 이면 Enter = 취소(조회/정리처럼 새 세션을 만들지 않는 실행).
    TTY 가 아니면(자동화/파이프) 묻지 않고 None.
    """
    candidates = find_move_candidates(root)
    if not candidates or not sys.stdin.isatty():
        return None

    print()
    print_info("이 폴더는 아직 세션에 연결되어 있지 않습니다.")
    print_info("사라진 폴더에 남은 세션 후보입니다 (폴더를 옮기셨나요?):")
    print_move_candidates(candidates, root)

    if new_session:
        tail = f"{C.dim}Enter=새 세션 '{new_session}'{C.reset}"
        cancel_txt = f"새 세션 '{new_session}'"
    else:
        tail = f"{C.dim}Enter=취소{C.reset}"
        cancel_txt = "취소"
    prompt_txt = (
        f"{C.yellow}❓ 이어받을 번호{C.reset} "
        f"{C.bold}[1-{len(candidates)}]{C.reset} / {tail}: "
    )
    while True:
        try:
            answer = input(prompt_txt).strip()
        except (EOFError, KeyboardInterrupt):
            eprint()
            return None
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1][0]
        print_warn(f"1~{len(candidates)} 사이 번호를 입력하거나 Enter 로 {cancel_txt} 하세요.")


def summaries_dir(session: str) -> Path:
    migrate_legacy_session(session)
    return HISTORY_BASE / SUMMARY_DIR_NAME / sanitize_session_name(session)


def history_chars(messages: list[dict[str, Any]]) -> int:
    """대화 전체의 대략적인 문자 수(도구 결과 포함)."""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        total += len(content)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += len(fn.get("arguments") or "") + len(fn.get("name") or "")
    return total


def last_summary_count(messages: list[dict[str, Any]]) -> int | None:
    """마지막 요약 시점의 메시지 수를 요약본에서 읽는다(없으면 None).

    재요약 과정에서 이전 스탬프가 요약문에 섞여 들어올 수 있으므로 가장
    마지막 값을 사용한다.
    """
    if not messages:
        return None
    content = messages[0].get("content") or ""
    matches = SUMMARY_STAMP_RE.findall(content)
    return int(matches[-1]) if matches else None


def compact_token_limit() -> int:
    raw = os.environ.get(COMPACT_TOKENS_ENV, "").strip()
    try:
        return max(10_000, int(raw)) if raw else COMPACT_TOKENS_DEFAULT
    except ValueError:
        return COMPACT_TOKENS_DEFAULT


def should_summarize(messages: list[dict[str, Any]]) -> bool:
    """압축 대상(최근 SUMMARY_KEEP_RECENT개 제외)이 임계를 넘으면 True.

    - 최근 메시지는 어차피 유지되므로 트리거 계산에서 제외한다.
    - 직전 요약 이후 새 메시지가 SUMMARY_MIN_NEW_MESSAGES개 미만이면 쉰다.
    """
    if len(messages) <= SUMMARY_KEEP_RECENT:
        return False

    head = messages[:-SUMMARY_KEEP_RECENT]
    # 직전 요약본 하나만 남아 있고 새 대화가 없는 상태(구버전 기록 대비)
    if len(head) == 1 and SUMMARY_MARKER in (head[0].get("content") or ""):
        return False

    # 실제 토큰이 한도에 다가가면(컨텍스트 초과 직전) 다른 조건보다 우선한다.
    if LAST_PROMPT_TOKENS >= compact_token_limit():
        return True

    last = last_summary_count(messages)
    if last is not None and len(messages) - last < SUMMARY_MIN_NEW_MESSAGES:
        return False

    if len(head) > SUMMARY_TRIGGER_MESSAGES:
        return True
    return history_chars(head) > SUMMARY_TRIGGER_CHARS


def summarize_prompt(messages: list[dict[str, Any]]) -> str:
    parts = [
        "다음은 사용자와 코딩 에이전트의 대화 기록입니다.",
        "이 대화를 한국어로 간결하게 요약하세요.",
        "반드시 포함할 것:",
        "- 사용자의 목표/요청",
        "- 결정된 사항과 이유",
        "- 생성/수정된 파일과 핵심 코드 변경",
        "- 미해결 과제(TODO)",
        "군더더기 없이 마크다운 불릿으로 정리하고, 요약만 출력하세요.",
        "",
        "=== 대화 시작 ===",
    ]
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role == "user" and content:
            parts.append(f"[사용자] {content}")
        elif role == "assistant":
            calls = msg.get("tool_calls") or []
            if content:
                parts.append(f"[에이전트] {content}")
            for tc in calls:
                fn = tc.get("function") or {}
                parts.append(f"[도구 호출] {fn.get('name', '?')} {fn.get('arguments', '')}")
        elif role == "tool" and content:
            preview = content if len(content) < 800 else content[:800] + "…"
            parts.append(f"[도구 결과] {preview}")
    parts.append("=== 대화 끝 ===")
    return "\n".join(parts)


def summarize_messages(messages: list[dict[str, Any]], model: str) -> str | None:
    """모델에 요약을 요청한다. 실패하면 None."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise summarizer. Summarize the conversation in Korean "
                    "with concise markdown bullets. Output only the summary."
                ),
            },
            {"role": "user", "content": summarize_prompt(messages)},
        ],
        "stream": False,
    }
    data = http_json(API_URL, payload)
    if data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(msg)
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    return content.strip() or None


def backup_conversation(
    session: str,
    messages: list[dict[str, Any]],
    summary: str,
    model: str,
) -> dict[str, Any]:
    """요약본(md)과 원본 스냅샷(json)을 세션 폴더에 누적 저장한다."""
    folder = summaries_dir(session)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    base = stamp
    n = 1
    while (folder / f"{base}.md").exists():
        n += 1
        base = f"{stamp}_{n}"

    md_path = folder / f"{base}.md"
    json_path = folder / f"{base}.json"
    chars = history_chars(messages)
    header = (
        f"# 대화 요약 — {session}\n\n"
        f"- 생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 메시지 수: {len(messages)}\n"
        f"- 문자 수: {chars:,}\n"
        f"- 모델: {model}\n\n"
        f"## 요약\n\n{summary}\n"
    )
    md_path.write_text(header, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "session": session,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "message_count": len(messages),
                "chars": chars,
                "model": model,
                "summary": summary,
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    entry = {
        "id": base,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "message_count": len(messages),
        "chars": chars,
        "summary_file": md_path.name,
        "backup_file": json_path.name,
        "summary": summary,
    }
    index_path = folder / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index, dict):
            index = {}
    except (OSError, json.JSONDecodeError):
        index = {}
    entries = index.get("entries")
    if not isinstance(entries, list):
        entries = []
    entries.append(entry)
    index["entries"] = entries
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return entry

def keep_recent_complete_messages(
    messages: list[dict[str, Any]],
    keep: int,
) -> list[dict[str, Any]]:
    """최근 메시지를 유지하되 Tool Calling 묶음을 절대 분리하지 않는다.

    assistant(tool_calls) + 연속된 tool 결과들은 하나의 묶음으로 취급한다.
    """

    if keep <= 0:
        return []

    if len(messages) <= keep:
        return list(messages)

    start = max(0, len(messages) - keep)

    # --------------------------------------------------
    # 최근 유지 영역이 tool 메시지 중간에서 시작하는 경우
    # 대응하는 assistant(tool_calls)까지 시작점을 뒤로 이동
    # --------------------------------------------------

    if messages[start].get("role") == "tool":

        # 현재 tool 묶음의 시작점 찾기
        tool_start = start

        while (
            tool_start > 0
            and messages[tool_start - 1].get("role") == "tool"
        ):
            tool_start -= 1

        # tool 묶음 바로 앞 assistant가 tool_calls를 가지고 있으면
        # assistant까지 포함
        assistant_index = tool_start - 1

        if (
            assistant_index >= 0
            and messages[assistant_index].get("role") == "assistant"
            and messages[assistant_index].get("tool_calls")
        ):
            start = assistant_index

        else:
            # 구조가 이상하면 현재 tool 묶음 전체를 버리고
            # 다음 일반 메시지부터 시작
            start = tool_start

            while (
                start < len(messages)
                and messages[start].get("role") == "tool"
            ):
                start += 1

    # --------------------------------------------------
    # 시작점이 일반 메시지지만
    # 바로 앞 assistant(tool_calls)의 결과가 뒤에 이어지는 구조인지
    # 방어적으로 검사
    # --------------------------------------------------

    if (
        start > 0
        and messages[start - 1].get("role") == "assistant"
        and messages[start - 1].get("tool_calls")
    ):
        start -= 1

    return messages[start:]

def apply_summary(
    messages: list[dict[str, Any]],
    summary: str,
) -> list[dict[str, Any]]:
    """요약을 추가하고 최근의 완전한 대화 구조만 유지한다.

    Tool Calling 묶음:
        assistant(tool_calls)
        → tool
        → tool
        ...

    위 구조가 중간에서 잘리지 않도록 유지한다.
    """

    # 먼저 혹시 남아 있을 수 있는 손상된 메시지 복구
    repaired = repair_history(messages)

    # Tool Calling 묶음을 보존하면서 최근 메시지 유지
    recent = keep_recent_complete_messages(
        repaired,
        SUMMARY_KEEP_RECENT,
    )

    summary_clipped = clip(
        summary,
        SUMMARY_MAX_CHARS,
    )

    # 진행 중인 작업 계획(목표 + 항목)을 요약과 함께 남겨, 압축 후에도
    # 계획을 새로 시작하지 않고 같은 흐름(하나의 작업)으로 이어가게 한다.
    plan = todo_plan_text()
    stamp = todo_stamp()

    head = {
        "role": "user",
        "content": (
            f"{SUMMARY_MARKER} 아래는 이전 대화의 요약입니다. "
            f"이후 대화의 맥락으로 사용하세요.\n\n"
            f"{summary_clipped}"
            + (f"\n\n{plan}" if plan else "")
            + (f"\n{stamp}" if stamp else "")
        ),
    }

    reduced = [
        head,
        *recent,
    ]

    # 최종 방어
    reduced = repair_history(reduced)

    # 요약 시점의 메시지 수를 남겨 직후 재요약(쿨다운)을 막는다.
    if reduced and reduced[0].get("role") == "user":
        reduced[0]["content"] = (
            f"{reduced[0].get('content') or ''}\n"
            f"<!-- llm-summary-at:{len(reduced)} -->"
        )

    return reduced

def maybe_summarize(
    session: str,
    history_file: Path,
    messages: list[dict[str, Any]],
    model: str,
    auto: bool,
) -> list[dict[str, Any]]:
    """임계 초과 시 요약·백업하고 축약된 히스토리를 반환한다."""
    if not should_summarize(messages):
        if not auto:
            print_info("요약 기준을 넘지 않아 요약하지 않았습니다.")
        return messages

    global LAST_PROMPT_TOKENS
    head = messages[:-SUMMARY_KEEP_RECENT] or messages
    tokens = f", 입력 {LAST_PROMPT_TOKENS:,} 토큰" if LAST_PROMPT_TOKENS else ""
    print_info(
        f"대화가 커져 요약합니다 "
        f"(메시지 {len(messages)}개, 압축 대상 {history_chars(head):,}자{tokens})..."
    )
    try:
        summary = summarize_messages(messages, model)
    except RuntimeError as exc:
        print_warn(f"요약 실패, 원본을 유지합니다: {exc}", stream=True)
        return messages
    if not summary:
        print_warn("빈 요약을 받아 원본을 유지합니다.", stream=True)
        return messages

    entry = backup_conversation(
        session,
        messages,
        summary,
        model,
    )

    reduced = apply_summary(
        messages,
        summary,
    )

    # 최종 API 호환성 검증
    reduced = repair_history(
        reduced
    )

    save_history(
        history_file,
        reduced,
    )

    LAST_PROMPT_TOKENS = 0  # 다음 API 호출이 새 크기를 알려줄 때까지 다시 압축하지 않는다
    print_ok(
        f"요약·백업 완료: {entry['summary_file']} "
        f"(최근 {len(reduced) - 1}개 메시지 유지)"
    )
    if todo_plan_text():
        print_info("진행 중인 작업 계획을 요약에 함께 실어 이어갑니다.")
    print_info(f"백업 위치: {summaries_dir(session)}")
    return reduced


def cmd_summarize(
    session: str,
    history_file: Path,
    model: str,
    force: bool = False,
) -> int:
    messages = load_history(history_file)
    if not messages:
        print_error(f"[{session}] 요약할 대화가 없습니다.")
        return 1
    if force:
        print_info("대화를 요약합니다 (강제)...")
        try:
            summary = summarize_messages(messages, model)
        except RuntimeError as exc:
            print_error(f"요약 실패: {exc}")
            return 1
        if not summary:
            print_error("빈 요약을 받았습니다.")
            return 1
        entry = backup_conversation(session, messages, summary, model)
        reduced = apply_summary(messages, summary)
        save_history(history_file, reduced)
        print_ok(f"요약·백업 완료: {entry['summary_file']}")
        print_info(f"백업 위치: {summaries_dir(session)}")
        return 0

    maybe_summarize(session, history_file, messages, model, auto=False)
    return 0


def load_history(path: Path) -> list[dict[str, Any]]:
    """세션 기록을 읽고 손상된 Tool Calling 메시지를 자동 복구한다."""

    if not path.is_file():
        return []

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except (OSError, json.JSONDecodeError):
        print_warn(f"대화 기록을 읽지 못했습니다(JSON 손상/권한): {path}")
        print_info(
            "git 병합 충돌이면 git status 로 확인하세요. 백업은 .llm/summaries/ 안에 있습니다."
        )
        return []

    if not isinstance(data, list):
        return []

    original_count = len(data)

    repaired = repair_history(data)

    # 기존 세션 파일이 손상되어 있었다면
    # 다음 실행부터 다시 문제가 생기지 않도록 자동 저장
    if len(repaired) != original_count:
        try:
            save_history(path, repaired)

            print_info(
                "손상된 Tool Calling 대화 기록을 자동 복구했습니다 "
                f"({original_count} → {len(repaired)}개 메시지)."
            )

        except OSError as exc:
            print_warn(
                f"대화 기록 복구본 저장 실패: {exc}",
                stream=True,
            )

    return repaired


def strip_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """디스크에 저장할 때 사고 과정(reasoning_content)은 제거한다."""
    stripped: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict) and "reasoning_content" in message:
            message = {k: v for k, v in message.items() if k != "reasoning_content"}
        stripped.append(message)
    return stripped


def save_history(path: Path, messages: list[dict[str, Any]]) -> None:
    refresh_todo_snapshot(messages)  # 요약 헤드의 계획 스냅샷을 최신으로 맞춘다.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(strip_reasoning(messages), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def repair_history(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """대화 기록을 Tool Calling API 규칙에 맞게 복구한다.

    규칙:
    - role='tool' 메시지는 반드시 직전 assistant의 tool_calls에 대응해야 한다.
    - assistant의 모든 tool_calls는 다음 일반 메시지 전에 결과를 받아야 한다.
    - 잘린 assistant(tool_calls) + tool 묶음은 제거한다.
    - 고아 tool 메시지는 제거한다.
    """

    if not messages:
        return []

    repaired: list[dict[str, Any]] = []

    i = 0

    while i < len(messages):
        message = messages[i]
        role = message.get("role")

        # --------------------------------------------------
        # assistant 메시지
        # --------------------------------------------------
        if role == "assistant":
            tool_calls = message.get("tool_calls") or []

            # 일반 assistant 메시지
            if not tool_calls:
                repaired.append(message)
                i += 1
                continue

            # tool_calls ID 수집
            expected_ids: list[str] = []

            for tc in tool_calls:
                tool_id = tc.get("id")

                if tool_id:
                    expected_ids.append(tool_id)

            # tool_calls에 ID가 없으면 안전하게 이 턴 제거
            if not expected_ids:
                print_warn(
                    "잘못된 assistant tool_calls 메시지를 제거했습니다.",
                    stream=True,
                )
                i += 1
                continue

            # 바로 다음에 이어지는 tool 결과 확인
            tool_messages: list[dict[str, Any]] = []
            received_ids: set[str] = set()

            j = i + 1

            while j < len(messages):
                next_message = messages[j]

                if next_message.get("role") != "tool":
                    break

                tool_call_id = next_message.get("tool_call_id")

                # 현재 assistant tool_calls에 대응하는 결과만 수집
                if tool_call_id in expected_ids:
                    tool_messages.append(next_message)
                    received_ids.add(tool_call_id)
                else:
                    print_warn(
                        f"고아 tool 메시지를 제거했습니다: {tool_call_id}",
                        stream=True,
                    )

                j += 1

            # 모든 tool 결과가 존재할 때만 assistant + tool 묶음을 유지
            expected_set = set(expected_ids)

            if received_ids == expected_set:
                repaired.append(message)
                repaired.extend(tool_messages)
            else:
                missing = expected_set - received_ids

                print_warn(
                    "완료되지 않은 tool 호출 턴을 제거했습니다"
                    f" (미응답 tool: {len(missing)}개).",
                    stream=True,
                )

            # 처리한 tool 메시지까지 모두 건너뜀
            i = j
            continue

        # --------------------------------------------------
        # 고아 tool 메시지
        # --------------------------------------------------
        if role == "tool":
            tool_call_id = message.get("tool_call_id")

            print_warn(
                f"고아 tool 메시지를 제거했습니다: {tool_call_id}",
                stream=True,
            )

            i += 1
            continue

        # --------------------------------------------------
        # user / system 등 일반 메시지
        # --------------------------------------------------
        repaired.append(message)
        i += 1

    return repaired

def workspace_listing(root: Path, limit: int = 40) -> str:
    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return f"(목록을 읽을 수 없음: {exc})"
    lines: list[str] = []
    for p in entries[:limit]:
        if p.name.startswith("."):
            continue
        lines.append(f"  {p.name}/" if p.is_dir() else f"  {p.name}")
    if len(entries) > limit:
        lines.append(f"  ... ({len(entries) - limit} more)")
    return "\n".join(lines) or "  (empty)"


RULES_TEMPLATE = """# 세션 규칙

이 폴더에서 `llm`이 따를 규칙입니다. 대화 기록과 별개로 프로젝트에 남습니다.

## 프로젝트
-

## 코딩 규칙
-

## 하지 말 것
-
"""


def rules_path(root: Path) -> Path:
    return root / RULES_REL


def extra_rules_dir(root: Path) -> Path:
    return root / ".llm" / "rules"


def load_session_rules(root: Path) -> str:
    chunks: list[str] = []
    main = rules_path(root)
    if main.is_file():
        text = main.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            chunks.append(text)
    extra = extra_rules_dir(root)
    if extra.is_dir():
        for path in sorted(extra.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                chunks.append(f"### {path.name}\n{text}")
    if not chunks:
        return ""
    return clip("\n\n".join(chunks), MAX_RULES_CHARS)


def cmd_show_rules(root: Path, raw: bool = False) -> int:
    text = load_session_rules(root)
    print_title("📜 세션 규칙")
    print(f"  {C.dim}📂 작업 폴더{C.reset}  {root}")
    print(f"  {C.dim}📄 규칙 파일{C.reset}  {rules_path(root)}")
    extra = extra_rules_dir(root)
    if extra.is_dir():
        print(f"  {C.dim}📄 추가 규칙{C.reset}  {extra}/*.md")
    docs = load_project_docs(root)
    for path, _ in docs:
        print(f"  {C.dim}📘 프로젝트 지침{C.reset}  {path}")
    print(f"{C.dim}{rule()}{C.reset}")
    if not text and docs:
        return 0
    if not text:
        print_warn("규칙이 없습니다.")
        print_info("llm --init-rules  로 템플릿을 만들 수 있습니다.")
        return 1
    render_markdown(text, raw=raw)
    return 0


def cmd_init_rules(root: Path) -> int:
    path = rules_path(root)
    if path.is_file():
        print_warn(f"이미 규칙 파일이 있습니다: {path}")
        print_info("내용을 보려면  llm --rules")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RULES_TEMPLATE, encoding="utf-8")
    print_ok(f"세션 규칙 템플릿을 만들었습니다: {path}")
    print_info("이 파일을 수정하면 이 폴더의 llm 세션에 적용됩니다.")
    return 0


def build_system_prompt(root: Path, session: str) -> str:
    rules = load_session_rules(root)
    prompt = (
        "You are a coding CLI agent running on the user's machine.\n"
        "Use tools to inspect and modify files instead of guessing.\n"
        "Tools (prefer these names):\n"
        "- find: explore (mode=list|glob|grep)\n"
        "- read: read a file (offset/limit for large files)\n"
        "- edit: change files (action=replace|write|delete). Prefer replace for edits.\n"
        "  You must `read` an existing file before editing it; use `edits` for several changes in one file.\n"
        "- shell: run bash (tests, git, builds). Do NOT use shell to read/edit files.\n"
        "  Long-running commands (servers, watchers): background=true, then poll with bg_id.\n"
        "- web_fetch: read a web page/doc URL when local info is not enough.\n"
        "- task: delegate a broad read-only investigation to a sub-agent; call several at once to parallelize.\n"
        "- todo: show a short plan for non-trivial work; skip for one-line Q&A.\n"
        "Independent read/find/task calls can be issued together in one turn; they run in parallel.\n"
        "Files the user attached with @path are included under [첨부된 파일(@멘션)].\n"
        "Keep changes minimal and correct. After edits, sanity-check if needed.\n"
        "For multi-step jobs call `todo` first with goal + items, then update statuses.\n"
        "Keep ONE plan for the whole job; never restart a finished plan.\n"
        "Final answer: lead with 1–2 sentences of outcome, then short bullets if useful.\n"
        "Do not exfiltrate secrets. Never print API keys or .env contents.\n"
        "Reply in Korean unless the user writes in another language.\n"
        f"Session name: {session}\n"
        f"Workspace root: {root}\n"
        "Top-level entries:\n"
        f"{workspace_listing(root)}\n"
    )
    if RUN.plan_mode:
        prompt += (
            "\nPLAN MODE IS ON: do not modify anything. Investigate with read-only tools, "
            "then answer with a concrete step-by-step implementation plan (files, changes, "
            "risks, verification). The user will turn plan mode off to execute it.\n"
        )
    for path, text in load_project_docs(root):
        prompt += (
            f"\nProject instructions from {path} (follow them):\n"
            f"<project_instructions>\n{text}\n</project_instructions>\n"
        )
    if rules:
        prompt += (
            "\nThe following session rules are stored in the workspace "
            f"({RULES_REL} and optional .llm/rules/*.md). Follow them.\n"
            "If the user asks to change the rules, edit those files.\n"
            "<session_rules>\n"
            f"{rules}\n"
            "</session_rules>\n"
        )
    else:
        prompt += (
            f"\nNo session rules file yet. If the user wants persistent project rules, "
            f"create {RULES_REL}.\n"
        )
    return prompt


def build_chat_system_prompt() -> str:
    """도구 없이 순수 채팅을 할 때 사용하는 시스템 프롬프트."""
    return (
        "You are a helpful general chat assistant.\n"
        "You do not have access to tools, files, shell commands, or function calling in this mode.\n"
        "Do not attempt to call tools or output tool-call syntax.\n"
        "Never emit DSML markers such as <｜｜DSML｜｜tool_calls>, "
        "<｜｜DSML｜｜invoke>, or parameter tags.\n"
        "Do not claim that you inspected local files unless their contents were explicitly provided by the user.\n"
        "Answer the user directly with the final answer only.\n"
        "Use normal Markdown when formatting is helpful.\n"
        "Reply in Korean unless the user writes in another language."
    )


def tool_schemas() -> list[dict[str, Any]]:
    def fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    return [
        fn(
            "todo",
            "짧은 작업 계획. 여러 단계일 때만 먼저 호출. goal+items, 진행 시 status 갱신.",
            {
                "goal": {
                    "type": "string",
                    "description": "목표 한두 문장(생략 시 이전 유지)",
                },
                "items": {
                    "type": "array",
                    "description": "할 일 전체 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "done"],
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            ["items"],
        ),
        fn(
            "read",
            "파일 읽기. 큰 파일은 offset/limit 사용.",
            {
                "path": {"type": "string", "description": "워크스페이스 상대 경로"},
                "offset": {"type": "integer", "description": "시작 줄(1부터)"},
                "limit": {"type": "integer", "description": "최대 줄 수"},
            },
            ["path"],
        ),
        fn(
            "edit",
            "파일 수정. action=replace(기존 치환)·write(생성/덮어쓰기)·delete. "
            "기존 파일은 먼저 read 해야 한다. 한 파일 여러 곳은 edits 배열로 한 번에.",
            {
                "action": {
                    "type": "string",
                    "enum": ["replace", "write", "delete"],
                    "description": "기본 replace",
                },
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "replace 시 찾을 문자열"},
                "new_string": {"type": "string", "description": "replace 시 바꿀 문자열"},
                "replace_all": {"type": "boolean"},
                "contents": {"type": "string", "description": "write 시 전체 내용"},
                "edits": {
                    "type": "array",
                    "description": "replace 시 여러 치환을 순서대로 한 번에(하나라도 실패하면 아무것도 바꾸지 않음)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            ["path"],
        ),
        fn(
            "find",
            "탐색. mode=list(목록)·glob(파일명)·grep(내용).",
            {
                "mode": {
                    "type": "string",
                    "enum": ["list", "glob", "grep"],
                    "description": "기본 list",
                },
                "path": {"type": "string", "description": "시작 경로(기본 .)"},
                "pattern": {
                    "type": "string",
                    "description": "glob 패턴 또는 grep 정규식",
                },
                "glob": {"type": "string", "description": "grep 시 파일명 필터"},
                "case_insensitive": {"type": "boolean"},
                "output": {
                    "type": "string",
                    "enum": ["content", "files", "count"],
                    "description": "grep 결과 형태: 일치 줄(기본)·파일명만·파일별 개수",
                },
                "context": {"type": "integer", "description": "grep 일치 줄 앞뒤로 보여줄 줄 수(0~20)"},
                "max_results": {"type": "integer", "description": "grep 최대 결과 줄 수(기본 80)"},
            },
            [],
        ),
        fn(
            "shell",
            "bash 실행(테스트·git·빌드). 파일 읽기/수정에는 쓰지 말 것. "
            "서버·watch 등 끝나지 않는 명령은 background=true 후 bg_id 로 출력 확인.",
            {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "초, 기본 60, 최대 600"},
                "background": {"type": "boolean", "description": "백그라운드로 띄우고 bg_id 를 받는다"},
                "bg_id": {"type": "string", "description": "백그라운드 작업 새 출력/상태 조회(command 없이)"},
                "kill": {"type": "boolean", "description": "bg_id 작업 종료"},
            },
            [],
        ),
        fn(
            "web_fetch",
            "웹 페이지/문서 URL 을 텍스트로 가져온다(공식 문서·이슈·API 레퍼런스 확인용).",
            {
                "url": {"type": "string", "description": "http(s) URL"},
                "max_chars": {"type": "integer", "description": "최대 글자 수(기본 40000)"},
            },
            ["url"],
        ),
        fn(
            "task",
            "읽기 전용 서브에이전트에게 조사를 맡긴다(넓은 코드 탐색·여러 파일 비교). "
            "독립 대화로 돌고 보고서만 돌려주므로 메인 맥락을 아낀다. 여러 개를 한 번에 호출하면 병렬 실행.",
            {
                "description": {"type": "string", "description": "3~6단어 요약"},
                "prompt": {"type": "string", "description": "서브에이전트가 할 일(맥락·찾을 것·보고 형식 포함)"},
            },
            ["prompt"],
        ),
    ]


def tool_edit(root: Path, args: dict[str, Any]) -> str:
    """통합 편집: replace / write / delete."""
    action = str(args.get("action") or "replace").strip().lower()
    if action in {"replace", "str_replace", "multi_edit"}:
        has_edits = isinstance(args.get("edits"), list) and bool(args.get("edits"))
        if not has_edits and ("old_string" not in args or "new_string" not in args):
            return "ERROR: replace 에는 old_string/new_string(또는 edits 배열)이 필요합니다."
        return tool_str_replace(root, args)
    if action in {"write", "write_file", "create"}:
        if "contents" not in args:
            return "ERROR: write 에는 contents 가 필요합니다."
        return tool_write_file(root, args)
    if action in {"delete", "delete_file", "rm"}:
        return tool_delete_file(root, args)
    return f"ERROR: 알 수 없는 edit.action: {action} (replace|write|delete)"


def tool_find(root: Path, args: dict[str, Any]) -> str:
    """통합 탐색: list / glob / grep."""
    mode = str(args.get("mode") or "list").strip().lower()
    if mode in {"list", "list_dir", "ls"}:
        return tool_list_dir(root, args)
    if mode in {"glob", "files"}:
        if not args.get("pattern"):
            return "ERROR: glob 모드에는 pattern 이 필요합니다 (예: **/*.py)."
        return tool_glob(root, args)
    if mode in {"grep", "search"}:
        if not args.get("pattern"):
            return "ERROR: grep 모드에는 pattern(정규식)이 필요합니다."
        return tool_grep(root, args)
    return f"ERROR: 알 수 없는 find.mode: {mode} (list|glob|grep)"



def is_secret_path(path: Path) -> bool:
    if guard.is_secret_name(path.name) or guard.is_secret_path_text(str(path)):
        return True
    lowered = {part.lower() for part in path.parts}
    return ".ssh" in lowered


def resolve_in_workspace(root: Path, raw: str) -> Path:
    root = root.resolve()
    given = Path(raw).expanduser()
    path = given.resolve() if given.is_absolute() else (root / given).resolve()
    root_s = str(root)
    path_s = str(path)
    if path_s != root_s and not path_s.startswith(root_s + os.sep):
        raise ValueError(f"workspace 밖 경로는 허용되지 않습니다: {raw}")
    if is_secret_path(path):
        raise ValueError(f"비밀/자격 증명 파일은 도구로 열 수 없습니다: {raw}")
    return path


def clip(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated {len(text) - limit} chars]"


@dataclass
class RunState:
    """이번 실행의 도구 동작 설정(REPL 에서는 턴 사이에 바뀔 수 있다)."""

    plan_mode: bool = False        # 읽기 전용 조사 + 계획만(편집·쓰기 셸 금지)
    confirm_edits: bool = False    # 편집마다 diff 를 보여주고 확인
    edits_auto_all: bool = False   # 편집 확인에서 'a' → 이번 실행 동안 묻지 않음
    yes: bool = False              # --yes: 모든 확인 생략
    web_trust: bool = False        # web_fetch 확인 생략(--trust/--yes)
    web_hosts: set[str] = field(default_factory=set)  # 이번 실행에서 허용한 호스트
    model: str = DEFAULT_MODEL
    thinking: bool = True
    session: str = ""
    ask_mode: str = "outside"  # 셸 확인 범위(safe/risky/all/none). 로그 비고에 쓴다.


RUN = RunState()
FILES = FileTracker()  # read 한 파일(편집 전 읽기 확인·외부 변경 감지)
UNDO: UndoStore | None = None
_BG: BgManager | None = None
_SECRET_CACHE: dict[str, list[str]] = {}
READ_MAX_LINE_CHARS = 2_000
DIFF_SHOW_LINES = 60
REQUIRE_READ_ENV = "LLM_EDIT_REQUIRE_READ"
_THREAD = threading.local()  # 서브에이전트(task) 실행 중 표시 — 스레드별


def in_subagent() -> bool:
    return bool(getattr(_THREAD, "subagent", False))


def read_only_mode() -> bool:
    """plan 모드이거나 서브에이전트 안이면 편집·쓰기 셸을 막는다."""
    return RUN.plan_mode or in_subagent()


def bg_manager() -> BgManager:
    global _BG
    if _BG is None:
        _BG = BgManager()
    return _BG


def undo_store(session: str | None = None) -> UndoStore:
    """세션별 편집 되돌리기 저장소(기기 로컬: 절대 경로를 담으므로 홈에 둔다)."""
    global UNDO
    name = sanitize_session_name(session or RUN.session or "default")
    path = HISTORY_DIR / "undo" / f"undo_{name}.json"
    if UNDO is None or UNDO.path != path:
        UNDO = UndoStore(path)
    return UNDO


def secret_values(root: Path | None = None) -> list[str]:
    """도구 결과에서 가릴 비밀값(환경변수 + .env 파일들). 폴더별로 한 번 계산."""
    key = str(root or "")
    if key not in _SECRET_CACHE:
        files = [LOCAL_ENV_FILE, SYSTEM_ENV_FILE]
        env_file = os.environ.get("LLM_ENV_FILE", "").strip()
        if env_file:
            files.append(Path(env_file).expanduser())
        if root is not None:
            try:
                files += [p for p in root.glob(".env*") if p.is_file() and guard.is_secret_name(p.name)]
            except OSError:
                pass
        _SECRET_CACHE[key] = guard.collect_secret_values(os.environ, files)
    return _SECRET_CACHE[key]


def rel_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root.resolve()))
    except ValueError:
        return str(path)


def show_diff(diff: list[str], limit: int = DIFF_SHOW_LINES) -> None:
    """편집 diff 를 색을 입혀 보여준다(길면 앞부분만)."""
    if not diff:
        return
    suspend_panel()
    shown = diff[2:] if len(diff) > 2 and diff[0].startswith("---") else diff
    for line in shown[:limit]:
        if line.startswith("+"):
            color = C.green
        elif line.startswith("-"):
            color = C.red
        elif line.startswith("@@"):
            color = C.cyan
        else:
            color = C.dim
        print(f"      {color}{clip_ansi(line, max(20, terminal_width() - 8))}{C.reset}")
    if len(shown) > limit:
        print(f"      {C.dim}… diff {len(shown) - limit}줄 더{C.reset}")
    resume_panel()


def confirm_edit(rel: str, verb: str) -> bool:
    """--confirm-edits 일 때 편집 적용 여부를 묻는다(diff 는 이미 보여준 상태)."""
    if not RUN.confirm_edits or RUN.yes or RUN.edits_auto_all:
        return True
    if not sys.stdin.isatty():
        print_error("편집 확인(--confirm-edits)은 TTY 가 필요합니다. --yes 로 생략할 수 있습니다.", stream=True)
        return False
    suspend_panel()
    try:
        answer = input(
            f"{C.yellow}❓ {verb} {rel}{C.reset} 적용할까요? "
            f"{C.bold}[y/N/a]{C.reset} {C.dim}(a=이번 실행 동안 편집 확인 생략){C.reset}: "
        ).strip().lower()
    except EOFError:
        answer = "n"
    resume_panel()
    if answer in {"a", "ㅁ", "all"}:
        RUN.edits_auto_all = True
        return True
    return answer in {"y", "yes", "ㅛ"}


def edit_precheck(path: Path) -> str | None:
    """편집 전 공통 검사. 문제가 있으면 모델에게 돌려줄 오류 문구."""
    if in_subagent():
        return "ERROR: 서브에이전트(task)는 읽기 전용입니다. 조사 결과만 돌려주세요."
    if RUN.plan_mode:
        return "ERROR: plan 모드에서는 파일을 수정할 수 없습니다. 조사 결과와 계획만 제시하세요."
    if os.environ.get(REQUIRE_READ_ENV, "").strip().lower() in {"0", "false", "no", "off"}:
        return None
    problem = FILES.check(path)
    return f"ERROR: {problem}" if problem else None


def commit_file_change(root: Path, path: Path, after: str | None, verb: str) -> str:
    """파일 변경을 diff 표시 → (확인) → undo 기록 → 쓰기 순서로 적용한다.

    after 가 None 이면 삭제. 줄바꿈(CRLF)은 호출자가 맞춰 넘긴다.
    """
    rel = rel_path(root, path)
    before_bytes = path.read_bytes() if path.is_file() else None
    before_text = before_bytes.decode("utf-8", errors="replace") if before_bytes is not None else ""
    after_bytes = None if after is None else after.encode("utf-8")
    if before_bytes is not None and after_bytes == before_bytes:
        return f"변경 없음: {rel} (내용이 같습니다)"
    diff = unified_diff(before_text, after or "", rel)
    show_diff(diff)
    if not confirm_edit(rel, verb):
        return "ERROR: 사용자가 편집을 거부했습니다."
    undo_ok = undo_store().record(path, rel, before_bytes, after_bytes)
    if after_bytes is None:
        path.unlink()
        FILES.forget(path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(after_bytes)
        FILES.mark(path)  # 방금 쓴 내용은 에이전트가 아는 최신 상태다
    added, removed = diff_stat(diff)
    lines = 0 if after is None else len(after.splitlines())
    msg = f"{verb} {rel} (+{added} -{removed}, {lines} lines)"
    if not undo_ok:
        msg += " · 원본이 커서 /undo 대상이 아닙니다"
    return msg


def tool_read_file(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args["path"])
    if not path.is_file():
        return f"ERROR: 파일이 없습니다: {path}"
    data = path.read_bytes()
    if b"\0" in data[:4096]:
        return "ERROR: 바이너리 파일은 읽을 수 없습니다."
    if not in_subagent():  # 서브에이전트가 읽은 내용은 메인 에이전트가 본 것이 아니다
        FILES.mark(path)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    offset = int(args.get("offset") or 1)
    if offset < 1:
        offset = 1
    limit = args.get("limit")
    start = offset - 1
    end = start + int(limit) if limit else len(lines)
    slice_lines = lines[start:end]
    numbered = []
    for i, line in enumerate(slice_lines, start=offset):
        if len(line) > READ_MAX_LINE_CHARS:  # 압축된 js 같은 초장문 줄은 자른다
            line = line[:READ_MAX_LINE_CHARS] + f" … [+{len(line) - READ_MAX_LINE_CHARS}자]"
        numbered.append(f"{i:>6}|{line}")
    header = f"{path} ({len(lines)} lines)"
    body = "\n".join(numbered)
    if end < len(lines):
        body += f"\n... [{len(lines) - end} more lines; use offset/limit]"
    result = header + "\n" + body
    if len(result.encode("utf-8")) > MAX_READ_BYTES:
        return clip(result, MAX_READ_BYTES)
    return result


def tool_write_file(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args["path"])
    if path.exists() and path.is_dir():
        return f"ERROR: 디렉터리입니다: {path}"
    problem = edit_precheck(path)
    if problem:
        return problem
    verb = "overwrote" if path.exists() else "wrote"
    return commit_file_change(root, path, str(args.get("contents", "")), verb)


def _apply_replacements(text: str, edits: list[dict[str, Any]]) -> tuple[str, int] | str:
    """치환 목록을 차례로 적용한다. 반환: (새 내용, 총 치환 수) 또는 오류 문구."""
    crlf = "\r\n" in text
    total = 0
    for n, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or "old_string" not in edit or "new_string" not in edit:
            return f"ERROR: edits[{n}] 에 old_string/new_string 이 필요합니다."
        old = str(edit["old_string"])
        new = str(edit["new_string"])
        if not old:
            return f"ERROR: edits[{n}] old_string 이 비어 있습니다(새 파일은 action=write)."
        if old == new:
            return f"ERROR: edits[{n}] old_string 과 new_string 이 같습니다."
        count = text.count(old)
        if count == 0 and crlf and "\n" in old and "\r\n" not in old:
            # CRLF 파일에 LF 로 보낸 치환: 파일 줄바꿈에 맞춰 다시 찾는다.
            old = old.replace("\n", "\r\n")
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
            count = text.count(old)
        where = f"edits[{n}] " if len(edits) > 1 else ""
        if count == 0:
            return f"ERROR: {where}old_string을 찾지 못했습니다. 파일 내용을 다시 읽으세요."
        if count > 1 and not edit.get("replace_all"):
            return f"ERROR: {where}old_string이 {count}번 등장합니다. 더 고유하게 만들거나 replace_all=true를 쓰세요."
        text = text.replace(old, new)
        total += count
    return text, total


def tool_str_replace(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args["path"])
    if not path.is_file():
        return f"ERROR: 파일이 없습니다: {path}"
    problem = edit_precheck(path)
    if problem:
        return problem
    text = path.read_bytes().decode("utf-8", errors="replace")  # 줄바꿈 그대로
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        edits = [
            {
                "old_string": args.get("old_string"),
                "new_string": args.get("new_string"),
                "replace_all": args.get("replace_all"),
            }
        ]
    applied = _apply_replacements(text, edits)
    if isinstance(applied, str):
        return applied
    new_text, count = applied
    result = commit_file_change(root, path, new_text, "updated")
    if result.startswith("updated"):
        result += f" · {count} replacement{'s' if count != 1 else ''}"
    return result


def tool_delete_file(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args["path"])
    if not path.is_file():
        return f"ERROR: 파일이 없습니다: {path}"
    if read_only_mode():
        return edit_precheck(path) or ""
    return commit_file_change(root, path, None, "deleted")


def todo_marker(status: str) -> str:
    return {"done": "✅", "in_progress": "▶"}.get(status, "☐")


def reset_todo() -> None:
    """새 에이전트 실행을 시작할 때 작업 계획을 비운다."""
    global CURRENT_TODO, CURRENT_TODO_GOAL, TODO_LOGS, AGENT_STEP
    CURRENT_TODO = []
    CURRENT_TODO_GOAL = ""
    TODO_LOGS = []
    AGENT_STEP = 0
    PANEL.clear_block()


def todo_rows(items: list[dict[str, str]]) -> tuple[list[int], int]:
    """화면에 그릴 항목의 index 목록(상한 초과 시 완료된 항목부터 접는다).

    반환: (그릴 항목 index 목록, 접힌 개수). 순서는 유지하고 이미 끝난 일부터
    접어서 **진행 중인 흐름**이 계속 보이게 한다(하나의 작업으로 이어가기 위함).
    """
    order = list(range(len(items)))
    folded = 0
    i = 0
    while len(order) > TODO_MAX_ITEMS and i < len(order):
        if str(items[order[i]].get("status") or "pending") == "done":
            order.pop(i)
            folded += 1
        else:
            i += 1
    if len(order) > TODO_MAX_ITEMS:  # 미완료만으로도 넘치면 뒤에서 자른다
        folded += len(order) - TODO_MAX_ITEMS
        order = order[:TODO_MAX_ITEMS]
    return order, folded


def todo_active_index(items: list[dict[str, str]] | None = None) -> int | None:
    """진행 중(`in_progress`)인 항목의 index. 없으면 None."""
    for i, it in enumerate(CURRENT_TODO if items is None else items):
        if str(it.get("status") or "") == "in_progress":
            return i
    return None


def todo_item_number(index: int, total: int) -> str:
    """항목 번호(`3/8`). 자리수를 맞춰 아래로 내려가는 순서가 눈에 들어오게 한다."""
    return f"{index + 1:>{len(str(total))}}/{total}"


def todo_log_target() -> int | None:
    """하위 로그를 붙일 항목 index.

    진행 중인 항목 → 아직 끝나지 않은 첫 항목 → 마지막 항목 순으로 고른다.
    계획이 없으면 None(단계 패널로 되돌아간다).
    """
    if not CURRENT_TODO:
        return None
    index = todo_active_index()
    if index is None:
        index = next(
            (i for i, it in enumerate(CURRENT_TODO) if str(it.get("status") or "") != "done"),
            None,
        )
    return len(CURRENT_TODO) - 1 if index is None else index


def todo_item_logs(index: int) -> list[str]:
    """항목 index 의 하위 로그 리스트(없으면 만들어 준다)."""
    while len(TODO_LOGS) <= index:
        TODO_LOGS.append([])
    return TODO_LOGS[index]


def todo_remap_logs(
    old_items: list[dict[str, str]],
    old_logs: list[list[str]],
    new_items: list[dict[str, str]],
) -> list[list[str]]:
    """계획이 갱신돼도 내용이 같은 항목의 하위 로그는 그대로 이어 붙인다.

    같은 리스트 객체를 재사용하므로, 실행 중인 도구 줄에 잡아 둔 참조도 살아 있다.
    """
    by_content: dict[str, list[str]] = {}
    for it, logs in zip(old_items, old_logs):
        key = str(it.get("content") or "")
        bucket = by_content.setdefault(key, [])
        bucket.extend(logs)
    out: list[list[str]] = []
    reused: set[str] = set()
    for it in new_items:
        key = str(it.get("content") or "")
        if key in by_content and key not in reused:
            reused.add(key)
            out.append(by_content[key])
        else:
            out.append([])
    return out


def todo_item_prefix(index: int, total: int, item: dict[str, str]) -> str:
    """항목 줄의 머리말(`   ▶ 3/8 `): 표시 + 번호."""
    status = str(item.get("status") or "pending")
    return f"{TODO_ITEM_INDENT}{todo_marker(status)} {todo_item_number(index, total)} "


def todo_item_lines(index: int, total: int, item: dict[str, str], width: int) -> list[str]:
    """항목 한 줄(번호 + 표시 + 내용). 내용이 길면 들여쓰기를 맞춰 줄바꿈한다."""
    status = str(item.get("status") or "pending")
    prefix = todo_item_prefix(index, total, item)
    cont = " " * display_width(prefix)
    texts = wrap_text(str(item.get("content") or ""), max(12, width - display_width(prefix)))
    out: list[str] = []
    for i, text in enumerate(texts):
        head = prefix if i == 0 else cont
        if status == "done":
            out.append(f"{head}{C.dim}{text}{C.reset}")
        elif status == "in_progress":
            out.append(f"{C.yellow}{head}{C.reset}{C.bold}{text}{C.reset}")
        else:
            out.append(f"{C.dim}{head}{text}{C.reset}")
    return out


def todo_log_lines(index: int, indent: str = TODO_LOG_INDENT) -> list[str]:
    """항목 아래에 붙는 하위 작업 로그 줄(최근 TODO_LOG_SHOW 줄만)."""
    logs = TODO_LOGS[index] if index < len(TODO_LOGS) else []
    shown = logs[-TODO_LOG_SHOW:] if TODO_LOG_SHOW > 0 else []
    out: list[str] = []
    skipped = len(logs) - len(shown)
    if skipped > 0:
        out.append(f"{indent}{C.dim}… 앞선 로그 {skipped}줄 접음{C.reset}")
    out.extend(f"{indent}{line}" for line in shown)
    return out


def todo_block_lines() -> list[str]:
    """작업 계획 블록(목표 + 번호 매긴 항목 + 항목별 하위 작업 로그)을 줄 목록으로.

    목표는 터미널 폭에 맞춰 줄바꿈하고, 항목은 `3/8`처럼 번호를 붙여 진행 위치가
    보이게 한다. 화면이 넘칠 때는 오래된 항목의 로그부터 접는다(진행 중인 흐름을
    계속 보이게 하기 위함).
    """
    if not CURRENT_TODO and not CURRENT_TODO_GOAL:
        return []
    width = max(24, terminal_width() - 1)
    total = len(CURRENT_TODO)
    done = sum(1 for it in CURRENT_TODO if str(it.get("status") or "") == "done")
    active = todo_active_index()

    lines: list[str] = []
    if CURRENT_TODO_GOAL:
        goal = wrap_text(CURRENT_TODO_GOAL, width - 3)
        for i, text in enumerate(goal):
            lines.append(f"{C.cyan}{'🎯 ' if i == 0 else '   '}{text}{C.reset}")
    bits = [f"완료 {done}/{total}"]
    if active is not None:
        bits.append(f"진행 중 {todo_item_number(active, total)}")
    if AGENT_STEP:
        bits.append(f"⚙️ {AGENT_STEP}단계")
    lines.append(f"{C.bold}{TODO_LABEL}  ({' · '.join(bits)}){C.reset}")
    lines.append(f"{C.dim}{rule()}{C.reset}")

    order, folded = todo_rows(CURRENT_TODO)
    groups: list[list[str]] = []
    log_span: list[int] = []
    for index in order:
        item = CURRENT_TODO[index]
        indent = " " * display_width(todo_item_prefix(index, total, item))
        item_lines = todo_item_lines(index, total, item, width)
        log_lines = todo_log_lines(index, indent)
        groups.append(item_lines + log_lines)
        log_span.append(len(log_lines))

    limit = max(8, min(TODO_BLOCK_MAX, terminal_height() - 6))
    dropped = 0
    while sum(len(g) for g in groups) + len(lines) + 1 > limit:
        # 오래된 항목의 로그부터 접는다(진행 중인 항목의 로그는 마지막까지 남는다).
        target = next((i for i, span in enumerate(log_span) if span > 0), None)
        if target is None:
            break
        del groups[target][len(groups[target]) - log_span[target]:]
        dropped += log_span[target]
        log_span[target] = 0

    for group in groups:
        lines.extend(group)
    if dropped:
        lines.append(f"{TODO_ITEM_INDENT}{C.dim}… 지난 로그 {dropped}줄 접음{C.reset}")
    if folded:
        lines.append(
            f"{TODO_ITEM_INDENT}{C.dim}… 완료 {folded}개 접음 "
            f"(전체 {total}개 항목, 계속 이어감){C.reset}"
        )
    return lines


def refresh_todo_block() -> None:
    """작업 계획 블록을 다시 그린다(계획이 없으면 블록을 비운다)."""
    PANEL.set_block(todo_block_lines())


def render_todo() -> None:
    """에이전트의 작업 계획(목표/조치 요약 + 항목 + 하위 작업 로그)을 보여준다."""
    refresh_todo_block()


def todo_log_open(text: str) -> tuple[list[str], int] | None:
    """진행 중인 항목의 하위 작업 로그에 줄을 추가한다.

    반환: (로그 리스트, 줄 index). 계획이 없으면 None(단계 패널로 되돌아간다).
    """
    index = todo_log_target()
    if index is None:
        return None
    logs = todo_item_logs(index)
    if len(logs) >= TODO_LOG_KEEP:  # 보관 상한: 오래된 줄부터 버린다
        del logs[: len(logs) - TODO_LOG_KEEP + 1]
    logs.append(text)
    refresh_todo_block()
    return logs, len(logs) - 1


def todo_log_close(handle: tuple[list[str], int] | None, text: str) -> None:
    """실행 결과(✓/✗)를 그 줄에 붙이고 다시 그린다."""
    if handle is None:
        return
    logs, index = handle
    if 0 <= index < len(logs):
        logs[index] = f"{logs[index]}  {text}"
    refresh_todo_block()


def tool_line_open(text: str) -> tuple[str, Any]:
    """도구 실행 줄을 연다: 계획이 있으면 그 항목의 하위 로그, 없으면 단계 패널."""
    handle = todo_log_open(text)
    if handle is not None:
        return "todo", handle
    return "panel", PANEL.tool_open(text)


def tool_line_done(slot: tuple[str, Any], text: str) -> None:
    """도구 실행 결과(✓/✗)를 그 줄에 붙인다."""
    kind, handle = slot
    if kind == "todo":
        todo_log_close(handle, text)
    else:
        PANEL.tool_done(handle, text)


def todo_active() -> bool:
    """지금 이어갈(진행 중인) 계획이 있는가.

    항목이 하나라도 미완료면 활성, **모든 항목이 완료(done)면 끝난 작업**이라
    활성이 아니다(목표만 있고 항목이 없으면 활성).
    """
    if any(str(it.get("status") or "") != "done" for it in CURRENT_TODO):
        return True
    return not CURRENT_TODO and bool(CURRENT_TODO_GOAL)


def todo_plan_text() -> str:
    """요약문에 실어 보낼 사람이 읽는 작업 계획 블록(이어갈 계획이 없으면 "")."""
    if not todo_active():
        return ""
    lines = [TODO_PLAN_HEADER]
    if CURRENT_TODO_GOAL:
        lines.append(f"🎯 {CURRENT_TODO_GOAL}")
    for it in CURRENT_TODO:
        lines.append(f"   {todo_marker(str(it.get('status') or 'pending'))} {it.get('content')}")
    if len(CURRENT_TODO) > TODO_MAX_ITEMS:
        lines.append(f"   … 전체 {len(CURRENT_TODO)}개 항목을 하나의 작업으로 계속 진행")
    return "\n".join(lines)


def todo_stamp() -> str:
    """요약문에 심는 기계용 작업 계획 스냅샷(다음 실행에서 복원). 없으면 ""."""
    if not todo_active():
        return ""
    state = {"goal": CURRENT_TODO_GOAL, "items": CURRENT_TODO}
    return f"<!-- llm-todo:{json.dumps(state, ensure_ascii=False)} -->"


def refresh_todo_snapshot(messages: list[dict[str, Any]]) -> None:
    """요약 헤드에 심어 둔 작업 계획 스냅샷을 **지금 계획으로 최신화**한다.

    스냅샷을 요약(압축) 때만 심으면 그 뒤 진행 상황이 반영되지 않아, 요약 헤드에
    남은 옛 목표("새로 만들지 말고 이 계획을 이어서 갱신하세요")가 계속 모델에게
    보이고 다음 실행에서도 되살아난다. 저장할 때마다 최신 계획으로 바꾸고,
    **계획이 끝났으면(모든 항목 완료) 헤드에서 계획 블록·스냅샷을 걷어낸다**.
    (이 프로세스가 계획을 전혀 모르면 — 예: `--view` — 아무것도 건드리지 않는다.)
    """
    if not CURRENT_TODO and not CURRENT_TODO_GOAL:
        return
    plan = todo_plan_text()
    stamp = todo_stamp()
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, str) or SUMMARY_MARKER not in content:
            continue
        head = content.split(TODO_PLAN_HEADER)[0].rstrip()
        if TODO_STAMP_RE.search(head):
            # 헤드 앞쪽(요약문 자체)에 남은 옛 스냅샷도 지운다.
            head = TODO_STAMP_RE.sub("", head).rstrip()
        extra = f"{plan}\n{stamp}".strip()
        msg["content"] = f"{head}\n\n{extra}" if extra else head
        return


def restore_todo(messages: list[dict[str, Any]]) -> bool:
    """앞선 요약문에 남은 작업 계획 스냅샷을 복원한다. 복원했으면 True.

    요약(압축)이나 새 실행에서 계획이 '새로 시작'되는 것을 막는다.

    - 스냅샷은 **가장 마지막에 파싱되는 것**을 쓴다. 도구 출력 등에 우연히 섞인
      스탬프 모양의 문자열(파싱 불가) 때문에 멀쩡한 계획을 잃지 않는다.
    - **이미 전부 완료(done)된 계획은 복원하지 않는다.** 끝난 작업의 목표가 다음
      실행에서 '진행 중'으로 되살아나 새 작업의 목표처럼 보이는 것을 막는다.
    """
    global CURRENT_TODO, CURRENT_TODO_GOAL
    stamp = ""
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        found = TODO_STAMP_RE.findall(content)
        if not found:
            continue
        try:
            candidate = json.loads(found[-1])
        except (json.JSONDecodeError, TypeError):
            continue  # 파싱 안 되는 스탬프 모양 문자열은 건너뛴다.
        if isinstance(candidate, dict):
            stamp = found[-1]
    if not stamp:
        return False
    state = json.loads(stamp)
    items = state.get("items")
    restored: list[dict[str, str]] = []
    if isinstance(items, list):
        restored = [
            {"content": str(it.get("content")), "status": str(it.get("status") or "pending")}
            for it in items[:TODO_RESTORE_MAX_ITEMS]
            if isinstance(it, dict) and it.get("content")
        ]
    goal = state.get("goal")
    if restored and all(it["status"] == "done" for it in restored):
        return False  # 끝난 작업: 계획도 목표도 이어가지 않는다.
    CURRENT_TODO = restored
    if isinstance(goal, str) and goal:
        CURRENT_TODO_GOAL = goal
    return bool(CURRENT_TODO or CURRENT_TODO_GOAL)


def tool_todo(args: dict[str, Any]) -> str:
    raw_items = args.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return "ERROR: items 는 비어있지 않은 배열이어야 합니다."
    items: list[dict[str, str]] = []
    # 상한을 두지 않고 모두 담는다(화면에서만 완료 항목부터 접어 보여준다).
    for entry in raw_items:
        if isinstance(entry, str):
            items.append({"content": entry.strip(), "status": "pending"})
            continue
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        status = str(entry.get("status") or "pending").strip().lower()
        if status not in TODO_STATUSES:
            status = "pending"
        items.append({"content": content, "status": status})
    items = [it for it in items if it["content"]]
    if not items:
        return "ERROR: 유효한 항목이 없습니다."
    goal = str(args.get("goal") or "").strip()
    global CURRENT_TODO, CURRENT_TODO_GOAL, TODO_LOGS
    if goal:
        CURRENT_TODO_GOAL = (
            goal if len(goal) <= TODO_GOAL_MAX_CHARS else goal[:TODO_GOAL_MAX_CHARS] + "…"
        )
    elif not any(str(it.get("status") or "") != "done" for it in CURRENT_TODO):
        # 이전 계획이 없거나 이미 전부 완료됐으면 옛 목표를 새 계획에 물려주지 않는다.
        # (끝난 작업의 목표가 다음 작업의 목표처럼 보이는 것을 막는다.)
        CURRENT_TODO_GOAL = ""
    TODO_LOGS = todo_remap_logs(CURRENT_TODO, TODO_LOGS, items)  # 같은 항목의 로그는 이어감
    CURRENT_TODO = items
    PANEL.begin()  # 계획을 그리기 전에 단계 패널 자리를 정리한다
    render_todo()
    done = sum(1 for it in items if it["status"] == "done")
    active = next((it["content"] for it in items if it["status"] == "in_progress"), "")
    bits = [f"총 {len(items)}개, 완료 {done}개"]
    if active:
        bits.append(f"진행 중: {active}")
    if CURRENT_TODO_GOAL:
        bits.append(f"목표 유지: {CURRENT_TODO_GOAL}")
    if len(items) > TODO_MAX_ITEMS:
        bits.append(
            f"화면에는 {TODO_MAX_ITEMS}개만 표시(완료 항목부터 접음), 전체 {len(items)}개를 "
            "하나의 작업으로 계속 진행"
        )
    return f"{TODO_LABEL} 갱신: " + " · ".join(bits)


def tool_list_dir(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args.get("path") or ".")
    if not path.is_dir():
        return f"ERROR: 디렉터리가 아닙니다: {path}"
    rows: list[str] = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name in IGNORE_DIRS:
            continue
        if child.is_dir():
            rows.append(f"dir  {child.name}/")
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            rows.append(f"file {child.name} ({size} B)")
    return f"{path.relative_to(root) if path != root else '.'}\n" + ("\n".join(rows) if rows else "(empty)")


def should_skip_dir(name: str) -> bool:
    return name in IGNORE_DIRS or name.startswith(".")


def tool_glob(root: Path, args: dict[str, Any]) -> str:
    base = resolve_in_workspace(root, args.get("path") or ".")
    if not base.is_dir():
        return f"ERROR: 디렉터리가 아닙니다: {base}"
    pattern = args["pattern"]
    hits: list[str] = []
    for match in base.glob(pattern):
        if any(should_skip_dir(part) for part in match.relative_to(root).parts):
            continue
        try:
            rel = match.relative_to(root)
        except ValueError:
            continue
        hits.append(str(rel) + ("/" if match.is_dir() else ""))
        if len(hits) >= 200:
            hits.append("... [truncated]")
            break
    return "\n".join(hits) if hits else "(no matches)"


def tool_grep(root: Path, args: dict[str, Any]) -> str:
    """정규식 검색. output=content(기본, 줄)|files(파일명만)|count(파일별 개수),
    context=N 이면 일치 줄 앞뒤 N줄을 함께 보여준다. 비밀 파일은 건너뛴다."""
    target = resolve_in_workspace(root, args.get("path") or ".")
    flags = re.IGNORECASE if args.get("case_insensitive") else 0
    try:
        rx = re.compile(args["pattern"], flags)
    except re.error as exc:
        return f"ERROR: 잘못된 정규식: {exc}"
    output = str(args.get("output") or "content").strip().lower()
    if output not in {"content", "files", "count"}:
        return f"ERROR: 알 수 없는 output: {output} (content|files|count)"
    try:
        context = max(0, min(int(args.get("context") or 0), 20))
        max_hits = max(1, min(int(args.get("max_results") or MAX_GREP_HITS), 500))
    except (TypeError, ValueError):
        return "ERROR: context/max_results 는 정수여야 합니다."
    glob_pat = args.get("glob")
    files: list[Path] = []
    if target.is_file():
        files = [target]
    elif target.is_dir():
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if not should_skip_dir(d))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if is_secret_path(path):
                    continue  # .env 등은 검색 결과로도 내보내지 않는다
                if glob_pat:
                    rel = str(path.relative_to(root))
                    if not (fnmatch.fnmatch(name, glob_pat) or fnmatch.fnmatch(rel, glob_pat)):
                        continue
                files.append(path)
    else:
        return f"ERROR: 경로가 없습니다: {target}"

    hits: list[str] = []
    matched = 0
    scanned = 0
    for path in files:
        scanned += 1
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        text = data.decode("utf-8", errors="replace")
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        lines = text.splitlines()
        found = [i for i, line in enumerate(lines) if rx.search(line)]
        found_set = set(found)
        if not found:
            continue
        matched += len(found)
        if output == "files":
            hits.append(str(rel))
        elif output == "count":
            hits.append(f"{rel}:{len(found)}")
        elif context:
            shown: set[int] = set()
            for i in found:
                lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                if hits and lo not in shown and lo - 1 not in shown:
                    hits.append("--")
                for k in range(lo, hi):
                    if k in shown:
                        continue
                    shown.add(k)
                    sep = ":" if k in found_set else "-"
                    hits.append(f"{rel}{sep}{k + 1}{sep}{lines[k][:300]}")
        else:
            hits.extend(f"{rel}:{i + 1}:{lines[i][:300]}" for i in found)
        if len(hits) >= max_hits:
            extra = f"\n... [stopped at {max_hits} results after {scanned} files]"
            return "\n".join(hits[:max_hits]) + extra
    if not hits:
        return f"(no matches in {scanned} files)"
    if output == "count":
        hits.append(f"(total {matched} matches)")
    return "\n".join(hits)




def allow_file(session: str) -> Path:
    return HISTORY_DIR / f"shell_allow_{sanitize_session_name(session)}.json"


def shell_log_file(session: str) -> Path:
    """셸 허용/실행 로그 파일(홈·세션별). 기기 로컬 정보라 기록 위치와 무관."""
    return shelllog.log_path(HISTORY_DIR, session)


def shell_log_enabled() -> bool:
    """로그 기록 여부(`LLM_SHELL_LOG=0` 이면 끔)."""
    return os.environ.get("LLM_SHELL_LOG", "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


_SHELL_LOG: shelllog.ShellLog | None = None


def shell_log() -> shelllog.ShellLog:
    """이 세션의 셸 허용/실행 로그(처음 쓸 때 만들어 캐시)."""
    global _SHELL_LOG
    path = shell_log_file(RUN.session)
    if _SHELL_LOG is None or _SHELL_LOG.path != path:
        _SHELL_LOG = shelllog.ShellLog(path, enabled=shell_log_enabled())
    return _SHELL_LOG


def short_home(path: Path) -> str:
    """홈 아래 경로는 `~` 로 줄여 보여준다."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home + os.sep) else text


def command_key(command: str) -> str:
    """공백만 정리한 비교용 명령 문자열."""
    return re.sub(r"\s+", " ", (command or "").strip())


def command_prefix(command: str) -> str | None:
    """같은 계열로 묶어 자동 허용할 접두 키.

    셸 메타문자(파이프/연결/리다이렉션/치환)가 섞이면 정확 일치만 인정하고,
    삭제·설치류처럼 위험한 프로그램은 접두 허용을 만들지 않는다. 명령 앞의
    환경변수 지정(`FOO=1 cmd`)은 건너뛰고 프로그램 기준으로 접두를 만든다.
    """
    text = (command or "").strip()
    if not text or SHELL_META_PATTERN.search(text):
        return None
    tokens = text.split()
    envs, bare = strip_env_assignments(tokens)
    # `PATH=... cmd`처럼 동작을 바꾸는 환경변수는 계열로 묶지 않는다(정확 일치만).
    if env_assignments_unsafe(envs):
        return None
    if not bare:
        return None
    head = Path(bare[0]).name
    if head in SHELL_NO_PREFIX_HEADS or head in SHELL_FLAG_SENSITIVE_HEADS:
        return None
    if head in SHELL_SUBCOMMAND_PROGRAMS:
        if len(bare) > 1 and not bare[1].startswith("-"):
            if (head, bare[1]) in SHELL_NO_PREFIX_SUBCOMMANDS:
                return None  # `cargo fmt`처럼 같은 접두 안에 안전/위험 변형이 공존
            return f"{head} {bare[1]}"
        return None
    return head


def has_write_args(command: str) -> bool:
    """인자에 따라 쓰기·임의 실행이 켜지는 명령인지(접두 자동 허용 차단용).

    예) `find . -exec rm {} +`, `mypy --install-types .`, `sed --in-place s/a/b/ f`.
    """
    tokens = (command or "").split()
    if any(tok in SHELL_CHECK_WRITE_FLAGS for tok in tokens):
        return True
    return bool(SHELL_WRITE_ARG_PATTERN.search(command or ""))


def scope_targets(command: str, root: Path) -> tuple[str, list[Path]] | None:
    """폴더 단위 자동 허용 후보인지 보고 (프로그램, 대상 경로들)을 돌려준다.

    `rm -rf build/a`를 허용하면 같은 폴더(`build/`) 안의 다른 파일은 다시 묻지
    않도록 범위를 만들기 위한 것. 조합 명령이나 대상 경로를 알 수 없는 경우
    (`$VAR`, 백틱, 공백 포함)에는 None을 돌려 정확 일치만 인정한다.
    """
    text = scan_text(command)
    if not text or SHELL_META_PATTERN.search(text):
        return None
    envs, bare = strip_env_assignments(shell_words(text))
    if env_assignments_unsafe(envs) or not bare:
        return None
    head = Path(bare[0]).name
    if head not in SHELL_SCOPE_HEADS:
        return None
    paths: list[Path] = []
    for arg in bare[1:]:
        if arg.startswith("-"):
            continue
        if any(ch.isspace() for ch in arg) or arg.startswith("$") or "`" in arg:
            return None  # 대상을 정적으로 알 수 없다
        if arg in {"~", "~/*", "*", "/*", "/"} or arg.startswith("~"):
            return None  # 셸이 홈으로 바꾸는 경로는 여기서 판단하지 않는다
        candidate = Path(arg)
        if not candidate.is_absolute():
            candidate = root / arg
        try:
            paths.append(candidate.resolve())
        except OSError:
            return None
    if not paths:
        return None
    return head, paths


def scope_dir(path: Path) -> Path:
    """대상 경로가 속한 폴더(확장자 없는 없는 경로는 그 자체를 폴더로 본다)."""
    if not path.exists() and not path.suffix:
        return path
    return path if path.is_dir() else path.parent


def scope_key(head: str, directory: Path) -> tuple[str, str]:
    return head, str(directory)


def scope_usable(directory: Path, root: Path) -> bool:
    """그 폴더를 '여기는 다시 묻지 않기' 범위로 저장해도 되는지 판단한다."""
    text = str(directory)
    if text in SHELL_SCOPE_BLOCK_DIRS or len(directory.parts) < SHELL_SCOPE_MIN_PARTS:
        return False
    try:
        anchors = (Path.home().resolve(), root.resolve())
    except OSError:
        return False
    for anchor in anchors:
        # 홈/작업 폴더 자체나 그 상위 폴더를 통째로 허용하면 너무 넓다
        if directory == anchor or anchor.is_relative_to(directory):
            return False
    return True


def scope_label(directory: Path, root: Path) -> str:
    """사용자에게 보여줄 범위 이름(작업 폴더 기준 상대 경로)."""
    try:
        relative = directory.relative_to(root.resolve())
        return f"{relative}/" if str(relative) != "." else "./"
    except (OSError, ValueError):
        return f"{directory}/"


class ShellApproval:
    """세션별 셸 명령 자동 허용 목록(프롬프트에서 a 선택 시 누적).

    매번 같은 명령을 묻지 않도록 정확 일치(`exact`)와 계열 접두(`prefix`),
    폴더 범위(`scopes`)를 세션 파일에 저장한다.
    """

    def __init__(self, session: str) -> None:
        self.session = session
        self.path = allow_file(session)
        self.exact: set[str] = set()
        self.prefixes: set[str] = set()
        self.scopes: set[tuple[str, str]] = set()
        self.auto_all = False  # 이번 실행 동안 확인 생략(저장하지 않음)
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.exact = {str(c) for c in data.get("exact") or [] if str(c).strip()}
        self.prefixes = {str(p) for p in data.get("prefix") or [] if str(p).strip()}
        scopes = set()
        for item in data.get("scopes") or []:
            if not isinstance(item, dict):
                continue
            head, directory = str(item.get("head") or ""), str(item.get("dir") or "")
            if head and directory.startswith("/"):
                scopes.add((head, directory))
        self.scopes = scopes

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "exact": sorted(self.exact),
                        "prefix": sorted(self.prefixes),
                        "scopes": [
                            {"head": head, "dir": directory}
                            for head, directory in sorted(self.scopes)
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print_warn(f"자동 허용 목록 저장 실패: {exc}", stream=True)

    def is_allowed(self, command: str) -> bool:
        """명령(조합이면 **모든 조각**)이 자동 허용 상태인지 판단한다."""
        if self.auto_all:
            return True
        if command_key(command) in self.exact:
            return True
        segments = [c for _, c in split_shell_commands(command)] or [command_key(command)]
        return all(self.segment_allowed(segment) for segment in segments)

    def range_allowed(self, segment: str, root: Path | None) -> bool:
        """폴더 단위 허용에 포함되는 조각인지(`rm build/b` ⊆ `rm build/`)."""
        if not self.scopes or root is None:
            return False
        found = scope_targets(segment, root)
        if not found:
            return False
        head, paths = found
        for scope_head, directory in self.scopes:
            if scope_head != head:
                continue
            base = Path(directory)
            if all(path == base or base in path.parents for path in paths):
                return True
        return False

    def segment_allowed(self, segment: str, root: Path | None = None) -> bool:
        """조각 하나가 자동 허용 상태인지 판단한다."""
        if self.auto_all or command_key(segment) in self.exact:
            return True
        # 계열 접두는 인자에 따라 부작용이 켜지는 명령(`find -exec`, `-i` 등)에는
        # 적용하지 않는다. 그런 명령은 매번 확인을 받는다.
        if has_write_args(segment):
            return False
        if self.range_allowed(segment, root):
            return True
        prefix = command_prefix(segment)
        return bool(prefix) and prefix in self.prefixes

    def remember(self, command: str, root: Path | None = None) -> str:
        """이번에 실행을 허용한 조각을 기억하고, 무엇을 기억했는지 알려준다."""
        self.exact.add(command_key(command))
        labels: list[str] = []
        prefix = command_prefix(command)
        if prefix:
            self.prefixes.add(prefix)
            labels.append(prefix)
        if root is not None:
            found = scope_targets(command, root)
            if found:
                head, paths = found
                dirs = {scope_dir(path) for path in paths}
                if len(dirs) == 1:
                    directory = dirs.pop()
                    if scope_usable(directory, root):
                        key = scope_key(head, directory)
                        if key not in self.scopes:
                            self.scopes.add(key)
                        labels.append(f"{head} ({scope_label(directory, root)} 이하)")
        self.save()
        return ", ".join(labels) or command_key(command)

    def snapshot(self) -> tuple[set[str], set[str], set[tuple[str, str]], bool]:
        """되돌리기용 현재 상태 복사본(`all`의 임시 플래그 포함)."""
        return set(self.exact), set(self.prefixes), set(self.scopes), self.auto_all

    def restore(self, snap: tuple[set[str], set[str], set[tuple[str, str]], bool]) -> None:
        """스냅샷 상태로 되돌린다(중단 시 이번에 추가한 항목 취소)."""
        exact, prefixes, scopes, auto_all = snap
        if (self.exact, self.prefixes, self.scopes, self.auto_all) == (
            exact,
            prefixes,
            scopes,
            auto_all,
        ):
            return
        self.exact, self.prefixes, self.scopes = exact, prefixes, scopes
        self.auto_all = auto_all
        self.save()

    def clear(self) -> None:
        self.exact.clear()
        self.prefixes.clear()
        self.scopes.clear()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print_warn(f"자동 허용 목록 삭제 실패: {exc}", stream=True)


def shell_allow_hint(
    approval: "ShellApproval | None", target: str, root: Path | None
) -> str:
    """`a` 선택 시 무엇이 자동 허용으로 저장되는지, `all`이 무슨 뜻인지 안내한다."""
    if approval is None:
        return ""
    lines: list[str] = []
    scope = None
    if root is not None:
        found = scope_targets(target, root)
        if found:
            head, paths = found
            dirs = {scope_dir(path) for path in paths}
            if len(dirs) == 1:
                directory = dirs.pop()
                if scope_usable(directory, root):
                    scope = scope_label(directory, root)
                    lines.append(f"a = 이 명령 + {head} ({scope} 이하) 자동 허용")
    if scope is None:
        prefix = command_prefix(target)
        if prefix:
            lines.append(f"a = 이 명령/{prefix} 계열 자동 허용 (다시 묻지 않음)")
        else:
            lines.append("a = 이 명령만 자동 허용 (다시 묻지 않음)")
    lines.append("all = 이번 실행 동안 확인 생략 (저장하지 않음)")
    return "".join(f"   {C.dim}{line}{C.reset}\n" for line in lines)


def ask_shell_segment(
    label: str,
    target: str,
    approval: "ShellApproval | None",
    root: Path | None = None,
) -> str:
    """조각 하나에 대한 동의를 묻는다. 반환: `y`(이번만) / `a` / `all` / `n`(거부)."""
    keys = "[y/N/a/all]" if approval is not None else "[y/N]"
    try:
        answer = input(
            f"{label}{C.dim}실행할 명령{C.reset}: {C.bold}{target}{C.reset}\n"
            f"{shell_allow_hint(approval, target, root)}"
            f"   {C.bold}{keys}{C.reset}: "
        ).strip().lower()
    except EOFError:
        return "n"
    if answer in {"y", "yes", "ㅛ"}:
        return "y"
    if approval is None:
        return "n"
    if answer in {"all", "always", "모두"}:
        return "all"
    if answer in {"a", "ㅁ"}:
        return "a"
    return "n"


def confirm_shell(
    command: str,
    yes: bool,
    approval: "ShellApproval | None" = None,
    only_risky: bool = False,
    root: Path | None = None,
) -> bool:
    """셸 실행 동의를 받는다.

    `&&`/`||`/`|`/`;`로 이어진 조합 명령은 **조각 단위로 하나씩** 묻고 자동
    허용도 조각별로 저장한다. 그래야 조합 순서나 인자가 바뀌어도 이미 허용한
    조각은 다시 묻지 않는다. `n`으로 중단하면 이 프롬프트에서 추가한 허용은
    되돌린다(전에 허용해 둔 항목은 그대로 둔다).

    `only_risky`(기본 모드)이면 권한 상승·설치·폴더 밖 삭제 조각만 묻고
    나머지 조각은 확인 없이 통과시킨다.

    `a`는 이 명령(과 같은 폴더·계열)을 앞으로 묻지 않도록 세션에 저장하고,
    `all`은 저장하지 않고 **이번 실행 동안만** 확인을 생략한다.
    """
    if yes:
        return True
    if approval is not None and approval.is_allowed(command):
        shell_log().record("auto", command, mode=RUN.ask_mode, note="허용 목록")
        return True
    if not sys.stdin.isatty():
        print_error("셸 실행은 TTY가 아니면 --yes 가 필요합니다.", stream=True)
        return False

    def log_kind(kind: str, segment: str, note: str = "") -> None:
        shell_log().record(kind, segment, mode=RUN.ask_mode, note=note)

    def risky(segment: str) -> bool:
        return not only_risky or needs_confirm_always(segment, root)

    segments = [c for _, c in split_shell_commands(command)] or [command_key(command)]
    pending = [
        s
        for s in segments
        if risky(s) and (approval is None or not approval.segment_allowed(s, root))
    ]
    if not pending:
        log_kind("auto", command, "허용 목록(조각)")
        return True

    compound = len(segments) > 1
    suspend_panel()  # 도구 결과 패널 위에 프롬프트가 겹치지 않게 지운다
    if compound:
        print(
            f"{C.yellow}❓ 확인이 필요합니다{C.reset} "
            f"{C.dim}(조각 {len(segments)}개 중 {len(pending)}개 확인){C.reset}"
        )
        print(f"   {C.dim}전체{C.reset}: {C.bold}{command}{C.reset}")
    else:
        print(f"{C.yellow}❓ 확인이 필요합니다{C.reset}")

    added: list[str] = []
    snap = approval.snapshot() if approval is not None else None
    for idx, segment in enumerate(segments, 1):
        if not risky(segment):
            continue  # 기본 모드: 위험하지 않은 조각은 확인 없이 실행
        if approval is not None and approval.segment_allowed(segment, root):
            if compound and not approval.auto_all:
                print(f"   {C.dim}{idx}) {segment} (이미 자동 허용){C.reset}")
            log_kind("auto", segment, "허용 목록(조각)")
            continue
        label = f"   {C.dim}{idx}) {C.reset}" if compound else "   "
        answer = ask_shell_segment(label, segment, approval, root=root)
        if answer == "y":
            log_kind("y", segment, "이번만")
            continue
        if answer in {"a", "all"}:
            if approval is not None:
                if answer == "a":
                    added.append(approval.remember(segment, root))
                    log_kind("a", segment, "자동 허용에 추가")
                else:
                    # all 은 저장하지 않는다: 이번 실행 동안만 확인을 생략한다.
                    approval.auto_all = True
                    log_kind("all", segment, "이번 실행 동안 생략")
                    print_ok("이번 실행 동안 셸 확인을 묻지 않습니다. (llm 재시작 시 해제)")
            continue
        log_kind("n", segment, "취소")
        print_warn("셸 실행을 취소했습니다.", stream=True)
        if approval is not None and snap is not None:
            approval.restore(snap)  # 중단했으니 이번에 추가한 허용은 취소
        return False
    if added:
        print_ok(
            "자동 허용에 추가했습니다: "
            + ", ".join(added)
            + " (해제: llm --allow-clear)"
        )
    return True


SHELL_MAX_TIMEOUT = 600


def confirm_secret_command(command: str, reason: str) -> bool:
    """비밀을 드러낼 수 있는 명령: 자동 허용 목록·trust 와 무관하게 매번 묻는다."""
    if RUN.yes:
        return True
    if not sys.stdin.isatty():
        return False
    suspend_panel()
    print(f"{C.yellow}🔐 비밀값이 모델에 전달될 수 있는 명령입니다{C.reset} {C.dim}({reason}){C.reset}")
    try:
        answer = input(f"   {C.bold}{command}{C.reset}\n   실행할까요? {C.bold}[y/N]{C.reset}: ").strip().lower()
    except EOFError:
        answer = "n"
    resume_panel()
    return answer in {"y", "yes", "ㅛ"}


def tool_shell(
    root: Path,
    args: dict[str, Any],
    yes: bool,
    auto_safe: bool = True,
    approval: "ShellApproval | None" = None,
    auto_workspace: bool = False,
    allow_all: bool = False,
) -> str:
    bg_id = str(args.get("bg_id") or "").strip()
    command = args.get("command") or ""
    if bg_id and not command.strip():
        # 백그라운드 작업 조회/종료(새 명령 실행이 아니므로 확인 불필요)
        if args.get("kill"):
            return bg_manager().kill(bg_id)
        return bg_manager().read_new(bg_id)
    if not command.strip():
        if args.get("background") or bg_id == "list":
            return bg_manager().summary()
        return "ERROR: command가 비어 있습니다."
    for rx in SHELL_DENY:
        if rx.search(command):
            return f"ERROR: 차단된 위험한 명령 패턴입니다: {command}"
    if read_only_mode() and not is_safe_command(command):
        return (
            "ERROR: plan 모드에서는 읽기 전용 명령(ls, cat, git status/diff/log, grep, "
            "테스트 조회 등)만 실행할 수 있습니다. 계획만 제시하세요."
        )
    secret = guard.command_touches_secret(command)
    if secret and in_subagent():
        return f"ERROR: 서브에이전트는 비밀값을 드러낼 수 있는 명령을 실행할 수 없습니다({secret})."
    if secret and not (yes or confirm_secret_command(command, secret)):
        return f"ERROR: 비밀값을 드러낼 수 있는 명령이라 실행하지 않았습니다({secret})."
    timeout = int(args.get("timeout") or 60)
    timeout = max(1, min(timeout, SHELL_MAX_TIMEOUT))
    auto_run = bool(secret) or (
        approval is not None
        and approval.is_allowed(command)
        or auto_safe
        and is_safe_command(command)
        or auto_workspace
        and is_workspace_command(command, root)
        or allow_all
        and not needs_confirm_always(command, root)
    )
    if not yes and not auto_run and not confirm_shell(
        command, yes, approval, only_risky=allow_all, root=root
    ):
        return "ERROR: 사용자가 셸 실행을 거부했습니다."
    env = guard.scrubbed_env()  # API 키·토큰은 하위 프로세스에 넘기지 않는다
    if args.get("background"):
        job = bg_manager().start(command, root, env)
        return (
            f"started {job.id} (pid {job.proc.pid}) — 출력 확인: shell(bg_id=\"{job.id}\"), "
            f"종료: shell(bg_id=\"{job.id}\", kill=true)"
        )
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            executable="/bin/bash",
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return (
            f"ERROR: {timeout}s 시간 초과 (최대 {SHELL_MAX_TIMEOUT}s. 서버·watch 처럼 "
            "끝나지 않는 명령은 background=true 로 실행하세요)"
        )
    parts = [f"exit {proc.returncode}"]
    if proc.stdout:
        parts.append("--- stdout ---\n" + proc.stdout)
    if proc.stderr:
        parts.append("--- stderr ---\n" + proc.stderr)
    return clip("\n".join(parts))


def tool_web_fetch(args: dict[str, Any]) -> str:
    """URL 본문을 텍스트로 가져온다. 호스트별로 처음 한 번 확인(--trust/--yes 는 생략)."""
    url = web.validate_url(str(args.get("url") or ""))
    host = web.url_host(url)
    if not (RUN.yes or RUN.web_trust or host in RUN.web_hosts):
        if in_subagent() or not sys.stdin.isatty():
            return (
                f"ERROR: {host} 접근이 아직 허용되지 않았습니다"
                + (" (서브에이전트는 확인을 받을 수 없습니다. 메인에서 먼저 web_fetch 하세요)." if in_subagent() else " (--trust 또는 --yes 필요).")
            )
        suspend_panel()
        try:
            answer = input(
                f"{C.yellow}🌐 웹 페이지를 가져올까요?{C.reset} {C.bold}{url}{C.reset}\n"
                f"   {C.bold}[y/N/a]{C.reset} {C.dim}(a={host} 는 이번 실행 동안 묻지 않음){C.reset}: "
            ).strip().lower()
        except EOFError:
            answer = "n"
        resume_panel()
        if answer in {"a", "ㅁ"}:
            RUN.web_hosts.add(host)
        elif answer not in {"y", "yes", "ㅛ"}:
            return "ERROR: 사용자가 웹 요청을 거부했습니다."
    try:
        max_chars = max(1_000, min(int(args.get("max_chars") or web.DEFAULT_MAX_CHARS), MAX_TOOL_CHARS))
    except (TypeError, ValueError):
        max_chars = web.DEFAULT_MAX_CHARS
    return web.fetch_text(url, max_chars=max_chars)


SUBAGENT_MAX_STEPS = 25
SUBAGENT_TOOLS = ("read", "find", "shell", "web_fetch")
SUBAGENT_NOTE = (
    "\nYou are a READ-ONLY sub-agent launched by the main agent for one focused task.\n"
    "Investigate with read/find/read-only shell, then reply with a concise report:\n"
    "concrete findings with file paths and line numbers, no speculation.\n"
    "You cannot edit files and cannot ask the user questions.\n"
)


def tool_task(root: Path, args: dict[str, Any]) -> str:
    """서브에이전트: 독립된 대화로 조사만 하고 보고서를 돌려준다(메인 맥락 절약)."""
    if in_subagent():
        return "ERROR: 서브에이전트 안에서는 task 를 다시 호출할 수 없습니다."
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "ERROR: task 에는 prompt 가 필요합니다."
    system = build_system_prompt(root, RUN.session) + SUBAGENT_NOTE
    tools = [t for t in tool_schemas() if t["function"]["name"] in SUBAGENT_TOOLS]
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    _THREAD.subagent = True
    calls = 0
    try:
        for step in range(1, SUBAGENT_MAX_STEPS + 1):
            content, tool_calls, _finish, reasoning = complete(
                [{"role": "system", "content": system}, *messages],
                tools,
                RUN.model,
                stream=False,
                thinking=RUN.thinking,
            )
            if not tool_calls:
                report = (content or "").strip() or "(빈 보고서)"
                return f"[서브에이전트 보고 · {step}단계 · 도구 {calls}회]\n{report}"
            assistant: dict[str, Any] = {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
            if reasoning:
                assistant["reasoning_content"] = reasoning
            messages.append(assistant)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or "?"
                calls += 1
                if name not in SUBAGENT_TOOLS:
                    result = f"ERROR: 서브에이전트에서 쓸 수 없는 도구: {name}"
                else:
                    result = run_tool(name, fn.get("arguments") or "{}", root, yes=False)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id") or "", "name": name, "content": clip(result, 30_000)}
                )
        return f"ERROR: 서브에이전트가 {SUBAGENT_MAX_STEPS}단계 안에 끝내지 못했습니다(도구 {calls}회)."
    except RuntimeError as exc:
        return f"ERROR: 서브에이전트 API 오류: {exc}"
    finally:
        _THREAD.subagent = False


def parse_tool_args(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return {"_error": f"arguments가 객체가 아닙니다: {type(data).__name__}"}
    except json.JSONDecodeError:
        return {"_error": f"JSON 파싱 실패: {raw[:200]}"}


def run_tool(
    name: str,
    raw_args: str,
    root: Path,
    yes: bool,
    auto_safe: bool = True,
    approval: "ShellApproval | None" = None,
    auto_workspace: bool = False,
    allow_all: bool = False,
) -> str:
    args = parse_tool_args(raw_args)
    if "_error" in args:
        return f"ERROR: {args['_error']}"
    # 구 도구명 alias → 통합 도구 (이전 대화/습관 호환)
    if name == "read_file":
        name = "read"
    elif name in {"write_file", "str_replace", "delete_file"}:
        mapped = {
            "write_file": "write",
            "str_replace": "replace",
            "delete_file": "delete",
        }[name]
        args = {**args, "action": mapped}
        name = "edit"
    elif name == "list_dir":
        args = {**args, "mode": "list"}
        name = "find"
    elif name == "glob":
        args = {**args, "mode": "glob"}
        name = "find"
    elif name == "grep":
        args = {**args, "mode": "grep"}
        name = "find"
    elif name == "multi_edit":
        args = {**args, "action": "replace"}
        name = "edit"
    try:
        if name == "todo":
            result = tool_todo(args)
        elif name == "read":
            result = tool_read_file(root, args)
        elif name == "edit":
            result = tool_edit(root, args)
        elif name == "find":
            result = tool_find(root, args)
        elif name == "shell":
            result = tool_shell(
                root, args, yes, auto_safe, approval, auto_workspace, allow_all
            )
        elif name == "web_fetch":
            result = tool_web_fetch(args)
        elif name == "task":
            result = tool_task(root, args)
        else:
            return f"ERROR: 알 수 없는 도구: {name}"
    except ValueError as exc:
        result = f"ERROR: {exc}"
    except OSError as exc:
        result = f"ERROR: {exc}"
    # 알려진 비밀값(API 키 등)이 결과에 섞였으면 모델에 보내기 전에 가린다.
    return guard.redact(result, secret_values(root))


def summarize_args(name: str, raw_args: str) -> str:
    args = parse_tool_args(raw_args)
    if "_error" in args:
        return raw_args[:80]
    if name == "shell":
        if args.get("bg_id") and not args.get("command"):
            return f"{args['bg_id']} {'종료' if args.get('kill') else '출력 확인'}"
        suffix = " (백그라운드)" if args.get("background") else ""
        return intent_summary(str(args.get("command", ""))) + suffix
    if name == "todo":
        items = args.get("items")
        if isinstance(items, list):
            done = sum(
                1 for it in items if isinstance(it, dict) and it.get("status") == "done"
            )
            active = next(
                (
                    str(it.get("content") or "")
                    for it in items
                    if isinstance(it, dict) and it.get("status") == "in_progress"
                ),
                "",
            )
            base = f"{len(items)}개 항목 ({done} 완료)"
            return f"{base} · {active}" if active else base
        return ""
    if name in {"edit", "str_replace", "write_file", "delete_file"}:
        action = str(args.get("action") or (
            "replace" if name == "str_replace"
            else "write" if name == "write_file"
            else "delete" if name == "delete_file"
            else "replace"
        ))
        path = str(args.get("path") or "")
        return f"{action} {path}".strip()
    if name in {"find", "grep", "glob", "list_dir"}:
        mode = str(args.get("mode") or (
            "grep" if name == "grep"
            else "glob" if name == "glob"
            else "list"
        ))
        bits = [mode]
        if args.get("pattern"):
            bits.append(str(args["pattern"]))
        if args.get("path"):
            bits.append(str(args["path"]))
        return " ".join(bits)[:120]
    if name in {"read", "read_file"}:
        return str(args.get("path") or "")
    if name == "web_fetch":
        return str(args.get("url") or "")[:120]
    if name == "task":
        return str(args.get("description") or args.get("prompt") or "")[:120]
    if "path" in args:
        extra = []
        if args.get("pattern"):
            extra.append(str(args["pattern"]))
        return " ".join([str(args.get("path", "")), *extra]).strip()
    if args.get("pattern"):
        return str(args["pattern"])
    keys = [k for k in args if k != "contents"]
    return ", ".join(f"{k}={args[k]}" for k in keys[:4])[:120]


def report_retry(attempt: int, total: int, reason: str, delay: float) -> None:
    """API 재시도 진행 상황(429·5xx·네트워크)을 한 줄로 알린다."""
    clear_status()
    print_warn(f"API 재시도 {attempt}/{total} — {reason} · {delay:.1f}초 후", stream=True)


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 180) -> Any:
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    }
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with urlopen_with_retry(req, timeout, on_retry=report_retry) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API HTTP {exc.code}: {err[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"네트워크 오류: {exc.reason}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API JSON 파싱 실패: {body[:300]}") from exc


class StreamInterrupted(RuntimeError):
    """스트리밍이 중간에 끊겼을 때. 지금까지 받은 내용을 보존한다."""

    def __init__(self, message: str, partial: str = "", finish: str | None = None) -> None:
        super().__init__(message)
        self.partial = partial
        self.finish = finish


def stream_chat(
    payload: dict[str, Any],
    on_delta: Callable[[str, str], None] | None = None,
) -> tuple[str | None, list[dict[str, Any]], str | None, str | None, Any]:
    """content, tool_calls, finish_reason, reasoning_content, usage

    스트림이 중간에 끊기면 StreamInterrupted(부분 응답 보존)를 발생시킨다.
    `stream_options.include_usage` 를 켜면 마지막 조각에 usage 가 실려 오므로
    이를 모아 돌려준다(없으면 None).
    """
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tools: dict[int, dict[str, str]] = {}
    finish: str | None = None
    usage: Any = None

    def emit(kind: str, text: str) -> None:
        if on_delta is not None:
            on_delta(kind, text)

    try:
        with urlopen_with_retry(req, 180, on_retry=report_retry) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    err = event["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(msg)
                chunk_usage = event.get("usage")
                if isinstance(chunk_usage, dict):
                    # usage 는 보통 선택지가 빈 마지막 조각에 실려 온다.
                    usage = chunk_usage
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    delta = {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    content_parts.append(piece)
                    emit("content", piece)
                thought = delta.get("reasoning_content")
                if isinstance(thought, str) and thought:
                    reasoning_parts.append(thought)
                    emit("reasoning", thought)
                delta_tools = delta.get("tool_calls")
                if not isinstance(delta_tools, list):
                    delta_tools = []
                for tc in delta_tools:
                    if not isinstance(tc, dict):
                        continue
                    try:
                        idx = int(tc.get("index") or 0)
                    except (TypeError, ValueError):
                        idx = 0
                    slot = tools.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = str(tc["id"])
                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        continue
                    if fn.get("name"):
                        slot["name"] = str(fn["name"])
                    if fn.get("arguments"):
                        slot["arguments"] += str(fn["arguments"])
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API HTTP {exc.code}: {err[:500]}") from exc
    except RuntimeError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise StreamInterrupted(
            f"{type(exc).__name__}: {exc}",
            "".join(content_parts),
            finish,
        ) from exc
    finally:
        emit("end", "")
    content = "".join(content_parts) if content_parts else None
    reasoning = "".join(reasoning_parts) if reasoning_parts else None
    tool_calls = []
    for idx in sorted(tools):
        slot = tools[idx]
        if not slot["name"]:
            continue
        tool_calls.append(
            {
                "id": slot["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
            }
        )
    return content, tool_calls, finish, reasoning, usage


def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str,
    stream: bool,
    thinking: bool = True,
    on_delta: Callable[[str, str], None] | None = None,
) -> tuple[str | None, list[dict[str, Any]], str | None, str | None]:
    """content, tool_calls, finish_reason, reasoning_content"""

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }

    if stream:
        # 스트림 마지막 조각으로 토큰 사용량(usage)을 받는다(종료 요약용).
        payload["stream_options"] = {"include_usage": True}

    if not thinking:
        # thinking 은 기본 활성. 비활성 시 사고 과정 없이 바로 답한다.
        payload["thinking"] = {"type": "disabled"}

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    if stream:
        try:
            content, tool_calls, finish, reasoning, usage = stream_chat(
                payload, on_delta=on_delta
            )
            record_usage(usage)
            return content, tool_calls, finish, reasoning
        except StreamInterrupted as exc:
            if exc.partial:
                print_warn(
                    f"스트리밍이 중단되어 받은 내용까지만 사용합니다 ({exc}).",
                    stream=True,
                )
                return exc.partial, [], exc.finish or "interrupted", None
            print_warn(
                f"스트리밍 실패, 비스트리밍으로 재시도합니다 ({exc}).",
                stream=True,
            )
        except RuntimeError as exc:
            print_warn(
                f"스트리밍 실패, 비스트리밍으로 재시도합니다 ({exc}).",
                stream=True,
            )
        # 스트림이 아예 성립하지 않았으므로 일반 요청으로 재시도한다.
        payload.pop("stream_options", None)
        payload["stream"] = False

    data = http_json(API_URL, payload)

    if data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(msg)

    record_usage(data.get("usage"))

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content")
    reasoning = message.get("reasoning_content")

    if on_delta is not None:
        # 비스트리밍/폴백 경로에서는 사고·본문을 완료 후 한 번에 표시한다.
        if thinking and reasoning:
            on_delta("reasoning", reasoning)
        if content:
            on_delta("content", content)
        on_delta("end", "")

    raw_tools = message.get("tool_calls") or []
    tool_calls = []

    for tc in raw_tools:
        fn = tc.get("function") or {}
        tool_calls.append(
            {
                "id": tc.get("id") or "",
                "type": "function",
                "function": {
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                },
            }
        )

    return content, tool_calls, choice.get("finish_reason"), reasoning


def print_banner(
    session: str,
    root: Path,
    chat_only: bool,
    ask_mode: str = "outside",
    ask_source: str = "",
) -> None:
    mode = "채팅" if chat_only else "에이전트"
    mode_color = C.magenta if chat_only else C.cyan
    has_rules = bool(load_session_rules(root))
    rules = f"{C.green}on{C.reset}" if has_rules else f"{C.dim}off{C.reset}"
    sync_on = auto_sync_enabled() and history_scope_is_repo()
    sync = f"{C.green}on{C.reset}" if sync_on else f"{C.dim}off{C.reset}"
    confirm = ""
    if not chat_only:
        label = SHELL_ASK_LABELS.get(ask_mode, ask_mode)
        confirm = f"  {C.dim}{label}{C.reset}"
        if RUN.confirm_edits:
            confirm += f"  {C.dim}편집=확인{C.reset}"
        if RUN.plan_mode:
            confirm += f"  {C.yellow}{C.bold}PLAN{C.reset}"
    print(
        f"{C.dim}──{C.reset} {mode_color}{C.bold}{mode}{C.reset}"
        f" {C.dim}v{VERSION}{C.reset}"
        f"  {C.dim}session={C.reset}{session}"
        f"  {C.dim}root={C.reset}{root}"
        f"  {C.dim}rules={C.reset}{rules}"
        f"  {C.dim}sync={C.reset}{sync}"
        f"{confirm}"
    )
    if ask_source:
        info = SHELL_ASK_SOURCES.get(ask_source)
        if info:
            print_info(info)


def print_phase(label: str) -> None:
    """진행 단계 한 줄(사용자가 지금 무엇이 돌아가는지 보이게)."""
    print(f"{C.cyan}▸{C.reset} {label}")


def print_continue_status(root: Path, session: str, history_file: Path) -> None:
    """이어가기 상태를 한 줄로 보여 준다(로컬/원격/막힘)."""
    turns = 0
    if history_file.is_file():
        try:
            msgs = load_history(history_file)
            turns = sum(1 for m in msgs if m.get("role") == "user")
        except OSError:
            turns = 0
    parts = [f"이어가기 · 로컬 {turns}턴"]
    if not history_scope_is_repo():
        parts.append("기록=홈(동기화 안 함)")
        print_info(" · ".join(parts))
        return
    if not (root / ".git").exists() and git_run(root, "rev-parse", "--git-dir")[0] != 0:
        parts.append("git 없음")
        print_info(" · ".join(parts))
        return
    dirty = False
    rel = ""
    try:
        rel = os.path.relpath(history_file, root)
    except ValueError:
        rel = ""
    if rel and not rel.startswith(".."):
        code, out = git_run(
            root, "status", "--porcelain", "--untracked-files=all", "--", rel
        )
        dirty = code == 0 and bool(out)
    if dirty:
        parts.append(f"{C.yellow}로컬 기록 미커밋{C.reset}")
        if auto_sync_enabled():
            parts.append("끝나면 자동 sync")
        else:
            parts.append("필요 시 llm --sync-history")
    ahead = behind = 0
    if git_run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")[0] == 0:
        code, out = git_run(root, "rev-list", "--count", "HEAD..@{u}")
        if code == 0 and out.isdigit():
            behind = int(out)
        code, out = git_run(root, "rev-list", "--count", "@{u}..HEAD")
        if code == 0 and out.isdigit():
            ahead = int(out)
        if behind and dirty:
            parts.append(f"{C.yellow}원격 +{behind} · 먼저 sync 권장{C.reset}")
        elif behind:
            parts.append(f"원격 +{behind} (곧 받아옴)")
        elif ahead:
            parts.append(f"로컬 +{ahead} (미 push)")
        else:
            parts.append("원격과 같음")
    else:
        parts.append("upstream 없음")
    print_info(" · ".join(parts))


def resolve_ask_mode(args: Any) -> tuple[bool, bool, str, str]:
    """셸 확인 범위. 반환: (auto_workspace, allow_all, 모드, 출처).

    공개 모드: safe(기본) / trust / 전부확인 / 확인없음.
    구 플래그(--auto-workspace/--allow-all)는 alias.
    """
    if args.confirm_all:
        return False, False, "all", "--confirm-all"
    if args.yes:
        return False, True, "none", "--yes"
    # trust = 구 allow-all
    if getattr(args, "trust", False) or args.allow_all:
        src = "--trust" if getattr(args, "trust", False) else "--allow-all"
        return False, True, "risky", src
    # safe = 구 auto-workspace
    if getattr(args, "safe", False) or args.auto_workspace:
        src = "--safe" if getattr(args, "safe", False) else "--auto-workspace"
        return True, False, "outside", src
    if env_flag("LLM_SHELL_TRUST") or env_flag("LLM_ALLOW_ALL"):
        src = "LLM_SHELL_TRUST" if env_flag("LLM_SHELL_TRUST") else "LLM_ALLOW_ALL"
        return False, True, "risky", src
    if env_flag("LLM_AUTO_WORKSPACE"):
        return True, False, "outside", "LLM_AUTO_WORKSPACE"
    # 기본: safe (작업 폴더 안 명령은 자동, 밖·설치 등은 확인)
    return True, False, "outside", ""


# repl 기본 확인 범위(명시 옵션·환경변수가 없을 때). 값은 “전부 확인”.
REPL_DEFAULT_ASK_MODE = "all"


def repl_ask_mode(args: Any) -> tuple[bool, bool, str, str, bool]:
    """repl(대화형)의 셸 확인 범위.

    반환: (auto_workspace, allow_all, 모드, 출처, repl기본적용여부)

    대화형은 한 프로세스에서 여러 턴을 이어가며 셸을 자주 부르므로, 명시
    옵션·환경변수가 없으면 기본을 **전부 확인**(`all`)으로 둔다. 허용한
    명령은 프롬프트의 `a`/`all`로 다시 묻지 않게 하고 그 내역을 로그로 남긴다.
    `--safe`/`--trust`/`--yes`(또는 `LLM_*` 환경변수)를 주면 그대로 따른다.
    """
    auto_workspace, allow_all, ask_mode, ask_source = resolve_ask_mode(args)
    # 기본값(=safe, 출처 없음)일 때만 바꾼다. 사용자가 준 옵션·환경변수는 존중.
    if ask_mode == "outside" and not ask_source:
        return False, False, REPL_DEFAULT_ASK_MODE, "", True
    return auto_workspace, allow_all, ask_mode, ask_source, False


# 이번 실행에서 누적한 API 토큰 사용량(usage). 종료 요약에 쓴다.
USAGE_TOTAL: dict[str, int] = {}
# usage 응답에서 누적할 키(모델/버전에 따라 없는 키는 무시한다).
USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)
# 에이전트 시작 시점에 조회한 잔액 (화폐별 잔액, 원화 환산 합계).
# 종료 시 소요 비용(시작 - 종료)을 계산하는 데 쓴다.
START_BALANCE: tuple[dict[str, str], float | None] | None = None
# 활성 환율(1 화폐 = 몇 원). 가정 환율로 시작해 실시간 조회로 갱신된다.
KRW_RATES: dict[str, float] = dict(CURRENCY_KRW_RATE)
# 환율 출처 표시(예: `2026-09-12 00:02 · open.er-api.com`). 가정 환율이면 빈 값.
RATES_INFO = ""
# 실시간 환율을 쓸 수 있는지(성공 여부)와, 조회를 끄는 스위치(`--no-rates`).
RATES_LIVE = False
RATES_OFFLINE = False


# 가장 최근 API 호출의 입력 토큰 수(대화 압축 판단용)와 REPL 세션 누적 사용량(/cost)
LAST_PROMPT_TOKENS = 0
SESSION_USAGE: dict[str, int] = {}


def reset_usage() -> None:
    USAGE_TOTAL.clear()


def record_usage(usage: Any) -> None:
    """API 응답의 usage(토큰 사용량)를 누적한다. 없거나 형식이 다르면 무시."""
    if not isinstance(usage, dict):
        return
    global LAST_PROMPT_TOKENS
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and value > 0:
            USAGE_TOTAL[key] = USAGE_TOTAL.get(key, 0) + value
            SESSION_USAGE[key] = SESSION_USAGE.get(key, 0) + value
    USAGE_TOTAL["calls"] = USAGE_TOTAL.get("calls", 0) + 1
    SESSION_USAGE["calls"] = SESSION_USAGE.get("calls", 0) + 1
    if not in_subagent() and isinstance(usage.get("prompt_tokens"), int):
        LAST_PROMPT_TOKENS = usage["prompt_tokens"]


def http_get_json(url: str, timeout: int = 15) -> Any:
    """인증 헤더 없이 JSON 을 받아온다(공개 환율 API 등 제3자 요청용).

    DeepSeek 키를 제3자에게 보내지 않도록 `http_json` 과 달리 Authorization
    헤더를 붙이지 않는다.
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"네트워크 오류: {exc.reason}") from exc
    except OSError as exc:  # 타임아웃 등
        raise RuntimeError(f"네트워크 오류: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("JSON 파싱 실패") from exc


def rates_enabled() -> bool:
    """실시간 환율 조회를 쓸지. `--no-rates` 또는 `LLM_RATES=0` 이면 끈다."""
    if RATES_OFFLINE:
        return False
    return os.environ.get("LLM_RATES", "").strip() not in ("0", "false", "no", "off")


def krw_rate(cur: str) -> float | None:
    """1 단위 화폐가 몇 원인지(모르면 None). 실시간 시세가 있으면 그 값을 쓴다."""
    rate = KRW_RATES.get(cur)
    return float(rate) if isinstance(rate, (int, float)) else None


def rates_from_usd(rates: dict[str, Any]) -> dict[str, float]:
    """`1 USD` 기준 시세표를 `1 화폐 = 몇 원` 표로 바꾼다(원화 기준 교차 환산)."""
    try:
        krw_per_usd = float(rates["KRW"])
    except (KeyError, TypeError, ValueError):
        return {}
    out: dict[str, float] = {"USD": krw_per_usd, "KRW": 1.0}
    for cur, value in rates.items():
        if cur in ("USD", "KRW"):
            continue
        try:
            per_usd = float(value)
        except (TypeError, ValueError):
            continue
        if per_usd > 0:
            out[str(cur).upper()] = krw_per_usd / per_usd
    return out


def read_rate_cache() -> tuple[dict[str, float], str, bool] | None:
    """환율 캐시 파일을 읽는다. (환율, 기준시각 표시, 최신 여부). 없으면 None."""
    try:
        data = json.loads(RATE_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != RATE_CACHE_VERSION:
        return None
    raw = data.get("krw")
    if not isinstance(raw, dict) or not raw:
        return None
    rates: dict[str, float] = {}
    for cur, value in raw.items():
        try:
            rates[str(cur).upper()] = float(value)
        except (TypeError, ValueError):
            continue
    if "USD" not in rates:
        return None
    try:
        age = time.time() - float(data.get("fetched_at") or 0)
    except (TypeError, ValueError):
        age = RATE_CACHE_TTL + 1
    return rates, str(data.get("at") or ""), age <= RATE_CACHE_TTL


def write_rate_cache(rates: dict[str, float], at: str) -> None:
    """환율을 캐시 파일에 저장한다(실패해도 조용히 넘어간다)."""
    try:
        RATE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RATE_CACHE_FILE.write_text(
            json.dumps(
                {
                    "version": RATE_CACHE_VERSION,
                    "fetched_at": time.time(),
                    "at": at,
                    "source": RATE_SOURCE,
                    "krw": rates,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def load_rates(force: bool = False) -> None:
    """적용 환율(`KRW_RATES`)을 준비한다. 실패하면 가정 환율을 그대로 쓴다.

    캐시가 신선하면 그대로 쓰고, 아니면 공개 환율 API 를 조회한다. 조회도
    실패하면 오래된 캐시 → 가정 환율 순으로 되돌아간다.
    """
    global RATES_INFO, RATES_LIVE
    if not rates_enabled():
        RATES_INFO = ""
        RATES_LIVE = False
        return
    cached = read_rate_cache()
    if cached and cached[2] and not force:
        RATES_INFO = cached[1]
        RATES_LIVE = True
        KRW_RATES.update(cached[0])
        return
    try:
        data = http_get_json(RATE_URL)
        if not isinstance(data, dict) or data.get("result") not in (None, "success"):
            raise RuntimeError(str(data.get("error-type") or "응답 형식 오류"))
        rates = rates_from_usd(data.get("rates") or {})
        if "USD" not in rates:
            raise RuntimeError("환율 응답에 USD 가 없습니다")
        at = str(data.get("time_last_update_utc") or datetime.now().strftime("%Y-%m-%d %H:%M"))
        KRW_RATES.update(rates)
        RATES_INFO = at
        RATES_LIVE = True
        write_rate_cache(rates, at)
        return
    except (Exception, SystemExit) as exc:  # 네트워크/JSON 오류 등
        if cached:
            KRW_RATES.update(cached[0])
            RATES_INFO = f"{cached[1]} (오래된 시세, 갱신 실패: {exc})"
            RATES_LIVE = True
            return
        RATES_INFO = f"가정 환율 (실시간 조회 실패: {exc})"
        RATES_LIVE = False


def ordered_currencies(amounts: dict[str, str]) -> list[str]:
    """화폐 표시 순서: 아는 화폐(CNY·USD…) 먼저, 나머지는 이름순."""
    known = [cur for cur in CURRENCY_LABELS if cur in amounts]
    extra = sorted(cur for cur in amounts if cur not in CURRENCY_LABELS)
    return known + extra


def amounts_krw(amounts: dict[str, str]) -> float | None:
    """화폐별 잔액을 원화로 환산한 합계. 환율을 모르거나 숫자가 아니면 None."""
    total = 0.0
    for cur, value in amounts.items():
        rate = krw_rate(cur)
        if rate is None:
            return None
        try:
            total += float(value) * rate
        except (TypeError, ValueError):
            return None
    return total


def format_krw(krw: float | None) -> str:
    """원화 금액을 `1,197.10` 처럼 표기(모르면 `?`)."""
    return "?" if krw is None else f"{krw:,.2f}"


def format_amounts(amounts: dict[str, str]) -> str:
    """화폐별 잔액을 `2.95 CNY · 0.41 USD` 처럼 잇는다(없으면 `?`)."""
    order = ordered_currencies(amounts)
    return " · ".join(f"{amounts[cur]} {cur}" for cur in order) or "?"


def fetch_balance() -> tuple[dict[str, str], float | None]:
    """API 가 주는 **화폐별** 계정 잔액과, 그 원화 환산 합계를 돌려준다.

    DeepSeek 계정 잔액은 화폐별로 존재할 수 있다(예: CNY, USD). API 가 준
    화폐별 금액을 그대로 담고(지어내지 않는다), 표시용으로 원화 환산 합계만
    활성 환율(`KRW_RATES`, 실시간 시세 또는 가정 환율)로 계산한다.
    잔액 정보가 없으면 `({}, None)`.
    """
    data = http_json(BALANCE_URL)
    amounts: dict[str, str] = {}
    for info in data.get("balance_infos") or []:
        cur = str(info.get("currency") or "").strip().upper()
        if not cur:
            continue
        amounts[cur] = str(info.get("total_balance") or "0.00")
    if not amounts:
        return {}, None
    return amounts, amounts_krw(amounts)


def print_balance_block(
    amounts: dict[str, str], krw: float | None, extras: list[str] | None = None
) -> None:
    print_title("💵 DeepSeek 잔액 정보")
    order = ordered_currencies(amounts)
    if not order:
        print(f"  {C.dim}(API 가 잔액 정보를 주지 않았습니다){C.reset}")
    for cur in order:
        label = CURRENCY_LABELS.get(cur)
        name = f"{C.dim}{pad(label, 4)}{C.reset} " if label else ""
        note = "" if krw_rate(cur) is not None else f"  {C.dim}(환율 미지정){C.reset}"
        print(
            f"  {name}{C.dim}({cur}){C.reset}"
            f"  {C.bold}{amounts[cur]}{C.reset} {cur}{note}"
        )
    print(
        f"  {C.dim}{pad(DISPLAY_CURRENCY_LABEL, 4)}{C.reset} {C.dim}({DISPLAY_CURRENCY}){C.reset}"
        f"  {C.bold}{format_krw(krw)}{C.reset} 원  {C.dim}(환산){C.reset}"
    )
    print(f"{C.dim}{rule()}{C.reset}")
    rates = " · ".join(
        f"1 {cur} = {krw_rate(cur):,.2f} KRW" for cur in order if krw_rate(cur) is not None
    )
    if rates:
        kind = "실시간" if RATES_LIVE else "가정"
        info = f"   {C.dim}({RATES_INFO}){C.reset}" if RATES_INFO else ""
        print(f"  {C.dim}적용 환율({kind}): {rates}{C.reset}{info}")
    for line in extras or []:
        print(f"  {line}")


def cmd_balance() -> int:
    print_info("DeepSeek 계정 잔액을 조회합니다...")
    load_rates()
    amounts, krw = fetch_balance()
    print_balance_block(amounts, krw)
    return 0


def prefetch_balance() -> None:
    """에이전트 시작 전 잔액과 적용 환율을 미리 조회해 캐시한다.

    종료 시 소요 비용(시작 잔액 - 종료 잔액)을 계산하는 데 쓴다. 시작 잔액은
    화면에 출력하지 않고(종료 시 시작→종료로만 보여준다) 조용히 캐시만 한다.
    실패해도 경고만 출력하고 흐름은 막지 않는다.
    """
    global START_BALANCE
    START_BALANCE = None
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return
    load_rates()
    try:
        amounts, krw = fetch_balance()
    except (Exception, SystemExit) as exc:  # 네트워크/JSON/키 오류 등
        print_warn(f"시작 잔액을 조회하지 못했습니다: {exc}", stream=True)
        return
    START_BALANCE = (amounts, krw)


def balance_cost(end_amounts: dict[str, str]) -> dict[str, float] | None:
    """시작 대비 **화폐별** 소모액(양수면 소모). 계산할 수 없으면 None."""
    if START_BALANCE is None:
        return None
    start_amounts = START_BALANCE[0]
    costs: dict[str, float] = {}
    for cur in ordered_currencies({**start_amounts, **end_amounts}):
        try:
            start_v = float(start_amounts.get(cur, "0") or "0")
            end_v = float(end_amounts.get(cur, "0") or "0")
        except (TypeError, ValueError):
            return None
        costs[cur] = start_v - end_v
    return costs


def cost_krw(costs: dict[str, float]) -> float | None:
    """화폐별 소모액을 원화로 환산한 합계. 환율을 모르는 화폐가 있으면 None."""
    total = 0.0
    for cur, value in costs.items():
        rate = krw_rate(cur)
        if rate is None:
            return None
        total += value * rate
    return total


def cost_negligible(costs: dict[str, float]) -> bool:
    """소모액이 화폐 최소 단위(0.01) 미만이라 구분할 수 없는지.

    잔액은 화폐마다 소수 2자리로만 오므로, 반올림 단위(0.01)의 절반에
    환산한 값보다 작으면 실제 소모인지 알 수 없다. 환율을 모르는 화폐가 있으면
    판단할 수 없으니 구분 불가(`0.01 미만`)라고 하지 않는다.
    """
    if not costs:
        return True
    steps = [0.005 * rate for cur in costs if (rate := krw_rate(cur)) is not None]
    if len(steps) != len(costs):
        return False
    krw = cost_krw(costs)
    if krw is None:
        return False
    return abs(krw) < min(steps)


def cost_text(costs: dict[str, float]) -> str:
    """화폐별 소모액을 원화 기준으로 표기한다(예: `0.05 CNY (11.00 원)`).

    환산해 보여주는 화폐는 **원화**다. 계정에 화폐가 여러 개면 화폐별 내역도
    괄호에 함께 보여준다. 구분할 수 없을 만큼 작으면 금액을 지어내지 않고
    `0.01 미만`이라고만 표시한다.
    """
    krw = cost_krw(costs)
    detail = " · ".join(f"{abs(v):.2f} {cur}" for cur, v in costs.items())
    if cost_negligible(costs):
        return "0.01 미만"
    if krw is None:
        return f"{detail} (원화 환산 불가)"
    items = [
        (cur, value)
        for cur, value in costs.items()
        if krw_rate(cur) is not None and abs(value) >= 0.005
    ]
    if len(items) == 1:
        cur, value = items[0]
        return f"{abs(value):.2f} {cur} ({format_krw(abs(krw))} 원)"
    return f"{format_krw(abs(krw))} 원" + (f" ({detail})" if detail else "")


def usage_lines(totals: dict[str, int] | None = None) -> list[str]:
    """누적 토큰 사용량 요약(기본: 이번 실행. usage 가 없으면 빈 목록)."""
    totals = USAGE_TOTAL if totals is None else totals
    prompt = int(totals.get("prompt_tokens", 0))
    completion = int(totals.get("completion_tokens", 0))
    if not prompt and not completion:
        return []
    total = int(totals.get("total_tokens", 0)) or (prompt + completion)
    parts = [f"입력 {prompt:,}", f"출력 {completion:,}", f"합계 {total:,} 토큰"]
    hit = int(totals.get("prompt_cache_hit_tokens", 0))
    miss = int(totals.get("prompt_cache_miss_tokens", 0))
    if hit or miss:
        parts.append(f"캐시 적중 {hit:,} / 미적중 {miss:,}")
    calls = int(totals.get("calls", 0))
    if calls:
        parts.append(f"API {calls}회")
    return ["토큰 사용: " + " · ".join(parts)]


def show_balance_footer() -> None:
    """에이전트 종료 후 잔액과 이번 실행의 소요 비용·토큰 사용량을 보여준다."""
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        print_warn("API 키가 없어 잔액 표시를 건너뜁니다.", stream=True)
        return
    try:
        end_amounts, end_krw = fetch_balance()
    except (Exception, SystemExit) as exc:  # 네트워크/JSON/키 오류 등
        print_warn(f"잔액을 조회하지 못했습니다: {exc}", stream=True)
        return

    extras: list[str] = []
    costs = balance_cost(end_amounts)
    if costs is not None:
        krw_signed = cost_krw(costs)
        signed = krw_signed if krw_signed is not None else sum(costs.values())
        label = "소요 비용" if signed >= 0 else "잔액 증가"
        extras.append(
            f"{label}: {cost_text(costs)}"
            f"   {C.dim}(시작 {format_amounts(START_BALANCE[0])}"
            f" → 종료 {format_amounts(end_amounts)}){C.reset}"
        )
        if cost_negligible(costs):
            extras.append(
                f"{C.dim}잔액이 화폐 최소 단위(0.01)로 반올림되어 정확한 비용은 알 수 없습니다{C.reset}"
            )
    extras += usage_lines()

    print()
    print_balance_block(end_amounts, end_krw, extras=extras)


def history_to_markdown(session: str, messages: list[dict[str, Any]]) -> str:
    lines = [f"# 📖 [{session}] 세션 대화 내용", ""]
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "user":
            lines += ["## 👤 사용자", "", content, "", "---", ""]
        elif role == "assistant":
            calls = msg.get("tool_calls") or []
            if calls:
                names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
                lines += ["## 🔧 도구 호출", "", f"`{names}`", "", "---", ""]
            if content:
                lines += ["## 🤖 딥시크", "", content, "", "---", ""]
        elif role == "tool":
            preview = content if len(content) < 400 else content[:400] + "…"
            lines += ["## 📄 도구 결과", "", f"```\n{preview}\n```", "", "---", ""]
    return "\n".join(lines)


def cmd_allow(session: str, clear: bool, root: Path | None = None) -> int:
    """세션의 셸 자동 허용 목록을 보거나 비운다."""
    approval = ShellApproval(session)
    empty = not approval.exact and not approval.prefixes and not approval.scopes
    if clear:
        if empty:
            print_info(f"[{session}] 자동 허용 목록이 이미 비어 있습니다.")
            return 0
        approval.clear()
        print_ok(f"[{session}] 셸 자동 허용 목록을 비웠습니다.")
        return 0

    print_title(f"셸 자동 허용 목록  [{session}]")
    if empty:
        print_info("등록된 항목이 없습니다. 확인 프롬프트에서 a 를 선택하면 추가됩니다.")
        return 0
    for prefix in sorted(approval.prefixes):
        print(f"   {C.green}계열{C.reset}  {prefix} {C.dim}(...){C.reset}")
    for head, directory in sorted(approval.scopes):
        label = scope_label(Path(directory), root) if root is not None else f"{directory}/"
        print(f"   {C.yellow}폴더{C.reset}  {head} {C.dim}({label} 이하){C.reset}")
    for command in sorted(approval.exact):
        if command_prefix(command) not in approval.prefixes:
            print(f"   {C.cyan}정확{C.reset}  {command}")
    print_info(f"파일: {approval.path}")
    print_info("해제: llm --allow-clear")
    return 0


# `llm shell-log` 색상(종류별로 구분).
SHELL_LOG_COLORS = {
    "auto": C.green,
    "a": C.green,
    "all": C.yellow,
    "y": C.cyan,
    "n": C.red,
}


def cmd_shell_log(
    session: str,
    limit: int = shelllog.DEFAULT_VIEW_LINES,
    clear: bool = False,
) -> int:
    """이 세션의 셸 허용/실행 로그를 최근 순으로 보거나 비운다.

    확인 프롬프트에서 허용(`a`/`all`/`y`)한 명령과, 허용 목록 덕분에 프롬프트
    없이 실행된 명령(`auto`), 거부(`n`)가 한 줄씩 남는다.
    """
    log = shelllog.ShellLog(shell_log_file(session), enabled=shell_log_enabled())
    if clear:
        if log.path.is_file() and log.clear():
            print_ok(f"[{session}] 셸 로그를 비웠습니다.")
        else:
            print_info(f"[{session}] 셸 로그 파일이 없습니다.")
        return 0

    entries = log.entries()
    shown = min(max(1, limit), len(entries))
    print_title(f"셸 허용 로그  [{session}]  최근 {shown}/{len(entries)}건")
    if not entries:
        print_info("기록이 없습니다. 확인 프롬프트에서 허용하면 여기에 쌓입니다.")
    else:
        counts = shelllog.tally(entries)
        if counts:
            print_info(counts)
        for stamp, kind, command, note in shelllog.rows(entries, limit):
            color = SHELL_LOG_COLORS.get(kind, C.dim)
            line = (
                f"   {C.dim}{pad(stamp, 12)}{C.reset}"
                f" {color}{pad(shelllog.kind_label(kind), 8)}{C.reset} {command}"
            )
            if note:
                line += f" {C.dim}({note}){C.reset}"
            print(line)
    print_info(f"파일: {log.path}")
    if not shell_log_enabled():
        print_warn("LLM_SHELL_LOG=0 이라 새 기록은 남지 않습니다.")
    print_info("비우기: llm shell-log clear")
    return 0


def ref_digest(messages: list[dict[str, Any]]) -> str:
    """다른 세션 기록에서 참고용 텍스트(사용자/에이전트 발화)만 추출한다."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role == "user" and content:
            lines.append(f"[사용자] {content}")
        elif role == "assistant" and content:
            lines.append(f"[에이전트] {content}")
    return "\n".join(lines)


def build_ref_context(sessions: list[str], model: str) -> str | None:
    """--ref 세션들의 맥락을 하나의 텍스트로 만든다. 참조 대상은 수정하지 않는다.

    길면 요약을 시도하고, 실패하면 앞부분만 잘라 쓴다.
    """
    blocks: list[str] = []
    for raw in sessions:
        name = (raw or "").strip()
        if not name:
            continue
        messages = load_history(find_history_file(name))
        if not messages:
            print_warn(f"참고 세션 '{name}'에 대화 기록이 없어 건너뜁니다.")
            continue
        digest = ref_digest(messages)
        if not digest:
            print_warn(f"참고 세션 '{name}'에 참고할 발화가 없어 건너뜁니다.")
            continue
        if len(digest) > REF_MAX_CHARS:
            print_info(f"참고 세션 '{name}'이 길어 요약합니다…")
            try:
                summary = summarize_messages(messages, model)
            except RuntimeError as exc:
                print_warn(f"요약 실패({exc}); 앞부분만 사용합니다.")
                summary = None
            digest = summary if summary else digest[:REF_MAX_CHARS] + "\n…(이하 생략)"
        blocks.append(f"### 세션 '{name}'\n{digest}")
    if not blocks:
        return None
    return REF_MARKER + "\n" + "\n\n".join(blocks)


def inject_ref_context(history_file: Path, sessions: list[str], model: str) -> str | None:
    """참고 맥락을 이번 세션 대화에 사용자 메시지로 1회 주입·저장한다."""
    context = build_ref_context(sessions, model)
    if context is None:
        return None
    messages = load_history(history_file)
    messages.append({"role": "user", "content": context})
    save_history(history_file, messages)
    return context


def cmd_view(session: str, history_file: Path) -> int:
    if not history_file.is_file():
        print_error(f"[{session}] 세션에 저장된 대화가 없습니다.")
        print_info("대화를 시작하면 이 세션에 기록이 저장됩니다.")
        return 1

    messages = load_history(history_file)
    render_markdown(history_to_markdown(session, messages))
    return 0


def last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and (msg.get("content") or "").strip():
            return msg["content"]
    return ""


def extract_bash_block(text: str) -> str | None:
    m = re.search(r"```(?:bash|sh)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


def git_run(root: Path, *args: str) -> tuple[int, str]:
    """작업 폴더에서 git 을 실행한다(표준출력+오류를 합쳐 돌려준다)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return 1, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def history_sync_paths(root: Path, session: str) -> list[str]:
    """커밋 대상(이 세션의 대화 기록·요약)만 추린다. 다른 변경은 건드리지 않는다."""
    paths: list[str] = []
    try:
        rel_hist = os.path.relpath(history_file_for(session), root)
    except ValueError:
        return []
    if rel_hist.startswith(".."):
        return []
    paths.append(rel_hist)
    summaries = summaries_dir(session)
    rel_summaries = os.path.relpath(summaries, root)
    if not rel_summaries.startswith("..") and summaries.is_dir():
        paths.append(rel_summaries)
    return paths


def repo_history_sessions(root: Path) -> list[str]:
    """이 폴더(`.llm/`)에 들어 있는 대화 기록의 세션 이름 목록(git clone 직후 안내용)."""
    base = root / REPO_HISTORY_DIR_NAME
    try:
        files = sorted(base.glob("history_*.json"))
    except OSError:
        return []
    names: list[str] = []
    for path in files:
        name = path.name[len("history_") : -len(".json")]
        if not name or name.endswith(".migrated") or name in names:
            continue
        names.append(name)
    return names


def hint_repo_history(root: Path) -> None:
    """저장소 폴더에 기록이 있으면 `-s` 로 이어가라고 알려준다(조용히 넘어감)."""
    if not history_scope_is_repo():
        return
    names = repo_history_sessions(root)
    if not names:
        return
    if len(names) == 1:
        print_info(f"이 폴더(.llm/)에 대화 기록이 있습니다: llm -s {names[0]} --view")
    else:
        print_info("이 폴더(.llm/)에 대화 기록이 있습니다: " + ", ".join(names))


def auto_bind_from_repo_history(
    folders: dict[str, str], root: Path
) -> tuple[dict[str, str], str | None]:
    """clone 직후 `.llm/history_*` 가 있으면 폴더에 자동 바인딩.

    - 폴더명과 같은 세션이 있으면 우선
    - 기록이 하나뿐이면 그것
    - 여러 개·이름 불일치면 None (사용자가 -s 로 고름)
    """
    if not history_scope_is_repo():
        return folders, None
    names = repo_history_sessions(root)
    if not names:
        return folders, None
    preferred = sanitize_session_name(root.name)
    if preferred in names:
        pick = preferred
    elif len(names) == 1:
        pick = names[0]
    else:
        return folders, None
    folders, err, _ = ensure_binding(folders, root, pick)
    if err:
        return folders, None
    return folders, pick


def print_history_sync_hint(root: Path, history_file: Path) -> None:
    """기록이 아직 커밋되지 않았으면 동기화 방법을 알려준다(조용히 넘어감)."""
    if not history_scope_is_repo() or not history_file.is_file():
        return
    code, out = git_run(root, "status", "--porcelain", "--untracked-files=all", "--", os.path.relpath(history_file, root))
    if code != 0:
        return  # git 저장소가 아니면 안내하지 않는다.
    if not out:
        return
    if auto_sync_enabled():
        print_info(
            "대화 기록에 아직 올리지 못한 변경이 있습니다 — 실행이 끝나면"
            f" 다시 커밋·push 를 시도합니다 (기본은 끔, 켜짐={AUTO_SYNC_ENV}=1)."
        )
        return
    print_info(
        "대화 기록이 아직 커밋되지 않았습니다 — llm --sync-history 로 올리면"
        " 다른 PC에서 이어집니다."
    )


def auto_sync_enabled() -> bool:
    """실행 종료 시 기록 자동 동기화 여부(기본 꺼짐, `LLM_AUTO_SYNC=1`이면 켬)."""
    val = os.environ.get(AUTO_SYNC_ENV, "").strip().lower()
    if not val:
        return False
    return val in {"1", "true", "yes", "on"}


def auto_sync_history(root: Path, session: str) -> None:
    """실행이 끝나면 대화 기록을 자동으로 커밋·push 한다(실패해도 실행 결과는 그대로)."""
    if not auto_sync_enabled() or not history_scope_is_repo():
        return
    paths = history_sync_paths(root, session)
    if not paths:
        return
    # 기록에 변경이 없거나 git 저장소가 아니면 조용히 넘어간다.
    code, out = git_run(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    if code != 0 or not out:
        return
    print(f"{C.dim}{rule()}{C.reset}")
    print_phase("대화 기록 동기화 (auto)")
    if cmd_sync_history(root, session) != 0:
        print_info(f"자동 동기화를 끄려면 {AUTO_SYNC_ENV} 를 비우거나 0 으로 두세요.")


def auto_pull_enabled() -> bool:
    """질의 전 원격 기록 자동 수신 여부(기본 켜짐, `LLM_AUTO_PULL=0`이면 끔)."""
    val = os.environ.get(AUTO_PULL_ENV, "").strip().lower()
    return val not in {"0", "false", "no", "off"}


def auto_pull_interval() -> int:
    """같은 폴더에서 원격을 다시 확인하기까지의 최소 간격(초)."""
    raw = os.environ.get(AUTO_PULL_TTL_ENV, "").strip()
    return int(raw) if raw.isdigit() else AUTO_PULL_TTL_DEFAULT


def _auto_pull_seen(repo: str) -> float:
    """이 폴더에서 마지막으로 원격을 확인한 시각(없으면 0)."""
    try:
        data = json.loads(AUTO_PULL_STAMP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get(repo) or 0)
    except (TypeError, ValueError):
        return 0.0


def _mark_auto_pull(repo: str) -> None:
    """확인 시각을 기록한다(실패해도 조용히 넘어감)."""
    try:
        data = json.loads(AUTO_PULL_STAMP_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[repo] = time.time()
    try:
        AUTO_PULL_STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTO_PULL_STAMP_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def history_receive_remote(root: Path, *, reason: str = "pull") -> str:
    """원격 대화 기록을 fast-forward 로만 받는다(공통 정책).

    반환: ok | uptodate | no_upstream | dirty | diverged | fail
    - rebase 는 쓰지 않는다(갈라지면 안내만).
    - 기록(.llm)에 미커밋 변경이 있으면 받지 않는다.
    """
    if not history_scope_is_repo():
        return "no_upstream"
    if not (root / ".git").exists() and git_run(root, "rev-parse", "--git-dir")[0] != 0:
        return "no_upstream"
    if git_run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")[0] != 0:
        return "no_upstream"

    rel_history = os.path.relpath(HISTORY_BASE, root)
    if not rel_history.startswith(".."):
        code, dirty = git_run(
            root,
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            rel_history,
        )
        if code == 0 and dirty:
            return "dirty"

    code, out = git_run(root, "fetch", "--quiet")
    if code != 0:
        return "fail"
    code, out = git_run(root, "rev-list", "--count", "HEAD..@{u}")
    behind = int(out) if code == 0 and out.isdigit() else 0
    if behind <= 0:
        return "uptodate"

    print_phase(f"원격 기록 수신 (+{behind} · {reason})")
    code, out = git_run(root, "pull", "--ff-only", "--autostash")
    if code != 0:
        # ff 불가 = 갈라짐 또는 기타 실패
        code2, _ = git_run(root, "merge-base", "--is-ancestor", "HEAD", "@{u}")
        if code2 != 0:
            return "diverged"
        return "fail"
    code, conflicted = git_run(root, "diff", "--name-only", "--diff-filter=U")
    if code == 0 and conflicted:
        print_warn("원격 기록은 받아왔지만 충돌 표시가 있습니다 — git status 로 확인하세요.")
        print_info("  충돌: " + ", ".join(conflicted.split()))
        return "diverged"
    print_ok("원격의 최신 대화 기록을 받아왔습니다.")
    return "ok"


def explain_receive_status(status: str) -> None:
    """history_receive_remote 결과를 사용자에게 짧게 안내."""
    if status in {"ok", "uptodate"}:
        return
    if status == "no_upstream":
        return
    if status == "dirty":
        print_warn(
            "커밋되지 않은 대화 기록 변경이 있어 자동 수신을 건너뜁니다"
            " — 로컬 기록으로 계속합니다."
        )
        print_info("  `llm sync` 로 먼저 올린 뒤 다시 실행하면 원격 기록을 받아옵니다.")
        return
    if status == "diverged":
        print_warn("로컬과 원격 기록이 갈라져 fast-forward 할 수 없습니다.")
        print_info("  로컬 기록으로 계속합니다. 필요하면 직접 git 상태를 확인하세요.")
        return
    if status == "fail":
        print_warn("원격 대화 기록을 받지 못했습니다 — 로컬 기록으로 계속합니다.")
        return


def maybe_auto_pull(root: Path, *, enabled: bool = True) -> None:
    """질의 전 원격 최신 기록을 ff-only 로 받는다(공통 history_receive_remote)."""
    if not enabled or not auto_pull_enabled() or not history_scope_is_repo():
        return
    repo = canonical_folder(root)
    if time.time() - _auto_pull_seen(repo) < auto_pull_interval():
        return
    _mark_auto_pull(repo)
    status = history_receive_remote(root, reason="auto")
    explain_receive_status(status)


def cmd_sync_history(root: Path, session: str) -> int:
    """대화 기록(.llm/)만 커밋하고 ff-only pull 후 push 한다(rebase 없음)."""
    if not history_scope_is_repo():
        print_error(
            f"{HISTORY_SCOPE_ENV}={HISTORY_SCOPE_HOME} 라서 기록이 홈에 있습니다"
            f" ({HISTORY_DIR})."
        )
        print_info("동기화하려면 이 변수를 지우고 실행하세요 (기본값 = 세션 폴더).")
        return 1
    if not (root / ".git").exists() and git_run(root, "rev-parse", "--git-dir")[0] != 0:
        print_error(f"git 저장소가 아닙니다: {root}")
        return 1

    paths = history_sync_paths(root, session)
    if not paths:
        print_error(f"동기화할 기록 경로를 찾지 못했습니다 (기록 폴더: {HISTORY_BASE}).")
        return 1
    rel = os.path.relpath(HISTORY_BASE, root)
    code, out = git_run(root, "check-ignore", "--no-index", rel)
    if code == 0:
        print_error(f"{rel} 이 .gitignore 로 제외되어 있어 push 되지 않습니다.")
        print_info(f".gitignore 에서 '{rel}' 관련 줄을 지운 뒤 다시 실행하세요.")
        return 1

    print_phase("대화 기록 동기화")
    has_upstream = (
        git_run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")[0] == 0
    )

    # 1) 로컬 기록 먼저 커밋(미커밋이면 pull 이 dirty 로 막힘)
    code, out = git_run(root, "add", "-A", "--", *paths)
    if code != 0:
        if "ignored" in out:
            print_error(f"{rel} 이 .gitignore 로 제외되어 있어 push 되지 않습니다.")
            print_info(f".gitignore 에서 '{rel}' 관련 줄을 지운 뒤 다시 실행하세요.")
        else:
            print_error(f"git add 실패: {out.splitlines()[0] if out else '알 수 없는 오류'}")
        return 1
    staged = git_run(root, "diff", "--cached", "--name-only", "--", *paths)[1]
    if staged:
        msg = f"llm: 대화 기록 동기화 (session={session})"
        code, out = git_run(root, "commit", "-q", "-m", msg, "--", *paths)
        if code != 0:
            print_error(f"커밋 실패: {out.splitlines()[0] if out else '알 수 없는 오류'}")
            return 1
        print_ok(f"기록 커밋: {msg}")
        for line in staged.splitlines()[:8]:
            print_info(f"  {line}")
    else:
        print_info("커밋할 기록 변경이 없습니다.")

    # 2) 원격은 ff-only 만 (갈라지면 안내)
    if has_upstream:
        status = history_receive_remote(root, reason="sync")
        if status == "diverged":
            print_warn("기록이 갈라져 push 를 건너뜁니다 — 로컬 커밋은 유지됩니다.")
            explain_receive_status(status)
            return 1
        if status == "fail":
            explain_receive_status(status)
            return 1
        if status == "dirty":
            # 커밋 직후인데 dirty 면 다른 파일이 .llm 에 있음
            explain_receive_status(status)
            return 1
    else:
        print_warn("upstream(@{u})이 없어 pull/push 는 건너뜁니다 (기록만 커밋).")
        return 0

    # 3) push
    code, out = git_run(root, "push")
    if code != 0:
        print_error("push 실패 — 인증·원격 상태를 확인하세요.")
        for line in out.splitlines()[:6]:
            print_info(f"  {line}")
        return 1
    print_ok("push 완료 — 다른 PC에서 git pull 하면 이 대화가 이어집니다.")
    return 0


def cmd_run(session: str, history_file: Path) -> int:
    if not history_file.is_file():
        print_error("대화 기록이 없습니다.")
        return 1
    reply = last_assistant_text(load_history(history_file))
    if not reply:
        print_error("실행할 수 있는 최근 모델 응답이 없습니다.")
        return 1
    script = extract_bash_block(reply)
    if not script:
        print_error("최근 응답에서 bash/sh 코드 블록을 찾지 못했습니다.")
        return 1

    print_title("⚙️  실행할 스크립트")
    print(script)
    print(f"{C.dim}{rule()}{C.reset}")
    if not sys.stdin.isatty():
        print_error("TTY가 아니라 확인을 받을 수 없습니다.")
        return 1
    try:
        choice = input(f"{C.yellow}❓ 위 스크립트를 즉시 실행할까요?{C.reset} {C.bold}[y/N]{C.reset}: ").strip()
    except EOFError:
        choice = ""
    if choice.lower() not in {"y", "yes"}:
        print_warn("실행이 취소되었습니다.")
        return 0
    subprocess.run(["bash", "-c", script], check=False)
    return 0


def list_sessions(active: str | None, folders: dict[str, str], root: Path) -> None:
    files: dict[str, Path] = {}
    # 현재 기록 폴더(기본: 세션 폴더 안) 우선, 예전 홈 기록도 함께 보여준다.
    for base in (HISTORY_BASE, HISTORY_DIR):
        if not base.is_dir():
            continue
        for f in base.glob("history_*.json"):
            files.setdefault(f.name[len("history_") : -len(".json")], f)
    names = sorted(set(files) | set(folders.values()))
    mapped = folders.get(canonical_folder(root))

    print_title(f"💡 DeepSeek LLM CLI v{VERSION}")
    print(f"  {C.dim}📂 작업 폴더{C.reset}  {root}")
    mapped_txt = mapped if mapped else f"{C.yellow}(없음 — 다음 대화 때 생성){C.reset}"
    print(f"  {C.dim}🔗 매핑 세션{C.reset}  {mapped_txt}")
    rules_file = rules_path(root)
    rules_txt = str(rules_file) if rules_file.is_file() else f"{C.yellow}(없음 — llm --init-rules){C.reset}"
    print(f"  {C.dim}📜 세션 규칙{C.reset}  {rules_txt}")
    if mapped is None:
        # 폴더를 옮겨 온 흔적이 있으면, 대화를 시작하기 전에 이어받을 후보를 알려준다.
        hint_move_candidates(root)

    print_section("사용법")
    usage = [
        ('llm "질문"', "이 폴더 세션으로 에이전트 실행"),
        ("llm -i · llm repl", "대화형 REPL (여러 턴)"),
        ('llm ask "질문"', "에이전트 (서브커맨드)"),
        ('llm chat "질문"', "도구 없이 순수 채팅"),
        ("llm sync", "기록만 커밋·ff pull·push"),
        ("llm session adopt", ".llm 기록을 이 폴더에 연결"),
        ('llm --trust "질문"', "셸 관대(위험만 확인)"),
        ('llm --confirm-all "질문"', "셸 전부 확인"),
        ('llm --yes "질문"', "셸 확인 생략"),
        ("llm -s <이름>", "이 폴더를 해당 세션에 매핑"),
        ("llm --view", "대화 내용 보기"),
        ("llm --balance", "API 잔액 조회"),
    ]
    width = max(display_width(cmd) for cmd, _ in usage)
    for cmd, desc in usage:
        print(f"   {C.cyan}{pad(cmd, width)}{C.reset}  {C.dim}{desc}{C.reset}")

    print_section(f"저장된 세션  ({HISTORY_BASE})")
    if history_scope_is_repo():
        print(
            f"   {C.dim}기록이 이 폴더 안(.llm/)에 있어 push/pull 하면 다른 PC에서 그대로"
            f" 이어집니다  →  llm sync{C.reset}"
        )
    if not names:
        print(f"   {C.dim}(저장된 세션이 없습니다){C.reset}")
        print(f"{C.dim}{rule()}{C.reset}")
        return

    current = active or mapped
    for name in names:
        f = files.get(name)
        size = f"{f.stat().st_size:,} B" if f else "0 B"
        folder = folder_for_session(folders, name) or "(폴더 없음)"
        if name == current:
            print(f"   {C.green}▶ {C.bold}{name}{C.reset}  {C.dim}→{C.reset}  {folder}  {C.dim}({size}){C.reset}")
        else:
            print(f"     {name}  {C.dim}→{C.reset}  {folder}  {C.dim}({size}){C.reset}")
    print(f"{C.dim}{rule()}{C.reset}")


def ask_continue_steps(limit: int, extra: int) -> bool:
    """최대 단계 도달 시 이어서 진행할지 묻는다. `y`면 True.

    대화를 끊지 않고 사용자에게 결정을 넘긴다. TTY가 아니면(자동화/파이프)
    물어볼 수 없으므로 중단한다.
    """
    if not sys.stdin.isatty():
        print_info("TTY가 아니어서 자동으로 중단합니다. --max-steps 로 한도를 늘릴 수 있습니다.")
        return False
    try:
        answer = input(
            f"{C.yellow}⚠️  최대 단계({limit})에 도달했습니다.{C.reset} "
            f"이어서 {extra}회 더 진행할까요? {C.bold}[y/N]{C.reset}: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        eprint()
        return False
    return answer in {"y", "yes", "ㅛ"}


# 도구 줄에 보여줄 이름
TOOL_LABELS = {
    "read": "읽기",
    "edit": "편집",
    "find": "탐색",
    "shell": "셸",
    "todo": "계획",
    "web_fetch": "웹",
    "task": "서브에이전트",
    "multi_edit": "편집",
    "read_file": "읽기",
    "write_file": "쓰기",
    "str_replace": "치환",
    "delete_file": "삭제",
    "list_dir": "목록",
    "glob": "glob",
    "grep": "검색",
}
# 한 턴에 이것들만 호출되면 병렬로 실행한다(사용자 확인을 묻지 않는 읽기 전용 도구).
PARALLEL_TOOLS = {"read", "find", "task", "read_file", "list_dir", "glob", "grep"}


def cmd_agent(
    prompt: str,
    session: str,
    history_file: Path,
    root: Path,
    chat_only: bool,
    yes: bool,
    model: str,
    max_steps: int,
    stream: bool,
    thinking: bool = True,
    show_thinking: bool = False,
    auto_safe: bool = True,
    auto_workspace: bool = False,
    allow_all: bool = False,
    ask_mode: str = "outside",
    ask_source: str = "",
    no_balance: bool = False,
    auto_pull: bool = True,
    startup: bool = True,
) -> int:
    if startup:
        print_banner(session, root, chat_only, ask_mode=ask_mode, ask_source=ask_source)
        print_continue_status(root, session, history_file)
        # 다른 PC에서 올려 둔 최신 대화를 먼저 받아온다(아래 load_history 가 그대로 읽는다).
        if auto_pull and auto_pull_enabled() and history_scope_is_repo():
            print_phase("원격 대화 기록 확인")
        maybe_auto_pull(root, enabled=auto_pull)
        print_history_sync_hint(root, history_file)

    stripped = prompt.strip().lower()
    if stripped in {"/summary", "/summarize", "/요약"}:
        return cmd_summarize(session, history_file, model, force=True)

    reset_usage()
    if not chat_only and not no_balance:
        # 종료 시 소요 비용을 계산할 수 있도록 시작 잔액을 먼저 조회해 캐시한다.
        prefetch_balance()

    RUN.session = session
    RUN.model = model
    RUN.thinking = thinking
    RUN.yes = yes
    RUN.web_trust = RUN.web_trust or allow_all
    if not chat_only:
        # `@파일` 멘션: 내용을 첨부하고 '읽은 파일'로 기록해 바로 편집할 수 있게 한다.
        prompt, attached = expand_mentions(prompt, root, resolve_in_workspace, on_file=FILES.mark)
        if attached:
            print_info("첨부: " + ", ".join(f"@{a}" for a in attached))
        undo_store(session).begin_turn(prompt)

    messages = load_history(history_file)
    if messages and messages[-1].get("role") == "user":
        # 직전 턴이 답 없이 끝났으면(알림·중단) 같은 user 메시지로 합친다(연속 user 방지).
        messages[-1] = {**messages[-1], "content": f"{messages[-1].get('content') or ''}\n\n{prompt}"}
    else:
        messages.append({"role": "user", "content": prompt})
    save_history(history_file, messages)

    if chat_only:
        system = build_chat_system_prompt()
        tools = None
    else:
        system = build_system_prompt(root, session)
        tools = tool_schemas()
        reset_todo()  # 이번 실행의 작업 계획은 비워 두고 시작한다.

    progress = make_stream_progress(show_thinking) if stream else None
    approval = ShellApproval(session) if (not chat_only and not yes) else None

    # 앞선 요약(압축)에 남은 작업 계획이 있으면 새로 세우지 않고 그대로 이어간다.
    todo_seen = bool(not chat_only and restore_todo(messages))
    # 최초 도구 턴에 계획(todo)을 세우지 않으면 딱 한 번 상기시킨다.
    todo_reminded = False
    if todo_seen:
        done = sum(1 for it in CURRENT_TODO if it.get("status") == "done")
        print_info(f"이어가는 작업 계획: 전체 {len(CURRENT_TODO)}개 (완료 {done}개)")

    step = 0
    step_limit = max_steps
    started = time.monotonic()
    try:
        while True:
            step += 1
            if step > step_limit:
                # 한도 도달: 바로 끝내지 않고 이어서 진행할지 묻는다.
                if not ask_continue_steps(step_limit, max_steps):
                    print_warn(
                        f"최대 단계({step_limit})에서 멈췄습니다. "
                        "--max-steps 로 한도를 늘리거나 이어서 요청을 보내세요.",
                        stream=True,
                    )
                    return 1
                step_limit += max_steps

            # 새 단계 자리를 마련한다. 작업 계획이 있으면 계획 블록(그 안의 하위 로그)으로
            # 진행 상황을 보여주고, 없으면 예전처럼 단계 머리말 아래에 도구 줄을 그린다.
            global AGENT_STEP
            AGENT_STEP = step
            if CURRENT_TODO:
                refresh_todo_block()
            else:
                PANEL.begin(f"{C.dim}⚙️ {step}단계{C.reset}")

            # 대화가 커지면 요약·백업 후 최근 메시지만 유지한다.

            messages = maybe_summarize(
                session,
                history_file,
                messages,
                model,
                auto=True,
            )
            # 오래된 긴 도구 결과는 한꺼번에 비워 맥락을 아낀다(최근 결과는 유지).
            messages, cleared, saved = clear_old_tool_results(messages)
            if cleared:
                print_info(f"오래된 도구 결과 {cleared}개를 비워 {saved:,}자를 줄였습니다 (최근 결과는 유지).")
                save_history(history_file, messages)

            # API 호출 직전 최종 구조 검증
            repaired_messages = repair_history(messages)

            if len(repaired_messages) != len(messages):
                print_warn(
                    "API 호출 전 손상된 Tool Calling 메시지를 제거했습니다.",
                    stream=True,
                )

                messages = repaired_messages

                save_history(
                    history_file,
                    messages,
                )

            api_messages = [
                {
                    "role": "system",
                    "content": system,
                },
                *messages,
            ]

            try:
                content, tool_calls, finish, reasoning = complete(
                    api_messages,
                    tools,
                    model,
                    stream,
                    thinking,
                    on_delta=progress,
                )
            except RuntimeError as exc:
                clear_status()
                print_error(f"API 오류: {exc}", stream=True)
                return 1

            clear_status()

            if not stream and thinking and reasoning:
                # 비스트리밍 모드: 사고 과정은 기본으로 감추고 요약만 알려준다.
                if show_thinking:
                    report_reasoning = make_stream_progress(True)
                    report_reasoning("reasoning", reasoning)
                    report_reasoning("end", "")
                else:
                    print_info(
                        f"사고 과정 {len(reasoning):,}자 진행 (원문은 --show-thinking)"
                    )

            # Agent mode: execute structured tool calls and continue the loop.
            if tool_calls and not chat_only:
                names = [(tc.get("function") or {}).get("name") for tc in tool_calls]
                if "todo" in names:
                    todo_seen = True
                if (
                    not todo_seen
                    and not todo_reminded
                    and len([n for n in names if n != "todo"]) >= 2
                ):
                    # 여러 도구를 쓰는데 계획이 없으면 안내만(차단하지 않음).
                    todo_reminded = True
                    print_info(TODO_REMINDER)

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": tool_calls,
                }
                if reasoning:
                    # tools 사용 시 사고 과정을 함께 되돌려줘야 한다(프로토콜 요구).
                    # 디스크 저장 시에는 strip_reasoning 이 제거한다.
                    assistant_message["reasoning_content"] = reasoning
                messages.append(assistant_message)

                total = len(tool_calls)

                def open_line(idx: int, tc: dict[str, Any]) -> tuple[str, Any]:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or "?"
                    summary = summarize_args(name, fn.get("arguments") or "{}")
                    counter = f"{C.dim}[{idx}/{total}]{C.reset} " if total > 1 else ""
                    pretty = TOOL_LABELS.get(name, name)
                    return tool_line_open(
                        f"{counter}{C.cyan}🔧 {pretty}{C.reset} {C.dim}{summary}{C.reset}"
                    )

                def execute(tc: dict[str, Any]) -> str:
                    fn = tc.get("function") or {}
                    return run_tool(
                        fn.get("name") or "?",
                        fn.get("arguments") or "{}",
                        root,
                        yes,
                        auto_safe,
                        approval,
                        auto_workspace,
                        allow_all,
                    )

                def close_line(slot: tuple[str, Any], tc: dict[str, Any], result: str) -> None:
                    preview = result.splitlines()[0][:120] if result else ""
                    if result.startswith("ERROR:"):
                        tool_line_done(slot, f"{C.red}✗ {preview}{C.reset}")
                    else:
                        tool_line_done(slot, f"{C.green}✓{C.reset} {C.dim}{preview}{C.reset}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or "",
                            "name": (tc.get("function") or {}).get("name") or "?",
                            "content": clip(result),
                        }
                    )

                names_set = {(tc.get("function") or {}).get("name") for tc in tool_calls}
                if total > 1 and names_set <= PARALLEL_TOOLS:
                    # 읽기 전용 호출만 모였으면 병렬로 실행한다(확인 프롬프트가 없는 도구뿐).
                    slots = [open_line(idx, tc) for idx, tc in enumerate(tool_calls, 1)]
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, total))
                    try:
                        futures = [pool.submit(execute, tc) for tc in tool_calls]
                        for slot, tc, fut in zip(slots, tool_calls, futures):
                            close_line(slot, tc, fut.result())
                    finally:
                        pool.shutdown(wait=False, cancel_futures=True)
                else:
                    for idx, tc in enumerate(tool_calls, 1):
                        slot = open_line(idx, tc)
                        close_line(slot, tc, execute(tc))

                save_history(history_file, messages)
                continue

            # Chat mode must never receive structured tool calls because tools=None.
            # If a provider unexpectedly returns them, report the protocol issue.
            if tool_calls and chat_only:
                print_warn(
                    "채팅 모드에서 예상치 못한 도구 호출을 받았습니다. "
                    "최종 텍스트만 처리합니다.",
                    stream=True,
                )

            # Final answer: save first, then render exactly once.
            if content:
                messages.append({"role": "assistant", "content": content})
                save_history(history_file, messages)

                clear_status()
                if chat_only:
                    PANEL.close()
                else:
                    PANEL.freeze()  # 마지막 계획(작업 순서)을 화면에 남긴다
                print()
                if not chat_only:
                    # 동작을 마친 뒤 처리 결과(답변)로 연결한다.
                    elapsed = duration_text(time.monotonic() - started)
                    print_ok(f"작업 완료 ({step}단계 · {elapsed})")
                    if not no_balance:
                        show_balance_footer()
                print_title("🤖 답변")
                render_markdown(content)
                # 답변 블록의 끝을 표시해 다음 프롬프트와 섞이지 않게 한다.
                print(f"{C.dim}{rule()}{C.reset}")
                return 0

            print_warn(f"빈 응답 (finish={finish})", stream=True)
            return 1
    except KeyboardInterrupt:
        # Ctrl+C: 이 턴만 멈추고 대화 기록은 API 규격에 맞게 닫아 둔다(세션은 유지).
        clear_status()
        PANEL.close()
        what = finalize_interrupted(messages)
        save_history(history_file, messages)
        eprint()
        print_warn(f"중단했습니다{f' ({what})' if what else ''}. 대화는 저장되었습니다.", stream=True)
        return 130



def expand_cli_argv(argv: list[str]) -> list[str]:
    """서브커맨드를 기존 플래그로 펼친다. 예: sync → --sync-history, -i → --interactive."""
    if not argv:
        return argv
    head, rest = argv[0], argv[1:]
    if head in {"sync"}:
        return ["--sync-history", *rest]
    if head == "ask":
        return list(rest)
    if head == "chat":
        return ["--chat", *rest]
    if head in {"repl", "i"}:
        return ["--interactive", *rest]
    if head == "undo":
        return ["--undo", *rest]
    if head == "plan":
        return ["--plan", *rest]
    if head in {"shell-log", "shelllog"}:
        if rest and rest[0] in {"clear", "reset"}:
            return ["--shell-log-clear", *rest[1:]]
        return ["--shell-log", *rest]
    if head == "session":
        if not rest or rest[0] in {"ls", "list", "status"}:
            return []  # 세션 목록(인자 없는 llm 과 동일)
        if rest[0] == "adopt":
            return ["--session-adopt", *rest[1:]]
        print_error("사용법: llm session [ls|adopt]")
        return ["--help"]
    return argv


def cmd_session_adopt(root: Path, folders: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """`.llm` 기록에서 세션을 골라 이 폴더에 연결한다."""
    folders, bound = auto_bind_from_repo_history(folders, root)
    if bound:
        print_ok(f"기록에 연결했습니다 · session={bound}")
        return folders, bound
    names = repo_history_sessions(root) if history_scope_is_repo() else []
    if not names:
        print_error("이 폴더 .llm/ 에 연결할 대화 기록이 없습니다.")
        return folders, None
    print_title("연결할 세션")
    for i, name in enumerate(names, 1):
        print(f"  {C.dim}{i}.{C.reset} {name}")
    if not sys.stdin.isatty():
        print_info("TTY가 아닙니다. llm -s <이름> 으로 지정하세요.")
        return folders, None
    try:
        raw = input(f"{C.cyan}번호 또는 이름{C.reset}: ").strip()
    except (EOFError, KeyboardInterrupt):
        eprint()
        return folders, None
    if not raw:
        return folders, None
    pick = names[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(names) else raw
    if pick not in names:
        print_error(f"없는 세션: {pick}")
        return folders, None
    folders, err, detail = ensure_binding(folders, root, pick)
    if err:
        print_error(f"세션 '{pick}'은(는) 다른 폴더에 바인딩되어 있습니다: {detail}")
        return folders, None
    print_ok(f"기록에 연결했습니다 · session={pick}")
    return folders, pick


UNDO_NOTE = "[알림] 사용자가 /undo 로 직전 턴의 파일 편집을 되돌렸습니다: {files}. 해당 파일은 다시 read 한 뒤 작업하세요."


def cmd_undo(session: str, history_file: Path, assume_yes: bool = False) -> int:
    """마지막 턴에 edit 도구로 바꾼 파일을 원래대로 되돌린다."""
    store = undo_store(session)
    turn = store.last()
    if not turn or not turn.get("files"):
        print_info("되돌릴 편집이 없습니다 (edit 도구로 바꾼 파일만 추적합니다. 셸 변경은 git 으로 되돌리세요).")
        return 1
    print_title("↩️  되돌리기")
    print_info(f"{turn.get('at', '')} · \"{turn.get('prompt', '')}\"")
    for item in turn["files"]:
        tag = "새 파일 → 삭제" if item.get("before") is None else "원본 복원"
        print(f"   {C.dim}·{C.reset} {item.get('rel') or item['path']} {C.dim}({tag}){C.reset}")
    changed = store.conflicts(turn)
    if changed:
        print_warn("편집 뒤에 다시 바뀐 파일이 있습니다(되돌리면 그 변경도 사라짐): " + ", ".join(changed))
    if not assume_yes:
        if not sys.stdin.isatty():
            print_error("확인이 필요합니다. 자동화에서는 llm undo --yes 를 쓰세요.")
            return 1
        try:
            answer = input(f"   되돌릴까요? {C.bold}[y/N]{C.reset}: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in {"y", "yes", "ㅛ"}:
            print_info("취소했습니다.")
            return 1
    turn, results = store.undo_last()
    for line in results:
        print_ok(line)
    files = ", ".join(item.get("rel") or item["path"] for item in (turn or {}).get("files", []))
    messages = load_history(history_file)
    note = UNDO_NOTE.format(files=files)
    if messages and messages[-1].get("role") == "user":
        messages[-1] = {**messages[-1], "content": f"{messages[-1].get('content') or ''}\n\n{note}"}
    else:
        messages.append({"role": "user", "content": note})
    save_history(history_file, messages)
    return 0


REPL_COMMANDS = {
    "/help": "명령 목록",
    "/exit": "종료 (빈 줄·Ctrl+D 도 종료)",
    "/clear": "대화 비우기 (이전 대화는 summaries/ 에 백업)",
    "/compact": "지금 대화 요약(압축)",
    "/model": "모델 보기/바꾸기  예) /model deepseek-reasoner",
    "/cost": "이번 대화형 세션의 토큰 사용량",
    "/undo": "직전 턴의 파일 편집 되돌리기",
    "/plan": "plan 모드 켜기/끄기 (읽기 전용 조사 + 계획만)",
    "/rules": "세션 규칙·프로젝트 지침 보기",
    "/bg": "백그라운드 셸 작업 목록",
    "/log": "셸 허용/실행 로그 (llm shell-log [N|clear])",
    "/sync": "대화 기록 올리기",
    "/chat": "채팅 모드(도구 없음)",
    "/agent": "에이전트 모드",
}


def ansi_prompt(text: str) -> str:
    """readline 이 색 코드를 글자 폭으로 세지 않도록 \\001/\\002 로 감싼다."""
    return ANSI_SEQ_RE.sub(lambda m: f"\001{m.group(0)}\002", text)


def setup_readline() -> None:
    """방향키 편집·입력 히스토리(~/.local/share/llm/repl_history)·/명령 Tab 완성."""
    try:
        import readline
    except ImportError:  # 표준 라이브러리지만 일부 플랫폼엔 없다
        return
    import atexit

    hist = HISTORY_DIR / "repl_history"
    try:
        readline.read_history_file(str(hist))
    except OSError:
        pass
    readline.set_history_length(1000)

    def save() -> None:
        try:
            readline.write_history_file(str(hist))
        except OSError:
            pass

    atexit.register(save)
    commands = sorted(REPL_COMMANDS)

    def complete_cmd(text: str, state: int) -> str | None:
        options = [c for c in commands if c.startswith(text)] if text.startswith("/") else []
        return options[state] if state < len(options) else None

    readline.set_completer_delims(" \t\n")
    readline.set_completer(complete_cmd)
    readline.parse_and_bind("tab: complete")


def read_repl_input(prompt_text: str) -> str:
    """한 줄 입력. 줄 끝 `\\` 는 다음 줄로 이어지고, `\"\"\"` 로 감싸면 여러 줄 블록."""
    line = input(ansi_prompt(prompt_text))
    if line.strip() == '"""':
        lines: list[str] = []
        while True:
            more = input(ansi_prompt(f"{C.dim}...{C.reset} "))
            if more.strip() == '"""':
                return "\n".join(lines)
            lines.append(more)
    while line.endswith("\\"):
        line = line[:-1] + "\n" + input(ansi_prompt(f"{C.dim}...{C.reset} "))
    return line


def cmd_repl(
    session: str,
    history_file: Path,
    root: Path,
    args: Any,
) -> int:
    """같은 프로세스에서 여러 턴 이어가기. 종료 시 1회 sync(켜져 있을 때)."""
    auto_workspace, allow_all, ask_mode, ask_source, repl_default = repl_ask_mode(args)
    RUN.ask_mode = ask_mode
    print_banner(session, root, args.chat, ask_mode=ask_mode, ask_source=ask_source)
    print_continue_status(root, session, history_file)
    print_phase("대화형 모드")
    print_info("/help 명령 목록 · 빈 줄·/exit 종료 · Ctrl+C 는 진행 중인 턴만 중단 · @파일 첨부 · 줄 끝 \\ 여러 줄")
    if repl_default:
        print_info(
            f"셸 {C.bold}{SHELL_ASK_LABELS['all']}{C.reset}(repl 기본) "
            f"{C.dim}— 완화 --safe/--trust/--yes{C.reset} · "
            f"자동 허용 기록 {C.bold}{short_home(shell_log_file(session))}{C.reset} "
            f"{C.dim}({shell_log_enabled() and '기록 켬' or 'LLM_SHELL_LOG=0 으로 끔'}"
            f" · 조회 /log){C.reset}"
        )
    if not args.no_pull:
        maybe_auto_pull(root, enabled=True)
    setup_readline()

    chat_only = bool(args.chat)
    model = args.model
    turn = 0
    interrupted = False
    while True:
        try:
            mode_tag = "chat" if chat_only else ("plan" if RUN.plan_mode else "agent")
            line = read_repl_input(f"{C.cyan}llm:{mode_tag}>{C.reset} ").strip()
            interrupted = False
        except KeyboardInterrupt:
            eprint()
            if interrupted:
                print_info("대화형 모드를 끝냅니다.")
                break
            interrupted = True
            print_info("입력을 취소했습니다. 끝내려면 Ctrl+C 를 한 번 더 누르거나 /exit")
            continue
        except EOFError:
            eprint()
            print_info("대화형 모드를 끝냅니다.")
            break
        if not line or line in {"/exit", "/quit", ":q"}:
            print_info("대화형 모드를 끝냅니다.")
            break
        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            rest = rest.strip()
            if cmd == "/help":
                for name, desc in REPL_COMMANDS.items():
                    print(f"  {C.cyan}{pad(name, 10)}{C.reset} {desc}")
            elif cmd == "/sync":
                cmd_sync_history(root, session)
            elif cmd == "/chat":
                chat_only = True
                print_ok("채팅 모드")
            elif cmd == "/agent":
                chat_only = False
                print_ok("에이전트 모드")
            elif cmd == "/plan":
                RUN.plan_mode = not RUN.plan_mode
                print_ok("plan 모드 켬 — 읽기 전용 조사 후 계획만 제시합니다" if RUN.plan_mode else "plan 모드 끔 — 계획을 실행할 수 있습니다")
            elif cmd == "/model":
                if rest:
                    model = rest
                    print_ok(f"모델을 바꿨습니다: {model}")
                else:
                    print_info(f"현재 모델: {model}")
            elif cmd == "/cost":
                lines = usage_lines(SESSION_USAGE)
                print_info(lines[0] if lines else "아직 사용량이 없습니다.")
            elif cmd == "/compact":
                cmd_summarize(session, history_file, model, force=True)
            elif cmd == "/clear":
                old = load_history(history_file)
                if old:
                    backup = summaries_dir(session) / f"cleared-{datetime.now():%Y-%m-%dT%H-%M-%S}.json"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    print_info(f"이전 대화 백업: {backup}")
                save_history(history_file, [])
                FILES.clear()
                reset_todo()
                print_ok("대화를 비웠습니다.")
            elif cmd == "/undo":
                cmd_undo(session, history_file, assume_yes=False)
            elif cmd == "/rules":
                cmd_show_rules(root)
            elif cmd == "/bg":
                print(bg_manager().summary())
            elif cmd in {"/log", "/shell-log", "/allow-log"}:
                if rest == "clear":
                    cmd_shell_log(session, clear=True)
                else:
                    limit = int(rest) if rest.isdigit() else shelllog.DEFAULT_VIEW_LINES
                    cmd_shell_log(session, limit=limit)
            else:
                print_warn("알 수 없는 명령입니다. /help 로 목록을 보세요.")
            continue

        turn += 1
        print_phase(f"턴 {turn}")
        rc = cmd_agent(
            prompt=line,
            session=session,
            history_file=history_file,
            root=root,
            chat_only=chat_only,
            yes=args.yes,
            model=model,
            max_steps=max(1, args.max_steps),
            stream=not args.no_stream,
            thinking=not args.no_thinking,
            show_thinking=args.show_thinking,
            auto_safe=not args.confirm_all and not repl_default,
            auto_workspace=auto_workspace,
            allow_all=allow_all,
            ask_mode=ask_mode,
            ask_source=ask_source,
            no_balance=args.no_balance or turn > 1,
            auto_pull=False,
            startup=False,
        )
        if rc not in (0, 130):
            print_warn(f"이 턴 종료 코드 {rc} — 다음 입력을 받을 수 있습니다.")

    auto_sync_history(root, session)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="llm",
        add_help=True,
        description="DeepSeek CLI 에이전트",
        epilog="서브커맨드: ask | chat | plan | sync | undo | session [ls|adopt] | repl(-i)",
    )
    parser.add_argument("-s", "--session", help="세션 이름 (이 폴더에 매핑, 기존 매핑은 갈아탐)")
    parser.add_argument(
        "--ref",
        action="append",
        metavar="세션",
        help="다른 세션의 맥락을 이번 세션에 1회 주입 (여러 번 지정 가능, 참조 대상은 읽기 전용)",
    )
    parser.add_argument("--chat", action="store_true", help="도구 없이 순수 채팅")
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="대화형 REPL (같은 세션에서 여러 턴)",
    )
    parser.add_argument(
        "--session-adopt",
        action="store_true",
        help=".llm 기록을 이 폴더 세션으로 연결 (llm session adopt)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="셸 명령 확인 생략")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"모델 (기본: {DEFAULT_MODEL})")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=f"에이전트 루프 최대 횟수 (기본: {DEFAULT_MAX_STEPS}, 도달 시 이어서 진행할지 확인)",
    )
    parser.add_argument("--no-stream", action="store_true", help="스트리밍 비활성화")
    parser.add_argument(
        "--no-balance",
        action="store_true",
        help="에이전트 종료 후 잔액 표시 생략",
    )
    parser.add_argument(
        "--no-rates",
        action="store_true",
        help="실시간 환율 조회를 끄고 가정 환율로 환산 (LLM_RATES=0과 동일)",
    )
    parser.add_argument("--no-thinking", action="store_true", help="사고(thinking) 비활성화 (더 빠른 응답)")
    parser.add_argument(
        "--show-thinking",
        action="store_true",
        help="모델의 사고 과정(reasoning) 원문을 회색으로 실시간 출력",
    )
    parser.add_argument("--confirm-all", action="store_true", help="모든 셸 명령과 파일 편집 확인 (가장 엄격)")
    parser.add_argument(
        "--confirm-edits",
        action="store_true",
        help="파일 편집마다 diff 를 보여주고 확인 (LLM_CONFIRM_EDITS=1 과 동일)",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="plan 모드: 읽기 전용 조사 후 구현 계획만 제시 (llm plan \"질문\")",
    )
    parser.add_argument(
        "--undo",
        action="store_true",
        help="직전 턴에 edit 도구로 바꾼 파일 되돌리기 (llm undo, -y 로 확인 생략)",
    )
    parser.add_argument(
        "--safe",
        action="store_true",
        help="작업 폴더 안 명령은 자동(기본과 동일). 밖·설치 등은 확인",
    )
    parser.add_argument(
        "--trust",
        action="store_true",
        help="sudo·설치·폴더 밖 삭제만 확인(관대)",
    )
    parser.add_argument(
        "--allow-all",
        action="store_true",
        help="(--trust 와 동일, 구 옵션)",
    )
    parser.add_argument(
        "--auto-workspace",
        action="store_true",
        help="(--safe 와 동일, 구 옵션)",
    )
    parser.add_argument("--allow-list", action="store_true", help="이 세션의 셸 자동 허용 목록 보기")
    parser.add_argument("--allow-clear", action="store_true", help="이 세션의 셸 자동 허용 목록 비우기")
    parser.add_argument(
        "--shell-log",
        nargs="?",
        const=shelllog.DEFAULT_VIEW_LINES,
        type=int,
        metavar="N",
        help=(
            "이 세션의 셸 허용/실행 로그 보기 (최근 N건, "
            f"기본 {shelllog.DEFAULT_VIEW_LINES}) — 허용해 자동 실행된 명령 기록"
        ),
    )
    parser.add_argument("--shell-log-clear", action="store_true", help="이 세션의 셸 로그 비우기")
    parser.add_argument("--init-rules", action="store_true", help="이 폴더에 .llm/rules.md 생성")
    parser.add_argument("--rules", action="store_true", help="이 폴더의 세션 규칙 보기")
    parser.add_argument("--summarize", action="store_true", help="현재 대화를 지금 요약·백업 (강제)")
    parser.add_argument(
        "--sync-history",
        action="store_true",
        help="대화 기록만 커밋·ff-only pull·push (llm sync)",
    )
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="이번 실행은 원격의 최신 대화 기록을 자동으로 받아오지 않음 (LLM_AUTO_PULL=0과 동일)",
    )
    parser.add_argument("--balance", action="store_true")
    parser.add_argument("--view", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("prompt", nargs=argparse.REMAINDER, help="질문")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    init_color()
    raw = list(argv if argv is not None else sys.argv[1:])
    args = parse_args(expand_cli_argv(raw))

    global RATES_OFFLINE
    RATES_OFFLINE = bool(args.no_rates)

    if args.balance:
        return cmd_balance()

    # 세션은 "지금 이 폴더"에만 묶인다. --root 같은 경로 우회는 두지 않는다.
    root = Path.cwd().resolve()
    if not root.is_dir():
        print_error(f"작업 디렉터리가 없습니다: {root}")
        return 1
    folder_key = canonical_folder(root)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    # 대화 기록을 둘 폴더를 확정한다(기본: 이 폴더 안 .llm/ — push 하면 다른 PC에서 이어짐).
    set_history_base(history_scope_dir(root))
    folders = load_bindings()

    if args.session_adopt:
        folders, bound = cmd_session_adopt(root, folders)
        return 0 if bound else 1

    if args.sync_history:
        return cmd_sync_history(root, folders.get(folder_key) or root.name)

    if args.init_rules:
        rc = cmd_init_rules(root)
        if args.rules:
            return cmd_show_rules(root) if rc == 0 else rc
        return rc
    if args.rules:
        return cmd_show_rules(root)

    prompt_parts = list(args.prompt or [])
    if prompt_parts and prompt_parts[0] == "--":
        prompt_parts = prompt_parts[1:]
    prompt = " ".join(prompt_parts).strip()

    inspect_only = (
        args.view or args.run or args.clear or args.delete or args.summarize
        or args.allow_list or args.allow_clear or args.undo
        or args.shell_log is not None or args.shell_log_clear
    )
    # 남의 세션에도 허용되는 "보기만" 하는 명령.
    read_only = bool(args.view or args.allow_list or args.shell_log is not None)

    owned = folders.get(folder_key)  # 이 폴더에 묶인 세션
    session: str | None = None

    if args.session:
        session = args.session.strip()
        if not session:
            print_error("세션 이름이 비어 있습니다.")
            return 1
        owner = folder_for_session(folders, session)  # 세션을 소유한 폴더

        if owner is not None and owner != folder_key:
            # 남의 세션: 설정·수정·삭제는 거부, 조회(--view/--allow-list)만 허용.
            if not (inspect_only and read_only):
                print_error(f"세션 '{session}'은(는) 다른 폴더에 바인딩되어 있습니다:")
                print_info(f"  {owner}")
                print_info("이 폴더에서는 조회(--view)만 가능합니다.")
                print_info(f"맥락이 필요하면  llm --ref {session} \"질문\"")
                return 1
        elif owner is None and inspect_only and read_only:
            pass  # 아직 주인 없는 이름 조회: 바인딩하지 않는다.
        else:
            # 내 세션이거나, 아직 주인이 없는 이름 → 이 폴더에 바인딩(기존 매핑은 갈아탐).
            folders, err, detail = ensure_binding(folders, root, session)
            if err:
                print_error(f"세션 '{session}'은(는) 다른 폴더에 바인딩되어 있습니다:")
                print_info(f"  {detail}")
                return 1
            if owned is not None and owned != session:
                print_info(f"이 폴더의 이전 세션 '{owned}' 매핑을 해제했습니다 (기록은 유지).")
    else:
        session = owned
        if session is None:
            # clone 등으로 .llm 기록만 있는 경우 자동 연결
            folders, bound = auto_bind_from_repo_history(folders, root)
            if bound:
                session = bound
                print_ok(f"이 폴더 대화 기록에 연결했습니다 · session={session}")
        if session is None:
            if prompt or args.interactive:
                session = auto_session_name(root, folders)
                # 폴더를 옮겨 온 흔적(고아 바인딩)이 있으면, 새 세션을 만들기 전에
                # 이전 세션을 이어받을지 확인한다(사용자 확인 후 적용).
                picked = prompt_move_binding(root, session)
                if picked:
                    folders, ok = apply_move_binding(folders, root, picked)
                    if ok:
                        session = picked
                folders, err, _ = ensure_binding(folders, root, session)
                if err:
                    print_error(f"세션을 만들지 못했습니다: {err}")
                    return 1
            elif inspect_only:
                # 조회/정리(--view/--clear/--summarize 등)도 폴더를 옮겨 온
                # 흔적이 있으면 이어받을 후보를 보여주고, TTY면 골라 이어받게 한다.
                picked = prompt_move_binding(root, None) if sys.stdin.isatty() else None
                if picked:
                    folders, ok = apply_move_binding(folders, root, picked)
                    if ok:
                        session = picked
                if session is None:
                    folders, bound = auto_bind_from_repo_history(folders, root)
                    if bound:
                        session = bound
                        print_ok(f"이 폴더 대화 기록에 연결했습니다 · session={session}")
                if session is None:
                    if not sys.stdin.isatty():
                        hint_move_candidates(root)  # 자동화/파이프: 안내만
                    print_error("이 폴더에 매핑된 세션이 없습니다.")
                    print_info("대화를 시작하거나  llm -s <이름>  으로 지정하세요.")
                    hint_repo_history(root)
                    return 1
            else:
                list_sessions(None, folders, root)
                return 0

    history_file = history_file_for(session)
    RUN.session = session
    RUN.plan_mode = bool(args.plan)
    RUN.confirm_edits = bool(args.confirm_edits or args.confirm_all or env_flag("LLM_CONFIRM_EDITS"))

    if args.undo:
        return cmd_undo(session, history_file, assume_yes=args.yes)

    if args.interactive:
        return cmd_repl(session, history_file, root, args)

    if args.view:
        return cmd_view(session, history_file)
    if args.run:
        return cmd_run(session, history_file)
    if args.summarize:
        rc = cmd_summarize(session, history_file, args.model, force=True)
        auto_sync_history(root, session)
        return rc
    if args.allow_list or args.allow_clear:
        return cmd_allow(session, clear=args.allow_clear, root=root)
    if args.shell_log is not None or args.shell_log_clear:
        limit = (
            args.shell_log
            if isinstance(args.shell_log, int) and args.shell_log > 0
            else shelllog.DEFAULT_VIEW_LINES
        )
        return cmd_shell_log(session, limit=limit, clear=args.shell_log_clear)
    if args.delete:
        existed = history_file.is_file()
        if existed:
            history_file.unlink()
        folders = unbind_session(folders, session)
        if existed:
            print_ok(f"[{session}] 세션 파일을 완전히 삭제했습니다.")
        else:
            print_ok(f"[{session}] 폴더 매핑을 해제했습니다.")
        auto_sync_history(root, session)
        return 0
    if args.clear:
        if history_file.is_file():
            save_history(history_file, [])
            print_ok(f"[{session}] 세션 내용을 비웠습니다 (초기화).")
            auto_sync_history(root, session)
            return 0
        print_error(f"[{session}] 세션 파일이 존재하지 않습니다.")
        return 1

    if args.ref:
        context = inject_ref_context(history_file, args.ref, args.model)
        if context is None:
            print_error("참고(--ref)할 세션이 없습니다. 기록이 있는 세션 이름을 지정하세요.")
            return 1
        names = ", ".join(n.strip() for n in args.ref if n.strip())
        print_ok(f"[{session}]에 참고 맥락을 주입했습니다: {names}")
        if not prompt:
            auto_sync_history(root, session)
            return 0

    if not prompt:
        list_sessions(session, folders, root)
        return 0

    # 셸 확인 범위: 명시 옵션이 환경변수보다 우선한다.
    auto_workspace, allow_all, ask_mode, ask_source = resolve_ask_mode(args)
    RUN.ask_mode = ask_mode

    rc = cmd_agent(
        prompt=prompt,
        session=session,
        history_file=history_file,
        root=root,
        chat_only=args.chat,
        yes=args.yes,
        model=args.model,
        max_steps=max(1, args.max_steps),
        stream=not args.no_stream,
        thinking=not args.no_thinking,
        show_thinking=args.show_thinking,
        auto_safe=not args.confirm_all,
        auto_workspace=auto_workspace,
        allow_all=allow_all,
        ask_mode=ask_mode,
        ask_source=ask_source,
        no_balance=args.no_balance,
        auto_pull=not args.no_pull,
    )
    # 실행이 끝나면 대화 기록을 자동으로 커밋·push 한다(다음 턴으로 미루지 않는다).
    auto_sync_history(root, session)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        eprint(f"\n{C.yellow}⚠️  중단되었습니다.{C.reset}")
        sys.exit(130)
