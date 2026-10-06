#!/usr/bin/env python3
"""`llm` bash 래퍼의 이식성 테스트: 심볼릭 링크 해석(BSD readlink) · LLM_PYTHON."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "llm"

FAKE_BSD_READLINK = """#!/bin/bash
# BSD/macOS 구형 readlink 모사: -f 를 지원하지 않는다.
case "$1" in -f) echo "readlink: illegal option -- f" >&2; exit 1 ;; esac
exec /usr/bin/readlink "$@"
"""


def _has_readlink() -> bool:
    return bool(shutil.which("readlink"))


@unittest.skipUnless(shutil.which("bash"), "bash 필요")
@unittest.skipUnless(_has_readlink(), "readlink 필요")
class LauncherResolution(unittest.TestCase):
    """래퍼가 심볼릭 링크를 직접 따라가 agent.py 를 찾는지."""

    def _run(
        self, argv: list[str], path_prepend: list[str] | None = None, **env_extra: str
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("LLM_PYTHON", None)
        if path_prepend:
            env["PATH"] = os.pathsep.join([*path_prepend, env.get("PATH", "")])
        env.update(env_extra)
        return subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=120
        )

    def test_symlink_chain_finds_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            first = bin_dir / "llm-real"
            first.symlink_to(WRAPPER)
            link = bin_dir / "llm"
            link.symlink_to(first)
            proc = self._run([str(link), "--help"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("usage: llm", proc.stdout)

    def test_works_without_readlink_dash_f(self) -> None:
        """readlink -f 없는 환경(macOS/BSD)에서도 동작해야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            fakebin = Path(tmp) / "fakebin"
            fakebin.mkdir()
            fake = fakebin / "readlink"
            fake.write_text(FAKE_BSD_READLINK)
            fake.chmod(0o755)
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            link = bin_dir / "llm"
            link.symlink_to(WRAPPER)
            proc = self._run([str(link), "--help"], path_prepend=[str(fakebin)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("usage: llm", proc.stdout)
            self.assertNotIn("illegal option", proc.stderr)

    def test_missing_agent_next_to_wrapper(self) -> None:
        """agent.py 가 없는 폴더에 래퍼만 있으면 안내하고 중단한다."""
        with tempfile.TemporaryDirectory() as tmp:
            lonely = Path(tmp) / "llm"
            shutil.copy2(WRAPPER, lonely)
            lonely.chmod(0o755)
            proc = self._run([str(lonely), "--help"])
            self.assertEqual(proc.returncode, 1)
            self.assertIn("agent.py", proc.stderr)

    def test_llm_python_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            link = bin_dir / "llm"
            link.symlink_to(WRAPPER)
            proc = self._run([str(link), "--help"], LLM_PYTHON="python9-does-not-exist")
            self.assertEqual(proc.returncode, 127)
            self.assertIn("LLM_PYTHON", proc.stderr)

    def test_llm_python_override_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            link = bin_dir / "llm"
            link.symlink_to(WRAPPER)
            proc = self._run([str(link), "--help"], LLM_PYTHON=sys.executable)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("usage: llm", proc.stdout)


if __name__ == "__main__":
    unittest.main()
