"""One-time Linux command registration: works outside checkout and is idempotent."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux/Bash only")
class LinuxInstaller(unittest.TestCase):
    def test_install_registers_command_from_any_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            work = Path(tmp) / "other-project"
            home.mkdir()
            work.mkdir()
            env = {**os.environ, "HOME": str(home)}
            for _ in range(2):
                done = subprocess.run(
                    ["bash", str(ROOT / "install.sh")],
                    cwd=str(work), env=env, text=True, capture_output=True, timeout=30,
                )
                self.assertEqual(done.returncode, 0, done.stderr)
            link = home / ".local/bin/llm"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (ROOT / "llm").resolve())
            for file in (home / ".profile", home / ".bashrc"):
                self.assertEqual(file.read_text().count('export PATH="$HOME/.local/bin:$PATH"'), 1)
            env["PATH"] = str(link.parent) + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                ["llm", "help"], cwd=str(work), env=env,
                text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DeepSeek CLI", result.stdout)

    def test_install_does_not_overwrite_unrelated_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".local/bin/llm"
            target.parent.mkdir(parents=True)
            target.write_text("unrelated", encoding="utf-8")
            env = {**os.environ, "HOME": str(home)}
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh")],
                cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), "unrelated")


if __name__ == "__main__":
    unittest.main()
