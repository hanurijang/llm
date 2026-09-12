#!/usr/bin/env python3
"""DeepSeek CLI agent — 파일/검색/셸 도구 루프."""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# 실행 배너에 표시할 버전. 사용자에게 보이는 동작이 바뀌면 함께 올린다.
VERSION = "0.3.0"

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
# (기기 로컬 정보인 bindings.json·rates.json·shell_allow_*.json은 항상 홈에 남는다.)
HISTORY_SCOPE_ENV = "LLM_HISTORY"
REPO_HISTORY_DIR_NAME = ".llm"
HISTORY_SCOPE_HOME = "home"
# 실행이 끝나면 대화 기록을 자동으로 커밋·push 할지 정하는 환경변수(기본 켜짐).
# `LLM_AUTO_SYNC=0`(false/no/off)이면 끄고 `--sync-history`로 직접 올린다.
AUTO_SYNC_ENV = "LLM_AUTO_SYNC"
# 실행 시 `set_history_base()`가 정한다. 기본값은 홈(폴더를 알기 전 폴백).
HISTORY_BASE = HISTORY_DIR
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
# 에이전트 루프 기본 최대 단계. 한도에 도달하면 이어서 진행할지 묻고,
# y면 같은 값만큼 한도를 늘려 계속한다.
DEFAULT_MAX_STEPS = 50
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
# 최초 턴에 계획을 세우지 않을 때 1회 주입하는 상기 메시지.
TODO_REMINDER = (
    "작업을 시작하기 전에 먼저 `todo` 도구로 `goal`(이번 작업의 목표·결정한 조치를 "
    "한두 문장으로 요약)과 전체 작업 계획(짧은 항목 목록)을 세워 사용자에게 보여주세요. "
    "이후에는 계획을 새로 만들지 말고 **같은 계획**을 이어서 "
    "status(pending/in_progress/done)만 갱신하세요. 대화가 요약(압축)된 뒤에도 요약에 "
    "남은 기존 계획을 그대로 이어가고, 항목이 많아 일부가 접혀 보여도 하나의 작업으로 "
    "계속 진행하세요."
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
SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
}
SHELL_DENY = (
    re.compile(r"\brm\s+-[rf]{1,2}\s+/\s*(?:$|;|&|\|)"),
    re.compile(r"\bmkfs\."),
    re.compile(r"\bdd\s+.*\bof=/dev/"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&"),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
)

# 기본 모드(권한 상승·설치만 확인): 이 두 부류는 그대로 확인을 받는다.
SHELL_ASK_HEADS = {"sudo", "su", "doas", "pkexec"}
SHELL_ASK_PATTERNS = (
    re.compile(
        r"\b(?:apt|apt-get|dnf|yum|zypper|pacman|snap|brew|apk|choco|winget|port)\b"
        r"[^|;&]*\b(?:install|add|upgrade|dist-upgrade|remove|purge|reinstall)\b"
    ),
    re.compile(r"\b(?:pip|pip3|pipx)\b[^|;&]*\b(?:install|uninstall|download)\b"),
    re.compile(r"\bpython3?\b[^|;&]*\b-m\s+pip\b"),
    re.compile(
        r"\b(?:npm|pnpm|yarn|bun|deno)\b[^|;&]*"
        r"\b(?:install|i|ci|add|link|global|update|upgrade)\b"
    ),
    re.compile(r"\buv\b[^|;&]*\b(?:install|add|sync|tool)\b"),
    re.compile(r"\b(?:poetry|pipenv|pdm|hatch)\b[^|;&]*\b(?:install|add|sync)\b"),
    re.compile(
        r"\b(?:cargo|rustup|gem|go|composer|bundle|dotnet|nuget|vcpkg)\b"
        r"[^|;&]*\b(?:install|add|require|restore|get|update|upgrade)\b"
    ),
    re.compile(r"\b(?:npx|bunx)\b"),
    re.compile(r"\bmake\s+(?:-\S+\s+)*install\b"),
)
# 작업 폴더 밖을 지우는 명령도 기본 모드에서 확인한다(홈/시스템 실수 방지)
SHELL_ASK_DELETE_HEADS = {"rm", "rmdir", "shred", "truncate", "dd"}
# 셸 확인 범위(모드)와 배너에 보여줄 이름
SHELL_ASK_LABELS = {
    "all": "all (모든 명령)",
    "risky": "risky (위험 명령만)",
    "outside": "outside (폴더 밖 포함)",
    "none": "none (확인 없음)",
}
# 모드가 CLI 옵션이 아니라 환경변수에서 온 경우의 안내 문구
SHELL_ASK_SOURCES = {
    "LLM_AUTO_WORKSPACE": (
        "환경변수 LLM_AUTO_WORKSPACE=1 로 'outside' 모드입니다"
        " (조합 명령의 조각마다 확인). .env 에서 지우면 기본 'risky' 로 돌아갑니다."
    ),
    "LLM_ALLOW_ALL": (
        "환경변수 LLM_ALLOW_ALL=1 로 'risky' 모드입니다"
        " (권한 상승·설치·폴더 밖 삭제만 확인)."
    ),
}
# heredoc 본문을 코드로 실행하는 셸/실행기: 본문도 명령으로 보고 확인을 받는다
SHELL_HEREDOC_EXEC_HEADS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "source", "eval", "sudo"}


def _deletes_outside_workspace(segment: str, root: Path) -> bool:
    """`rm ~/x`, `rm -rf /etc/...`, `rm -rf ../x`처럼 폴더 밖을 지우는지."""
    tokens = shell_words(segment)
    _, tokens = strip_env_assignments(tokens)
    if not tokens or Path(tokens[0]).name not in SHELL_ASK_DELETE_HEADS:
        return False
    for tok in tokens[1:]:
        if tok.startswith("-") or any(ch.isspace() for ch in tok):
            continue
        cleaned = tok.strip().strip("'\"")
        if not cleaned or cleaned.startswith("$"):
            continue
        if cleaned.startswith(("/", "~")) or ".." in cleaned:
            return True
    return False


def heredoc_shell_exec(command: str) -> bool:
    """heredoc 본문을 `bash`/`sh` 같은 셸에 코드로 넘기는 명령인지.

    본문은 보통 데이터라 `install` 같은 단어가 있어도 무시하지만, 셸
    인터프리터에 넘길 때는 그대로 실행되는 코드이므로 확인을 받는다.
    """
    for line in (command or "").splitlines():
        if not heredoc_marker(line):
            continue
        tokens = shell_words(line)
        for tok in tokens:
            if tok in SHELL_HEREDOC_EXEC_HEADS:
                return True
    return False


def needs_confirm_always(command: str, root: Path | None = None) -> bool:
    """기본 모드에서도 확인을 받아야 하는 명령인지(권한 상승·설치·폴더 밖 삭제).

    heredoc 본문(예: `python3 - <<EOF ... EOF`의 코드)은 실행할 명령이 아니라
    데이터이므로 `install` 같은 단어가 들어 있어도 설치로 오해하지 않는다.
    단 `bash <<EOF`처럼 본문이 그대로 실행되는 경우는 확인을 받는다.
    """
    raw = (command or "").strip()
    text = scan_text(raw)
    if not text:
        return True
    if heredoc_shell_exec(raw):
        return True
    segments = chain_segments(text) or [text]
    for segment in segments:
        _, bare = strip_env_assignments(shell_words(segment))
        if bare and Path(bare[0]).name in SHELL_ASK_HEADS:
            return True
        if any(rx.search(segment) for rx in SHELL_ASK_PATTERNS):
            return True
        if root is not None and _deletes_outside_workspace(segment, root):
            return True
    return False

# 확인 없이 바로 실행해도 되는 읽기 전용 명령(첫 토큰 기준)
SHELL_SAFE_COMMANDS = {
    "pwd", "ls", "tree", "cat", "head", "tail", "wc", "stat", "file", "du", "df",
    "grep", "rg", "egrep", "fgrep", "find", "fd", "sort", "uniq", "cut", "nl",
    "tr", "column", "diff", "cmp", "comm", "paste", "fold", "rev", "tac",
    "jq", "which", "whereis", "type", "command", "env", "printenv", "echo",
    "date", "whoami", "id", "uname", "hostname", "dirname", "basename",
    "realpath", "readlink", "md5sum", "sha1sum", "sha256sum", "base64",
    # 셸 자체 부작용이 없는 명령(서브프로세스라 cd는 이후 명령에 영향 없음)
    "cd", "sleep", "true", "false",
}
SHELL_SAFE_GIT_SUBCOMMANDS = {
    "status", "diff", "log", "show", "branch", "remote", "ls-files",
    "rev-parse", "rev-list", "blame", "shortlog", "describe", "tag",
    # index만 바꾸고 `git reset`으로 되돌릴 수 있어 자동 실행 대상으로 둔다
    "add",
}
SHELL_SAFE_PY_MODULES = {
    "py_compile", "compileall", "json.tool", "unittest", "pytest", "mypy",
}
# 테스트/린트/타입 검사 도구: 기본 동작이 "검사"라 자동 실행한다
SHELL_SAFE_CHECKERS = {
    "pytest", "ruff", "flake8", "mypy", "pyright", "eslint", "shellcheck",
    "hadolint", "yamllint", "gofmt", "checkov", "bandit",
}
# 위 검사 도구라도 파일을 고치는 플래그가 있으면 확인 대상
SHELL_CHECK_WRITE_FLAGS = {
    "--fix", "--write", "--fix-only", "-w", "--in-place", "--install-types",
}
# black/prettier 등은 기본 동작이 "쓰기"라 검사 플래그일 때만 자동 실행
SHELL_PAIRS_ONLY_READONLY = (("black", {"--check", "--diff"}), ("prettier", {"--check"}))
# 첫 토큰이 안전 목록에 없어도 두 토큰 조합이 조회 전용이면 자동 실행한다
SHELL_SAFE_PAIRS = {
    ("pip", "list"), ("pip", "show"), ("pip", "freeze"), ("pip", "--version"),
    ("pip3", "list"), ("pip3", "show"), ("pip3", "freeze"), ("pip3", "--version"),
    ("node", "--version"), ("node", "-v"),
    ("rustc", "--version"), ("java", "-version"),
    ("tsc", "--noEmit"), ("tsc", "--version"),
}
# 서브커맨드로 동작이 갈리는 프로그램: 조회/검증 서브커맨드만 자동 실행
SHELL_SAFE_SUBCOMMANDS = {
    "npm": {"ls", "list", "view", "outdated", "test", "why"},
    "yarn": {"test", "list", "why", "info"},
    "pnpm": {"test", "list", "why", "outdated"},
    "bun": {"test"},
    "cargo": {"test", "build", "check", "clippy", "metadata", "tree", "bench", "doc"},
    "go": {"test", "build", "vet", "list", "doc", "version", "env"},
    "docker": {"ps", "images", "version", "info", "inspect"},
    "kubectl": {
        "get", "describe", "logs", "version", "explain", "top",
        "api-resources", "api-versions", "cluster-info",
    },
    "systemctl": {
        "status", "is-active", "is-enabled", "is-failed", "show", "cat",
        "list-units", "list-timers", "list-dependencies",
    },
    "gradle": {"test", "build", "tasks", "--status"},
    "dotnet": {"test", "build", "--info", "--list-sdks", "--list-runtimes"},
    "poetry": {"show", "check", "about", "version"},
}
# `npm run <script>` / `yarn <script>`: 아래 스크립트 이름만 조회/검증으로 본다
SHELL_SAFE_RUN_SCRIPTS = {
    "test", "tests", "lint", "typecheck", "check", "build", "fmt", "format",
    "verify", "ci", "coverage", "e2e",
}
# `make <target>`: 아래 타깃만 자동 실행(clean/install/deploy 등은 확인)
SHELL_SAFE_MAKE_TARGETS = {
    "test", "tests", "check", "lint", "build", "fmt", "format", "verify",
    "ci", "all", "docs", "typecheck",
}
# 안전 판정을 막는 위험 요소(쓰기·임의 실행 유발)
SHELL_UNSAFE_PATTERN = re.compile(
    r"[><`;]|\$\(|\bsudo\b|\bxargs\b"
    r"|\|\s*(?:sh|bash|zsh|node|python3?|perl|ruby|eval|tee|dd|rm|mv|cp)\b"
    r"|\b(?:rm|mv|cp|dd|tee|chmod|chown|chgrp|kill|pkill|truncate|ln|touch"
    r"|mkdir|install|curl|wget|eval|source)\b"
    r"|\bgit\s+(?:push|commit|checkout|switch|reset|clean|rm|mv|apply"
    r"|stash|fetch|pull)\b"
    r"|-(?:exec|execdir|delete)\b|--output|--in-place"
)
# 무해한 리다이렉션(출력 버리기/에러 합치기)은 무시한다
SHELL_HARMLESS_REDIRECT = re.compile(r"\d?>\s*/dev/null|\d?>&\s*\d|&>\s*/dev/null")

# `FOO=bar cmd`처럼 명령 앞에 붙는 환경변수 지정
SHELL_ENV_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
# 값이 코드 로딩/경로 해석을 바꿔 "같은 계열"이라도 동작이 달라지는 이름
SHELL_ENV_UNSAFE_NAMES = {
    "PATH", "IFS", "SHELLOPTS", "BASH_ENV", "ENV", "PROMPT_COMMAND",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "PERL5OPT", "RUBYOPT",
    "RUBYLIB", "GEM_HOME", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_DIR", "GIT_WORK_TREE",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH", "GOFLAGS",
    "HISTFILE", "PS4", "BASH_XTRACEFD", "SSH_AUTH_SOCK", "HOME",
}


def strip_env_assignments(tokens: list[str]) -> tuple[list[str], list[str]]:
    """앞머리 `NAME=value` 지정을 분리해 (지정 목록, 남은 토큰)을 돌려준다."""
    envs: list[str] = []
    rest = list(tokens)
    while len(rest) > 1 and SHELL_ENV_ASSIGN_RE.fullmatch(rest[0]):
        envs.append(rest.pop(0))
    return envs, rest


def env_assignments_unsafe(envs: list[str]) -> bool:
    """환경변수 지정이 동작·코드 로딩을 바꿀 수 있는 이름인지 판단한다."""
    return any(env.split("=", 1)[0] in SHELL_ENV_UNSAFE_NAMES for env in envs)


def shell_words(text: str) -> list[str]:
    """셸 토큰으로 나눈다(인용부호 해석).

    단순 `split()`은 `echo "== /etc/x =="`를 `["==`, `/etc/x`, `=="]`로 쪼개
    메시지 속 경로를 실제 경로로 오해한다(불필요한 확인 요청의 큰 원인).
    인용부호가 닫히지 않아 해석할 수 없으면 공백 분리로 대체한다.
    """
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:
        return text.split()

# 계열(접두) 자동 허용을 만들 때는 인자에 따라 동작이 바뀌는 명령을 막는다.
# 접두로 묶인 뒤 다른 인자가 붙어 부작용이 생기는 것을 예방한다.
SHELL_WRITE_ARG_PATTERN = re.compile(
    r"-(?:exec|execdir|delete|fprint0?)\b|--in-place|--output\b|\bsudo\b|\bxargs\b"
)

# 셸 메타문자(파이프/연결/리다이렉션/치환) — 있으면 접두 자동 허용을 만들지 않는다
SHELL_META_PATTERN = re.compile(r"[;&|<>`\n]|\$\(")
# 삭제·설치·임의 실행류: 접두 허용 금지(정확 일치만)
SHELL_NO_PREFIX_HEADS = {
    "rm", "rmdir", "mv", "cp", "dd", "truncate", "shred", "chmod", "chown",
    "chgrp", "ln", "install", "sudo", "su", "sh", "bash", "zsh", "eval",
    "source", "kill", "pkill", "killall", "tee", "curl", "wget", "env",
    "python", "python3", "perl", "ruby", "node", "nohup", "xargs",
}
# 서브커맨드형 프로그램: "git add"처럼 첫 두 토큰을 접두로 묶는다
SHELL_SUBCOMMAND_PROGRAMS = {
    "git", "npm", "yarn", "pnpm", "bun", "cargo", "go", "docker",
    "docker-compose", "kubectl", "make", "pip", "pip3", "apt", "apt-get",
    "systemctl", "gh", "poetry", "uv", "deno", "gradle", "mvn", "dotnet",
}
# 같은 접두 안에 안전/위험 변형이 공존해 계열 자동 허용을 금지하는 조합
SHELL_NO_PREFIX_SUBCOMMANDS = {("cargo", "fmt"), ("cargo", "clippy")}
# 인자(플래그)에 따라 쓰기 동작이 켜지는 프로그램: 계열 허용 금지(정확 일치만)
SHELL_FLAG_SENSITIVE_HEADS = {
    "black", "prettier", "ruff", "eslint", "gofmt", "biome", "checkov",
    "standard", "stylelint",
}

# --- 폴더 단위 자동 허용: `rm build/x`를 허용하면 `rm build/y`는 다시 묻지 않는다 ---
# 대상 경로가 한 폴더 안에 모이는 프로그램만 대상(인자에 따라 위험도가 달라지지 않는 것).
SHELL_SCOPE_HEADS = {
    "rm", "rmdir", "shred", "truncate", "mv", "cp", "ln", "chmod", "chown",
    "chgrp", "touch", "mkdir", "tee", "unzip", "tar",
}
# 폴더 통째로 자동 허용하면 위험한 상위 경로(시스템·홈·임시 루트)
SHELL_SCOPE_BLOCK_DIRS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt",
    "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var",
}
# 이보다 얕은 경로는 범위로 삼지 않는다(`/tmp/sub` 같은 넓은 폴더 방지)
SHELL_SCOPE_MIN_PARTS = 3

# --- `--auto-workspace`: 작업 폴더 안에서 끝나는 부작용만 확인 없이 실행 ---
# 권한·시스템·원격·설치·컨테이너·임의 코드 실행: workspace 경계를 넘을 수 있어 제외
SHELL_WORKSPACE_BLOCK_HEADS = {
    "sudo", "su", "doas", "systemctl", "service", "mount", "umount", "insmod",
    "modprobe", "crontab", "at", "batch", "launchctl", "shutdown", "reboot",
    "halt", "poweroff", "kill", "pkill", "killall",
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync",
    "telnet", "ftp", "socat", "nohup",
    "apt", "apt-get", "yum", "dnf", "pacman", "brew", "snap", "gem",
    "pip", "pip3", "uv", "npm", "npx", "yarn", "pnpm", "bun", "conda",
    "mamba", "poetry", "go", "cargo", "gradle", "mvn", "dotnet", "make",
    "docker", "docker-compose", "podman", "kubectl", "helm", "minikube",
    "vagrant",
    "sh", "bash", "zsh", "fish", "source", "eval", "env", "xargs",
}
# git 이력·설정·원격을 건드리는 서브커맨드(되돌리기 어려운 것)
SHELL_WORKSPACE_BLOCK_GIT = {
    "push", "pull", "fetch", "reset", "clean", "checkout", "switch", "stash",
    "rm", "mv", "rebase", "apply", "restore", "config", "remote", "submodule",
    "clone", "init", "filter-branch", "filter-repo", "daemon", "worktree",
}
SHELL_WORKSPACE_BLOCK_GIT_FLAGS = {"--amend", "--hard", "--force"}
# 코드를 문자열로 받아 실행하는 인터프리터 플래그
SHELL_WORKSPACE_CODE_FLAGS = {"-c", "--command", "-e", "--eval"}
SHELL_WORKSPACE_INTERPRETERS = {
    "python", "python3", "perl", "ruby", "node", "php", "lua", "deno",
}
# find로 쓰기·삭제·임의 실행을 켜는 인자
SHELL_WORKSPACE_FIND_WRITE_ARGS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fls",
}
# 아래 조합은 workspace 안이어도 되돌릴 수 없어 제외(복구 수단·작업 폴더 자체)
SHELL_WORKSPACE_PROTECTED_TARGETS = {".", "..", "/", "~", "*", "./*", ".git", "./.git"}
SHELL_WORKSPACE_PROTECTED_HEADS = {"rm", "rmdir", "mv", "chmod", "chown", "shred"}
SHELL_WORKSPACE_SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".fish", ".command", ".ps1")
SHELL_WORKSPACE_SPECIAL_PATHS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
# 대상 경로를 정적으로 알 수 없으면(=변수/치환) 확인을 받아야 하는 명령.
# 삭제·이동·권한 변경처럼 되돌리기 어려운 것만 해당한다(읽기용은 `$t`를 무시).
SHELL_PATH_STRICT_HEADS = (
    SHELL_WORKSPACE_PROTECTED_HEADS | SHELL_ASK_DELETE_HEADS
    | {"cp", "ln", "tee", "install", "sed", "tar", "unzip", "find"}
)


def is_safe_command(command: str) -> bool:
    """확인 없이 실행해도 되는 읽기 전용/검증 명령인지 판단한다.

    `ls && git status`처럼 연결된 명령은 **모든 조각이 자동 실행 대상일 때만**
    자동 실행한다. 위험 요소는 연결 전체를 대상으로 먼저 검사하므로,
    `ls && rm -rf x` 같은 조합은 여기서 걸러진다.
    """
    text = scan_text(command)
    if not text or SHELL_UNSAFE_PATTERN.search(text):
        return False
    parts = chain_segments(text)
    if not parts:
        return False
    return all(_is_safe_simple_command(part) for part in parts)


def _is_safe_simple_command(part: str) -> bool:
    """연결 없는 단일 명령이 자동 실행 대상인지 판단한다."""
    tokens = shell_words(part)
    if not tokens:
        return False
    # `FOO=1 cmd`의 앞머리 지정은 떼고 프로그램으로 판단한다.
    envs, tokens = strip_env_assignments(tokens)
    if env_assignments_unsafe(envs):
        return False  # PATH/LD_PRELOAD 등은 실행 대상이 달라질 수 있다
    head = Path(tokens[0]).name
    rest = tokens[1:]
    if head in {"python", "python3"}:
        return _safe_python(rest)
    if head == "git":
        return bool(rest) and (
            rest[0] in SHELL_SAFE_GIT_SUBCOMMANDS
            or rest[0] in {"--version", "--help", "-v"}
        )
    if head in SHELL_SAFE_COMMANDS:
        return True
    if head in SHELL_SAFE_CHECKERS:
        if head == "ruff" and rest[:1] == ["format"]:
            return "--check" in rest or "--diff" in rest  # `ruff format`은 파일을 고친다
        return not any(tok in SHELL_CHECK_WRITE_FLAGS for tok in rest)
    for program, readonly_args in SHELL_PAIRS_ONLY_READONLY:
        if head == program:
            return bool(rest) and rest[0] in readonly_args
    if head in {"npm", "yarn", "pnpm", "bun"}:
        return _safe_package_command(head, rest)
    if head == "make":
        return _safe_make(rest)
    # 경로/스크립트 실행(`./run.sh`)은 제외하고, 순수 명령의 버전/도움말만 허용
    if tokens[0] == head and "." not in head and rest[:1] in (
        ["--version"], ["--help"], ["-V"], ["-h"], ["-v"]
    ):
        return True
    if head in SHELL_SAFE_SUBCOMMANDS:
        if head == "cargo" and rest[:1] == ["fmt"]:
            return "--check" in rest  # `cargo fmt`는 파일을 고친다
        if any(tok in SHELL_CHECK_WRITE_FLAGS for tok in rest):
            return False  # `cargo clippy --fix`처럼 서브커맨드에 쓰기 플래그가 붙는 경우
        return bool(rest) and rest[0] in SHELL_SAFE_SUBCOMMANDS[head]
    return bool(rest) and (head, rest[0]) in SHELL_SAFE_PAIRS


def _safe_package_command(head: str, rest: list[str]) -> bool:
    """npm/yarn/pnpm/bun 명령이 조회·검증 수준인지 판단한다."""
    if not rest:
        return False
    if rest[0] == "run":
        return len(rest) > 1 and rest[1] in SHELL_SAFE_RUN_SCRIPTS
    return rest[0] in SHELL_SAFE_SUBCOMMANDS.get(head, set())


def _safe_make(rest: list[str]) -> bool:
    """make 타깃이 빌드/검증 계열인지 판단한다(clean·install·deploy는 제외)."""
    targets = [tok for tok in rest if not tok.startswith("-")]
    if not targets:
        return False
    return all(tok in SHELL_SAFE_MAKE_TARGETS for tok in targets)


def _safe_python(rest: list[str]) -> bool:
    """python3 호출이 검증/실행 수준인지 판단한다(-c 등 임의 코드는 제외)."""
    if not rest:
        return False
    if any(arg in {"-c", "-i", "-e", "-"} for arg in rest):
        return False
    if "-m" in rest:
        idx = rest.index("-m")
        return len(rest) > idx + 1 and rest[idx + 1] in SHELL_SAFE_PY_MODULES
    if rest[0].startswith("-"):
        return all(arg in {"--version", "-V", "-h", "--help"} for arg in rest)
    script = rest[0]
    if script.startswith(("/", "~")) or ".." in script:
        return False
    return script.endswith(".py")


def _workspace_path_ok(root: Path, token: str, strict: bool = False) -> bool:
    """토큰이 가리키는 경로가 workspace 안(또는 무해한 특수 경로)인지.

    `strict`는 삭제·이동처럼 대상 경로를 정적으로 알 수 없으면 위험한 명령에
    쓴다. 그 외에는 인용된 산문(공백 포함)과 변수(`$t`)를 경로로 보지 않아
    `echo "== /etc/x 여부 =="` 같은 읽기용 메시지로 확인을 요구하지 않는다.
    """
    raw = token.strip()
    if not raw:
        return True
    if any(ch.isspace() for ch in raw):
        return True  # 인용된 문장은 경로가 아니라 화면에 찍는 글이다
    raw = raw.strip("'\"")
    if not raw:
        return True
    if raw.startswith("-"):
        if "=" not in raw:
            return True  # 플래그 자체는 경로가 아니다
        raw = raw.split("=", 1)[1].strip().strip("'\"")
    if not raw:
        return True
    if raw in SHELL_WORKSPACE_SPECIAL_PATHS or raw.startswith("/dev/fd/"):
        return True
    if any(ch in raw for ch in "$`"):
        return not strict  # 대상 경로를 정적으로 알 수 없다(삭제류는 확인)
    if raw.startswith("~"):
        return False  # 홈 디렉터리는 workspace 밖
    if "/" not in raw and raw not in {".", ".."}:
        return True  # 단순 파일명은 cwd(=workspace) 기준
    try:
        base = Path(raw).resolve() if raw.startswith("/") else (root / raw).resolve()
    except (OSError, RuntimeError):
        return False
    root_s = str(root.resolve())
    base_s = str(base)
    return base_s == root_s or base_s.startswith(root_s + os.sep)


def _workspace_fragment_safe(fragment: str, root: Path) -> bool:
    """연결 명령의 한 조각이 workspace 안에서 끝나고 되돌릴 수 있는지."""
    tokens = shell_words(fragment)
    if not tokens:
        return False
    envs, bare = strip_env_assignments(tokens)
    head = Path(bare[0]).name if bare else ""
    strict = head in SHELL_PATH_STRICT_HEADS
    # 경로가 workspace를 벗어나면 무엇이든 확인 대상
    if not all(_workspace_path_ok(root, tok, strict) for tok in tokens):
        return False
    # 읽기 전용/검증 명령이면 조각 단독으로도 그대로 허용(`npm test`, `tail -5`)
    if is_safe_command(fragment):
        return True
    # 리다이렉션 대상만 뺀 명령이 읽기 전용/검증이면 허용(`npm test > out.txt`)
    body = re.sub(r"\d*>>?\s*\S+", " ", fragment).strip()
    if body and body != fragment and is_safe_command(body):
        return True
    if env_assignments_unsafe(envs):
        return False  # PATH/LD_PRELOAD 등은 실행 대상이 달라질 수 있다
    if head in SHELL_WORKSPACE_BLOCK_HEADS:
        return False
    if head in SHELL_WORKSPACE_INTERPRETERS:
        if any(tok in SHELL_WORKSPACE_CODE_FLAGS for tok in bare):
            return False  # `python3 -c ...` 같은 임의 코드 실행
        if not [tok for tok in bare[1:] if not tok.startswith("-")]:
            return False  # 인터프리터 단독 실행(REPL/stdin)
    elif bare[0].startswith("./") or head.endswith(SHELL_WORKSPACE_SCRIPT_SUFFIXES):
        # 스크립트 파일 실행은 내용을 검증할 수 없다. 인터프리터는 경로로 지정해도
        # (`./.venv/bin/python - <<EOF`) 위 인터프리터 규칙으로 판단한다.
        return False
    if head == "find" and any(tok in SHELL_WORKSPACE_FIND_WRITE_ARGS for tok in bare):
        return False
    if head == "git":
        if any(tok in SHELL_WORKSPACE_BLOCK_GIT for tok in bare[1:]):
            return False
        if any(tok in SHELL_WORKSPACE_BLOCK_GIT_FLAGS for tok in bare[1:]):
            return False
    if head in SHELL_WORKSPACE_PROTECTED_HEADS:
        for tok in bare[1:]:
            cleaned = tok.strip().strip("'\"")
            if cleaned in SHELL_WORKSPACE_PROTECTED_TARGETS or cleaned.startswith(".git"):
                return False
    return True


def is_workspace_command(command: str, root: Path) -> bool:
    """`--auto-workspace`: workspace 안에서 끝나는 부작용만 자동 실행한다.

    경로가 workspace를 벗어나거나(`~/x`, `/etc/x`, `../x`), 설치·네트워크·권한
    명령, 임의 코드 실행, git 이력 파괴로 이어지면 False를 돌려 확인을 받는다.
    """
    text = scan_text(command)
    if not text:
        return False
    if any(rx.search(text) for rx in SHELL_DENY):
        return False
    fragments = chain_segments(text)
    if not fragments:
        return False
    return all(_workspace_fragment_safe(frag, root) for frag in fragments)


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
    """터미널에서는 glow로 Markdown을 렌더링하고, 불가능하면 원문을 출력한다."""
    if not text:
        return

    use_glow = (
        not raw
        and sys.stdout.isatty()
        and shutil.which("glow")
        and not os.environ.get("LLM_RAW")
    )

    if not use_glow:
        print(text)
        return

    width = max(40, min(terminal_width(), 120))
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".md",
            delete=False,
        ) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)

        proc = subprocess.run(
            [
                "glow",
                "--style",
                "dark",
                "-w",
                str(width),
                "-n",
                str(tmp_path),
            ],
            check=False,
        )

        if proc.returncode != 0:
            print(text)

    except OSError:
        print(text)

    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def clear_status() -> None:
    if sys.stderr.isatty():
        eprint("\r\033[K", end="")


def make_stream_progress(show_thinking: bool = False) -> Callable[[str, str], None]:
    """스트리밍 진행 상황을 stderr에 표시한다.

    종류(kind)는 `reasoning` | `content` | `end`.
    - `reasoning`: 사고 과정(reasoning) 진행 상황. show_thinking 이면 원문을
      회색으로 실시간 출력하고, 기본값(False)에서는 글자 수만 한 줄로 보여준다.
    - `content`: 최종 답변은 완료 후 glow 로 한 번만 렌더링하므로 글자 수만 표시.
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

    head = messages[:-SUMMARY_KEEP_RECENT] or messages
    print_info(
        f"대화가 커져 요약합니다 "
        f"(메시지 {len(messages)}개, 압축 대상 {history_chars(head):,}자)..."
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
    print(f"{C.dim}{rule()}{C.reset}")
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
        "Prefer grep/glob/list_dir before reading large files.\n"
        "Edit with str_replace when changing existing files; use write_file for new files.\n"
        "Keep changes minimal and correct. After edits, sanity-check with read or tests if appropriate.\n"
        "Work with a todo list: ALWAYS call the `todo` tool first to lay out a concise\n"
        "plan with its `goal` (one or two sentences summarizing your intended approach),\n"
        "then update each item's status (pending/in_progress/done) as you progress.\n"
        "Keep ONE plan for the whole job: if a plan (or a goal) already exists in the\n"
        "context — including a previous summary — continue that SAME plan, just update\n"
        "its statuses; never restart a new plan. If the list is folded in the terminal\n"
        "(over the display limit), keep going with the same single job.\n"
        "Final answer format: lead with one or two sentences stating the outcome,\n"
        "then short sections only when they apply (changed files, verification,\n"
        "remaining work). Do not restate the plan — the todo panel already shows it —\n"
        "and keep each bullet to a single line.\n"
        "Do not exfiltrate secrets. Never print API keys or .env contents.\n"
        "Reply in Korean unless the user writes in another language.\n"
        f"Session name: {session}\n"
        f"Workspace root: {root}\n"
        "Top-level entries:\n"
        f"{workspace_listing(root)}\n"
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
            "작업 계획(todo)을 설정하거나 갱신한다. 에이전트는 작업을 시작할 때 "
            "반드시 이것을 먼저 호출해 goal(목표·조치 요약)과 할 일 목록을 사용자에게 "
            "보여주고, 진행에 따라 각 항목의 status 를 갱신한다. 호출할 때마다 전체 "
            "목록을 다시 보낸다. 이미 계획이 있으면(요약을 넘어 유지됨) 새로 만들지 "
            "말고 같은 계획을 이어서 갱신한다. goal 을 생략하면 이전 목표가 유지된다.",
            {
                "goal": {
                    "type": "string",
                    "description": (
                        "이번 작업의 목표 또는 결정한 조치를 한두 문장으로 요약. "
                        "목록 상단에 표시되며, 생략하면 이전 목표를 유지한다."
                    ),
                },
                "items": {
                    "type": "array",
                    "description": "전체 할 일 목록(현재 상태 포함).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "할 일 한 줄"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "done"],
                                "description": "진행 상태 (기본 pending)",
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            ["items"],
        ),
        fn(
            "read_file",
            "Read a text file. 1-indexed line numbers are prefixed. Use offset/limit for large files.",
            {
                "path": {"type": "string", "description": "Path relative to workspace root"},
                "offset": {"type": "integer", "description": "1-indexed start line"},
                "limit": {"type": "integer", "description": "Max number of lines to return"},
            },
            ["path"],
        ),
        fn(
            "write_file",
            "Create or overwrite a text file with the given contents.",
            {
                "path": {"type": "string"},
                "contents": {"type": "string"},
            },
            ["path", "contents"],
        ),
        fn(
            "str_replace",
            "Replace a unique old_string with new_string in a file. Fails if old_string is missing or not unique unless replace_all is true.",
            {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
            },
            ["path", "old_string", "new_string"],
        ),
        fn(
            "delete_file",
            "Delete a file inside the workspace.",
            {"path": {"type": "string"}},
            ["path"],
        ),
        fn(
            "list_dir",
            "List files and directories at a path.",
            {"path": {"type": "string", "description": "Directory relative to workspace; default ."}},
            [],
        ),
        fn(
            "glob",
            "Find files by glob pattern (e.g. **/*.py) under the workspace.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Subdirectory to search from"},
            },
            ["pattern"],
        ),
        fn(
            "grep",
            "Search file contents with a regular expression.",
            {
                "pattern": {"type": "string", "description": "Python/regex pattern"},
                "path": {"type": "string", "description": "File or directory to search"},
                "glob": {"type": "string", "description": "Optional filename glob filter, e.g. *.py"},
                "case_insensitive": {"type": "boolean"},
            },
            ["pattern"],
        ),
        fn(
            "shell",
            "Run a bash command in the workspace root. Use for git, tests, builds, and inspection. Not for reading secrets.",
            {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Seconds, default 60, max 180"},
            },
            ["command"],
        ),
    ]


def is_secret_path(path: Path) -> bool:
    name = path.name
    if name in SECRET_NAMES or name.endswith(".pem") or name.endswith(".key"):
        return True
    lowered = {part.lower() for part in path.parts}
    if ".ssh" in lowered:
        return True
    return False


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


def tool_read_file(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args["path"])
    if not path.is_file():
        return f"ERROR: 파일이 없습니다: {path}"
    data = path.read_bytes()
    if b"\0" in data[:4096]:
        return "ERROR: 바이너리 파일은 읽을 수 없습니다."
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    offset = int(args.get("offset") or 1)
    if offset < 1:
        offset = 1
    limit = args.get("limit")
    start = offset - 1
    end = start + int(limit) if limit else len(lines)
    slice_lines = lines[start:end]
    numbered = [f"{i:>6}|{line}" for i, line in enumerate(slice_lines, start=offset)]
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
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = args.get("contents", "")
    path.write_text(contents, encoding="utf-8")
    rel = path.relative_to(root)
    return f"wrote {rel} ({len(contents.splitlines())} lines, {len(contents.encode('utf-8'))} bytes)"


def tool_str_replace(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args["path"])
    if not path.is_file():
        return f"ERROR: 파일이 없습니다: {path}"
    text = path.read_text(encoding="utf-8")
    old = args["old_string"]
    new = args["new_string"]
    replace_all = bool(args.get("replace_all"))
    count = text.count(old)
    if count == 0:
        return "ERROR: old_string을 찾지 못했습니다. 파일 내용을 다시 읽으세요."
    if count > 1 and not replace_all:
        return f"ERROR: old_string이 {count}번 등장합니다. 더 고유하게 만들거나 replace_all=true를 쓰세요."
    path.write_text(text.replace(old, new), encoding="utf-8")
    rel = path.relative_to(root)
    return f"updated {rel} ({count} replacement{'s' if count != 1 else ''})"


def tool_delete_file(root: Path, args: dict[str, Any]) -> str:
    path = resolve_in_workspace(root, args["path"])
    if not path.is_file():
        return f"ERROR: 파일이 없습니다: {path}"
    path.unlink()
    return f"deleted {path.relative_to(root)}"


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


def todo_plan_text() -> str:
    """요약문에 실어 보낼 사람이 읽는 작업 계획 블록(계획이 없으면 "")."""
    if not CURRENT_TODO and not CURRENT_TODO_GOAL:
        return ""
    lines = ["[진행 중인 작업 계획 — 새로 만들지 말고 이 계획을 이어서 갱신하세요]"]
    if CURRENT_TODO_GOAL:
        lines.append(f"🎯 {CURRENT_TODO_GOAL}")
    for it in CURRENT_TODO:
        lines.append(f"   {todo_marker(str(it.get('status') or 'pending'))} {it.get('content')}")
    if len(CURRENT_TODO) > TODO_MAX_ITEMS:
        lines.append(f"   … 전체 {len(CURRENT_TODO)}개 항목을 하나의 작업으로 계속 진행")
    return "\n".join(lines)


def todo_stamp() -> str:
    """요약문에 심는 기계용 작업 계획 스냅샷(다음 실행에서 복원). 없으면 ""."""
    if not CURRENT_TODO and not CURRENT_TODO_GOAL:
        return ""
    state = {"goal": CURRENT_TODO_GOAL, "items": CURRENT_TODO}
    return f"<!-- llm-todo:{json.dumps(state, ensure_ascii=False)} -->"


def restore_todo(messages: list[dict[str, Any]]) -> bool:
    """앞선 요약문에 남은 작업 계획 스냅샷을 복원한다. 복원했으면 True.

    요약(압축)이나 새 실행에서 계획이 '새로 시작'되는 것을 막는다.
    """
    global CURRENT_TODO, CURRENT_TODO_GOAL
    stamp = ""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            found = TODO_STAMP_RE.findall(content)
            if found:
                stamp = found[-1]
    if not stamp:
        return False
    try:
        state = json.loads(stamp)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(state, dict):
        return False
    items = state.get("items")
    if isinstance(items, list):
        CURRENT_TODO = [
            {"content": str(it.get("content")), "status": str(it.get("status") or "pending")}
            for it in items[:TODO_RESTORE_MAX_ITEMS]
            if isinstance(it, dict) and it.get("content")
        ]
    goal = state.get("goal")
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
    target = resolve_in_workspace(root, args.get("path") or ".")
    flags = re.IGNORECASE if args.get("case_insensitive") else 0
    try:
        rx = re.compile(args["pattern"], flags)
    except re.error as exc:
        return f"ERROR: 잘못된 정규식: {exc}"
    glob_pat = args.get("glob")
    files: list[Path] = []
    if target.is_file():
        files = [target]
    elif target.is_dir():
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
            for name in filenames:
                path = Path(dirpath) / name
                if glob_pat:
                    rel = str(path.relative_to(root))
                    if not (fnmatch.fnmatch(name, glob_pat) or fnmatch.fnmatch(rel, glob_pat)):
                        continue
                files.append(path)
    else:
        return f"ERROR: 경로가 없습니다: {target}"

    hits: list[str] = []
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
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}:{line[:300]}")
                if len(hits) >= MAX_GREP_HITS:
                    extra = f"\n... [stopped at {MAX_GREP_HITS} hits after {scanned} files]"
                    return "\n".join(hits) + extra
    return "\n".join(hits) if hits else f"(no matches in {scanned} files)"


SHELL_SPLIT_RE = re.compile(r"\s*(;|&&|\|\||\||\n)\s*")


def split_shell_commands(command: str) -> list[tuple[str, str]]:
    """셸 명령을 (앞 연산자, 명령) 목록으로 분해한다.

    인용부호 안의 연산자와 이스케이프된 연산자(`\\|`, `\\;`)는 무시한다.
    첫 항목의 연산자는 빈 문자열이다.
    """
    parts: list[tuple[str, str]] = []
    buf: list[str] = []
    op = ""
    quote: str | None = None
    i = 0
    text = command.strip()
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i : i + 2])  # 이스케이프된 문자는 연산자가 아니다
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = text[i : i + 2]
        if two in {"&&", "||"}:
            parts.append((op, "".join(buf).strip()))
            op = two
            buf = []
            i += 2
            continue
        if ch in {";", "|", "&", "\n"}:
            parts.append((op, "".join(buf).strip()))
            op = ch
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append((op, "".join(buf).strip()))
    return [(o, c) for o, c in parts if c]


def chain_segments(command: str) -> list[str]:
    """`&&`/`||`/`|`/`&`/`;`로 이어진 명령의 조각 목록(인용부호 무시)."""
    return [c for _, c in split_shell_commands(command)]


# 셸 명령을 "무엇을 하려는지"로 요약할 때 쓰는 프로그램별 의도 문구.
# 명령 원문을 설명하지 않고 작업 의도만 한 줄로 보여주기 위한 표다.
SHELL_INTENT_PROGRAMS = {
    "ls": "폴더 내용을 확인합니다",
    "cat": "파일 내용을 읽습니다",
    "head": "파일 앞부분을 읽습니다",
    "tail": "파일 끝부분을 읽습니다",
    "less": "파일 내용을 읽습니다",
    "wc": "코드 줄 수를 셉니다",
    "grep": "코드에서 패턴을 찾습니다",
    "rg": "코드에서 패턴을 찾습니다",
    "find": "파일을 찾습니다",
    "du": "용량을 확인합니다",
    "df": "디스크 여유 공간을 확인합니다",
    "tree": "폴더 구조를 봅니다",
    "mkdir": "새 폴더를 만듭니다",
    "touch": "새 파일을 만듭니다",
    "rm": "파일을 삭제합니다",
    "rmdir": "폴더를 삭제합니다",
    "shred": "파일을 삭제합니다",
    "mv": "파일을 이동하거나 이름을 바꿉니다",
    "cp": "파일을 복사합니다",
    "chmod": "실행 권한을 조정합니다",
    "chown": "파일 소유자를 바꿉니다",
    "ln": "링크를 만듭니다",
    "sed": "파일 내용을 수정합니다",
    "awk": "텍스트를 가공합니다",
    "truncate": "파일을 비웁니다",
    "tee": "결과를 파일로 저장합니다",
    "echo": "결과를 출력합니다",
    "printf": "결과를 출력합니다",
    "tar": "압축을 풀거나 묶습니다",
    "zip": "압축 파일을 만듭니다",
    "unzip": "압축을 풉니다",
    "curl": "네트워크에서 데이터를 가져옵니다",
    "wget": "네트워크에서 파일을 받습니다",
    "sudo": "관리자 권한으로 실행합니다",
    "kill": "프로세스를 종료합니다",
    "pkill": "프로세스를 종료합니다",
    "docker": "컨테이너를 다룹니다",
    "kubectl": "클러스터를 제어합니다",
    "systemctl": "서비스를 제어합니다",
    "pytest": "테스트를 실행합니다",
    "ruff": "코드 스타일을 검사합니다",
    "flake8": "코드 스타일을 검사합니다",
    "black": "코드 스타일을 정리합니다",
    "eslint": "코드 스타일을 검사합니다",
    "prettier": "코드 스타일을 정리합니다",
    "mypy": "타입을 검사합니다",
    "tsc": "타입을 검사합니다",
    "make": "빌드를 실행합니다",
    "sleep": "잠시 기다립니다",
    "which": "설치 경로를 확인합니다",
}

SHELL_INTENT_GIT = {
    "status": "저장소 상태를 확인합니다",
    "diff": "변경 내용을 확인합니다",
    "log": "커밋 기록을 확인합니다",
    "show": "커밋 내용을 확인합니다",
    "branch": "브랜치를 확인합니다",
    "remote": "원격 저장소를 확인합니다",
    "add": "변경을 커밋 대상에 추가합니다",
    "commit": "변경을 커밋합니다",
    "tag": "태그를 추가합니다",
    "rm": "추적 중인 파일을 삭제합니다",
    "push": "원격 저장소로 올립니다",
    "pull": "원격 저장소에서 받아옵니다",
    "fetch": "원격 저장소 정보를 갱신합니다",
    "clone": "저장소를 복제합니다",
    "checkout": "다른 브랜치나 버전으로 바꿉니다",
    "switch": "다른 브랜치로 바꿉니다",
    "restore": "파일을 되돌립니다",
    "reset": "커밋 상태를 되돌립니다",
    "clean": "추적하지 않는 파일을 지웁니다",
    "stash": "작업 내용을 임시 보관합니다",
    "init": "저장소를 초기화합니다",
    "config": "git 설정을 변경합니다",
    "mv": "파일을 이동합니다",
}

SHELL_INTENT_PACKAGE_HEADS = {
    "pip",
    "pip3",
    "uv",
    "poetry",
    "conda",
    "apt",
    "apt-get",
    "brew",
    "snap",
    "gem",
}

SHELL_INTENT_WRITE_HEADS = {"echo", "printf", "cat", "tee", "sed"}
SHELL_INTENT_SCRIPT_HEADS = {"bash", "sh", "zsh", "fish", "node", "deno", "ruby", "perl"}
SHELL_INTENT_CHECKER_HEADS = {"ruff", "black", "prettier", "eslint", "flake8"}
SHELL_INTENT_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def strip_heredoc_bodies(command: str) -> str:
    """heredoc 본문을 제거해 명령 뼈대만 남긴다.

    본문(`print(1)` 같은 코드·데이터 줄)이 별개 명령처럼 해석되는 것을 막는다.
    의도 요약과 보안 판정(설치·권한 상승 오탐 방지)에 함께 쓴다.
    """
    lines = command.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        match = heredoc_marker(line)
        if not match:
            continue
        while i < len(lines) and lines[i].strip() != match:
            i += 1
        i += 1  # 종료 토큰 줄 건너뛰기
    return "\n".join(kept)


def heredoc_marker(line: str) -> str | None:
    """줄에서 heredoc 시작 토큰(`<<EOF`)을 찾는다(인용부호 밖의 `<<`만).

    `echo "<<EOF"`처럼 인용부호 안에 든 `<<`를 진짜 heredoc으로 오해하면
    뒤따르는 명령까지 본문으로 지워버리므로, 따옴표·이스케이프를 존중한다.
    """
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < len(line) and line[i] != quote:
                i += 2 if line[i] == "\\" and quote == '"' else 1
            i += 1
            continue
        if line[i : i + 2] == "<<":
            match = SHELL_INTENT_HEREDOC.match(line, i)
            if match:
                return match.group(1)
        i += 1
    return None


def scan_text(command: str) -> str:
    """보안 판정용으로 데이터(heredoc 본문)와 무해한 리다이렉션을 뺀 명령."""
    return SHELL_HARMLESS_REDIRECT.sub(" ", strip_heredoc_bodies((command or "").strip()))


def intent_for_step(command: str) -> str | None:
    """단일 셸 명령에서 "무엇을 하려는지" 한 줄을 뽑는다."""
    single = command.strip()
    if not single:
        return None
    tokens = single.split()
    # `FOO=bar cmd ...` 처럼 앞에 붙은 환경변수 지정은 건너뛴다.
    _, tokens = strip_env_assignments(tokens)
    head = tokens[0]
    name = Path(head).name
    sub = tokens[1] if len(tokens) > 1 else ""

    if name == "cd":
        return "작업 폴더를 이동합니다"
    if re.search(r"(?<![0-9&])>>?", single) and name in SHELL_INTENT_WRITE_HEADS:
        return "파일을 작성하거나 수정합니다"
    if name in {"python", "python3", "py"}:
        if sub == "-m":
            mod = tokens[2] if len(tokens) > 2 else ""
            if "pytest" in mod or "unittest" in mod:
                return "테스트를 실행합니다"
            if "py_compile" in mod:
                return "문법을 검사합니다"
            if "json.tool" in mod:
                return "JSON을 검사합니다"
            return "파이썬 모듈을 실행합니다"
        if sub in SHELL_WORKSPACE_CODE_FLAGS:
            return "파이썬 코드를 실행합니다"
        return "파이썬 스크립트를 실행합니다"
    if name in SHELL_INTENT_SCRIPT_HEADS:
        return "스크립트를 실행합니다"
    if name == "git":
        return SHELL_INTENT_GIT.get(sub, "Git 저장소를 다룹니다")
    if name in {"npm", "yarn", "pnpm", "bun"}:
        if sub in {"install", "i", "ci", "add"}:
            return "패키지를 설치합니다"
        if sub in {"test", "t"}:
            return "테스트를 실행합니다"
        if sub in {"run", "exec"}:
            script = (tokens[2] if len(tokens) > 2 else "").lower()
            if "test" in script:
                return "테스트를 실행합니다"
            if "build" in script:
                return "빌드를 실행합니다"
            if "lint" in script or "format" in script:
                return "코드 스타일을 검사하거나 정리합니다"
            return "스크립트를 실행합니다"
        if sub in {"ls", "list"}:
            return "설치된 패키지를 확인합니다"
        return "Node 패키지를 다룹니다"
    if name in SHELL_INTENT_PACKAGE_HEADS:
        return "패키지를 설치하거나 관리합니다"
    if name == "cargo":
        return {
            "test": "테스트를 실행합니다",
            "build": "빌드합니다",
            "check": "코드를 검사합니다",
            "fmt": "코드 스타일을 정리합니다",
            "clippy": "코드를 검사합니다",
        }.get(sub, "Rust 프로젝트를 다룹니다")
    if name == "go":
        return {
            "test": "테스트를 실행합니다",
            "build": "빌드합니다",
            "vet": "코드를 검사합니다",
            "run": "코드를 실행합니다",
            "mod": "모듈을 관리합니다",
        }.get(sub, "Go 프로젝트를 다룹니다")
    label = SHELL_INTENT_PROGRAMS.get(name)
    if name in SHELL_INTENT_CHECKER_HEADS:
        if "--fix" in tokens or name in {"black", "prettier"}:
            return "코드 스타일을 정리합니다"
        return "코드 스타일을 검사합니다"
    if label:
        return label
    if head.startswith("./") or name.endswith(SHELL_WORKSPACE_SCRIPT_SUFFIXES):
        return "스크립트를 실행합니다"
    return f"{name} 명령을 실행합니다"


def intent_summary(command: str) -> str:
    """실행할 명령을 "무엇을 하려는지" 한 줄로 요약한다(명령 원문 설명 대신)."""
    skeleton = strip_heredoc_bodies(command)
    steps = split_shell_commands(skeleton) or [("", skeleton.strip())]
    intents: list[str] = []
    for _, single in steps:
        text = intent_for_step(single)
        if text and text not in intents:
            intents.append(text)
    if len(intents) > 1:
        # `cd` 이동은 다른 작업과 함께면 의도 요약에서 뺀다.
        intents = [i for i in intents if i != "작업 폴더를 이동합니다"] or intents
    if not intents:
        return "명령을 실행합니다"
    if len(intents) > 2:
        return intents[0] + f" 외 {len(intents) - 1}가지"
    return " → ".join(intents)


def allow_file(session: str) -> Path:
    return HISTORY_DIR / f"shell_allow_{sanitize_session_name(session)}.json"


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
        return True
    if not sys.stdin.isatty():
        print_error("셸 실행은 TTY가 아니면 --yes 가 필요합니다.", stream=True)
        return False

    def risky(segment: str) -> bool:
        return not only_risky or needs_confirm_always(segment, root)

    segments = [c for _, c in split_shell_commands(command)] or [command_key(command)]
    pending = [
        s
        for s in segments
        if risky(s) and (approval is None or not approval.segment_allowed(s, root))
    ]
    if not pending:
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
            continue
        label = f"   {C.dim}{idx}) {C.reset}" if compound else "   "
        answer = ask_shell_segment(label, segment, approval, root=root)
        if answer == "y":
            continue
        if answer in {"a", "all"}:
            if approval is not None:
                if answer == "a":
                    added.append(approval.remember(segment, root))
                else:
                    # all 은 저장하지 않는다: 이번 실행 동안만 확인을 생략한다.
                    approval.auto_all = True
                    print_ok("이번 실행 동안 셸 확인을 묻지 않습니다. (llm 재시작 시 해제)")
            continue
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


def tool_shell(
    root: Path,
    args: dict[str, Any],
    yes: bool,
    auto_safe: bool = True,
    approval: "ShellApproval | None" = None,
    auto_workspace: bool = False,
    allow_all: bool = False,
) -> str:
    command = args.get("command") or ""
    if not command.strip():
        return "ERROR: command가 비어 있습니다."
    for rx in SHELL_DENY:
        if rx.search(command):
            return f"ERROR: 차단된 위험한 명령 패턴입니다: {command}"
    timeout = int(args.get("timeout") or 60)
    timeout = max(1, min(timeout, 180))
    auto_run = (
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
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            executable="/bin/bash",
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: {timeout}s 시간 초과"
    parts = [f"exit {proc.returncode}"]
    if proc.stdout:
        parts.append("--- stdout ---\n" + proc.stdout)
    if proc.stderr:
        parts.append("--- stderr ---\n" + proc.stderr)
    return clip("\n".join(parts))


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
    try:
        if name == "todo":
            return tool_todo(args)
        if name == "read_file":
            return tool_read_file(root, args)
        if name == "write_file":
            return tool_write_file(root, args)
        if name == "str_replace":
            return tool_str_replace(root, args)
        if name == "delete_file":
            return tool_delete_file(root, args)
        if name == "list_dir":
            return tool_list_dir(root, args)
        if name == "glob":
            return tool_glob(root, args)
        if name == "grep":
            return tool_grep(root, args)
        if name == "shell":
            return tool_shell(
                root, args, yes, auto_safe, approval, auto_workspace, allow_all
            )
        return f"ERROR: 알 수 없는 도구: {name}"
    except ValueError as exc:
        return f"ERROR: {exc}"
    except OSError as exc:
        return f"ERROR: {exc}"


def summarize_args(name: str, raw_args: str) -> str:
    args = parse_tool_args(raw_args)
    if "_error" in args:
        return raw_args[:80]
    if name == "shell":
        return intent_summary(str(args.get("command", "")))
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
    if name == "str_replace":
        return str(args.get("path", ""))
    if "path" in args:
        extra = []
        if args.get("pattern"):
            extra.append(str(args["pattern"]))
        return " ".join([str(args.get("path", "")), *extra]).strip()
    if args.get("pattern"):
        return str(args["pattern"])
    keys = [k for k in args if k != "contents"]
    return ", ".join(f"{k}={args[k]}" for k in keys[:4])[:120]


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 180) -> Any:
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    }
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        with urllib.request.urlopen(req, timeout=180) as resp:
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
    ask_mode: str = "risky",
    ask_source: str = "",
) -> None:
    mode = "채팅" if chat_only else "에이전트"
    mode_color = C.magenta if chat_only else C.cyan
    has_rules = bool(load_session_rules(root))
    rules = f"{C.green}on{C.reset}" if has_rules else f"{C.dim}off{C.reset}"
    confirm = ""
    if not chat_only:
        label = SHELL_ASK_LABELS.get(ask_mode, ask_mode)
        confirm = f"  {C.dim}확인={C.reset}{label}"
    print(
        f"{C.dim}──{C.reset} {mode_color}{C.bold}{mode}{C.reset}"
        f" {C.dim}v{VERSION}{C.reset}"
        f"  {C.dim}session={C.reset}{session}"
        f"  {C.dim}root={C.reset}{root}"
        f"  {C.dim}rules={C.reset}{rules}{confirm}"
    )
    if ask_source:
        # 안내 문구는 모드가 "환경변수"에서 온 경우에만 있다(CLI 옵션은 배너의 `확인=`으로 충분).
        info = SHELL_ASK_SOURCES.get(ask_source)
        if info:
            print_info(info)


def resolve_ask_mode(args: Any) -> tuple[bool, bool, str, str]:
    """셸 확인 범위를 정한다. 반환: (auto_workspace, allow_all, 모드, 출처).

    명시한 CLI 옵션이 환경변수보다 우선한다. 그래야 `.env`에 남은 설정 때문에
    의도와 다른 모드로 조용히 동작하는 일이 없다.
    """
    if args.confirm_all:
        return False, False, "all", "--confirm-all"
    if args.allow_all:
        return False, True, "risky", "--allow-all"
    if args.auto_workspace:
        return True, False, "outside", "--auto-workspace"
    if args.yes:
        return False, True, "none", "--yes"
    if env_flag("LLM_ALLOW_ALL"):
        return False, True, "risky", "LLM_ALLOW_ALL"
    if env_flag("LLM_AUTO_WORKSPACE"):
        return True, False, "outside", "LLM_AUTO_WORKSPACE"
    return False, True, "risky", ""


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


def reset_usage() -> None:
    USAGE_TOTAL.clear()


def record_usage(usage: Any) -> None:
    """API 응답의 usage(토큰 사용량)를 누적한다. 없거나 형식이 다르면 무시."""
    if not isinstance(usage, dict):
        return
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and value > 0:
            USAGE_TOTAL[key] = USAGE_TOTAL.get(key, 0) + value
    USAGE_TOTAL["calls"] = USAGE_TOTAL.get("calls", 0) + 1


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


def usage_lines() -> list[str]:
    """이번 실행에서 누적한 토큰 사용량 요약(usage 가 없으면 빈 목록)."""
    prompt = int(USAGE_TOTAL.get("prompt_tokens", 0))
    completion = int(USAGE_TOTAL.get("completion_tokens", 0))
    if not prompt and not completion:
        return []
    total = int(USAGE_TOTAL.get("total_tokens", 0)) or (prompt + completion)
    parts = [f"입력 {prompt:,}", f"출력 {completion:,}", f"합계 {total:,} 토큰"]
    hit = int(USAGE_TOTAL.get("prompt_cache_hit_tokens", 0))
    miss = int(USAGE_TOTAL.get("prompt_cache_miss_tokens", 0))
    if hit or miss:
        parts.append(f"캐시 적중 {hit:,} / 미적중 {miss:,}")
    calls = int(USAGE_TOTAL.get("calls", 0))
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
            "대화 기록에 아직 올리지 못한 변경이 있습니다 — 실행이 끝나면 다시"
            f" 커밋·push 를 시도합니다 (끄려면 {AUTO_SYNC_ENV}=0)."
        )
        return
    print_info(
        "대화 기록이 아직 커밋되지 않았습니다 — llm --sync-history 로 올리면"
        " 다른 PC에서 이어집니다."
    )


def auto_sync_enabled() -> bool:
    """실행 종료 시 기록 자동 동기화 여부(기본 켜짐, `LLM_AUTO_SYNC=0`이면 끔)."""
    val = os.environ.get(AUTO_SYNC_ENV, "").strip().lower()
    return val not in {"0", "false", "no", "off"}


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
    print_title("🔄 대화 기록 동기화")
    if cmd_sync_history(root, session) != 0:
        print_info(f"자동 동기화를 끄려면 {AUTO_SYNC_ENV}=0 으로 실행하세요.")


def cmd_sync_history(root: Path, session: str) -> int:
    """대화 기록(.llm/)만 커밋하고 pull --rebase 후 push 한다."""
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
    # 이미 추적 중인 파일이어도 패턴을 반영해 알려주려면 --no-index 가 필요하다.
    code, out = git_run(root, "check-ignore", "--no-index", rel)
    if code == 0:
        print_error(f"{rel} 이 .gitignore 로 제외되어 있어 push 되지 않습니다.")
        print_info(f".gitignore 에서 '{rel}' 관련 줄을 지운 뒤 다시 실행하세요.")
        return 1

    # 1) 원격 변경을 먼저 받아 둔다(내 변경은 autostash 로 잠시 보관).
    has_upstream = git_run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")[0] == 0
    if has_upstream:
        print_info("원격 변경을 받아오는 중… (git pull --rebase --autostash)")
        code, out = git_run(root, "pull", "--rebase", "--autostash")
        if code != 0:
            print_error("pull 실패 — 충돌을 해결한 뒤 다시 실행하세요.")
            for line in out.splitlines()[:6]:
                print_info(f"  {line}")
            return 1
    else:
        print_warn("upstream(@{u})이 없어 pull/push 는 건너뜁니다 (기록만 커밋).")

    # 2) 대화 기록만 스테이징해서 커밋.
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

    # 3) 올린다.
    if not has_upstream:
        return 0
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
        ('llm --chat "질문"', "도구 없이 순수 채팅"),
        ('llm --yes "질문"', "셸 명령 확인 생략"),
        ('llm --auto-workspace "질문"', "폴더 안 명령은 자동 실행"),
        ('llm --show-thinking "질문"', "사고 과정 원문까지 표시"),
        ("llm -s <이름>", "이 폴더를 해당 세션에 매핑 (기존 매핑은 갈아탐)"),
        ("llm --ref <이름>", "다른 세션 맥락을 이번 세션에 1회 주입"),
        ("llm --init-rules", ".llm/rules.md 생성"),
        ("llm --rules", "세션 규칙 보기"),
        ("llm --run", "직전 응답의 스크립트 실행"),
        ("llm --view", "대화 내용 보기 (마크다운)"),
        ("llm --summarize", "현재 대화를 요약·백업 (강제)"),
        ("llm --sync-history", "이 세션 기록·요약만 커밋·push (다른 PC에서 이어가기)"),
        ("llm --clear", "현재 세션 내용 비우기"),
        ("llm --delete", "현재 세션 파일 삭제"),
        ("llm --allow-list", "셸 자동 허용 목록 보기"),
        ("llm --allow-clear", "셸 자동 허용 목록 비우기"),
        ("llm --balance", "API 잔액 조회 (화폐별 + 원화 환산)"),
        ("llm --no-balance", "에이전트 종료 후 잔액 표시 생략"),
        ("llm --no-rates", "실시간 환율 조회 끄기 (가정 환율로 환산)"),
    ]
    width = max(display_width(cmd) for cmd, _ in usage)
    for cmd, desc in usage:
        print(f"   {C.cyan}{pad(cmd, width)}{C.reset}  {C.dim}{desc}{C.reset}")

    print_section(f"저장된 세션  ({HISTORY_BASE})")
    if history_scope_is_repo():
        print(
            f"   {C.dim}기록이 이 폴더 안(.llm/)에 있어 push/pull 하면 다른 PC에서 그대로"
            f" 이어집니다  →  llm --sync-history{C.reset}"
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
    ask_mode: str = "risky",
    ask_source: str = "",
    no_balance: bool = False,
) -> int:
    print_banner(session, root, chat_only, ask_mode=ask_mode, ask_source=ask_source)
    print_history_sync_hint(root, history_file)

    stripped = prompt.strip().lower()
    if stripped in {"/summary", "/summarize", "/요약"}:
        return cmd_summarize(session, history_file, model, force=True)

    reset_usage()
    if not chat_only and not no_balance:
        # 종료 시 소요 비용을 계산할 수 있도록 시작 잔액을 먼저 조회해 캐시한다.
        prefetch_balance()

    messages = load_history(history_file)
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
            if not todo_seen and not todo_reminded:
                # 아직 계획이 없다: 도구를 실행하지 않고 계획을 먼저 세우도록 1회 요청.
                # (assistant 턴을 저장하지 않으므로 tool_calls/tool 짝도 깨지지 않는다.)
                todo_reminded = True
                print_warn("먼저 작업 계획(todo)을 세우도록 요청합니다.", stream=True)
                messages.append({"role": "user", "content": TODO_REMINDER})
                save_history(history_file, messages)
                continue

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
            for idx, tc in enumerate(tool_calls, 1):
                fn = tc.get("function") or {}
                name = fn.get("name") or "?"
                raw_args = fn.get("arguments") or "{}"

                summary = summarize_args(name, raw_args)
                counter = f"{C.dim}[{idx}/{total}]{C.reset} " if total > 1 else ""
                slot = tool_line_open(
                    f"{counter}{C.cyan}🔧 {name}{C.reset} {C.dim}{summary}{C.reset}"
                )

                result = run_tool(
                    name,
                    raw_args,
                    root,
                    yes,
                    auto_safe,
                    approval,
                    auto_workspace,
                    allow_all,
                )
                preview = result.splitlines()[0][:120] if result else ""
                if result.startswith("ERROR:"):
                    tool_line_done(slot, f"{C.red}✗ {preview}{C.reset}")
                else:
                    tool_line_done(slot, f"{C.green}✓{C.reset} {C.dim}{preview}{C.reset}")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "",
                        "name": name,
                        "content": clip(result),
                    }
                )

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

        # Final answer: save first, then render exactly once with glow.
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="llm",
        add_help=True,
        description="DeepSeek CLI 에이전트",
    )
    parser.add_argument("-s", "--session", help="세션 이름 (이 폴더에 매핑, 기존 매핑은 갈아탐)")
    parser.add_argument(
        "--ref",
        action="append",
        metavar="세션",
        help="다른 세션의 맥락을 이번 세션에 1회 주입 (여러 번 지정 가능, 참조 대상은 읽기 전용)",
    )
    parser.add_argument("--chat", action="store_true", help="도구 없이 순수 채팅")
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
    parser.add_argument("--confirm-all", action="store_true", help="읽기 전용 명령도 확인 후 실행 (자동 실행 비활성)")
    parser.add_argument(
        "--allow-all",
        action="store_true",
        help="sudo·설치·폴더 밖 삭제만 확인하고 나머지는 확인 없이 실행 (기본값, LLM_ALLOW_ALL=1과 동일)",
    )
    parser.add_argument(
        "--auto-workspace",
        action="store_true",
        help="작업 폴더 안에서 끝나는 명령은 확인 없이 실행 (폴더 밖·설치·이력 변경은 확인, LLM_AUTO_WORKSPACE=1이면 기본 활성)",
    )
    parser.add_argument("--allow-list", action="store_true", help="이 세션의 셸 자동 허용 목록 보기")
    parser.add_argument("--allow-clear", action="store_true", help="이 세션의 셸 자동 허용 목록 비우기")
    parser.add_argument("--init-rules", action="store_true", help="이 폴더에 .llm/rules.md 생성")
    parser.add_argument("--rules", action="store_true", help="이 폴더의 세션 규칙 보기")
    parser.add_argument("--summarize", action="store_true", help="현재 대화를 지금 요약·백업 (강제)")
    parser.add_argument(
        "--sync-history",
        action="store_true",
        help="대화 기록(.llm/)만 커밋하고 pull --rebase 후 push (다른 PC에서 이어가기)",
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
    args = parse_args(argv if argv is not None else sys.argv[1:])

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
        or args.allow_list or args.allow_clear
    )
    # 남의 세션에도 허용되는 "보기만" 하는 명령.
    read_only = bool(args.view or args.allow_list)

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
            if prompt:
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
