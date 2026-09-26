"""셸 명령 안전 분류(자동 실행/확인/차단)와 의도 요약.

agent.py 에서 분리한 순수 함수 모음이다. 전역 상태·UI 에 의존하지 않으므로
단위 테스트로 표 형태 검증을 한다(tests/test_shell_policy.py).
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

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
    "all": "확인=전부",
    "risky": "확인=trust(위험만)",
    "outside": "확인=safe(폴더 안)",
    "none": "확인=없음",
}
# 모드가 CLI 옵션이 아니라 환경변수에서 온 경우의 안내 문구
SHELL_ASK_SOURCES = {
    "LLM_AUTO_WORKSPACE": (
        "환경변수 LLM_AUTO_WORKSPACE=1 → safe 모드"
        " (작업 폴더 밖·설치 등은 확인). .env 에서 지워도 기본은 safe 입니다."
    ),
    "LLM_ALLOW_ALL": (
        "환경변수 LLM_ALLOW_ALL=1 → trust 모드"
        " (권한 상승·설치·폴더 밖 삭제만 확인)."
    ),
    "LLM_SHELL_TRUST": (
        "환경변수 LLM_SHELL_TRUST=1 → trust 모드"
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
