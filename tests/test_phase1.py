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
        self.assertEqual(llm.VERSION, "0.9.0")

    def test_version_matches_readme(self) -> None:
        """README 머리의 버전 표기는 VERSION 과 같아야 한다(버전 올릴 때 함께 갱신)."""
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"**버전:** v{llm.VERSION} ", readme)
        self.assertIn(f"## v{llm.VERSION.rsplit('.', 1)[0]} 변경 사항", readme)

    def test_tool_schema_names(self) -> None:
        names = [t["function"]["name"] for t in llm.tool_schemas()]
        self.assertEqual(names, ["todo", "read", "edit", "find", "shell", "web_fetch", "task"])

    def test_auto_sync_default_off(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_AUTO_SYNC", None)
            self.assertFalse(llm.auto_sync_enabled())
            os.environ["LLM_AUTO_SYNC"] = "1"
            self.assertTrue(llm.auto_sync_enabled())
            os.environ["LLM_AUTO_SYNC"] = "0"
            self.assertFalse(llm.auto_sync_enabled())
            os.environ.pop("LLM_AUTO_SYNC", None)

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
        self.assertEqual(llm.expand_cli_argv(["ask", "hi"]), ["hi"])
        self.assertEqual(llm.expand_cli_argv(["chat", "x"]), ["--chat", "x"])
        self.assertEqual(llm.expand_cli_argv(["repl"]), ["--interactive"])
        self.assertEqual(llm.expand_cli_argv(["i"]), ["--interactive"])
        self.assertEqual(llm.expand_cli_argv(["session", "adopt"]), ["--session-adopt"])
        self.assertEqual(llm.expand_cli_argv(["session"]), [])
        self.assertEqual(llm.expand_cli_argv(["-s", "a"]), ["-s", "a"])

    def test_parse_interactive(self) -> None:
        args = llm.parse_args(llm.expand_cli_argv(["-i"]))
        self.assertTrue(args.interactive)
        args = llm.parse_args(llm.expand_cli_argv(["sync"]))
        self.assertTrue(args.sync_history)


if __name__ == "__main__":
    unittest.main()
