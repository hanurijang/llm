"""CLI 서브커맨드 확장과 실행 형태 결정(순수 함수).

`llm <서브커맨드> …` 를 기존 플래그 조합으로 펼치고(`expand`), 인자만 주고
실행했을 때 대화형(REPL)으로 갈지(`wants_repl`)를 정한다. 출력·전역 상태에
기대지 않으므로 단위 테스트 대상이다(agent.py 가 얇게 감싸 쓴다).
"""

from __future__ import annotations

# `session` 하위 명령 → 플래그. 세션 이름을 함께 주면 `-s <이름>`으로 앞에 붙인다.
SESSION_ACTIONS: dict[str, str] = {
    "view": "--view",
    "show": "--view",
    "clear": "--clear",
    "reset": "--clear",
    "delete": "--delete",
    "rm": "--delete",
    "compact": "--summarize",
    "summarize": "--summarize",
    "summary": "--summarize",
    "run": "--run",
}

SESSION_USAGE = (
    "사용법: llm session [ls | adopt | view [이름] | clear [이름] | delete [이름] | "
    "compact [이름] | run [이름] | ref <이름> …]"
)
SHELL_USAGE = "사용법: llm shell [log [N|clear] | allow [clear]]"
RULES_USAGE = "사용법: llm rules [show|init]"


def _session(rest: list[str]) -> tuple[list[str] | None, str | None]:
    if not rest or rest[0] in {"ls", "list", "status"}:
        return ["--list-sessions"], None
    sub, tail = rest[0], rest[1:]
    if sub in {"adopt", "bind"}:
        return ["--session-adopt"], None
    if sub in {"ref", "refs"}:
        names = [n for n in tail if n.strip()]
        if not names:
            return None, "사용법: llm session ref <이름> [<이름> …]"
        out: list[str] = []
        for name in names:
            out += ["--ref", name]
        return out, None
    flag = SESSION_ACTIONS.get(sub)
    if flag is None or len(tail) > 1:
        return None, SESSION_USAGE
    if tail:
        return ["-s", tail[0], flag], None
    return [flag], None


def _shell(rest: list[str]) -> tuple[list[str] | None, str | None]:
    if not rest:
        return None, SHELL_USAGE
    sub, tail = rest[0], rest[1:]
    if sub in {"log", "logs"}:
        if tail and tail[0] in {"clear", "reset"}:
            return ["--shell-log-clear"], None
        if len(tail) > 1:
            return None, SHELL_USAGE
        return ["--shell-log", *tail], None
    if sub in {"allow", "allows", "allow-list", "list"}:
        if tail and tail[0] in {"clear", "reset"}:
            return ["--allow-clear"], None
        if tail:
            return None, SHELL_USAGE
        return ["--allow-list"], None
    return None, SHELL_USAGE


def _rules(rest: list[str]) -> tuple[list[str] | None, str | None]:
    if not rest or rest[0] in {"show", "view", "list"}:
        return ["--rules"], None
    if rest[0] in {"init", "new", "create"}:
        return ["--init-rules"], None
    return None, RULES_USAGE


def expand(argv: list[str]) -> tuple[list[str], str | None]:
    """서브커맨드를 플래그로 펼친다. (펼친 argv, 오류문구) 반환.

    오류문구가 있으면 호출 쪽에서 사용법을 보여 주고 실행을 중단한다.
    """
    if not argv:
        return list(argv), None
    head, rest = argv[0], argv[1:]
    if head == "sync":
        return ["--sync-history", *rest], None
    if head in {"ask", "once", "print", "p"}:
        return ["--once", *rest], None
    if head == "chat":
        return ["--chat", *rest], None
    if head in {"repl", "i", "agent"}:
        return ["--interactive", *rest], None
    if head == "undo":
        return ["--undo", *rest], None
    if head == "plan":
        return ["--plan", *rest], None
    if head in {"bal", "balance"}:
        return ["--balance", *rest], None
    if head == "help":
        return ["--help", *rest], None
    if head == "rules":
        return _rules(rest)
    if head in {"shell", "sh"}:
        return _shell(rest)
    if head in {"shell-log", "shelllog"}:
        if rest and rest[0] in {"clear", "reset"}:
            return ["--shell-log-clear", *rest[1:]], None
        return ["--shell-log", *rest], None
    if head == "session":
        return _session(rest)
    return list(argv), None


def wants_repl(once: bool, stdin_tty: bool, stdout_tty: bool) -> bool:
    """인자 없이(또는 질문만 주고) 실행했을 때 대화형으로 갈지.

    터미널(stdin·stdout 모두 TTY)이고 `--once`(`llm ask`) 가 아니면 대화형이
    기본이다. 파이프·리다이렉션(스크립트·자동화)에서는 기존처럼 1턴으로 끝낸다.
    """
    return bool(stdin_tty and stdout_tty and not once)
