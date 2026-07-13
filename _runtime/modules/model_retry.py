import time
from collections.abc import Callable
from typing import Any


TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAY_SEC = 5.0
RETRY_STATUS_TEXT = "일시적 오류 · 5초 후 1회 다시 시도합니다"


def post_with_transient_retry(
    requests_module: Any,
    url: str,
    *,
    on_retry: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    **request_kwargs: Any,
) -> Any:
    """POST once, retrying exactly once only for the shared transient policy."""
    sleep = sleep or time.sleep
    for attempt in range(2):
        try:
            response = requests_module.post(url, **request_kwargs)
        except (requests_module.exceptions.Timeout, requests_module.exceptions.ConnectionError):
            if attempt == 0:
                if on_retry:
                    on_retry(RETRY_STATUS_TEXT)
                sleep(RETRY_DELAY_SEC)
                continue
            raise

        if response.status_code in TRANSIENT_HTTP_STATUSES and attempt == 0:
            if on_retry:
                on_retry(RETRY_STATUS_TEXT)
            sleep(RETRY_DELAY_SEC)
            continue
        return response

    raise RuntimeError("unreachable retry state")
