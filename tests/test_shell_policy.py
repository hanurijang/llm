#!/usr/bin/env python3
"""셸 안전 분류 표 테스트: 명령 → 자동 실행 여부 / 비밀 노출 여부."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmlib import guard  # noqa: E402
from llmlib.shell_policy import (  # noqa: E402
    is_safe_command,
    is_workspace_command,
    needs_confirm_always,
)

ROOT = Path("/tmp/llm-policy-root")

# (명령, 읽기 전용 자동 실행 기대값)
SAFE_TABLE = [
    ("ls -la", True),
    ("git status", True),
    ("git log --oneline -5", True),
    ("git diff HEAD~1", True),
    ("cat README.md", True),
    ("grep -rn foo src", True),
    ("python3 -m pytest -q", True),
    ("ls && git status", True),
    ("rm -rf build", False),
    ("git push", False),
    ("git commit -m x", False),
    ("echo hi > out.txt", False),
    ("curl https://example.com", False),
    ("python3 -c 'print(1)'", False),
    ("ls && rm x", False),
    ("find . -delete", False),
    ("sed -i s/a/b/ f", False),
    ("npm install", False),
    ("cat x | sh", False),
    ("PATH=/tmp ls", False),
]

# 비밀을 드러낼 수 있는 명령(기대: 이유 문자열) / 무해한 명령(기대: None)
SECRET_TABLE = [
    ("cat .env", True),
    ("cat ./config/.env.local", True),
    ("cat /etc/env/.env", True),
    ("head -3 ~/.ssh/id_rsa", True),
    ("echo $DEEPSEEK_API_KEY", True),
    ("echo ${GITHUB_TOKEN}", True),
    ("printenv", True),
    ("printenv OPENAI_API_KEY", True),
    ("env", True),
    ("env | grep KEY", True),
    ("export -p", True),
    ("python3 app.py --env-file=.env", True),
    ("cat .env.example", False),
    ("printenv HOME", False),
    ("env FOO=1 make test", False),
    ("export FOO=1", False),
    ("git status", False),
    ("grep -rn TOKEN src", False),
    ("ls -la", False),
]


class ShellPolicyTable(unittest.TestCase):
    def test_safe_table(self) -> None:
        for command, expected in SAFE_TABLE:
            with self.subTest(command=command):
                self.assertEqual(is_safe_command(command), expected)

    def test_secret_table(self) -> None:
        for command, expected in SECRET_TABLE:
            with self.subTest(command=command):
                self.assertEqual(guard.command_touches_secret(command) is not None, expected)

    def test_confirm_always(self) -> None:
        for command in ("sudo ls", "pip install x", "npm i left-pad", "rm -rf ~/x", "bash <<EOF\nls\nEOF"):
            with self.subTest(command=command):
                self.assertTrue(needs_confirm_always(command, ROOT))
        for command in ("ls", "rm build/x.o", "python3 - <<EOF\nprint('install')\nEOF"):
            with self.subTest(command=command):
                self.assertFalse(needs_confirm_always(command, ROOT))

    def test_workspace_scope(self) -> None:
        self.assertTrue(is_workspace_command("mkdir -p build && touch build/x", ROOT))
        self.assertFalse(is_workspace_command("touch ~/x", ROOT))
        self.assertFalse(is_workspace_command("rm -rf ../other", ROOT))
        self.assertFalse(is_workspace_command("git push --force", ROOT))


class GuardHelpers(unittest.TestCase):
    def test_scrubbed_env(self) -> None:
        env = guard.scrubbed_env(
            {
                "DEEPSEEK_API_KEY": "sk-1",
                "AWS_SECRET_ACCESS_KEY": "a",
                "GH_TOKEN": "g",
                "DB_PASSWORD": "p",
                "HOME": "/home/x",
                "PATH": "/bin",
                "SSH_AUTH_SOCK": "/tmp/s",
            }
        )
        self.assertEqual(set(env), {"HOME", "PATH", "SSH_AUTH_SOCK"})

    def test_keep_env_override(self) -> None:
        env = guard.scrubbed_env({"GH_TOKEN": "g", guard.KEEP_ENV_VAR: "GH_TOKEN"})
        self.assertEqual(env["GH_TOKEN"], "g")

    def test_redact(self) -> None:
        values = guard.collect_secret_values({"DEEPSEEK_API_KEY": "sk-abcdef123456", "HOME": "/home/x"})
        self.assertEqual(values, ["sk-abcdef123456"])
        self.assertEqual(guard.redact("key=sk-abcdef123456!", values), f"key={guard.REDACTED}!")

    def test_env_file_values(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "DEEPSEEK_API_KEY=sk-live-0123456789\nMODE=production\nSESSION_SEED=a8f3k29dk3m2x9q1\n",
                encoding="utf-8",
            )
            values = guard.collect_secret_values({}, [env_file])
        self.assertIn("sk-live-0123456789", values)
        self.assertIn("a8f3k29dk3m2x9q1", values)  # 이름은 평범해도 토큰처럼 생긴 값
        self.assertNotIn("production", values)       # 평범한 설정값은 가리지 않는다

    def test_secret_names(self) -> None:
        for name in (".env", ".env.local", ".env.production", "id_rsa", "server.pem", ".netrc"):
            self.assertTrue(guard.is_secret_name(name), name)
        for name in (".env.example", ".env.sample", "README.md", "env.py"):
            self.assertFalse(guard.is_secret_name(name), name)


if __name__ == "__main__":
    unittest.main()
