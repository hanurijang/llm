#!/usr/bin/env python3
"""v0.7 개선 사항: 편집 안전장치 · undo · 중단 처리 · 재시도 · 맥락 관리 · 새 도구."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent as llm  # noqa: E402
from llmlib import conversation, netretry, web  # noqa: E402
from llmlib.context import expand_mentions, project_doc_files  # noqa: E402


class Isolated(unittest.TestCase):
    """홈의 ~/.local/share/llm 을 건드리지 않도록 저장 위치와 전역 상태를 격리한다."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = (base / "ws").resolve()
        self.root.mkdir()
        patches = [
            mock.patch.object(llm, "HISTORY_DIR", base / "home"),
            mock.patch.object(llm, "RUN", llm.RunState(session="t")),
            mock.patch.object(llm, "FILES", llm.FileTracker()),
            mock.patch.object(llm, "UNDO", None),
            mock.patch.object(llm, "_SECRET_CACHE", {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def tool(self, name: str, args: dict) -> str:
        return llm.run_tool(name, json.dumps(args), self.root, yes=True)


class EditSafety(Isolated):
    def test_edit_requires_read(self) -> None:
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        out = self.tool("edit", {"path": "a.py", "old_string": "1", "new_string": "2"})
        self.assertIn("먼저 read", out)
        self.tool("read", {"path": "a.py"})
        out = self.tool("edit", {"path": "a.py", "old_string": "1", "new_string": "2"})
        self.assertTrue(out.startswith("updated a.py (+1 -1"), out)

    def test_stale_file_detected(self) -> None:
        f = self.root / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        self.tool("read", {"path": "a.py"})
        time.sleep(0.01)
        f.write_text("x = 1  # 사용자가 바깥에서 고침\n", encoding="utf-8")
        os.utime(f, ns=(time.time_ns(), time.time_ns() + 1_000_000))
        out = self.tool("edit", {"path": "a.py", "old_string": "1", "new_string": "2"})
        self.assertIn("바깥에서 변경", out)

    def test_new_file_needs_no_read(self) -> None:
        out = self.tool("edit", {"action": "write", "path": "new/b.txt", "contents": "hi\n"})
        self.assertTrue(out.startswith("wrote new/b.txt"), out)

    def test_multi_edit_atomic(self) -> None:
        f = self.root / "m.py"
        f.write_text("a = 1\nb = 2\n", encoding="utf-8")
        self.tool("read", {"path": "m.py"})
        bad = self.tool(
            "edit",
            {"path": "m.py", "edits": [{"old_string": "a = 1", "new_string": "a = 10"}, {"old_string": "zzz", "new_string": "q"}]},
        )
        self.assertIn("edits[2]", bad)
        self.assertEqual(f.read_text(encoding="utf-8"), "a = 1\nb = 2\n")  # 아무것도 안 바뀜
        ok = self.tool(
            "edit",
            {"path": "m.py", "edits": [{"old_string": "a = 1", "new_string": "a = 10"}, {"old_string": "b = 2", "new_string": "b = 20"}]},
        )
        self.assertIn("2 replacements", ok)
        self.assertEqual(f.read_text(encoding="utf-8"), "a = 10\nb = 20\n")

    def test_crlf_preserved(self) -> None:
        f = self.root / "w.txt"
        f.write_bytes(b"one\r\ntwo\r\n")
        self.tool("read", {"path": "w.txt"})
        out = self.tool("edit", {"path": "w.txt", "old_string": "one\ntwo", "new_string": "1\n2"})
        self.assertTrue(out.startswith("updated"), out)
        self.assertEqual(f.read_bytes(), b"1\r\n2\r\n")

    def test_plan_mode_blocks_edits_and_writes(self) -> None:
        llm.RUN.plan_mode = True
        out = self.tool("edit", {"action": "write", "path": "x.txt", "contents": "x"})
        self.assertIn("plan 모드", out)
        out = self.tool("shell", {"command": "touch x.txt"})
        self.assertIn("plan 모드", out)
        out = self.tool("shell", {"command": "ls"})
        self.assertTrue(out.startswith("exit 0"), out)
        self.assertFalse((self.root / "x.txt").exists())

    def test_undo_restores_turn(self) -> None:
        f = self.root / "u.txt"
        f.write_text("orig\n", encoding="utf-8")
        llm.undo_store("t").begin_turn("바꿔줘")
        self.tool("read", {"path": "u.txt"})
        self.tool("edit", {"path": "u.txt", "old_string": "orig", "new_string": "one"})
        self.tool("edit", {"path": "u.txt", "old_string": "one", "new_string": "two"})
        self.tool("edit", {"action": "write", "path": "created.txt", "contents": "n\n"})
        history = self.root / "h.json"
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            rc = llm.cmd_undo("t", history, assume_yes=True)
        self.assertEqual(rc, 0)
        self.assertEqual(f.read_text(encoding="utf-8"), "orig\n")  # 턴의 첫 원본으로
        self.assertFalse((self.root / "created.txt").exists())
        note = json.loads(history.read_text(encoding="utf-8"))[-1]
        self.assertEqual(note["role"], "user")
        self.assertIn("/undo", note["content"])


class ToolsAndSecrets(Isolated):
    def test_grep_skips_secret_files_and_context(self) -> None:
        (self.root / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
        (self.root / "app.py").write_text("a\nTOKEN = get()\nb\n", encoding="utf-8")
        out = self.tool("find", {"mode": "grep", "pattern": "TOKEN", "context": 1})
        self.assertNotIn(".env", out)
        self.assertIn("app.py-1-a", out)
        self.assertIn("app.py:2:TOKEN", out)
        self.assertEqual(self.tool("find", {"mode": "grep", "pattern": "TOKEN", "output": "files"}), "app.py")

    def test_shell_env_scrubbed_and_redacted(self) -> None:
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-9876543210"}):
            llm._SECRET_CACHE.clear()
            out = self.tool("shell", {"command": "echo key=[$DEEPSEEK_API_KEY]"})
            self.assertIn("key=[]", out)  # 하위 프로세스에는 키가 없다
            (self.root / "notes.txt").write_text("leak sk-test-9876543210\n", encoding="utf-8")
            out = self.tool("read", {"path": "notes.txt"})
            self.assertNotIn("sk-test-9876543210", out)
            self.assertIn("[REDACTED]", out)

    def test_secret_command_needs_confirmation(self) -> None:
        (self.root / ".env").write_text("A=1\n", encoding="utf-8")
        llm.RUN.yes = False
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            out = llm.run_tool("shell", json.dumps({"command": "cat .env"}), self.root, yes=False)
        self.assertIn("비밀값", out)

    def test_background_shell(self) -> None:
        out = self.tool("shell", {"command": "echo started; sleep 0.2; echo done", "background": True})
        self.assertIn("started bg_", out)
        job_id = out.split()[1]
        time.sleep(0.6)
        polled = self.tool("shell", {"bg_id": job_id})
        self.assertIn("exited 0", polled)
        self.assertIn("done", polled)
        self.assertIn("새 출력 없음", self.tool("shell", {"bg_id": job_id}))

    def test_mentions_attach_and_mark_read(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "m.py").write_text("print(1)\n", encoding="utf-8")
        (self.root / ".env").write_text("K=1\n", encoding="utf-8")
        prompt, attached = expand_mentions(
            "@src/m.py 고쳐줘, @.env 도, 메일 a@b.com", self.root, llm.resolve_in_workspace, on_file=llm.FILES.mark
        )
        self.assertEqual(attached, ["src/m.py"])
        self.assertIn('<file path="src/m.py">', prompt)
        self.assertNotIn("K=1", prompt)
        self.assertIsNone(llm.FILES.check(self.root / "src" / "m.py"))  # 첨부 = 읽은 것

    def test_html_to_text(self) -> None:
        title, text = web.html_to_text(
            "<html><head><title>T</title><style>x{}</style></head><body><h1>Hi</h1><p>a &amp; b</p>"
            "<script>bad()</script><ul><li>one</li></ul></body></html>"
        )
        self.assertEqual(title, "T")
        self.assertIn("# Hi", text)
        self.assertIn("a & b", text)
        self.assertIn("- one", text)
        self.assertNotIn("bad()", text)
        with self.assertRaises(ValueError):
            web.validate_url("file:///etc/passwd")

    def test_project_docs_discovery(self) -> None:
        home = self.root.parent
        (home / ".claude").mkdir()
        (home / ".claude" / "CLAUDE.md").write_text("global", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("agents", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("claude", encoding="utf-8")
        names = [p.name for p in project_doc_files(self.root, home=home)]
        self.assertEqual(names, ["CLAUDE.md", "CLAUDE.md", "AGENTS.md"])
        prompt = llm.build_system_prompt(self.root, "t")
        self.assertIn("<project_instructions>", prompt)


class Conversation(unittest.TestCase):
    def test_finalize_missing_tool_results(self) -> None:
        msgs = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "read", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "shell", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        what = conversation.finalize_interrupted(msgs)
        self.assertIn("1개", what)
        self.assertEqual(msgs[-1]["tool_call_id"], "c2")
        self.assertEqual(llm.repair_history(msgs), msgs)  # API 규격상 온전

    def test_finalize_mid_generation(self) -> None:
        msgs = [{"role": "user", "content": "x"}]
        conversation.finalize_interrupted(msgs)
        self.assertEqual(msgs[-1]["role"], "assistant")

    def test_clear_old_tool_results(self) -> None:
        msgs: list[dict] = []
        for i in range(10):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": "read", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 10_000})
        out, cleared, saved = conversation.clear_old_tool_results(msgs)
        self.assertEqual(cleared, 4)  # 최근 6개는 유지
        self.assertGreater(saved, 35_000)
        self.assertTrue(out[1]["content"].startswith(conversation.CLEARED_PREFIX))
        self.assertEqual(out[-1]["content"], "x" * 10_000)
        again, cleared2, _ = conversation.clear_old_tool_results(out)
        self.assertEqual(cleared2, 0)  # 기준 아래로 내려가 다시 비우지 않음(캐시 접두부 고정)

    def test_token_trigger(self) -> None:
        msgs = [{"role": "user", "content": "x"}] * (llm.SUMMARY_KEEP_RECENT + 2)
        with mock.patch.object(llm, "LAST_PROMPT_TOKENS", 0):
            self.assertFalse(llm.should_summarize(msgs))
        with mock.patch.object(llm, "LAST_PROMPT_TOKENS", llm.COMPACT_TOKENS_DEFAULT + 1):
            self.assertTrue(llm.should_summarize(msgs))


class Retry(unittest.TestCase):
    def test_retries_then_succeeds(self) -> None:
        calls = []

        def opener(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError("u", 429, "busy", {"Retry-After": "0"}, io.BytesIO(b""))
            return "OK"

        seen = []
        out = netretry.urlopen_with_retry(
            object(), 5, on_retry=lambda *a: seen.append(a), retries=4, sleep=lambda s: None, opener=opener
        )
        self.assertEqual(out, "OK")
        self.assertEqual(len(calls), 3)
        self.assertEqual(seen[0][2], "HTTP 429")

    def test_no_retry_on_400(self) -> None:
        def opener(req, timeout):
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b""))

        with self.assertRaises(urllib.error.HTTPError):
            netretry.urlopen_with_retry(object(), 5, retries=4, sleep=lambda s: None, opener=opener)

    def test_gives_up(self) -> None:
        def opener(req, timeout):
            raise urllib.error.URLError("down")

        with self.assertRaises(urllib.error.URLError):
            netretry.urlopen_with_retry(object(), 5, retries=2, sleep=lambda s: None, opener=opener)


class Cli(unittest.TestCase):
    def test_new_subcommands(self) -> None:
        self.assertEqual(llm.expand_cli_argv(["undo", "-y"]), ["--undo", "-y"])
        self.assertEqual(llm.expand_cli_argv(["plan", "x"]), ["--plan", "x"])
        args = llm.parse_args(["--plan", "--confirm-edits", "q"])
        self.assertTrue(args.plan and args.confirm_edits)


if __name__ == "__main__":
    unittest.main()
