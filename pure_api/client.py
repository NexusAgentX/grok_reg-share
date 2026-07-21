"""AuthManagement gRPC-web + Next.js server-action client."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from . import pb
from .errors import CancelFn, raise_if_cancelled
from .session import (
    ACCOUNTS_ORIGIN,
    RPC_CREATE,
    RPC_VALIDATE_PW,
    RPC_VERIFY,
    SIGNUP_URL,
    cookie_map,
    proxies_dict,
)
from .tokens import discover_next_action

LogFn = Callable[[str], None]


class AuthClient:
    def __init__(
        self,
        session,
        proxy: str = "",
        log: LogFn | None = None,
        cancel_callback: CancelFn | None = None,
        bootstrap_html: str = "",
    ):
        self.session = session
        self.proxy = proxy
        self.log = log or (lambda _m: None)
        self.next_action = ""
        self.router_tree = ""
        self.signup_url = SIGNUP_URL
        self.cancel_callback = cancel_callback
        self.bootstrap_html = bootstrap_html

    def _grpc_headers(self) -> dict[str, str]:
        return {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "accept": "*/*",
            "origin": ACCOUNTS_ORIGIN,
            "referer": self.signup_url,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }

    def _rpc(self, url: str, body: bytes) -> dict[str, Any]:
        raise_if_cancelled(self.cancel_callback)
        r = self.session.post(
            url,
            data=body,
            headers=self._grpc_headers(),
            proxies=proxies_dict(self.proxy),
            timeout=30,
        )
        raise_if_cancelled(self.cancel_callback)
        if not 200 <= int(r.status_code) < 300:
            raise RuntimeError(
                f"RPC {url.rsplit('/', 1)[-1]} returned HTTP {r.status_code}"
            )
        parsed = pb.parse_response(r.content)
        parsed["http_status"] = r.status_code
        if parsed.get("grpc_status") not in (0, None):
            self.log(f"[rpc] {url.rsplit('/',1)[-1]} grpc_status={parsed.get('grpc_status')} trailers={parsed.get('trailers')}")
        return parsed

    def bootstrap(self, html: str = "") -> None:
        action, tree = discover_next_action(
            self.session,
            self.proxy,
            html=html,
            cancel_callback=self.cancel_callback,
        )
        if not action:
            raise RuntimeError("live create-user Next-Action was not found")
        self.next_action = action
        self.router_tree = tree
        self.log(f"[rpc] next-action={self.next_action}")

    def create_email_code(self, email: str, castle_token: str = "") -> dict[str, Any]:
        body = pb.create_email_body(email, castle_token)
        res = self._rpc(RPC_CREATE, body)
        self.log(f"[rpc] CreateEmailValidationCode status={res.get('grpc_status')}")
        return res

    def verify_email_code(self, email: str, code: str) -> dict[str, Any]:
        body = pb.verify_email_body(email, code)
        res = self._rpc(RPC_VERIFY, body)
        self.log(f"[rpc] VerifyEmailValidationCode status={res.get('grpc_status')}")
        return res

    def validate_password(self, email: str, password: str) -> dict[str, Any]:
        body = pb.validate_password_body(email, password)
        res = self._rpc(RPC_VALIDATE_PW, body)
        self.log(f"[rpc] ValidatePassword status={res.get('grpc_status')}")
        return res

    def create_user(
        self,
        *,
        email: str,
        code: str,
        password: str,
        given_name: str,
        family_name: str,
        turnstile_token: str,
        castle_token: str,
        conversion_id: str | None = None,
        tos_version: int = 1,
        body_style: str = "args2",
    ) -> dict[str, Any]:
        if not self.next_action:
            self.bootstrap(html=self.bootstrap_html)
        # Live browser (2026-07-17 capture) posts:
        # [ {emailValidationCode, createUserAndSessionRequest, turnstileToken,
        #    conversionId, castleRequestToken},
        #   {client:"$T", meta:"$undefined", mutationKey:"$undefined"} ]
        arg0 = {
            "emailValidationCode": str(code).replace("-", "").strip(),
            "createUserAndSessionRequest": {
                "email": email,
                "givenName": given_name,
                "familyName": family_name,
                "clearTextPassword": password,
                "tosAcceptedVersion": tos_version,
            },
            "turnstileToken": turnstile_token,
            "conversionId": conversion_id or str(uuid.uuid4()),
            "castleRequestToken": castle_token,
        }
        if body_style == "raw":
            payload: Any = arg0
        elif body_style == "args1":
            payload = [arg0]
        else:  # args2 (captured live default)
            payload = [
                arg0,
                {
                    "client": "$T",
                    "meta": "$undefined",
                    "mutationKey": "$undefined",
                },
            ]
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "text/x-component",
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "next-action": self.next_action,
            "next-router-state-tree": self.router_tree,
            "origin": ACCOUNTS_ORIGIN,
            "referer": self.signup_url,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        raise_if_cancelled(self.cancel_callback)
        r = self.session.post(
            self.signup_url,
            data=body,
            headers=headers,
            proxies=proxies_dict(self.proxy),
            timeout=45,
        )
        raise_if_cancelled(self.cancel_callback)
        text = r.text or ""
        sso = self._extract_sso(r, text)
        # Detect full-page RSC fallback (action not executed)
        looks_like_page = text.startswith("2:") or '"$Sreact.fragment"' in text[:200]
        self.log(
            f"[rpc] create_user style={body_style} http={r.status_code} "
            f"sso={'yes' if sso else 'no'} body_len={len(text)} page_fallback={looks_like_page}"
        )
        return {
            "http_status": r.status_code,
            "sso": sso,
            "page_fallback": looks_like_page,
            "body_style": body_style,
        }

    def _extract_sso(self, response, text: str) -> str:
        # jar
        jar = cookie_map(self.session)
        for key in ("sso", "sso-rw"):
            if jar.get(key):
                return jar[key]
        # set-cookie header
        sc = response.headers.get("set-cookie") or response.headers.get("Set-Cookie") or ""
        if isinstance(sc, list):
            sc = "\n".join(sc)
        m = re.search(r"\b(sso|sso-rw)=([^;,\s]+)", sc)
        if m:
            return m.group(2)
        # body JWT-ish near sso
        m = re.search(r"sso[\"'=:\s]+(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", text)
        if m:
            return m.group(1)
        m = re.search(r"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", text)
        if m and "session" in text.lower():
            return m.group(1)
        # last resort: visit grok.com to materialize cookie
        try:
            raise_if_cancelled(self.cancel_callback)
            gr = self.session.get(
                "https://grok.com/",
                proxies=proxies_dict(self.proxy),
                timeout=20,
                allow_redirects=True,
                headers={
                    "referer": self.signup_url,
                    "sec-fetch-site": "cross-site",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-dest": "document",
                    "upgrade-insecure-requests": "1",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            raise_if_cancelled(self.cancel_callback)
            jar = cookie_map(self.session)
            for key in ("sso", "sso-rw"):
                if jar.get(key):
                    return jar[key]
            sc = gr.headers.get("set-cookie") or ""
            m = re.search(r"\b(sso|sso-rw)=([^;,\s]+)", sc)
            if m:
                return m.group(2)
        except Exception as exc:
            self.log(f"[rpc] grok.com sso fallback failed: {exc}")
        return ""
