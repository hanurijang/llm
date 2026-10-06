#!/usr/bin/env python3
"""내장 Markdown 렌더러(llmlib/mdrender.py): 구조 정리·폭 계산·색 처리."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmlib import mdrender as md  # noqa: E402


def render(text: str, width: int = 60, color: bool = False) -> list[str]:
    return md.render(text, width=width, color=color).split("\n")


class Inline(unittest.TestCase):
    def test_markers_removed_without_color(self) -> None:
        # 색이 없으면 기호는 지우고 글자만 남긴다(로그 가독성).
        self.assertEqual(md.plain("**굵게** 와 `코드`"), "굵게 와 코드")
        self.assertEqual(md.plain("~~취소~~"), "취소")
        self.assertEqual(md.plain("*기울임*"), "기울임")

    def test_link_keeps_url(self) -> None:
        self.assertEqual(md.plain("[문서](https://x.dev/a)"), "문서 (https://x.dev/a)")
        self.assertEqual(md.plain("<https://x.dev>"), "<https://x.dev>")

    def test_color_adds_ansi(self) -> None:
        out = md.inline("**굵게**", md.Palette(True))
        self.assertIn("\033[1m", out)
        self.assertTrue(out.endswith("\033[0m"))

    def test_snake_case_not_italic(self) -> None:
        # 식별자의 밑줄을 기울임으로 오해하지 않는다.
        self.assertEqual(md.plain("snake_case_name"), "snake_case_name")
        self.assertIn("a_b", md.plain("`a_b`"))

    def test_code_span_kept_whole(self) -> None:
        lines = md.wrap_md("앞 `a b c` 뒤", 40)
        self.assertEqual(lines, ["앞 `a b c` 뒤"])
        # 코드 스팬은 한 덩어리로 다뤄 안의 공백에서 끊기지 않는다.
        wrapped = md.wrap_md("앞 `a b c` 뒤", 8)
        self.assertEqual(wrapped, ["앞 `a b c`", "뒤"])


class Blocks(unittest.TestCase):
    def test_heading_and_rule(self) -> None:
        lines = render("# 제목 하나\n\n본문")
        self.assertEqual(lines[0], "제목 하나")
        self.assertTrue(set(lines[1]) == {"─"})
        self.assertNotIn("#", "\n".join(lines))

    def test_lists_and_tasks(self) -> None:
        text = "- 하나\n- [x] 끝난 일\n  - 중첩\n1. 첫째\n2) 둘째\n"
        lines = render(text)
        self.assertIn("• 하나", lines)
        self.assertIn("☑ 끝난 일", lines)
        self.assertIn("  • 중첩", lines)
        self.assertIn("1. 첫째", lines)
        self.assertIn("2. 둘째", lines)

    def test_long_item_hanging_indent(self) -> None:
        lines = render("- 긴 항목입니다 " + "가나다라 " * 8, width=40)
        self.assertTrue(lines[0].startswith("• "))
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))

    def test_code_block(self) -> None:
        text = "```python\nprint('x')\n```\n"
        lines = render(text, width=40)
        self.assertIn("┌─ python", lines[0])
        self.assertTrue(lines[1].startswith("│ print('x')"))
        self.assertTrue(lines[-1].startswith("└"))

    def test_code_block_clipped_to_width(self) -> None:
        long_line = "x" * 300
        lines = render(f"```\n{long_line}\n```\n", width=40)
        body = [line for line in lines if line.startswith("│")]
        self.assertTrue(body)
        self.assertTrue(all(md.display_width(line) <= 40 for line in body))

    def test_quote(self) -> None:
        lines = render("> 인용 한 줄\n> 인용 둘\n")
        self.assertTrue(lines[0].startswith("│ 인용 한 줄"))
        self.assertTrue(lines[1].startswith("│ 인용 둘"))

    def test_hr(self) -> None:
        lines = render("앞\n\n---\n\n뒤")
        self.assertIn("─" * 60, lines)

    def test_table_alignment_with_wide_chars(self) -> None:
        text = "| 항목 | 값 |\n|:--|--:|\n| glow | 없음 |\n| 한글 | 2칸 |\n"
        lines = render(text)
        widths = {md.display_width(line) for line in lines if line.strip()}
        self.assertEqual(len(widths), 1)  # 모든 줄이 같은 폭으로 정렬된다
        self.assertNotIn("|", "\n".join(lines))
        self.assertIn("──", lines[1])  # 헤더 아래 구분선


class Invariants(unittest.TestCase):
    def test_no_ansi_when_color_false(self) -> None:
        text = "# 제목\n\n- **굵게** `코드`\n\n```sh\nls\n```\n"
        self.assertNotIn("\033", md.render(text, width=50, color=False))

    def test_lines_within_width(self) -> None:
        text = (
            "# 제목\n\n문단입니다 " + "단어 " * 40 + "\n\n- 항목 " + "내용 " * 40 + "\n"
            "\n> 인용 " + "말 " * 40 + "\n"
        )
        for line in render(text, width=50):
            self.assertLessEqual(md.display_width(line), 50)

    def test_paragraph_lines_joined(self) -> None:
        # Markdown 소프트 줄바꿈: 연속된 문단 줄은 한 문단으로 붙인다.
        self.assertEqual(
            render("첫 줄입니다.\n둘째 줄입니다.\n\n새 문단."),
            ["첫 줄입니다. 둘째 줄입니다.", "", "새 문단."],
        )

    def test_paragraph_split_by_list(self) -> None:
        lines = render("설명입니다.\n- 항목 하나")
        self.assertEqual(lines[0], "설명입니다.")
        self.assertIn("• 항목 하나", lines)

    def test_empty_and_plain_text(self) -> None:
        self.assertEqual(md.render(""), "")
        self.assertEqual(md.render("그냥 문장입니다"), "그냥 문장입니다")

    def test_render_lines_helper(self) -> None:
        self.assertEqual(md.render_lines("문장", width=40, color=False), ["문장"])

    def test_trailing_blank_lines_trimmed(self) -> None:
        self.assertFalse(md.render("# 제목\n\n", width=40).endswith("\n"))

    def test_markers_gone_from_output(self) -> None:
        # 색 없는 출력에는 Markdown 기호가 남지 않는다(로그 가독성).
        out = md.render("# 제목\n\n- **굵게** `코드`\n\n| a | b |\n|:--|:--|\n| 1 | 2 |\n", width=40, color=False)
        for marker in ("#", "**", "`", "|"):
            self.assertNotIn(marker, out)


if __name__ == "__main__":
    unittest.main()
