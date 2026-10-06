#!/usr/bin/env python3
"""터미널용 경량 Markdown 렌더러(표준 라이브러리만).

`glow` 같은 외부 도구 없이 최종 답변·규칙 문서를 읽기 좋게 보여주기 위한
순수 함수 모음이다. 입력은 Markdown 원문, 출력은 ANSI 색이 섞일 수 있는
문자열(줄바꿈 포함)이며 터미널·파일에 직접 쓰지 않는다(`agent.py` 가 출력).

지원하는 것:
- ATX 헤딩(`#`~`######`)과 `===` setext 헤딩
- 순서/비순서/체크 목록(들여쓰기 단계 유지, 긴 항목은 행잉 인덴트로 줄바꿈)
- 펜스 코드블록(언어 라벨 + 좌측 세로줄)
- 인용(`>`), 수평선(`---`), 파이프 표(열 정렬)
- 인라인 코드·굵게·기울임·취소선·링크

한글/전각 문자는 `display_width`(동아시아 wide=2)로 폭을 세므로 표·목록이
어긋나지 않는다. `color=False` 면 색 없이 구조만 정리하고 Markdown 기호는
지운다(로그·리다이렉트용).
"""

from __future__ import annotations

import re
import unicodedata

ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# --- 표시 폭 계산(agent.py 의 display_width 와 같은 규칙) -------------------


def char_width(ch: str) -> int:
    """터미널 표시 폭: 동아시아 wide/전각 문자는 2, 그 외는 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def display_width(text: str) -> int:
    """색 코드를 뺀 표시 폭."""
    return sum(char_width(ch) for ch in ANSI_RE.sub("", text))


def pad_right(text: str, width: int) -> str:
    """표시 폭 기준으로 오른쪽을 공백으로 채운다(색 코드는 폭을 차지 않음)."""
    return text + " " * max(0, width - display_width(text))


def clip(text: str, width: int, ellipsis: str = "…") -> str:
    """표시 폭 기준으로 자른다(한글 2칸 고려, 넘치면 말줄임)."""
    if display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    limit = max(0, width - display_width(ellipsis))
    idx = 0
    while idx < len(text):
        seq = ANSI_RE.match(text, idx)
        if seq:
            out.append(seq.group())
            idx = seq.end()
            continue
        ch = text[idx]
        if used + char_width(ch) > limit:
            break
        out.append(ch)
        used += char_width(ch)
        idx += 1
    if ANSI_RE.search(text):
        out.append("\033[0m")
    return "".join(out) + ellipsis


# --- 색 팔레트 --------------------------------------------------------------


class Palette:
    """ANSI 색 모음. `color=False` 면 모두 빈 문자열이라 색 없이 동작한다."""

    __slots__ = (
        "color",
        "reset",
        "bold",
        "dim",
        "italic",
        "strike",
        "code",
        "h1",
        "h2",
        "h3",
        "link",
        "bullet",
        "bar",
    )

    def __init__(self, color: bool = True):
        self.color = bool(color)
        on = self.color
        self.reset = "\033[0m" if on else ""
        self.bold = "\033[1m" if on else ""
        self.dim = "\033[2m" if on else ""
        self.italic = "\033[3m" if on else ""
        self.strike = "\033[9m" if on else ""
        self.code = "\033[36m" if on else ""
        self.h1 = "\033[1;36m" if on else ""
        self.h2 = "\033[1m" if on else ""
        self.h3 = "\033[1;2m" if on else ""
        self.link = "\033[4;36m" if on else ""
        self.bullet = "\033[36m" if on else ""
        self.bar = "\033[2m" if on else ""


EMPTY = Palette(False)  # 폭 계산용(스타일 없음)


def _style(text: str, code: str, pal: Palette) -> str:
    if not text or not code:
        return text
    # 안쪽에서 색이 끝나도 바깥 강조(굵게 등)가 유지되도록 다시 켠다.
    if pal.reset:
        text = text.replace(pal.reset, pal.reset + code)
    return f"{code}{text}{pal.reset}"


# --- 인라인 ----------------------------------------------------------------

INLINE_RE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|~~(?P<strike>.+?)~~"
    r"|\[(?P<ltext>[^\]\n]*)\]\((?P<lurl>[^)\s]*)\)"
    r"|(?<![A-Za-z0-9*])\*(?P<ital>[^\s*](?:[^*\n]*[^\s*])?)\*(?![A-Za-z0-9*])"
    r"|(?<![A-Za-z0-9_])_(?P<ital2>[^\s_](?:[^_\n]*[^\s_])?)_(?![A-Za-z0-9_])"
)


def inline(text: str, pal: Palette | None = None) -> str:
    """인라인 Markdown 을 ANSI 로 바꾼다(색 없는 팔레트면 기호만 지운다)."""
    pal = EMPTY if pal is None else pal
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
        pos = m.end()
        if m.group("code") is not None:
            out.append(_style(m.group("code"), pal.code, pal))
        elif m.group("bold") is not None:
            out.append(_style(inline(m.group("bold"), pal), pal.bold, pal))
        elif m.group("strike") is not None:
            out.append(_style(inline(m.group("strike"), pal), pal.strike, pal))
        elif m.group("ltext") is not None or m.group("lurl") is not None:
            label = m.group("ltext") or ""
            url = m.group("lurl") or ""
            if not label or label == url:
                out.append(_style(url, pal.link, pal))
            else:
                out.append(_style(label, pal.link, pal))
                if url:
                    out.append(f" ({_style(url, pal.dim, pal)})")
        else:
            body = m.group("ital") if m.group("ital") is not None else m.group("ital2")
            out.append(_style(inline(body or "", pal), pal.italic, pal))
    out.append(text[pos:])
    return "".join(out)


def plain(text: str) -> str:
    """인라인 기호를 지운 순수 텍스트(폭 계산·색 없는 출력용)."""
    return inline(text, EMPTY)


# --- 줄바꿈 ----------------------------------------------------------------

_TOKEN_RE = re.compile(r"`[^`\n]*`|\S+")


def wrap_md(text: str, width: int) -> list[str]:
    """Markdown 한 줄을 표시 폭에 맞춰 공백 기준으로 나눈다.

    백틱 코드 스팬은 한 덩어리로 취급해 안의 공백에서 끊기지 않게 한다.
    (공백 없는 긴 토큰은 그대로 넘긴다.)
    """
    width = max(8, int(width))
    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        return [""]
    lines: list[str] = []
    cur = ""
    cur_w = 0
    for tok in tokens:
        w = display_width(plain(tok))
        if not cur:
            cur, cur_w = tok, w
            continue
        if cur_w + 1 + w > width:
            lines.append(cur)
            cur, cur_w = tok, w
        else:
            cur = f"{cur} {tok}"
            cur_w += 1 + w
    lines.append(cur)
    return lines


def _indented(lines: list[str], indent: str) -> list[str]:
    return [indent + line for line in lines]


# --- 블록 ------------------------------------------------------------------

HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.*?)\s*#*\s*$")
SETEXT_RE = re.compile(r"^[ \t]*(?P<char>=+)[ \t]*$")
LIST_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d{1,3}[.)])[ \t]+(?P<body>.*)$")
TASK_RE = re.compile(r"^\[(?P<state>[ xX])\][ \t]+(?P<body>.*)$")
QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?(?P<body>.*)$")
HR_RE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[\w.+#-]*)[ \t]*$")
TABLE_SEP_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$"
)


def _block_start(src: list[str], idx: int) -> bool:
    """`idx` 줄에서 새 블록이 시작되는지(문단 이어붙이기를 멈출지) 판단한다."""
    line = src[idx]
    if FENCE_RE.match(line) or HR_RE.match(line) or HEADING_RE.match(line):
        return True
    if QUOTE_RE.match(line) or LIST_RE.match(line) or SETEXT_RE.match(line):
        return True
    if (
        "|" in line
        and idx + 1 < len(src)
        and "|" in src[idx + 1]
        and TABLE_SEP_RE.match(src[idx + 1])
    ):
        return True
    return False


def _rule_line(pal: Palette, width: int) -> str:
    return pal.dim + "─" * max(8, width) + pal.reset


def _code_block(body: list[str], info: str, pal: Palette, width: int) -> list[str]:
    """펜스 코드블록: 언어 라벨 + 좌측 세로줄(긴 줄은 폭에 맞춰 자른다)."""
    prefix = f"{pal.bar}│{pal.reset} "
    avail = max(8, width - 2)
    out: list[str] = []
    if info:
        out.append(f"{pal.bar}┌─ {info}{pal.reset}")
    else:
        out.append(f"{pal.bar}┌──{pal.reset}")
    for line in body:
        out.append(prefix + clip(line, avail))
    out.append(f"{pal.bar}└──{pal.reset}")
    return out


def _heading(level: int, title: str, pal: Palette, width: int) -> list[str]:
    styled = inline(title, pal)
    if level == 1:
        rule_w = min(width, max(12, display_width(title) + 2))
        return [_style(styled, pal.h1, pal), _rule_line(pal, rule_w)]
    if level == 2:
        return [_style(styled, pal.h2, pal), _rule_line(pal, min(width, max(12, display_width(title) + 2)))]
    return [_style(("▎ " if level == 3 else "  ") + styled, pal.h3, pal)]


def _list_item(indent: str, marker: str, body: str, pal: Palette, width: int) -> list[str]:
    """목록 한 항목: 기호를 정리하고 긴 본문은 행잉 인덴트로 접는다."""
    depth = len(indent.replace("\t", "  ")) // 2
    pad = "  " * max(0, depth)
    task = TASK_RE.match(body)
    if task:
        box = "☑" if task.group("state").lower() == "x" else "☐"
        bullet = f"{pal.bullet}{box}{pal.reset}"
        body = task.group("body")
    elif marker[0].isdigit():
        num = marker.rstrip(".)")
        bullet = f"{pal.bullet}{num}.{pal.reset}"
    else:
        bullet = f"{pal.bullet}•{pal.reset}"
    prefix = f"{pad}{bullet} "
    hang = " " * display_width(prefix)
    avail = max(8, width - display_width(prefix))
    wrapped = wrap_md(body, avail)
    lines = [prefix + inline(wrapped[0], pal)]
    lines += [hang + inline(part, pal) for part in wrapped[1:]]
    return lines


def _split_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _aligns(sep: str, count: int) -> list[str]:
    aligns: list[str] = []
    for cell in _split_row(sep):
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        else:
            aligns.append("left")
    while len(aligns) < count:
        aligns.append("left")
    return aligns[:count]


def _cell(text: str, width: int, align: str, pal: Palette) -> str:
    styled = inline(text, pal)
    space = max(0, width - display_width(plain(text)))
    if align == "right":
        return " " * space + styled
    if align == "center":
        left = space // 2
        return " " * left + styled + " " * (space - left)
    return styled + " " * space


def _table(rows: list[list[str]], aligns: list[str], pal: Palette) -> list[str]:
    cols = max(len(r) for r in rows)
    widths = [3] * cols
    for row in rows:
        for idx, cell in enumerate(row[:cols]):
            widths[idx] = max(widths[idx], display_width(plain(cell)))
    out: list[str] = []
    for idx, row in enumerate(rows):
        cells = [
            _cell(row[c] if c < len(row) else "", widths[c], aligns[c], pal)
            for c in range(cols)
        ]
        line = "  ".join(cells).rstrip()
        if idx == 0:
            out.append(_style(line, pal.bold, pal) if pal.bold else line)
            out.append(pal.dim + "  ".join("─" * w for w in widths) + pal.reset)
        else:
            out.append(line)
    return out


def render(text: str, width: int = 80, color: bool = True) -> str:
    """Markdown 원문을 터미널용 문자열로 렌더링한다(줄바꿈 포함)."""
    pal = Palette(color)
    width = max(20, int(width))
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    total = len(src)

    def blank() -> None:
        if out and out[-1] != "":
            out.append("")

    while i < total:
        line = src[i]

        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group("fence")
            close = re.compile(r"^[ \t]*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*$")
            body: list[str] = []
            i += 1
            while i < total and not close.match(src[i]):
                body.append(src[i])
                i += 1
            i += 1  # 닫는 펜스(없으면 끝)
            blank()
            out.extend(_code_block(body, fence.group("info") or "", pal, width))
            out.append("")
            continue

        if not line.strip():
            blank()
            i += 1
            continue

        if HR_RE.match(line):
            blank()
            out.append(_rule_line(pal, min(width, 60)))
            i += 1
            continue

        head = HEADING_RE.match(line)
        if head:
            blank()
            out.extend(_heading(len(head.group(1)), head.group("title"), pal, width))
            i += 1
            continue

        setext = SETEXT_RE.match(src[i + 1]) if i + 1 < total else None
        if setext and line.strip():
            blank()
            out.extend(_heading(1, line.strip(), pal, width))
            i += 2
            continue

        # 파이프 표: 다음 줄이 구분선이면 표로 본다.
        if "|" in line and i + 1 < total and "|" in src[i + 1] and TABLE_SEP_RE.match(src[i + 1]):
            sep = src[i + 1]
            raw_rows = [line]
            i += 2
            while i < total and src[i].strip() and "|" in src[i] and not FENCE_RE.match(src[i]):
                raw_rows.append(src[i])
                i += 1
            rows = [_split_row(row) for row in raw_rows]
            aligns = _aligns(sep, max(len(r) for r in rows))
            blank()
            out.extend(_table(rows, aligns, pal))
            out.append("")
            continue

        if QUOTE_RE.match(line):
            body: list[str] = []
            while i < total and QUOTE_RE.match(src[i]):
                body.append(QUOTE_RE.match(src[i]).group("body"))
                i += 1
            blank()
            inner_width = max(8, width - 2)
            for idx, part in enumerate(body):
                for j, piece in enumerate(wrap_md(part, inner_width)):
                    mark = "│ " if (idx == 0 or j == 0) else "  "
                    out.append(f"{pal.bar}{mark}{pal.reset}{_style(inline(piece, pal), pal.italic, pal)}")
            continue

        item = LIST_RE.match(line)
        if item:
            blank()
            while i < total:
                m2 = LIST_RE.match(src[i])
                if not m2:
                    break
                # 항목 본문이 다음 줄로 이어지면 붙인다(들여쓰기 2칸 이상).
                extra: list[str] = []
                j = i + 1
                while j < total and src[j].strip() and not LIST_RE.match(src[j]) and (
                    src[j].startswith("  ") or src[j].startswith("\t")
                ):
                    extra.append(src[j].strip())
                    j += 1
                body_text = " ".join([m2.group("body")] + extra)
                out.extend(
                    _list_item(m2.group("indent"), m2.group("marker"), body_text, pal, width)
                )
                i = j
            continue

        # 문단: 빈 줄이 나오거나 새 블록이 시작될 때까지 이어 붙인다(Markdown
        # 소프트 줄바꿈). 한 줄씩 띄우면 원문보다 오히려 읽기 나빠진다.
        blank()
        parts: list[str] = []
        while i < total and src[i].strip() and not _block_start(src, i):
            parts.append(src[i].strip())
            i += 1
        if not parts:  # 안전장치: 어떤 이유로든 못 모으면 한 줄만 소비한다.
            parts = [line.strip()]
            i += 1
        for piece in wrap_md(" ".join(parts), width):
            out.append(inline(piece, pal))

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def render_lines(text: str, width: int = 80, color: bool = True) -> list[str]:
    """`render` 와 같지만 줄 목록으로 돌려준다(테스트·부분 출력용)."""
    return render(text, width=width, color=color).split("\n")
