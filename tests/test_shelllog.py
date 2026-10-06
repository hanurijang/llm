#!/usr/bin/env python3
"""셸 허용/실행 로그(shelllog)와 repl 기본 확인 모드 테스트."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent as llm  # noqa: E402
from llmlib import shelllog  # noqa: E402


def _capture(fn) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


class LogFile(unittest.TestCase):
    """경로·세션 이름 정리."""

    def test_log_path_uses_session(self) -> None:
        self.assertEqual(
            shelllog.log_path(Path("/tmp/h"), "llm").name, "shell_log_llm.jsonl"
        )

    def test_sanitize_blocks_path_traversal(self) -> None:
        self.assertEqual(shelllog.sanitize("../../evil"), "evil")
        self.assertEqual(shelllog.sanitize("a/b c"), "a_b_c")
        self.assertEqual(shelllog.sanitize(""), "default")
        self.assertEqual(shelllog.sanitize("..."), "default")

    def test_sanitize_matches_agent_file_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "HISTORY_DIR", Path(tmp)):
                got = llm.shell_log_file("my session")
            self.assertEqual(got.name, "shell_log_my_session.jsonl")


class RecordAndRead(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "shell_log_t.jsonl"
        self.log = shelllog.ShellLog(self.path)

    def test_record_then_entries(self) -> None:
        self.log.record("a", "  make   build  ", mode="all", note="자동 허용에 추가")
        entries = self.log.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "a")
        self.assertEqual(entries[0]["cmd"], "make build")  # 공백 정리
        self.assertEqual(entries[0]["mode"], "all")
        self.assertEqual(entries[0]["note"], "자동 허용에 추가")
        self.assertIn("at", entries[0])

    def test_record_skips_empty_and_unknown(self) -> None:
        self.log.record("a", "   ")
        self.log.record("zzz", "ls")
        self.assertEqual(self.log.entries(), [])
        self.assertFalse(self.path.exists())

    def test_disabled_log_writes_nothing(self) -> None:
        off = shelllog.ShellLog(self.path, enabled=False)
        off.record("auto", "ls")
        self.assertFalse(self.path.exists())
        self.assertEqual(off.entries(), [])

    def test_broken_lines_are_skipped(self) -> None:
        self.path.write_text(
            '{"at":"x","kind":"y","cmd":"ok"}\nnot json\n\n', encoding="utf-8"
        )
        self.assertEqual([e["cmd"] for e in self.log.entries()], ["ok"])

    def test_long_command_is_clipped(self) -> None:
        self.log.record("y", "x" * (shelllog.MAX_CMD_CHARS + 50))
        self.assertEqual(len(self.log.entries()[0]["cmd"]), shelllog.MAX_CMD_CHARS)

    def test_trim_keeps_recent_entries(self) -> None:
        for i in range(shelllog.MAX_ENTRIES + 5):
            self.log.record("y", f"cmd{i}")
        entries = self.log.entries()
        self.assertEqual(len(entries), shelllog.MAX_ENTRIES)
        self.assertEqual(entries[-1]["cmd"], f"cmd{shelllog.MAX_ENTRIES + 4}")
        self.assertEqual(entries[0]["cmd"], "cmd5")

    def test_clear(self) -> None:
        self.log.record("y", "ls")
        self.assertTrue(self.log.clear())
        self.assertFalse(self.log.clear())  # 이미 없음
        self.assertEqual(self.log.count(), 0)

    def test_read_error_is_silent(self) -> None:
        directory = Path(self._tmp.name) / "as_dir.jsonl"
        directory.mkdir()
        log = shelllog.ShellLog(directory)  # 파일이 아니라 디렉터리
        log.record("y", "ls")  # 예외 없이 무시
        self.assertEqual(log.entries(), [])


class Rows(unittest.TestCase):
    def test_rows_newest_first_and_limited(self) -> None:
        entries = [
            {"at": "2026-02-05T10:11:12", "kind": "y", "cmd": "one"},
            {"at": "2026-02-05T11:22:33", "kind": "n", "cmd": "two", "note": "취소"},
        ]
        self.assertEqual(shelllog.rows(entries, 1), [("02-05 11:22", "n", "two", "취소")])
        self.assertEqual([r[2] for r in shelllog.rows(entries, 20)], ["two", "one"])

    def test_kind_label_and_tally(self) -> None:
        self.assertEqual(shelllog.kind_label("auto"), "자동 허용 실행")
        self.assertEqual(shelllog.kind_label("??"), "??")
        entries = [{"kind": "auto"}, {"kind": "auto"}, {"kind": "n"}]
        self.assertEqual(shelllog.tally(entries), "자동 허용 실행 2 · 거부 1")
        self.assertEqual(shelllog.tally([]), "")


def ns(**kwargs) -> argparse.Namespace:
    """parse_args 와 같은 기본값을 가진 간단한 네임스페이스."""
    base = {
        "confirm_all": False,
        "yes": False,
        "trust": False,
        "allow_all": False,
        "safe": False,
        "auto_workspace": False,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


class ReplAskMode(unittest.TestCase):
    """repl 기본 확인 모드 = 전부 확인(all), 명시 옵션은 그대로 존중."""

    def setUp(self) -> None:
        patcher = mock.patch.dict("os.environ", {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_repl_default_is_all(self) -> None:
        auto_workspace, allow_all, mode, source, default = llm.repl_ask_mode(ns())
        self.assertEqual(mode, llm.REPL_DEFAULT_ASK_MODE)
        self.assertEqual(mode, "all")
        self.assertFalse(auto_workspace)
        self.assertFalse(allow_all)
        self.assertEqual(source, "")
        self.assertTrue(default)

    def test_agent_default_stays_safe(self) -> None:
        auto_workspace, allow_all, mode, source = llm.resolve_ask_mode(ns())
        self.assertEqual((mode, source), ("outside", ""))
        self.assertTrue(auto_workspace)

    def test_explicit_options_win(self) -> None:
        for kwargs, expected in (
            ({"safe": True}, "outside"),
            ({"trust": True}, "risky"),
            ({"yes": True}, "none"),
            ({"confirm_all": True}, "all"),
        ):
            _, _, mode, source, default = llm.repl_ask_mode(ns(**kwargs))
            self.assertEqual(mode, expected, kwargs)
            self.assertFalse(default, kwargs)
            self.assertNotEqual(source, "", kwargs)

    def test_env_var_wins_over_repl_default(self) -> None:
        with mock.patch.dict("os.environ", {"LLM_AUTO_WORKSPACE": "1"}):
            _, _, mode, source, default = llm.repl_ask_mode(ns())
        self.assertEqual(mode, "outside")
        self.assertEqual(source, "LLM_AUTO_WORKSPACE")
        self.assertFalse(default)


class Subcommand(unittest.TestCase):
    def test_expand_shell_log(self) -> None:
        self.assertEqual(llm.expand_cli_argv(["shell-log", "5"]), ["--shell-log", "5"])
        self.assertEqual(llm.expand_cli_argv(["shelllog"]), ["--shell-log"])
        self.assertEqual(
            llm.expand_cli_argv(["shell-log", "clear"]), ["--shell-log-clear"]
        )

    def test_parse_shell_log_flags(self) -> None:
        args = llm.parse_args(["--shell-log"])
        self.assertEqual(args.shell_log, shelllog.DEFAULT_VIEW_LINES)
        self.assertEqual(llm.parse_args(["--shell-log", "5"]).shell_log, 5)
        self.assertIsNone(llm.parse_args([]).shell_log)
        self.assertTrue(llm.parse_args(["--shell-log-clear"]).shell_log_clear)


class _TTY(io.StringIO):
    """확인 프롬프트를 받기 위한 가짜 TTY 입력."""

    def isatty(self) -> bool:  # noqa: D102
        return True


class AgentLogHooks(unittest.TestCase):
    """agent 쪽 로그 연결: 허용된 명령이 실제로 파일에 남는지."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        patches = [
            mock.patch.object(llm, "HISTORY_DIR", base / "home"),
            mock.patch.object(llm, "RUN", llm.RunState(session="t", ask_mode="all")),
            mock.patch.object(llm, "_SHELL_LOG", None),
            mock.patch.object(llm.sys, "stdin", _TTY()),
            mock.patch.dict("os.environ", {}, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.path = llm.shell_log_file("t")

    def entries(self) -> list[tuple[str, str]]:
        return [(e["kind"], e["cmd"]) for e in shelllog.ShellLog(self.path).entries()]

    def test_auto_run_of_allowed_command_is_logged(self) -> None:
        approval = llm.ShellApproval("t")
        approval.remember("make build", Path(self._tmp.name))
        with mock.patch.object(
            llm, "ask_shell_segment", side_effect=AssertionError("물으면 안 됨")
        ):
            self.assertTrue(
                llm.confirm_shell("make build", False, approval, only_risky=False)
            )
        self.assertEqual(self.entries(), [("auto", "make build")])
        self.assertEqual(shelllog.ShellLog(self.path).entries()[0]["mode"], "all")

    def test_granted_allow_is_logged(self) -> None:
        approval = llm.ShellApproval("t")
        with mock.patch.object(llm, "ask_shell_segment", return_value="a"):
            self.assertTrue(
                llm.confirm_shell("pytest -q", False, approval, only_risky=False)
            )
        self.assertEqual(self.entries(), [("a", "pytest -q")])

    def test_session_skip_is_logged(self) -> None:
        approval = llm.ShellApproval("t")
        with mock.patch.object(llm, "ask_shell_segment", return_value="all"):
            self.assertTrue(
                llm.confirm_shell("pytest -q", False, approval, only_risky=False)
            )
        self.assertEqual(self.entries(), [("all", "pytest -q")])

    def test_deny_is_logged(self) -> None:
        approval = llm.ShellApproval("t")
        with mock.patch.object(llm, "ask_shell_segment", return_value="n"):
            self.assertFalse(
                llm.confirm_shell("rm -rf /x", False, approval, only_risky=False)
            )
        self.assertEqual(self.entries(), [("n", "rm -rf /x")])

    def test_shell_log_enabled_env(self) -> None:
        self.assertTrue(llm.shell_log_enabled())
        with mock.patch.dict("os.environ", {"LLM_SHELL_LOG": "0"}):
            self.assertFalse(llm.shell_log_enabled())
            self.assertFalse(llm.shell_log().enabled)

    def test_cmd_shell_log_prints_entries(self) -> None:
        shelllog.ShellLog(self.path).record("auto", "ls -la", mode="all")
        out = _capture(lambda: llm.cmd_shell_log("t", limit=5))
        self.assertIn("셸 허용 로그", out)
        self.assertIn("ls -la", out)
        self.assertIn("자동 허용 실행", out)

    def test_cmd_shell_log_clear(self) -> None:
        shelllog.ShellLog(self.path).record("auto", "ls")
        out = _capture(lambda: llm.cmd_shell_log("t", clear=True))
        self.assertIn("비웠습니다", out)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
