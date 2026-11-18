#!/usr/bin/env python3
"""Phase 1 UX: 도구 통합 · 셸 기본 safe · AUTO_SYNC 기본 off."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 저장소 루트에서 import
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent as llm  # noqa: E402


class Phase1Defaults(unittest.TestCase):
    def test_version(self) -> None:
        self.assertEqual(llm.VERSION, "0.13.0")

    def test_version_matches_readme(self) -> None:
        """README 머리의 버전 표기는 VERSION 과 같아야 한다(버전 올릴 때 함께 갱신)."""
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"**버전:** v{llm.VERSION} ", readme)
        self.assertIn(f"## v{llm.VERSION.rsplit('.', 1)[0]} 변경 사항", readme)

    def test_header_rows_align_and_show_state(self) -> None:
        """시작 헤더는 라벨 정렬 행으로 세션·폴더·셸·설정을 보여 준다."""
        lines: list[str] = []
        with mock.patch("builtins.print", lambda *a, **k: lines.append(" ".join(map(str, a)))):
            with mock.patch.object(llm, "load_session_rules", lambda root: "x"):
                llm.print_banner("sess", Path("/tmp/proj"), False, ask_mode="readonly", repl=True)
        text = "\n".join(lines)
        self.assertIn("· 대화형", text)
        for label in ("세션", "폴더", "셸", "설정"):
            self.assertIn(label, text)
        self.assertEqual(llm.header_line("세션", "v").startswith("  "), True)
        self.assertIn("rules on", text)

    def test_tool_schema_names(self) -> None:
        names = [t["function"]["name"] for t in llm.tool_schemas()]
        self.assertEqual(names, ["todo", "read", "edit", "find", "shell", "web_fetch", "task"])

    def test_conversation_git_sync_is_disabled(self) -> None:
        """환경변수를 켜도 대화 기록이 원격 저장소로 전송되지 않는다."""
        with mock.patch.dict(os.environ, {"LLM_AUTO_SYNC": "1", "LLM_AUTO_PULL": "1"}):
            self.assertFalse(llm.auto_sync_enabled())
            self.assertFalse(llm.auto_pull_enabled())
            with mock.patch.object(llm, "git_run", side_effect=AssertionError("git must not run")):
                self.assertEqual(llm.cmd_sync_history(Path("/tmp"), "test"), 1)
                llm.auto_sync_history(Path("/tmp"), "test")
                llm.maybe_auto_pull(Path("/tmp"))

    def test_history_directory_remains_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "HISTORY_DIR", Path(tmp) / "home"):
                with mock.patch.dict(os.environ, {"LLM_HISTORY": ""}):
                    self.assertEqual(llm.history_scope_dir(Path(tmp)), Path(tmp) / ".llm")
                with mock.patch.dict(os.environ, {"LLM_HISTORY": "home"}):
                    self.assertEqual(llm.history_scope_dir(Path(tmp)), Path(tmp) / "home")

    def test_history_directory_from_env(self) -> None:
        """LLM_HISTORY_DIR 우선, 빈 값은 프로젝트 .llm/ 폴백."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "project"
            root.mkdir()
            with mock.patch.dict(os.environ, {"LLM_HISTORY_DIR": "", "LLM_HISTORY": ""}):
                self.assertEqual(llm.history_scope_dir(root), root / ".llm")
            with mock.patch.dict(os.environ, {"LLM_HISTORY_DIR": "/data/llm-sessions", "LLM_HISTORY": ""}):
                self.assertEqual(llm.history_scope_dir(root), Path("/data/llm-sessions"))
            with mock.patch.dict(os.environ, {"LLM_HISTORY_DIR": "private_sessions", "LLM_HISTORY": ""}):
                self.assertEqual(llm.history_scope_dir(root), (root / "private_sessions").resolve())
            with mock.patch.dict(os.environ, {"LLM_HISTORY_DIR": "", "LLM_HISTORY": "home"}):
                self.assertEqual(llm.history_scope_dir(root), llm.HISTORY_DIR)

    def test_shared_history_directory_keeps_project_sessions_separate(self) -> None:
        """공유 경로에 다른 폴더의 세션 하나만 있어도 잘못 자동 바인딩하지 않는다."""
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp) / "sessions"
            shared.mkdir()
            (shared / "history_unrelated.json").write_text("[]", encoding="utf-8")
            root = Path(temp) / "project"
            root.mkdir()
            with mock.patch.dict(os.environ, {"LLM_HISTORY_DIR": str(shared), "LLM_HISTORY": ""}):
                with mock.patch.object(llm, "HISTORY_DIR", Path(temp) / "config"):
                    bound, name = llm.auto_bind_from_repo_history({}, root)
                    self.assertIsNone(name)
                    self.assertEqual(bound, {})

    def test_ask_mode_default_safe(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("LLM_ALLOW_ALL", "LLM_AUTO_WORKSPACE", "LLM_SHELL_TRUST"):
                os.environ.pop(key, None)
            args = SimpleNamespace(
                confirm_all=False,
                yes=False,
                trust=False,
                allow_all=False,
                safe=False,
                auto_workspace=False,
            )
            aw, aa, mode, _src = llm.resolve_ask_mode(args)
            self.assertTrue(aw)
            self.assertFalse(aa)
            self.assertEqual(mode, "outside")

    def test_ask_mode_trust(self) -> None:
        args = SimpleNamespace(
            confirm_all=False,
            yes=False,
            trust=True,
            allow_all=False,
            safe=False,
            auto_workspace=False,
        )
        aw, aa, mode, src = llm.resolve_ask_mode(args)
        self.assertFalse(aw)
        self.assertTrue(aa)
        self.assertEqual(mode, "risky")
        self.assertEqual(src, "--trust")

    def test_run_tool_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            llm, "HISTORY_DIR", Path(tmp) / "home"  # undo 기록이 실제 홈에 남지 않게
        ), mock.patch.object(llm, "UNDO", None):
            root = Path(tmp)
            f = root / "a.txt"
            f.write_text("hello\n", encoding="utf-8")
            # v0.7: 기존 파일은 먼저 read 해야 편집할 수 있다(구 도구명 read_file 로 확인).
            self.assertIn("hello", llm.run_tool("read_file", '{"path":"a.txt"}', root, yes=True))
            out = llm.run_tool(
                "str_replace",
                '{"path":"a.txt","old_string":"hello","new_string":"hi"}',
                root,
                yes=True,
            )
            self.assertIn("updated", out)
            self.assertEqual(f.read_text(encoding="utf-8"), "hi\n")
            out = llm.run_tool(
                "find",
                '{"mode":"grep","pattern":"hi","path":"."}',
                root,
                yes=True,
            )
            self.assertIn("hi", out)

    def test_auto_bind_single_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hist = root / ".llm"
            hist.mkdir()
            (hist / "history_demo.json").write_text("[]", encoding="utf-8")
            with mock.patch.object(llm, "history_scope_is_repo", return_value=True):
                with mock.patch.object(llm, "save_bindings", lambda folders: None):
                    folders, session = llm.auto_bind_from_repo_history({}, root)
            self.assertEqual(session, "demo")
            self.assertEqual(folders.get(llm.canonical_folder(root)), "demo")


class Phase2Cli(unittest.TestCase):
    def test_expand_cli_argv(self) -> None:
        self.assertEqual(llm.expand_cli_argv(["sync"]), ["--sync-history"])
        self.assertEqual(llm.expand_cli_argv(["ask", "hi"]), ["--once", "hi"])
        self.assertEqual(llm.expand_cli_argv(["chat", "x"]), ["--chat", "x"])
        self.assertEqual(llm.expand_cli_argv(["repl"]), ["--interactive"])
        self.assertEqual(llm.expand_cli_argv(["i"]), ["--interactive"])
        self.assertEqual(llm.expand_cli_argv(["agent"]), ["--interactive"])
        self.assertEqual(llm.expand_cli_argv(["session", "adopt"]), ["--session-adopt"])
        self.assertEqual(llm.expand_cli_argv(["session"]), ["--list-sessions"])
        self.assertEqual(llm.expand_cli_argv(["-s", "a"]), ["-s", "a"])

    def test_parse_interactive(self) -> None:
        args = llm.parse_args(llm.expand_cli_argv(["-i"]))
        self.assertTrue(args.interactive)
        args = llm.parse_args(llm.expand_cli_argv(["sync"]))
        self.assertTrue(args.sync_history)
        args = llm.parse_args(llm.expand_cli_argv(["ask", "hi"]))
        self.assertTrue(args.once)
        self.assertFalse(getattr(args, "interactive"))

    def test_help_has_subcommands(self) -> None:
        """`llm --help` 는 새 서브커맨드 안내를 보여야 한다."""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                llm.parse_args(["--help"])
        out = buf.getvalue()
        self.assertIn("session ls|adopt|view", out)
        self.assertIn("shell log", out)
        self.assertNotIn("--shell-log ", out)  # 옛 플래그는 help 에서 숨긴다


if __name__ == "__main__":
    unittest.main()
