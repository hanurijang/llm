"""대화 메시지 정리: 중단(Ctrl+C) 뒤 마무리 · 오래된 도구 결과 비우기."""

from __future__ import annotations

from typing import Any

INTERRUPTED_TOOL = "ERROR: 사용자가 실행을 중단했습니다(Ctrl+C). 결과가 없습니다."
INTERRUPTED_REPLY = "[사용자가 응답 생성을 중단했습니다]"
CLEARED_PREFIX = "[이전 도구 결과 비움"

# 오래된 도구 결과 비우기: 도구 결과 합계가 TRIGGER 를 넘을 때 **한꺼번에**
# 최근 KEEP 개를 뺀 긴 결과를 비운다. 조금씩 비우면 매 단계 프롬프트 앞부분이
# 바뀌어 캐시 적중이 깨지므로, 한 번에 정리하고 저장해 접두부를 고정한다.
CLEAR_TRIGGER_CHARS = 60_000
CLEAR_KEEP_RECENT = 6
CLEAR_MIN_CHARS = 1_500


def finalize_interrupted(messages: list[dict[str, Any]]) -> str:
    """중단된 턴을 API 규격에 맞게 닫는다. 반환: 무엇을 했는지 한 줄.

    - 마지막 assistant 의 tool_calls 중 결과가 없는 호출에 '중단됨' 결과를 채운다.
    - 응답 생성 도중이었다면(마지막이 user) 중단 표시 assistant 메시지를 남긴다.
    """
    if not messages:
        return ""
    # 마지막 assistant(tool_calls) 위치를 찾는다.
    idx = len(messages) - 1
    while idx >= 0 and messages[idx].get("role") == "tool":
        idx -= 1
    last = messages[idx] if idx >= 0 else None
    if last is not None and last.get("role") == "assistant" and last.get("tool_calls"):
        answered = {m.get("tool_call_id") for m in messages[idx + 1:] if m.get("role") == "tool"}
        missing = 0
        for tc in last["tool_calls"]:
            call_id = tc.get("id") or ""
            if call_id not in answered:
                name = (tc.get("function") or {}).get("name") or ""
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": INTERRUPTED_TOOL})
                missing += 1
        return f"실행하지 못한 도구 호출 {missing}개를 '중단됨'으로 기록" if missing else "도구 실행 후 중단"
    if messages[-1].get("role") == "user":
        messages.append({"role": "assistant", "content": INTERRUPTED_REPLY})
        return "응답 생성 중 중단"
    return ""


def tool_result_chars(messages: list[dict[str, Any]]) -> int:
    return sum(
        len(m.get("content") or "")
        for m in messages
        if m.get("role") == "tool" and not str(m.get("content") or "").startswith(CLEARED_PREFIX)
    )


def clear_old_tool_results(
    messages: list[dict[str, Any]],
    trigger: int = CLEAR_TRIGGER_CHARS,
    keep: int = CLEAR_KEEP_RECENT,
    min_chars: int = CLEAR_MIN_CHARS,
) -> tuple[list[dict[str, Any]], int, int]:
    """반환: (메시지, 비운 개수, 줄인 문자 수). 기준 미달이면 그대로."""
    if tool_result_chars(messages) <= trigger:
        return messages, 0, 0
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protected = set(tool_idx[-keep:]) if keep > 0 else set()
    # tool_call_id → 호출 이름/인자(비운 자리에 무엇이었는지 남긴다)
    calls: dict[str, str] = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls[tc.get("id") or ""] = f"{fn.get('name', '?')} {str(fn.get('arguments', ''))[:120]}"
    out: list[dict[str, Any]] = []
    cleared = saved = 0
    for i, m in enumerate(messages):
        content = str(m.get("content") or "")
        if (
            m.get("role") == "tool"
            and i not in protected
            and len(content) >= min_chars
            and not content.startswith(CLEARED_PREFIX)
        ):
            what = calls.get(m.get("tool_call_id") or "", m.get("name") or "")
            stub = f"{CLEARED_PREFIX}: {len(content):,}자 · {what}. 필요하면 도구를 다시 실행하세요]"
            out.append({**m, "content": stub})
            cleared += 1
            saved += len(content) - len(stub)
        else:
            out.append(m)
    return out, cleared, saved
