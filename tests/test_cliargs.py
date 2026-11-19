"""CLI 서브커맨드 확장(llmlib/cliargs)과 기본 실행 형태 테스트."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmlib import cliargs  # noqa: E402


class Expand(unittest.TestCase):
    def expand(self, argv: list[str]) -> list[str]:
        out, err = cliargs.expand(argv)
        self.assertIsNone(err, f"오류: {err}")
        return out

    def test_plain_passthrough(self) -> None:
        self.assertEqual(self.expand(["프로젝트 요약해줘"]), ["프로젝트 요약해줘"])
        self.assertEqual(self.expand([]), [])
        self.assertEqual(self.expand(["-s", "a", "질문"]), ["-s", "a", "질문"])

    def test_run_modes(self) -> None:
        self.assertEqual(self.expand(["ask", "hi"]), ["--once", "hi"])
        self.assertEqual(self.expand(["once", "hi"]), ["--once", "hi"])
        self.assertEqual(self.expand(["chat", "hi"]), ["--chat", "hi"])
        self.assertEqual(self.expand(["plan", "hi"]), ["--plan", "hi"])
        for name in ("repl", "i", "agent"):
            self.assertEqual(self.expand([name]), ["--interactive"], name)
        self.assertEqual(self.expand(["agent", "hi"]), ["--interactive", "hi"])

    def test_topics(self) -> None:
        self.assertEqual(self.expand(["sync"]), ["--sync-history"])
        self.assertEqual(self.expand(["undo"]), ["--undo"])
        self.assertEqual(self.expand(["balance"]), ["--balance"])
        self.assertEqual(self.expand(["bal"]), ["--balance"])
        self.assertEqual(self.expand(["help"]), ["--help"])
        self.assertEqual(self.expand(["rules"]), ["--rules"])
        self.assertEqual(self.expand(["rules", "show"]), ["--rules"])
        self.assertEqual(self.expand(["rules", "init"]), ["--init-rules"])

    def test_shell(self) -> None:
        self.assertEqual(self.expand(["shell", "log"]), ["--shell-log"])
        self.assertEqual(self.expand(["shell", "log", "5"]), ["--shell-log", "5"])
        self.assertEqual(self.expand(["shell", "log", "clear"]), ["--shell-log-clear"])
        self.assertEqual(self.expand(["shell", "allow"]), ["--allow-list"])
        self.assertEqual(self.expand(["shell", "allow", "clear"]), ["--allow-clear"])
        # 옛 표기도 계속 동작한다.
        self.assertEqual(self.expand(["shell-log", "clear"]), ["--shell-log-clear"])
        self.assertEqual(self.expand(["shelllog"]), ["--shell-log"])

    def test_session(self) -> None:
        self.assertEqual(self.expand(["session"]), ["--list-sessions"])
        self.assertEqual(self.expand(["session", "ls"]), ["--list-sessions"])
        self.assertEqual(self.expand(["session", "adopt"]), ["--session-adopt"])
        self.assertEqual(self.expand(["session", "view"]), ["--view"])
        self.assertEqual(self.expand(["session", "view", "other"]), ["-s", "other", "--view"])
        self.assertEqual(self.expand(["session", "clear"]), ["--clear"])
        self.assertEqual(self.expand(["session", "delete", "x"]), ["-s", "x", "--delete"])
        self.assertEqual(self.expand(["session", "compact"]), ["--summarize"])
        self.assertEqual(self.expand(["session", "run"]), ["--run"])
        self.assertEqual(
            self.expand(["session", "ref", "a", "b"]),
            ["--ref", "a", "--ref", "b"],
        )

    def test_bad_usage(self) -> None:
        for argv in (["shell"], ["shell", "nope"], ["session", "nope"], ["session", "ref"], ["rules", "nope"]):
            out, err = cliargs.expand(argv)
            self.assertIsNone(out, argv)
            self.assertTrue(err and "사용법" in err, argv)


class DefaultMode(unittest.TestCase):
    def test_wants_repl(self) -> None:
        self.assertTrue(cliargs.wants_repl(False, True, True))  # 터미널 → 대화형
        self.assertFalse(cliargs.wants_repl(True, True, True))  # --once/ask → 1턴
        self.assertFalse(cliargs.wants_repl(False, False, True))  # 파이프 → 1턴
        self.assertFalse(cliargs.wants_repl(False, True, False))  # 리다이렉션 → 1턴


if __name__ == "__main__":
    unittest.main()
