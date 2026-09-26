"""API 연결 재시도: 429·5xx·일시적 네트워크 오류에 지수 백오프로 다시 연다.

스트리밍은 **연결을 여는 단계**만 재시도한다. 본문을 받기 시작한 뒤 끊긴
경우는 부분 응답을 보존해야 하므로 호출자(stream_chat)가 따로 처리한다.
"""

from __future__ import annotations

import email.utils
import http.client
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any, Callable

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
RETRIES_ENV = "LLM_RETRIES"
DEFAULT_RETRIES = 4          # 첫 시도 뒤 추가로 다시 여는 횟수
BASE_DELAY = 1.0
MAX_DELAY = 30.0


def retry_count() -> int:
    raw = os.environ.get(RETRIES_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_RETRIES
    except ValueError:
        return DEFAULT_RETRIES


def retry_after_seconds(value: str | None) -> float | None:
    """`Retry-After` 헤더(초 또는 HTTP 날짜)를 초로 바꾼다."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """attempt(0부터)에 따른 대기 시간. 서버가 알려준 값이 있으면 따른다."""
    if retry_after is not None:
        return min(retry_after, MAX_DELAY * 2)
    delay = min(MAX_DELAY, BASE_DELAY * (2 ** attempt))
    return delay * (0.75 + random.random() * 0.5)  # 동시 재시도 분산(jitter)


def urlopen_with_retry(
    req: urllib.request.Request,
    timeout: float,
    on_retry: Callable[[int, int, str, float], None] | None = None,
    retries: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    opener: Callable[..., Any] | None = None,
) -> Any:
    """urlopen 을 재시도하며 연다. 재시도할 수 없는 오류는 그대로 올린다.

    on_retry(시도 번호, 전체 재시도 수, 이유, 대기 초)로 진행 상황을 알린다.
    """
    total = retry_count() if retries is None else retries
    open_url = opener or urllib.request.urlopen  # 호출 시점에 찾는다(테스트 mock 반영)
    attempt = 0
    while True:
        try:
            return open_url(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt >= total:
                raise
            reason = f"HTTP {exc.code}"
            delay = backoff_delay(attempt, retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None))
            try:
                exc.read()  # 연결을 비워 재사용 가능하게 한다
            except OSError:
                pass
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.RemoteDisconnected) as exc:
            if attempt >= total:
                raise
            reason = f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"
            delay = backoff_delay(attempt)
        attempt += 1
        if on_retry is not None:
            on_retry(attempt, total, reason, delay)
        sleep(delay)
