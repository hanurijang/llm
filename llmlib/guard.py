"""비밀값 보호: 셸 환경 정리 · 비밀 파일/변수 참조 감지 · 도구 출력 마스킹.

- `scrubbed_env`: 셸 하위 프로세스에 API 키·토큰 같은 변수를 넘기지 않는다.
- `command_touches_secret`: `cat .env`, `echo $DEEPSEEK_API_KEY`, `printenv`처럼
  비밀을 화면(=모델)으로 꺼낼 수 있는 명령을 찾아 항상 확인을 받게 한다.
- `redact`: 알려진 비밀값이 도구 결과에 섞여 나오면 모델에 보내기 전에 가린다.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable, Mapping

SECRET_NAMES = {
    ".env",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".netrc",
    ".pgpass",
    ".npmrc",
    ".pypirc",
}
# `.env.example` 처럼 값이 없는 견본 파일은 읽어도 된다.
SECRET_SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
# 경로 일부로 판단하는 비밀 위치(홈 자격 증명 폴더 등)
SECRET_PATH_PARTS = (
    "/.ssh/",
    "/.aws/credentials",
    "/.docker/config.json",
    "/.kube/config",
    "/.config/gh/hosts.yml",
    "/etc/env/",
)
# 이름에 이 단어가 들어간 환경변수는 비밀로 본다.
SECRET_ENV_RE = re.compile(
    r"(API_?KEY|ACCESS_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY)",
    re.I,
)
# 비밀 이름이어도 값이 비밀이 아닌 변수(경로·소켓 등)
SECRET_ENV_ALLOW = {"SSH_AUTH_SOCK", "GPG_AGENT_INFO", "TOKENIZERS_PARALLELISM"}
# 사용자가 셸에 꼭 넘겨야 하는 변수: LLM_SHELL_KEEP_ENV="GH_TOKEN,NPM_TOKEN"
KEEP_ENV_VAR = "LLM_SHELL_KEEP_ENV"
REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 8

_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_DUMP_HEADS = {"set", "export", "declare", "compgen"}


def is_secret_name(name: str) -> bool:
    """파일 이름만으로 비밀 파일인지(.env, .env.local, id_rsa 등)."""
    lowered = name.lower()
    if lowered.endswith(SECRET_SAFE_SUFFIXES):
        return False
    if lowered in SECRET_NAMES:
        return True
    return lowered.startswith(".env.") or lowered.endswith((".pem", ".key", ".p12", ".pfx"))


def is_secret_path_text(path: str) -> bool:
    """경로 문자열이 비밀 파일/폴더를 가리키는지."""
    cleaned = path.strip().strip("'\"")
    if not cleaned:
        return False
    if is_secret_name(Path(cleaned).name):
        return True
    probe = "/" + cleaned.lstrip("~").lstrip("/") if not cleaned.startswith("/") else cleaned
    return any(part in probe or probe.endswith(part.rstrip("/")) for part in SECRET_PATH_PARTS)


def is_secret_env_name(name: str) -> bool:
    return name not in SECRET_ENV_ALLOW and bool(SECRET_ENV_RE.search(name))


def keep_env_names(environ: Mapping[str, str]) -> set[str]:
    raw = environ.get(KEEP_ENV_VAR, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def scrubbed_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """셸 하위 프로세스용 환경: 비밀 변수를 뺀 사본."""
    source = dict(os.environ if environ is None else environ)
    keep = keep_env_names(source)
    return {k: v for k, v in source.items() if k in keep or not is_secret_env_name(k)}


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def command_touches_secret(command: str) -> str | None:
    """비밀을 드러낼 수 있는 명령이면 이유를, 아니면 None 을 돌려준다."""
    text = command or ""
    for name in _VAR_REF_RE.findall(text):
        if is_secret_env_name(name):
            return f"비밀 환경변수 ${name} 참조"
    for segment in re.split(r"&&|\|\||[;|&\n]", text):
        words = _words(segment)
        # `FOO=1 cmd` 의 앞머리 지정은 떼고 본다.
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        if not words:
            continue
        head = Path(words[0]).name
        rest = words[1:]
        if head in {"env", "printenv"}:
            args = [w for w in rest if not w.startswith("-")]
            if head == "printenv" and args:
                if any(is_secret_env_name(a) for a in args):
                    return f"비밀 환경변수 {args[0]} 출력"
            elif head == "printenv" or all("=" in a for a in args):
                return f"환경변수 목록 출력({head})"  # `env FOO=1 cmd` 는 명령 실행이라 제외
        elif head in _DUMP_HEADS and (not rest or rest[0] in {"-p", "-x"}):
            return f"환경변수 목록 출력({head})"
        for word in words:
            for piece in re.split(r"[=:,]", word):
                if piece and is_secret_path_text(piece):
                    return f"비밀 파일 {piece}"
    return None


def collect_secret_values(
    environ: Mapping[str, str] | None = None,
    env_files: Iterable[Path] = (),
) -> list[str]:
    """마스킹할 비밀값 목록: 비밀 이름 환경변수 값 + .env 파일들의 값."""
    source = os.environ if environ is None else environ
    values: set[str] = set()
    for key, value in source.items():
        if is_secret_env_name(key) and len(value.strip()) >= MIN_SECRET_LEN:
            values.add(value.strip())
    for path in env_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip("'\"")
            if len(value) >= MIN_SECRET_LEN and (is_secret_env_name(key) or looks_random(value)):
                values.add(value)
    # 긴 값부터 가려야 부분 문자열이 먼저 바뀌어 긴 값이 남는 일이 없다.
    return sorted(values, key=len, reverse=True)


def looks_random(value: str) -> bool:
    """키 이름이 평범해도 값이 토큰처럼 생겼는지(길고, 글자+숫자 섞임, 공백 없음)."""
    return (
        len(value) >= 16
        and not any(ch.isspace() for ch in value)
        and any(ch.isdigit() for ch in value)
        and any(ch.isalpha() for ch in value)
        and "/" not in value  # 경로·URL 은 제외
    )


def redact(text: str, values: Iterable[str]) -> str:
    for value in values:
        if value and value in text:
            text = text.replace(value, REDACTED)
    return text
