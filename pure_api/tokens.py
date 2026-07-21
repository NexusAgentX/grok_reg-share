"""Short-lived Chromium harvest for Castle + Turnstile tokens.

Protocol registration is HTTP; only anti-bot tokens need a live page.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .errors import CancelFn, raise_if_cancelled
from .session import (
    DEFAULT_UA,
    ROOT,
    SIGNUP_URL,
    chromium_proxy_arg,
    normalize_proxy,
)

def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _create_proxy_auth_extension(proxy: str, tmp: Path) -> Path | None:
    parsed = urlparse(normalize_proxy(proxy))
    if not parsed.username:
        return None
    username = unquote(parsed.username)
    password = unquote(parsed.password or "")
    host = parsed.hostname or ""
    extension_dir = tmp / "proxy-auth"
    extension_dir.mkdir(mode=0o700)
    os.chmod(extension_dir, 0o700)
    manifest = {
        "manifest_version": 3,
        "name": "Ephemeral Proxy Authentication",
        "version": "1.0.0",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
    }
    worker = f"""
const proxyHost = {json.dumps(host)};
const credentials = {{
  username: {json.dumps(username)},
  password: {json.dumps(password)}
}};
chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {{
    const challenger = details.challenger || {{}};
    if (!details.isProxy || (challenger.host && challenger.host !== proxyHost)) {{
      callback({{}});
      return;
    }}
    callback({{authCredentials: credentials}});
  }},
  {{urls: ["<all_urls>"]}},
  ["asyncBlocking"]
);
""".strip()
    _write_private(extension_dir / "manifest.json", json.dumps(manifest))
    _write_private(extension_dir / "background.js", worker)
    return extension_dir


def _create_browser(
    proxy: str = "",
    headless: bool = False,
    config: dict[str, Any] | None = None,
):
    from DrissionPage import Chromium, ChromiumOptions

    cfg = config or {}
    opts = ChromiumOptions()
    opts.auto_port()
    opts.set_timeouts(base=1)
    for flag in (
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--no-first-run",
    ):
        opts.set_argument(flag)
    if cfg.get("chromium_no_sandbox", False):
        opts.set_argument("--no-sandbox")
    opts.set_argument(f"--user-agent={str(cfg.get('user_agent') or DEFAULT_UA)}")
    data_dir = Path(os.environ.get("GROK_REG_DATA_DIR", ROOT)).expanduser().resolve()
    tmp = data_dir / "browser-data" / f"pure-api-{os.getpid()}-{time.time_ns()}"
    tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(tmp, 0o700)
    try:
        if headless:
            try:
                opts.headless(True)
            except Exception:
                opts.set_argument("--headless=new")
        px = chromium_proxy_arg(proxy)
        if px:
            opts.set_argument(f"--proxy-server={px}")
            proxy_extension = _create_proxy_auth_extension(proxy, tmp)
            if proxy_extension is not None:
                opts.add_extension(str(proxy_extension))
        for candidate in (
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ):
            if os.path.isfile(candidate):
                opts.set_browser_path(candidate)
                break
        ext = ROOT / "turnstilePatch"
        if ext.is_dir():
            opts.add_extension(str(ext))
        profile_dir = tmp / "profile"
        profile_dir.mkdir(mode=0o700)
        opts.set_tmp_path(str(profile_dir))
        browser = Chromium(opts)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tab = browser.latest_tab
    try:
        browser._pure_api_tmp = str(tmp)  # type: ignore[attr-defined]
    except Exception:
        pass
    return browser, tab, tmp


def _install_hooks(page) -> None:
    # Castle is sent inside gRPC-web binary bodies; decode as latin1 and regex IBYIll...
    page.run_js(
        r"""
(() => {
  if (window.__pure_net_hooked) return true;
  window.__pure_net_hooked = true;
  window.__pure_castle = '';
  window.__pure_castles = [];
  window.__pure_turnstile = '';
  window.__pure_net = [];
  function bodyToText(body) {
    if (body == null) return '';
    if (typeof body === 'string') return body;
    if (body instanceof ArrayBuffer) return new TextDecoder('latin1').decode(new Uint8Array(body));
    if (ArrayBuffer.isView(body)) return new TextDecoder('latin1').decode(new Uint8Array(body.buffer, body.byteOffset, body.byteLength));
    try { return String(body); } catch (e) { return ''; }
  }
  function consider(body, url) {
    try {
      const text = bodyToText(body);
      const u = String(url || '');
      if (text) window.__pure_net.push({url: u.slice(0, 160), len: text.length});
      let tok = '';
      try {
        const j = JSON.parse(text);
        if (j && typeof j.castleRequestToken === 'string') tok = j.castleRequestToken;
        if (!tok && Array.isArray(j) && j[0] && typeof j[0].castleRequestToken === 'string') tok = j[0].castleRequestToken;
      } catch (e) {}
      if (!tok) {
        const m = text.match(/IBYIll[\x20-\x7e]{80,}/);
        if (m) tok = m[0];
      }
      if (!tok) {
        const m = text.match(/IBYIll[A-Za-z0-9+/=_\-]{80,}/);
        if (m) tok = m[0];
      }
      if (tok && tok.length >= 100) {
        window.__pure_castle = tok;
        window.__pure_castles.push(tok);
      }
    } catch (e) { window.__pure_hook_err = String(e); }
  }
  const ofetch = window.fetch;
  window.fetch = async function(input, init = {}) {
    try {
      const url = (typeof input === 'string') ? input : (input && input.url) || '';
      consider(init && init.body, url);
    } catch (e) {}
    return ofetch.apply(this, arguments);
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__pure_url = url;
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    try { consider(body, this.__pure_url); } catch (e) {}
    return send.apply(this, arguments);
  };
  return true;
})()
        """
    )


def _read_castle(page) -> str:
    try:
        tok = page.run_js(
            "return String(window.__pure_castle || (window.__pure_castles||[]).slice(-1)[0] || '')"
        )
        return str(tok or "").strip()
    except Exception:
        return ""


def _read_turnstile(page) -> str:
    try:
        tok = page.run_js(
            r"""
(() => {
  const a = String((document.querySelector('input[name="cf-turnstile-response"]')||{}).value||'').trim();
  if (a.length >= 80) return a;
  try { if (window.turnstile && turnstile.getResponse) {
    const b = String(turnstile.getResponse()||'').trim();
    if (b.length >= 80) return b;
  }} catch(e) {}
  return String(window.__pure_turnstile||'').trim();
})()
            """
        )
        return str(tok or "").strip()
    except Exception:
        return ""


def _click_email_signup(page) -> bool:
    try:
        return bool(
            page.run_js(
                r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const t = nodes.find(n => {
  const s = (n.innerText||n.textContent||'').replace(/\s+/g,'').toLowerCase();
  return s.includes('使用邮箱注册') || s.includes('email') || s.includes('signupwithemail') || s.includes('continuewithemail');
});
if (!t) return false;
t.click();
return true;
                """
            )
        )
    except Exception:
        return False


def _fill_email_and_continue(page, email: str) -> bool:
    try:
        return bool(
            page.run_js(
                r"""
const email = String(arguments[0]||'');
function vis(n){if(!n)return false;const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden')return false;const r=n.getBoundingClientRect();return r.width>0&&r.height>0}
const input = Array.from(document.querySelectorAll('input[type="email"],input[name="email"],input[autocomplete="email"],input[data-testid="email"]')).find(vis);
if(!input) return false;
const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
const tracker = input._valueTracker; if(tracker) tracker.setValue('');
if(setter) setter.call(input,email); else input.value=email;
input.dispatchEvent(new Event('input',{bubbles:true}));
input.dispatchEvent(new Event('change',{bubbles:true}));
const btn = Array.from(document.querySelectorAll('button[type="submit"],button')).find(n=>{
  if(!vis(n)||n.disabled) return false;
  const t=(n.innerText||'').replace(/\s+/g,'').toLowerCase();
  return t.includes('注册')||t.includes('继续')||t.includes('下一步')||t.includes('continue')||t.includes('next')||t.includes('sign');
});
if(btn) btn.click();
return true;
                """,
                email,
            )
        )
    except Exception:
        return False


def _export_cookies(page) -> list[dict]:
    try:
        cookies = page.cookies(all_domains=True, all_info=True) or []
        return [c for c in cookies if isinstance(c, dict)]
    except Exception:
        try:
            cookies = page.cookies() or []
            return [c for c in cookies if isinstance(c, dict)]
        except Exception:
            return []


# Captured live from accounts.x.ai sign-up createUser mutation (2026-07-17).
DEFAULT_ROUTER_TREE = (
    '["",{"children":["(app)",{"children":["(auth)",{"children":["sign-up",'
    '{"children":["__PAGE__",{},null,null,0]},null,null,0]},null,null,0]},'
    "null,null,0]},null,null,16]"
)


def discover_next_action(
    session,
    proxy: str = "",
    html: str = "",
    cancel_callback: CancelFn | None = None,
) -> tuple[str, str]:
    """Return (next_action_id, router_state_tree_urlencoded)."""
    import re
    from urllib.parse import quote

    from .session import proxies_dict

    if not html:
        raise_if_cancelled(cancel_callback)
        r = session.get(SIGNUP_URL, proxies=proxies_dict(proxy), timeout=30)
        raise_if_cancelled(cancel_callback)
        html = r.text

    # Prefer the known-good live tree; only override when scrape finds a fuller match.
    tree = DEFAULT_ROUTER_TREE
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S):
        seg = m.group(1)
        if "sign-up" in seg and "__PAGE__" in seg:
            try:
                unescaped = bytes(seg, "utf-8").decode("unicode_escape")
            except Exception:
                unescaped = seg
            idx = unescaped.find('["",{"children"')
            if idx >= 0:
                candidate = unescaped[idx : idx + 400]
                if candidate.count("[") >= 8 and "sign-up" in candidate:
                    tree = candidate
                    break
    tree_enc = quote(tree, safe="")

    action = ""
    chunks = dict.fromkeys(re.findall(r"/_next/static/chunks/[^\"']+\.js", html))
    for path in chunks:
        raise_if_cancelled(cancel_callback)
        try:
            cr = session.get(
                f"https://accounts.x.ai{path}",
                proxies=proxies_dict(proxy),
                timeout=15,
            )
            raise_if_cancelled(cancel_callback)
            text = cr.text
        except Exception:
            continue
        if "createUserAndSessionRequest" in text or "emailValidationCode" in text:
            m = re.search(r'createServerReference\)?\(\s*"([a-f0-9]{40,50})"', text)
            if m:
                action = m.group(1)
                break
            ids = re.findall(r'"([a-f0-9]{42})"', text)
            if ids:
                action = ids[0]
                break
    return action, tree_enc
