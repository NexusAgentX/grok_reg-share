"""HTTP session helpers for pure-api registration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests

ROOT = Path(__file__).resolve().parent.parent
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
ACCOUNTS_ORIGIN = "https://accounts.x.ai"
RPC_CREATE = f"{ACCOUNTS_ORIGIN}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
RPC_VERIFY = f"{ACCOUNTS_ORIGIN}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
RPC_VALIDATE_PW = f"{ACCOUNTS_ORIGIN}/auth_mgmt.AuthManagement/ValidatePassword"
HTTP_IMPERSONATE = "chrome146"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    data_dir = Path(os.environ.get("GROK_REG_DATA_DIR", ROOT)).expanduser().resolve()
    cfg_path = Path(path or os.environ.get("GROK_REGISTER_CONFIG_PATH") or (data_dir / "config.json"))
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        data = {k: v for k, v in raw.items() if not str(k).startswith("//")}
    return data


def normalize_proxy(proxy: str | None) -> str:
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return proxy


def proxies_dict(proxy: str | None) -> dict[str, str] | None:
    p = normalize_proxy(proxy)
    if not p:
        return None
    return {"http": p, "https": p}


def make_session(config: dict[str, Any] | None = None) -> tuple[requests.Session, str]:
    cfg = config or load_config()
    proxy = normalize_proxy(cfg.get("proxy") or "")
    ua = str(cfg.get("user_agent") or DEFAULT_UA)
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", ua)
    browser_major = match.group(1) if match else "146"
    session = requests.Session(impersonate=HTTP_IMPERSONATE)
    session.headers.update(
        {
            "user-agent": ua,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "sec-ch-ua": (
                f'"Chromium";v="{browser_major}", '
                f'"Google Chrome";v="{browser_major}", "Not/A)Brand";v="99"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    )
    return session, proxy


def warm_signup(session: requests.Session, proxy: str = "") -> str:
    r = session.get(
        SIGNUP_URL,
        proxies=proxies_dict(proxy),
        timeout=30,
        allow_redirects=True,
    )
    r.raise_for_status()
    return r.text


def cookie_map(session: requests.Session) -> dict[str, str]:
    try:
        return {c.name: c.value for c in session.cookies}
    except Exception:
        try:
            return dict(session.cookies)
        except Exception:
            return {}


def apply_browser_cookies(session: requests.Session, cookies: list[dict] | dict) -> None:
    items: list[tuple[str, str, str]] = []
    if isinstance(cookies, dict):
        for k, v in cookies.items():
            items.append((str(k), str(v), ".x.ai"))
    else:
        for c in cookies or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            value = str(c.get("value") or "").strip()
            domain = str(c.get("domain") or ".x.ai").strip() or ".x.ai"
            if name and value:
                items.append((name, value, domain))
    for name, value, domain in items:
        try:
            session.cookies.set(name, value, domain=domain)
        except Exception:
            try:
                session.cookies.set(name, value)
            except Exception:
                pass


def chromium_proxy_arg(proxy: str) -> str:
    p = normalize_proxy(proxy)
    if not p:
        return ""
    u = urlparse(p)
    host = u.hostname or ""
    if not host:
        return ""
    port = u.port or (443 if (u.scheme or "http") == "https" else 80)
    scheme = u.scheme or "http"
    return f"{scheme}://{host}:{port}"
