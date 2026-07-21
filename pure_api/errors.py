from __future__ import annotations

import time
from typing import Callable

CancelFn = Callable[[], bool]


class FastRegistrationCancelled(RuntimeError):
    """The shared cancellation guard stopped fast registration."""


class FastRegistrationAmbiguous(RuntimeError):
    """Account creation may have succeeded, so automatic fallback is unsafe."""


def raise_if_cancelled(cancel_callback: CancelFn | None) -> None:
    if cancel_callback and cancel_callback():
        raise FastRegistrationCancelled("fast registration cancelled")


def sleep_with_cancel(seconds: float, cancel_callback: CancelFn | None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))
