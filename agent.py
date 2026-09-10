#!/usr/bin/env python3
"""DeepSeek CLI agent — 파일/검색/셸 도구 루프."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

API_URL = "https://api.deepseek.com/chat/completions"
BALANCE_URL = "https://api.deepseek.com/user/balance"
HISTORY_DIR = Path.home() / ".local/share/llm"
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
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

# 계열(접두) 자동 허용을 만들 때는 인자에 따라 동작이 바뀌는 명령을 막는다.
# 접두로 묶인 뒤 다른 인자가 붙어 부작용이 생기는 것을 예방한다.
SHELL_WRITE_ARG_PATTERN = re.compile(
    r"-(?:exec|execdir|delete|fprint0?)\b|--in-place|--output\b|\bsudo\b|\bxargs\b"
)

# `&&`, `||`, `|`, `;`로 이어진 명령을 조각 단위로 나눈다(자동 실행 판정용)
SHELL_CHAIN_SPLIT = re.compile(r"&&|\|\||\||;|&")

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


def is_safe_command(command: str) -> bool:
    """확인 없이 실행해도 되는 읽기 전용/검증 명령인지 판단한다.

    `ls && git status`처럼 연결된 명령은 **모든 조각이 자동 실행 대상일 때만**
    자동 실행한다. 위험 요소는 연결 전체를 대상으로 먼저 검사하므로,
    `ls && rm -rf x` 같은 조합은 여기서 걸러진다.
    """
    text = SHELL_HARMLESS_REDIRECT.sub(" ", (command or "").strip())
    if not text or SHELL_UNSAFE_PATTERN.search(text):
        return False
    parts = [part.strip() for part in SHELL_CHAIN_SPLIT.split(text) if part.strip()]
    if not parts:
        return False
    return all(_is_safe_simple_command(part) for part in parts)


def _is_safe_simple_command(part: str) -> bool:
    """연결 없는 단일 명령이 자동 실행 대상인지 판단한다."""
    tokens = part.split()
    if not tokens:
        return False
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


def _workspace_path_ok(root: Path, token: str) -> bool:
    """토큰이 가리키는 경로가 workspace 안(또는 무해한 특수 경로)인지."""
    raw = token.strip().strip("'\"")
    if raw.startswith("-"):
        if "=" not in raw:
            return True  # 플래그 자체는 경로가 아니다
        raw = raw.split("=", 1)[1].strip().strip("'\"")
    if not raw:
        return True
    if raw in SHELL_WORKSPACE_SPECIAL_PATHS or raw.startswith("/dev/fd/"):
        return True
    if any(ch in raw for ch in "$`"):
        return False  # 변수·명령 치환은 대상 경로를 정적으로 알 수 없다
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
    tokens = fragment.split()
    if not tokens:
        return False
    # 경로가 workspace를 벗어나면 무엇이든 확인 대상
    if not all(_workspace_path_ok(root, tok) for tok in tokens):
        return False
    # 읽기 전용/검증 명령이면 조각 단독으로도 그대로 허용(`npm test`, `tail -5`)
    if is_safe_command(fragment):
        return True
    # 리다이렉션 대상만 뺀 명령이 읽기 전용/검증이면 허용(`npm test > out.txt`)
    body = re.sub(r"\d*>>?\s*\S+", " ", fragment).strip()
    if body and body != fragment and is_safe_command(body):
        return True
    head = Path(tokens[0]).name
    if head in SHELL_WORKSPACE_BLOCK_HEADS:
        return False
    if tokens[0].startswith("./") or head.endswith(SHELL_WORKSPACE_SCRIPT_SUFFIXES):
        return False  # 스크립트 파일 실행은 내용을 검증할 수 없다
    if head in SHELL_WORKSPACE_INTERPRETERS:
        if any(tok in SHELL_WORKSPACE_CODE_FLAGS for tok in tokens):
            return False  # `python3 -c ...` 같은 임의 코드 실행
        if not [tok for tok in tokens[1:] if not tok.startswith("-")]:
            return False  # 인터프리터 단독 실행(REPL/stdin)
    if head == "find" and any(tok in SHELL_WORKSPACE_FIND_WRITE_ARGS for tok in tokens):
        return False
    if head == "git":
        if any(tok in SHELL_WORKSPACE_BLOCK_GIT for tok in tokens[1:]):
            return False
        if any(tok in SHELL_WORKSPACE_BLOCK_GIT_FLAGS for tok in tokens[1:]):
            return False
    if head in SHELL_WORKSPACE_PROTECTED_HEADS:
        for tok in tokens[1:]:
            cleaned = tok.strip().strip("'\"")
            if cleaned in SHELL_WORKSPACE_PROTECTED_TARGETS or cleaned.startswith(".git"):
                return False
    return True


def is_workspace_command(command: str, root: Path) -> bool:
    """`--auto-workspace`: workspace 안에서 끝나는 부작용만 자동 실행한다.

    경로가 workspace를 벗어나거나(`~/x`, `/etc/x`, `../x`), 설치·네트워크·권한
    명령, 임의 코드 실행, git 이력 파괴로 이어지면 False를 돌려 확인을 받는다.
    """
    text = (command or "").strip()
    if not text:
        return False
    if any(rx.search(text) for rx in SHELL_DENY):
        return False
    cleaned = SHELL_HARMLESS_REDIRECT.sub(" ", text)
    fragments = [frag.strip() for frag in SHELL_CHAIN_SPLIT.split(cleaned) if frag.strip()]
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


RULE_WIDTH = 60


def char_width(ch: str) -> int:
    """터미널 표시 폭: 동아시아 wide/전각 문자는 2, 그 외는 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def pad(text: str, width: int) -> str:
    """표시 폭 기준으로 오른쪽을 공백으로 채운다."""
    return text + " " * max(0, width - display_width(text))


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


def print_title(label: str) -> None:
    line = rule("─", label)
    print(f"{C.bold}{C.cyan}{line}{C.reset}")


def print_section(label: str) -> None:
    print()
    print(f"{C.bold}{label}{C.reset}")
    print(f"{C.dim}{rule()}{C.reset}")


def print_info(message: str) -> None:
    print(f"{C.cyan}ℹ️  {message}{C.reset}")


def print_ok(message: str) -> None:
    print(f"{C.green}✅ {message}{C.reset}")


def print_warn(message: str, stream: bool = False) -> None:
    out = eprint if stream else print
    out(f"{C.yellow}⚠️  {message}{C.reset}")


def print_error(message: str, stream: bool = True) -> None:
    out = eprint if stream else print
    out(f"{C.red}❌ {message}{C.reset}")


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


def load_dotenv(path: Path = Path("/etc/env/.env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def env_flag(name: str) -> bool:
    """환경변수를 불리언으로 읽는다(1/true/yes/on/y 만 참)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on", "y"}


def api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        eprint("DEEPSEEK_API_KEY가 없습니다. /etc/env/.env 또는 환경변수를 확인하세요.")
        sys.exit(1)
    return key


def session_paths(name: str) -> tuple[Path, Path]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    current = HISTORY_DIR / "current_session"
    history = HISTORY_DIR / f"history_{name}.json"
    return current, history


def bindings_file() -> Path:
    return HISTORY_DIR / "bindings.json"


def load_bindings() -> dict[str, str]:
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
    return {str(k): str(v) for k, v in folders.items() if k and v}


def save_bindings(folders: dict[str, str]) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = bindings_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"folders": folders}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def sanitize_session_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-가-힣]", "_", name.strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("._") or "workspace"
    return cleaned[:80]


def auto_session_name(root: Path, folders: dict[str, str]) -> str:
    root_s = str(root.resolve())
    parts = [p for p in root.resolve().parts if p not in {"/", ""}]
    slugs: list[str] = []
    for i in range(1, min(4, len(parts) + 1)):
        slug = sanitize_session_name("_".join(parts[-i:]))
        if slug and slug not in slugs:
            slugs.append(slug)
    if not slugs:
        slugs = ["workspace"]
    for name in slugs:
        owner = next((path for path, sess in folders.items() if sess == name), None)
        if owner is None or owner == root_s:
            return name
    digest = hashlib.sha1(root_s.encode("utf-8")).hexdigest()[:6]
    return f"{slugs[0]}_{digest}"


def bind_folder(folders: dict[str, str], root: Path, session: str) -> tuple[dict[str, str], str | None]:
    """한 세션은 폴더 하나에만 붙인다. 이전 폴더가 있으면 그 경로를 반환한다."""
    root_s = str(root.resolve())
    previous = next((path for path, sess in folders.items() if sess == session and path != root_s), None)
    folders = {path: sess for path, sess in folders.items() if sess != session and path != root_s}
    folders[root_s] = session
    save_bindings(folders)
    return folders, previous


def unbind_session(folders: dict[str, str], session: str) -> dict[str, str]:
    folders = {path: sess for path, sess in folders.items() if sess != session}
    save_bindings(folders)
    return folders


def folder_for_session(folders: dict[str, str], session: str) -> str | None:
    return next((path for path, sess in folders.items() if sess == session), None)


def summaries_dir(session: str) -> Path:
    return HISTORY_DIR / SUMMARY_DIR_NAME / sanitize_session_name(session)


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

    head = {
        "role": "user",
        "content": (
            f"{SUMMARY_MARKER} 아래는 이전 대화의 요약입니다. "
            f"이후 대화의 맥락으로 사용하세요.\n\n"
            f"{summary_clipped}"
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

    인용부호 안의 연산자는 무시한다. 첫 항목의 연산자는 빈 문자열이다.
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
        if ch in {";", "|", "\n"}:
            parts.append((op, "".join(buf).strip()))
            op = ch
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append((op, "".join(buf).strip()))
    return [(o, c) for o, c in parts if c]


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
    """heredoc 본문을 제거해 명령 뼈대만 남긴다(의도 요약용).

    본문(`print(1)` 같은 코드 줄)이 별개 명령처럼 해석되는 것을 막는다.
    """
    lines = command.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        match = SHELL_INTENT_HEREDOC.search(line)
        if not match:
            continue
        end = match.group(1)
        while i < len(lines) and lines[i].strip() != end:
            i += 1
        i += 1  # 종료 토큰 줄 건너뛰기
    return "\n".join(kept)


def intent_for_step(command: str) -> str | None:
    """단일 셸 명령에서 "무엇을 하려는지" 한 줄을 뽑는다."""
    single = command.strip()
    if not single:
        return None
    tokens = single.split()
    # `FOO=bar cmd ...` 처럼 앞에 붙은 환경변수 지정은 건너뛴다.
    while len(tokens) > 1 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", tokens[0]):
        tokens = tokens[1:]
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
    삭제·설치류처럼 위험한 프로그램은 접두 허용을 만들지 않는다.
    """
    text = (command or "").strip()
    if not text or SHELL_META_PATTERN.search(text):
        return None
    tokens = text.split()
    head = Path(tokens[0]).name
    if head in SHELL_NO_PREFIX_HEADS or head in SHELL_FLAG_SENSITIVE_HEADS:
        return None
    if head in SHELL_SUBCOMMAND_PROGRAMS:
        if len(tokens) > 1 and not tokens[1].startswith("-"):
            if (head, tokens[1]) in SHELL_NO_PREFIX_SUBCOMMANDS:
                return None  # `cargo fmt`처럼 같은 접두 안에 안전/위험 변형이 공존
            return f"{head} {tokens[1]}"
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


class ShellApproval:
    """세션별 셸 명령 자동 허용 목록(프롬프트에서 a 선택 시 누적).

    매번 같은 명령을 묻지 않도록 정확 일치(`exact`)와 계열 접두(`prefix`)를
    세션 파일에 저장한다.
    """

    def __init__(self, session: str) -> None:
        self.session = session
        self.path = allow_file(session)
        self.exact: set[str] = set()
        self.prefixes: set[str] = set()
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

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"exact": sorted(self.exact), "prefix": sorted(self.prefixes)},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print_warn(f"자동 허용 목록 저장 실패: {exc}", stream=True)

    def is_allowed(self, command: str) -> bool:
        if command_key(command) in self.exact:
            return True
        # 계열 접두는 인자에 따라 부작용이 켜지는 명령(`find -exec`, `-i` 등)에는
        # 적용하지 않는다. 그런 명령은 매번 확인을 받는다.
        if has_write_args(command):
            return False
        prefix = command_prefix(command)
        return bool(prefix) and prefix in self.prefixes

    def remember(self, command: str) -> str:
        self.exact.add(command_key(command))
        prefix = command_prefix(command)
        if prefix:
            self.prefixes.add(prefix)
        self.save()
        return prefix or command_key(command)

    def clear(self) -> None:
        self.exact.clear()
        self.prefixes.clear()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print_warn(f"자동 허용 목록 삭제 실패: {exc}", stream=True)


def confirm_shell(command: str, yes: bool, approval: "ShellApproval | None" = None) -> bool:
    if yes:
        return True
    if approval is not None and approval.is_allowed(command):
        return True
    if not sys.stdin.isatty():
        print_error("셸 실행은 TTY가 아니면 --yes 가 필요합니다.", stream=True)
        return False
    prefix = command_prefix(command) if approval is not None else None
    hint = (
        f"   {C.dim}a = 이 명령{'/' + prefix if prefix else ''} 계열 자동 허용 (다시 묻지 않음){C.reset}\n"
        if approval is not None
        else ""
    )
    try:
        answer = input(
            f"{C.yellow}❓ 확인이 필요합니다{C.reset}\n"
            f"   {C.dim}실행할 명령{C.reset}: {C.bold}{command}{C.reset}\n"
            f"{hint}"
            f"   {C.bold}[y/N/a]{C.reset}: "
        ).strip().lower()
    except EOFError:
        return False
    if answer in {"y", "yes", "ㅛ"}:
        return True
    if answer in {"a", "all", "always", "ㅁ"} and approval is not None:
        key = approval.remember(command)
        print_ok(f"자동 허용에 추가했습니다: {key} (해제: llm --allow-clear)")
        return True
    return False


def tool_shell(
    root: Path,
    args: dict[str, Any],
    yes: bool,
    auto_safe: bool = True,
    approval: "ShellApproval | None" = None,
    auto_workspace: bool = False,
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
    )
    if not yes and not auto_run and not confirm_shell(command, yes, approval):
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
) -> str:
    args = parse_tool_args(raw_args)
    if "_error" in args:
        return f"ERROR: {args['_error']}"
    try:
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
            return tool_shell(root, args, yes, auto_safe, approval, auto_workspace)
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
) -> tuple[str | None, list[dict[str, Any]], str | None, str | None]:
    """content, tool_calls, finish_reason, reasoning_content

    스트림이 중간에 끊기면 StreamInterrupted(부분 응답 보존)를 발생시킨다.
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
    return content, tool_calls, finish, reasoning


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

    if not thinking:
        # thinking 은 기본 활성. 비활성 시 사고 과정 없이 바로 답한다.
        payload["thinking"] = {"type": "disabled"}

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    if stream:
        try:
            return stream_chat(payload, on_delta=on_delta)
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
        payload["stream"] = False

    data = http_json(API_URL, payload)

    if data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(msg)

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


def print_banner(session: str, root: Path, chat_only: bool) -> None:
    mode = "채팅" if chat_only else "에이전트"
    mode_color = C.magenta if chat_only else C.cyan
    has_rules = bool(load_session_rules(root))
    rules = f"{C.green}on{C.reset}" if has_rules else f"{C.dim}off{C.reset}"
    print(
        f"{C.dim}──{C.reset} {mode_color}{C.bold}{mode}{C.reset}"
        f"  {C.dim}session={C.reset}{session}"
        f"  {C.dim}root={C.reset}{root}"
        f"  {C.dim}rules={C.reset}{rules}"
    )


def cmd_balance() -> int:
    print_info("DeepSeek 계정 잔액을 조회합니다...")
    data = http_json(BALANCE_URL)
    infos = data.get("balance_infos") or []
    cny = "0.00"
    for info in infos:
        if info.get("currency") == "CNY":
            cny = str(info.get("total_balance") or "0.00")
            break
    try:
        krw = float(cny) * 220
        krw_s = f"{krw:,.2f}"
    except ValueError:
        krw_s = "?"
    print_title("💵 DeepSeek 잔액 정보")
    print(f"  {C.dim}위안화 (CNY){C.reset}  {C.bold}{cny}{C.reset} CNY")
    print(f"  {C.dim}원화   (KRW){C.reset}  {C.bold}{krw_s}{C.reset} 원")
    print(f"{C.dim}{rule()}{C.reset}")
    print(f"  {C.dim}적용 환율: 1 CNY = 220 KRW{C.reset}")
    return 0


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


def cmd_allow(session: str, clear: bool) -> int:
    """세션의 셸 자동 허용 목록을 보거나 비운다."""
    approval = ShellApproval(session)
    if clear:
        if not approval.exact and not approval.prefixes:
            print_info(f"[{session}] 자동 허용 목록이 이미 비어 있습니다.")
            return 0
        approval.clear()
        print_ok(f"[{session}] 셸 자동 허용 목록을 비웠습니다.")
        return 0

    print_title(f"셸 자동 허용 목록  [{session}]")
    if not approval.exact and not approval.prefixes:
        print_info("등록된 항목이 없습니다. 확인 프롬프트에서 a 를 선택하면 추가됩니다.")
        return 0
    for prefix in sorted(approval.prefixes):
        print(f"   {C.green}계열{C.reset}  {prefix} {C.dim}(...){C.reset}")
    for command in sorted(approval.exact):
        if command_prefix(command) not in approval.prefixes:
            print(f"   {C.cyan}정확{C.reset}  {command}")
    print_info(f"파일: {approval.path}")
    print_info("해제: llm --allow-clear")
    return 0


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
    files = {f.name[len("history_") : -len(".json")]: f for f in HISTORY_DIR.glob("history_*.json")}
    names = sorted(set(files) | set(folders.values()))
    mapped = folders.get(str(root))

    print_title("💡 DeepSeek LLM CLI")
    print(f"  {C.dim}📂 작업 폴더{C.reset}  {root}")
    mapped_txt = mapped if mapped else f"{C.yellow}(없음 — 다음 대화 때 생성){C.reset}"
    print(f"  {C.dim}🔗 매핑 세션{C.reset}  {mapped_txt}")
    rules_file = rules_path(root)
    rules_txt = str(rules_file) if rules_file.is_file() else f"{C.yellow}(없음 — llm --init-rules){C.reset}"
    print(f"  {C.dim}📜 세션 규칙{C.reset}  {rules_txt}")

    print_section("사용법")
    usage = [
        ('llm "질문"', "이 폴더 세션으로 에이전트 실행"),
        ('llm --chat "질문"', "도구 없이 순수 채팅"),
        ('llm --yes "질문"', "셸 명령 확인 생략"),
        ('llm --auto-workspace "질문"', "폴더 안 명령은 자동 실행"),
        ('llm --show-thinking "질문"', "사고 과정 원문까지 표시"),
        ('llm --root <경로> "질문"', "작업 디렉터리 지정"),
        ("llm -s <이름>", "이 폴더를 해당 세션에 매핑"),
        ("llm --init-rules", ".llm/rules.md 생성"),
        ("llm --rules", "세션 규칙 보기"),
        ("llm --run", "직전 응답의 스크립트 실행"),
        ("llm --view", "대화 내용 보기 (마크다운)"),
        ("llm --summarize", "현재 대화를 요약·백업 (강제)"),
        ("llm --clear", "현재 세션 내용 비우기"),
        ("llm --delete", "현재 세션 파일 삭제"),
        ("llm --allow-list", "셸 자동 허용 목록 보기"),
        ("llm --allow-clear", "셸 자동 허용 목록 비우기"),
        ("llm --balance", "API 잔액 조회 (KRW 환산)"),
    ]
    width = max(display_width(cmd) for cmd, _ in usage)
    for cmd, desc in usage:
        print(f"   {C.cyan}{pad(cmd, width)}{C.reset}  {C.dim}{desc}{C.reset}")

    print_section(f"저장된 세션  ({HISTORY_DIR})")
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
) -> int:
    print_banner(session, root, chat_only)

    stripped = prompt.strip().lower()
    if stripped in {"/summary", "/summarize", "/요약"}:
        return cmd_summarize(session, history_file, model, force=True)

    messages = load_history(history_file)
    messages.append({"role": "user", "content": prompt})
    save_history(history_file, messages)

    if chat_only:
        system = build_chat_system_prompt()
        tools = None
    else:
        system = build_system_prompt(root, session)
        tools = tool_schemas()

    progress = make_stream_progress(show_thinking) if stream else None
    approval = ShellApproval(session) if (not chat_only and not yes) else None

    for _step in range(1, max_steps + 1):
        # 대화가 커지면 요약·백업 후 최근 메시지만 유지한다.

        if not chat_only:
            # 몇 번째 단계인지 한 줄로 남겨 진행 과정을 따라갈 수 있게 한다.
            print(f"{C.dim}{rule('─', f'{_step}/{max_steps}단계')}{C.reset}")

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
                print(f"{counter}{C.cyan}🔧 {name}{C.reset} {C.dim}{summary}{C.reset}")

                result = run_tool(
                    name, raw_args, root, yes, auto_safe, approval, auto_workspace
                )
                preview = result.splitlines()[0][:120] if result else ""
                if result.startswith("ERROR:"):
                    print(f"      {C.red}✗ {preview}{C.reset}")
                else:
                    print(f"      {C.green}✓{C.reset} {C.dim}{preview}{C.reset}")

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
            print()
            print_title("🤖 답변")
            render_markdown(content)
            return 0

        print_warn(f"빈 응답 (finish={finish})", stream=True)
        return 1

    print_warn(
        f"최대 단계({max_steps})에 도달했습니다. --max-steps 로 늘리세요.",
        stream=True,
    )
    return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="llm",
        add_help=True,
        description="DeepSeek CLI 에이전트",
    )
    parser.add_argument("-s", "--session", help="세션 이름 (이 폴더에 매핑)")
    parser.add_argument("--chat", action="store_true", help="도구 없이 순수 채팅")
    parser.add_argument("-y", "--yes", action="store_true", help="셸 명령 확인 생략")
    parser.add_argument("--root", help="작업 디렉터리 (기본: 현재 디렉터리)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"모델 (기본: {DEFAULT_MODEL})")
    parser.add_argument("--max-steps", type=int, default=40, help="에이전트 루프 최대 횟수")
    parser.add_argument("--no-stream", action="store_true", help="스트리밍 비활성화")
    parser.add_argument("--no-thinking", action="store_true", help="사고(thinking) 비활성화 (더 빠른 응답)")
    parser.add_argument(
        "--show-thinking",
        action="store_true",
        help="모델의 사고 과정(reasoning) 원문을 회색으로 실시간 출력",
    )
    parser.add_argument("--confirm-all", action="store_true", help="읽기 전용 명령도 확인 후 실행 (자동 실행 비활성)")
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
    parser.add_argument("--balance", action="store_true")
    parser.add_argument("--view", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("prompt", nargs=argparse.REMAINDER, help="질문")
    return parser.parse_args(argv)


def resolve_root(args: argparse.Namespace) -> Path:
    if args.root:
        return Path(args.root).expanduser().resolve()
    return Path.cwd().resolve()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    init_color()
    args = parse_args(argv if argv is not None else sys.argv[1:])

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    current_file = HISTORY_DIR / "current_session"
    folders = load_bindings()

    if args.balance:
        return cmd_balance()

    root = resolve_root(args)
    if not root.is_dir():
        print_error(f"작업 디렉터리가 없습니다: {root}")
        return 1

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
    session: str | None = None

    if args.session:
        session = args.session.strip()
        if not session:
            print_error("세션 이름이 비어 있습니다.")
            return 1
        # 대화 시작이거나 `llm -s name`만 친 경우에 이 폴더에 매핑.
        # --view/--delete 등 조회·삭제는 다른 세션을 훔치지 않는다.
        if prompt or not inspect_only:
            folders, previous = bind_folder(folders, root, session)
            if previous:
                print_info(f"세션 '{session}' 폴더 매핑: {previous} → {root}")
    else:
        session = folders.get(str(root))
        if session is None:
            if prompt:
                session = auto_session_name(root, folders)
                folders, _ = bind_folder(folders, root, session)
            elif inspect_only:
                print_error("이 폴더에 매핑된 세션이 없습니다.")
                print_info("대화를 시작하거나  llm -s <이름>  으로 지정하세요.")
                return 1
            else:
                list_sessions(None, folders, root)
                return 0

    current_file.write_text(session + "\n", encoding="utf-8")
    _, history_file = session_paths(session)

    if args.view:
        return cmd_view(session, history_file)
    if args.run:
        return cmd_run(session, history_file)
    if args.summarize:
        return cmd_summarize(session, history_file, args.model, force=True)
    if args.allow_list or args.allow_clear:
        return cmd_allow(session, clear=args.allow_clear)
    if args.delete:
        existed = history_file.is_file()
        if existed:
            history_file.unlink()
        folders = unbind_session(folders, session)
        if existed:
            print_ok(f"[{session}] 세션 파일을 완전히 삭제했습니다.")
        else:
            print_ok(f"[{session}] 폴더 매핑을 해제했습니다.")
        return 0
    if args.clear:
        if history_file.is_file():
            save_history(history_file, [])
            print_ok(f"[{session}] 세션 내용을 비웠습니다 (초기화).")
            return 0
        print_error(f"[{session}] 세션 파일이 존재하지 않습니다.")
        return 1

    if not prompt:
        list_sessions(session, folders, root)
        return 0

    return cmd_agent(
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
        auto_workspace=(
            args.auto_workspace or env_flag("LLM_AUTO_WORKSPACE")
        ) and not args.confirm_all,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        eprint(f"\n{C.yellow}⚠️  중단되었습니다.{C.reset}")
        sys.exit(130)
