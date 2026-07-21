"""Temp-mail helpers for pure-api (MoeMail first; falls back to project providers)."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from curl_cffi import requests

from .errors import CancelFn, raise_if_cancelled, sleep_with_cancel
from .session import HTTP_IMPERSONATE, normalize_proxy, proxies_dict


LogFn = Callable[[str], None]


def _headers(config: dict[str, Any]) -> dict[str, str]:
    api_key = str(config.get("moemail_api_key") or "").strip()
    cookie = str(config.get("moemail_cookie") or "").strip()
    if api_key:
        return {"X-API-Key": api_key, "Content-Type": "application/json"}
    if cookie:
        return {"Cookie": cookie, "Content-Type": "application/json"}
    raise RuntimeError("MoeMail 需要 moemail_api_key 或 moemail_cookie")



def create_mailbox(
    config: dict[str, Any],
    log: LogFn | None = None,
    cancel_callback: CancelFn | None = None,
) -> tuple[str, str]:
    """Create one MoeMail address without deleting pre-existing mailboxes."""
    raise_if_cancelled(cancel_callback)
    base = str(config.get("moemail_api_base") or "https://moemail.app").rstrip("/")
    domain = str(config.get("moemail_domain") or "moemail.app").strip() or "moemail.app"
    expiry = int(config.get("moemail_expiry_time") or 3600000)
    proxy = normalize_proxy(config.get("proxy") or "")
    payload = {"name": "", "expiryTime": expiry, "domain": domain}

    r = requests.post(
        f"{base}/api/emails/generate",
        json=payload,
        headers=_headers(config),
        proxies=proxies_dict(proxy),
        timeout=30,
        impersonate=HTTP_IMPERSONATE,
    )
    raise_if_cancelled(cancel_callback)
    if r.status_code == 403:
        raise RuntimeError("MoeMail 邮箱数量已达上限；快速模式不会自动删除已有邮箱")
    r.raise_for_status()
    data = r.json()
    email = data.get("email") or data.get("address")
    email_id = data.get("id") or data.get("emailId")
    if not email or not email_id:
        raise RuntimeError(f"MoeMail create failed: {data}")
    if log:
        log(f"[mail] created {email}")
    return str(email), str(email_id)


def extract_code(text: str, subject: str = "") -> str:
    if subject:
        m = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.I)
        if m:
            return m.group(1).upper()
        m = re.search(r"([A-Z0-9]{3}-[A-Z0-9]{3})", subject, re.I)
        if m:
            return m.group(1).upper()
    m = re.search(r"([A-Z0-9]{3}-[A-Z0-9]{3})", text or "", re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(\d{6})\b", text or "")
    return m.group(1) if m else ""


def poll_code(
    config: dict[str, Any],
    email: str,
    email_id: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 0.5,
    log: LogFn | None = None,
    cancel_callback: CancelFn | None = None,
) -> str:
    base = str(config.get("moemail_api_base") or "https://moemail.app").rstrip("/")
    proxy = normalize_proxy(config.get("proxy") or "")
    headers = _headers(config)
    deadline = time.time() + timeout
    seen: set[str] = set()
    target = email.strip().lower()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        r = requests.get(
            f"{base}/api/emails/{email_id}",
            headers=headers,
            proxies=proxies_dict(proxy),
            timeout=20,
            impersonate=HTTP_IMPERSONATE,
        )
        if r.status_code in (401, 403):
            raise RuntimeError(f"MoeMail authentication failed: HTTP {r.status_code}")
        if r.status_code == 404:
            raise RuntimeError("MoeMail mailbox disappeared while waiting for code")
        if not 200 <= int(r.status_code) < 300:
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        try:
            payload = r.json()
        except Exception:
            payload = {}
        messages = payload.get("messages")
        if not isinstance(messages, list):
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            mid = str(message.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            recipient = str(message.get("to_address") or "").strip().lower()
            if recipient and recipient != target:
                continue
            subject = str(message.get("subject") or "")
            code = extract_code("", subject)
            if code:
                if log:
                    log(f"[mail] code from subject: {code}")
                return code
            dr = requests.get(
                f"{base}/api/emails/{email_id}/{mid}",
                headers=headers,
                proxies=proxies_dict(proxy),
                timeout=20,
                impersonate=HTTP_IMPERSONATE,
            )
            if dr.status_code in (401, 403):
                raise RuntimeError(
                    f"MoeMail authentication failed: HTTP {dr.status_code}"
                )
            if not 200 <= int(dr.status_code) < 300:
                continue
            try:
                detail = dr.json()
            except Exception:
                detail = {}
            detail = detail.get("message", detail) if isinstance(detail, dict) else {}
            parts = [subject]
            if isinstance(detail, dict):
                for field in ("content", "text", "body", "raw", "subject"):
                    v = detail.get(field)
                    if isinstance(v, str) and v.strip():
                        parts.append(v)
                html = detail.get("html")
                if isinstance(html, str):
                    parts.append(re.sub(r"<[^>]+>", " ", html))
            code = extract_code("\n".join(parts), subject)
            if code:
                if log:
                    log(f"[mail] code from body: {code}")
                return code
            seen.add(mid)
        sleep_with_cancel(poll_interval, cancel_callback)
    raise TimeoutError(f"MoeMail timeout waiting code for {email}")


def delete_mailbox(
    config: dict[str, Any],
    email_id: str,
    *,
    log: LogFn | None = None,
) -> bool:
    """Delete one MoeMail address by id. Returns True on HTTP success."""
    if not email_id:
        return False
    base = str(config.get("moemail_api_base") or "https://moemail.app").rstrip("/")
    proxy = normalize_proxy(config.get("proxy") or "")
    try:
        r = requests.delete(
            f"{base}/api/emails/{email_id}",
            headers=_headers(config),
            proxies=proxies_dict(proxy),
            timeout=15,
            impersonate=HTTP_IMPERSONATE,
        )
        ok = 200 <= int(r.status_code) < 300 or int(r.status_code) == 404
        if log:
            log(f"[mail] delete {email_id[:8]}… status={r.status_code}")
        return ok
    except Exception as exc:
        if log:
            log(f"[mail] delete fail {email_id[:8]}…: {exc}")
        return False
