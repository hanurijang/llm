#!/usr/bin/env python3
"""`/cost` 세션 누적 소요 금액(잔액 차이) 계산과 불가 안내 테스트."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent as llm  # noqa: E402


class CostMath(unittest.TestCase):
    """잔액 차이 → 금액 줄(순수 계산)."""

    def test_balance_cost_from_single_currency(self) -> None:
        costs = llm.balance_cost_from({"CNY": "3.00"}, {"CNY": "2.95"})
        self.assertIsNotNone(costs)
        assert costs is not None
        self.assertAlmostEqual(costs["CNY"], 0.05, places=6)

    def test_balance_cost_from_bad_format_is_none(self) -> None:
        self.assertIsNone(llm.balance_cost_from({"CNY": "??"}, {"CNY": "1.00"}))

    def test_summary_lines_show_money_and_range(self) -> None:
        lines = llm.cost_summary_lines({"CNY": "3.00"}, {"CNY": "2.95"})
        self.assertEqual(len(lines), 1)
        self.assertIn("세션 소요 금액", lines[0])
        self.assertIn("시작", lines[0])
        self.assertIn("현재", lines[0])

    def test_summary_lines_flag_negligible_amount(self) -> None:
        """0.01 미만이면 금액을 지어내지 않고 안내 줄을 덧붙인다."""
        lines = llm.cost_summary_lines({"CNY": "3.00"}, {"CNY": "3.00"})
        self.assertEqual(lines[0].count("0.01 미만"), 1)
        self.assertEqual(len(lines), 2)
        self.assertIn("정확한 금액은 알 수 없습니다", lines[1])

    def test_summary_lines_label_balance_increase(self) -> None:
        lines = llm.cost_summary_lines({"CNY": "1.00"}, {"CNY": "3.00"})
        self.assertIn("세션 잔액 증가", lines[0])


class SessionCostLines(unittest.TestCase):
    """`/cost` 에 붙는 금액 줄(불가하면 이유 안내)."""

    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        base = llm.SESSION_BALANCE_BASE
        self.addCleanup(setattr, llm, "SESSION_BALANCE_BASE", base)
        llm.SESSION_BALANCE_BASE = ({"CNY": "3.00"}, 600.0)
        self.addCleanup(setattr, llm, "COST_BALANCE_CACHE", llm.COST_BALANCE_CACHE)
        llm.COST_BALANCE_CACHE = None

    def test_amount_uses_balance_difference(self) -> None:
        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", lambda: ({"CNY": "2.95"}, 590.0)):
                lines = llm.session_cost_lines()
        self.assertIn("세션 소요 금액", lines[0])

    def test_no_balance_flag_explains_reason(self) -> None:
        lines = llm.session_cost_lines(no_balance=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("--no-balance", lines[0])

    def test_missing_api_key_explains_reason(self) -> None:
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}):
            lines = llm.session_cost_lines()
        self.assertIn("API 키", lines[0])

    def test_missing_base_balance_explains_reason(self) -> None:
        llm.SESSION_BALANCE_BASE = None
        lines = llm.session_cost_lines()
        self.assertIn("기준 잔액", lines[0])

    def test_fetch_failure_explains_reason(self) -> None:
        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", side_effect=OSError("boom")):
                lines = llm.session_cost_lines()
        self.assertIn("조회하지 못해", lines[0])

    def test_repeated_call_uses_cache(self) -> None:
        calls = []

        def fake_fetch():
            calls.append(1)
            return ({"CNY": "2.95"}, 590.0)

        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", fake_fetch):
                llm.session_cost_lines()
                llm.session_cost_lines()
        self.assertEqual(len(calls), 1)


class PrefetchBase(unittest.TestCase):
    """시작 잔액 조회가 세션 기준 잔액을 한 번만 기록하는지."""

    def setUp(self) -> None:
        for name in ("START_BALANCE", "SESSION_BALANCE_BASE", "COST_BALANCE_CACHE"):
            self.addCleanup(setattr, llm, name, getattr(llm, name))
        self._env = mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_first_fetch_becomes_base(self) -> None:
        llm.START_BALANCE = None
        llm.SESSION_BALANCE_BASE = None
        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", lambda: ({"CNY": "3.00"}, 600.0)):
                llm.prefetch_balance()
                llm.START_BALANCE = None  # 다음 턴이 시작 잔액을 다시 조회한 상황
                with mock.patch.object(
                    llm, "fetch_balance", lambda: ({"CNY": "2.50"}, 500.0)
                ):
                    llm.prefetch_balance()
        assert llm.SESSION_BALANCE_BASE is not None
        self.assertEqual(llm.SESSION_BALANCE_BASE[0], {"CNY": "3.00"})

    def test_quiet_prefetch_suppresses_warning(self) -> None:
        llm.START_BALANCE = None
        llm.SESSION_BALANCE_BASE = None
        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", side_effect=OSError("down")):
                with mock.patch.object(llm, "print_warn") as warn:
                    llm.prefetch_balance(quiet=True)
                    self.assertFalse(warn.called)
                    llm.prefetch_balance()
                    self.assertTrue(warn.called)


class BalanceCommand(unittest.TestCase):
    """REPL `/balance` — 잔액 보기 + `/cost` 캐시 공유."""

    def setUp(self) -> None:
        for name in ("START_BALANCE", "SESSION_BALANCE_BASE", "COST_BALANCE_CACHE"):
            self.addCleanup(setattr, llm, name, getattr(llm, name))
        llm.COST_BALANCE_CACHE = None
        self._env = mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "k"}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_balance_now_refreshes_cost_cache(self) -> None:
        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", lambda: ({"CNY": "3.00"}, 600.0)):
                amounts, krw = llm.balance_now()
        self.assertEqual(amounts, {"CNY": "3.00"})
        self.assertEqual(krw, 600.0)
        self.assertIsNotNone(llm.COST_BALANCE_CACHE)
        assert llm.COST_BALANCE_CACHE is not None
        self.assertEqual(llm.COST_BALANCE_CACHE[1], {"CNY": "3.00"})

    def test_cost_reuses_balance_cache(self) -> None:
        """`/balance` 직후의 `/cost` 는 잔액을 다시 조회하지 않는다."""
        llm.SESSION_BALANCE_BASE = ({"CNY": "3.00"}, 600.0)
        calls = []

        def fake_fetch():
            calls.append(1)
            return ({"CNY": "2.99"}, 590.0)

        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", fake_fetch):
                llm.balance_now()
                lines = llm.session_cost_lines()
        self.assertEqual(len(calls), 1)
        self.assertIn("2.99", lines[0])

    def test_repl_commands_register_balance(self) -> None:
        self.assertIn("/balance", llm.REPL_COMMANDS)
        self.assertIn("잔액", llm.REPL_COMMANDS["/balance"])

    def test_cmd_balance_reports_failure(self) -> None:
        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", side_effect=OSError("down")):
                with mock.patch.object(llm, "print_warn") as warn:
                    self.assertEqual(llm.cmd_balance(), 1)
        self.assertTrue(warn.called)

    def test_cmd_balance_prints_block(self) -> None:
        with mock.patch.object(llm, "load_rates", lambda *a, **k: None):
            with mock.patch.object(llm, "fetch_balance", lambda: ({"CNY": "3.00"}, 600.0)):
                with mock.patch.object(llm, "print_balance_block") as block:
                    self.assertEqual(llm.cmd_balance(), 0)
        block.assert_called_once()


if __name__ == "__main__":
    unittest.main()
