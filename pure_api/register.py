"""Single-account hybrid protocol registration backend.

HTTP performs email verification and account creation. A short Chromium session
only harvests Castle and Turnstile tokens. Batch orchestration, persistence, and
CPA minting remain owned by register_cli.
"""

from __future__ import annotations

import json
import random
import secrets
import time
from typing import Any, Callable

from .client import AuthClient
from .errors import (
    CancelFn,
    FastRegistrationAmbiguous,
    FastRegistrationCancelled,
    raise_if_cancelled,
    sleep_with_cancel,
)
from .mail import create_mailbox, delete_mailbox, poll_code
from .session import (
    SIGNUP_URL,
    apply_browser_cookies,
    load_config,
    make_session,
    warm_signup,
)
from .browser_util import force_close_browser, kill_project_orphan_browsers
from .tokens import (
    _click_email_signup,
    _create_browser,
    _export_cookies,
    _fill_email_and_continue,
    _install_hooks,
    _read_castle,
    _read_turnstile,
)

LogFn = Callable[[str], None]


def _build_profile() -> tuple[str, str, str]:
    given = random.choice(
        ["Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo", "Owen", "Kai", "Felix", "Adam"]
    )
    family = random.choice(
        ["Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun", "Guo", "He", "Yang", "Wu"]
    )
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given, family, password


def _set_input(page, selectors: str, value: str) -> bool:
    return bool(
        page.run_js(
            """
const selectors = String(arguments[0]||'').split(',');
const value = String(arguments[1]||'');
function vis(n){if(!n)return false;const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden')return false;const r=n.getBoundingClientRect();return r.width>0&&r.height>0}
function setVal(input,value){
  input.focus(); input.click();
  const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
  const t=input._valueTracker; if(t)t.setValue('');
  if(setter) setter.call(input,value); else input.value=value;
  input.dispatchEvent(new InputEvent('beforeinput',{bubbles:true,data:value,inputType:'insertText'}));
  input.dispatchEvent(new InputEvent('input',{bubbles:true,data:value,inputType:'insertText'}));
  input.dispatchEvent(new Event('change',{bubbles:true}));
  input.blur();
  return String(input.value||'')===value;
}
for (const sel of selectors) {
  const nodes=[...document.querySelectorAll(sel.trim())].filter(vis);
  if(nodes[0] && setVal(nodes[0], value)) return true;
}
return false;
""",
            selectors,
            value,
        )
    )


def _click_any(page, *needles: str) -> bool:
    joined = "||".join(str(x) for x in needles)
    return bool(
        page.run_js(
            """
const needles = String(arguments[0]||'').split('||').filter(Boolean).map(s=>s.toLowerCase());
const extra = ['\\u786e\\u8ba4\\u90ae\\u7bb1','\\u786e\\u8ba4','\\u7ee7\\u7eed','\\u4e0b\\u4e00\\u6b65','\\u5b8c\\u6210\\u6ce8\\u518c','\\u521b\\u5efa\\u8d26\\u6237','\\u6ce8\\u518c'];
for (const x of extra) needles.push(x);
function vis(n){if(!n)return false;const s=getComputedStyle(n);if(s.display==='none'||s.visibility==='hidden')return false;const r=n.getBoundingClientRect();return r.width>0&&r.height>0}
const btn=[...document.querySelectorAll('button, [role=button], a')].find(n=>{
  if(!vis(n) || n.disabled) return false;
  const t=(n.innerText||n.textContent||'').replace(/\\s+/g,'').toLowerCase();
  return needles.some(x=>t.includes(x));
});
if(!btn) return false;
btn.focus(); btn.click();
return true;
""",
            joined,
        )
    )


def _has_profile(page) -> bool:
    try:
        return bool(
            page.run_js(
                """
return !!(document.querySelector('input[type=password]') &&
  document.querySelector('input[name=givenName],input[autocomplete=given-name],input[data-testid=givenName]'));
"""
            )
        )
    except Exception:
        return False


def _has_otp(page) -> bool:
    try:
        return bool(
            page.run_js(
                """
return [...document.querySelectorAll('input')].some(n =>
  Number(n.maxLength||0)===1 ||
  (n.autocomplete||'')==='one-time-code' ||
  n.name==='code' ||
  n.dataset.inputOtp==='true'
);
"""
            )
        )
    except Exception:
        return False


def _install_create_intercept(page, *, block_submit: bool = True) -> None:
    page.run_js(
        """
(() => {
  const block = !!arguments[0];
  window.__pure_create = window.__pure_create || {posts:[], castles:[]};
  const ofetch = window.__pure_ofetch || window.fetch;
  window.__pure_ofetch = ofetch;
  window.fetch = async function(input, init = {}) {
    const url = (typeof input === 'string') ? input : (input && input.url) || '';
    const headers = {};
    try {
      const h = (init && init.headers) || (input && input.headers);
      if (h && typeof h.forEach === 'function') h.forEach((v,k)=>headers[String(k).toLowerCase()]=v);
      else if (h && typeof h === 'object') {
        for (const [k,v] of Object.entries(h)) headers[String(k).toLowerCase()] = v;
      }
    } catch (e) {}
    let body = init && init.body;
    if (typeof body !== 'string' && body != null) {
      try {
        if (body instanceof ArrayBuffer) body = new TextDecoder('utf-8').decode(new Uint8Array(body));
        else if (ArrayBuffer.isView(body)) body = new TextDecoder('utf-8').decode(new Uint8Array(body.buffer, body.byteOffset, body.byteLength));
        else body = String(body);
      } catch (e) { body = ''; }
    }
    const text = typeof body === 'string' ? body : '';
    if (text.includes('createUserAndSessionRequest')) {
      const rec = {url:String(url), headers, body:text, t:Date.now(), blocked: block};
      try {
        const m = text.match(/IBYIll[\\x20-\\x7e]{80,}/);
        if (m) {
          window.__pure_create.castles.push(m[0]);
          window.__pure_castle = m[0];
        }
      } catch (e) {}
      window.__pure_create.posts.push(rec);
      if (block) {
        return new Response('0:{"error":"pure_api_blocked_for_protocol_create"}', {
          status: 200,
          headers: {'content-type': 'text/x-component'}
        });
      }
      const resp = await ofetch.apply(this, arguments);
      try {
        const clone = resp.clone();
        rec.respStatus = resp.status;
        rec.respText = (await clone.text()).slice(0, 8000);
      } catch (e) {}
      return resp;
    }
    return ofetch.apply(this, arguments);
  };
  return true;
})()
""",
        bool(block_submit),
    )


def _fill_otp(
    page,
    code: str,
    cancel_callback: CancelFn | None = None,
) -> None:
    clean = str(code).replace("-", "").strip()
    page.run_js(
        """
const code=String(arguments[0]||'');
function setVal(input,value){
  const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
  const t=input._valueTracker; if(t)t.setValue('');
  if(setter)setter.call(input,value); else input.value=value;
  input.dispatchEvent(new InputEvent('input',{bubbles:true,data:value,inputType:'insertText'}));
  input.dispatchEvent(new Event('change',{bubbles:true}));
}
const boxes=[...document.querySelectorAll('input')].filter(n=>Number(n.maxLength||0)===1 && !n.disabled);
if(boxes.length>=Math.min(6,code.length)){
  for(let i=0;i<code.length && i<boxes.length;i++){ boxes[i].focus(); setVal(boxes[i], code[i]); }
} else {
  const agg=[...document.querySelectorAll('input')].find(n=>Number(n.maxLength||0)>1 || (n.autocomplete||'')==='one-time-code' || n.name==='code' || n.dataset.inputOtp==='true');
  if(agg){ agg.focus(); setVal(agg, code); }
}
return true;
""",
        clean,
    )
    sleep_with_cancel(0.3, cancel_callback)
    _click_any(page, "confirm", "continue", "next")


def _click_turnstile(page, log: LogFn) -> bool:
    try:
        rect = page.run_js(
            """
const input = document.querySelector('input[name="cf-turnstile-response"]');
const host = input && input.parentElement;
if (!host) return null;
const r = host.getBoundingClientRect();
if (!r.width || !r.height) return null;
return {x:r.x, y:r.y, width:r.width, height:r.height};
            """
        )
        if not isinstance(rect, dict):
            return False
        x = float(rect.get("x", 0)) + min(
            max(float(rect.get("width", 0)) * 0.18, 16), 28
        )
        y = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2
        for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            page.run_cdp(
                "Input.dispatchMouseEvent",
                type=event_type,
                x=x,
                y=y,
                button="left" if event_type != "mouseMoved" else "none",
                clickCount=1 if event_type != "mouseMoved" else 0,
            )
        log("[token] clicked Turnstile with real CDP pointer")
        return True
    except Exception as exc:
        log(f"[token] Turnstile click unavailable: {exc}")
        return False


def _wait_turnstile(
    page,
    log: LogFn,
    timeout: float = 70.0,
    cancel_callback: CancelFn | None = None,
) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        tok = _read_turnstile(page)
        if len(tok or "") >= 80:
            log(f"[token] turnstile via DOM len={len(tok)}")
            return tok
        _click_turnstile(page, log)
        sleep_with_cancel(0.3, cancel_callback)
    raise RuntimeError("Turnstile harvest failed on profile page")


def _parse_create_capture(page, log: LogFn) -> dict[str, str]:
    out: dict[str, str] = {}
    cap = page.run_js("return window.__pure_create || {}") or {}
    posts = cap.get("posts") or []
    if not posts:
        castle = page.run_js("return String(window.__pure_castle||'')") or ""
        if len(castle) >= 100:
            out["castle"] = castle
        return out
    body = str(posts[-1].get("body") or "")
    headers = posts[-1].get("headers") or {}
    if headers.get("next-action"):
        out["next_action"] = str(headers.get("next-action"))
    if headers.get("next-router-state-tree"):
        out["router_tree"] = str(headers.get("next-router-state-tree"))
    try:
        parsed = json.loads(body)
        arg0 = parsed[0] if isinstance(parsed, list) and parsed else parsed
        if isinstance(arg0, dict):
            if arg0.get("castleRequestToken"):
                out["castle"] = str(arg0["castleRequestToken"])
            if arg0.get("turnstileToken") and len(str(arg0["turnstileToken"])) >= 80:
                out["turnstile"] = str(arg0["turnstileToken"])
            if arg0.get("conversionId"):
                out["conversion_id"] = str(arg0["conversionId"])
    except Exception as exc:
        log(f"[token] parse create capture failed: {exc}")
    if "castle" not in out:
        castle = page.run_js("return String(window.__pure_castle||'')") or ""
        if len(castle) >= 100:
            out["castle"] = castle
    return out


def register_one(
    config: dict[str, Any] | None = None,
    *,
    log_callback: LogFn | None = None,
    cancel_callback: CancelFn | None = None,
) -> dict[str, Any]:
    cfg = dict(config or load_config())
    log = log_callback or (lambda m: print(m, flush=True))
    t0 = time.time()
    raise_if_cancelled(cancel_callback)

    session, proxy = make_session(cfg)
    html = warm_signup(session, proxy)
    raise_if_cancelled(cancel_callback)
    log("[pure] warmed signup page")

    email, email_id = "", ""
    given, family, password = _build_profile()
    headless = bool(cfg.get("pure_api_headless", False))

    browser = None
    page = None
    browser_tmp = None
    castle = ""
    turnstile = ""
    next_action = ""
    router_tree = ""
    conversion_id = ""
    sso = ""
    mail_deleted = False
    browser_cookies: list[dict] = []
    create_user_attempted = False

    def _drop_mailbox(reason: str) -> None:
        nonlocal mail_deleted
        if mail_deleted or not email_id:
            return
        log(f"[mail] drop mailbox ({reason})")
        if delete_mailbox(cfg, email_id, log=log):
            mail_deleted = True

    try:
        email, email_id = create_mailbox(
            cfg, log=log, cancel_callback=cancel_callback
        )
        raise_if_cancelled(cancel_callback)
        browser, page, browser_tmp = _create_browser(
            proxy=proxy, headless=headless, config=cfg
        )
        log(f"[pure] browser started headless={headless}")
        page.get(SIGNUP_URL)
        try:
            page.wait.doc_loaded()
        except Exception:
            pass
        sleep_with_cancel(1.0, cancel_callback)
        _install_hooks(page)
        _install_create_intercept(page, block_submit=True)
        html = str(getattr(page, "html", "") or html)

        # 1) email submit → castle from CreateEmailValidationCode body
        _click_email_signup(page)
        sleep_with_cancel(0.6, cancel_callback)
        _fill_email_and_continue(page, email)
        log("[pure] email submitted for castle")
        for _ in range(50):
            raise_if_cancelled(cancel_callback)
            castle = _read_castle(page)
            if castle and len(castle) >= 100:
                log(f"[pure] castle1 len={len(castle)}")
                break
            sleep_with_cancel(0.3, cancel_callback)
        if not castle or len(castle) < 100:
            raise RuntimeError("Castle token harvest failed at email step")

        apply_browser_cookies(session, _export_cookies(page))
        client = AuthClient(
            session,
            proxy=proxy,
            log=log,
            cancel_callback=cancel_callback,
            bootstrap_html=html,
        )

        raise_if_cancelled(cancel_callback)
        create_res = client.create_email_code(email, castle)
        if create_res.get("grpc_status") not in (0, None):
            raise RuntimeError(
                f"CreateEmailValidationCode failed: {create_res.get('trailers')}"
            )

        code = poll_code(
            cfg,
            email,
            email_id,
            timeout=float(cfg.get("mail_timeout", 120) or 120),
            poll_interval=float(cfg.get("mail_poll_interval", 0.5) or 0.5),
            log=log,
            cancel_callback=cancel_callback,
        )
        # Code received → free MoeMail slot immediately (cap ~20).
        _drop_mailbox("code-received")
        raise_if_cancelled(cancel_callback)
        verify_res = client.verify_email_code(email, code)
        if verify_res.get("grpc_status") not in (0, None):
            raise RuntimeError(
                f"VerifyEmailValidationCode failed: {verify_res.get('trailers')}"
            )
        raise_if_cancelled(cancel_callback)
        password_res = client.validate_password(email, password)
        if password_res.get("grpc_status") not in (0, None):
            raise RuntimeError(
                f"ValidatePassword failed: {password_res.get('trailers')}"
            )

        # 2) browser OTP → profile (same session)
        if not _has_profile(page):
            otp_filled = False
            for _ in range(40):
                raise_if_cancelled(cancel_callback)
                if _has_profile(page):
                    break
                if not otp_filled and _has_otp(page):
                    _fill_otp(page, code, cancel_callback)
                    otp_filled = True
                sleep_with_cancel(0.4, cancel_callback)
        if not _has_profile(page):
            # hard refresh path: open signup and hope verified session advances
            log("[pure] profile not visible; retry otp/profile wait")
            for _ in range(20):
                raise_if_cancelled(cancel_callback)
                if _has_otp(page):
                    _fill_otp(page, code, cancel_callback)
                if _has_profile(page):
                    break
                sleep_with_cancel(0.5, cancel_callback)
        if not _has_profile(page):
            raise RuntimeError("Profile form not reached after OTP")

        _set_input(
            page,
            "input[name=givenName],input[data-testid=givenName],input[autocomplete=given-name]",
            given,
        )
        _set_input(
            page,
            "input[name=familyName],input[data-testid=familyName],input[autocomplete=family-name]",
            family,
        )
        _set_input(
            page,
            "input[type=password],input[name=password],input[autocomplete=new-password]",
            password,
        )
        sleep_with_cancel(0.6, cancel_callback)

        # 3) native turnstile on profile
        turnstile = _wait_turnstile(
            page,
            log,
            timeout=float(cfg.get("pure_api_token_timeout", 80) or 80),
            cancel_callback=cancel_callback,
        )

        # 4) click complete-registration; intercept fresh castle (POST blocked)
        _install_create_intercept(page, block_submit=True)
        _click_any(page, "sign up", "createaccount", "create account")
        for i in range(35):
            raise_if_cancelled(cancel_callback)
            info = page.run_js(
                "return {n:(window.__pure_create&&window.__pure_create.posts||[]).length, c:(window.__pure_create&&window.__pure_create.castles||[]).length}"
            )
            if info and (info.get("n", 0) > 0 or info.get("c", 0) > 0):
                break
            if i in (8, 18, 28):
                _click_any(page, "sign up", "createaccount", "create account")
            sleep_with_cancel(0.4, cancel_callback)

        captured = _parse_create_capture(page, log)
        if captured.get("castle"):
            castle = captured["castle"]
            log(f"[pure] castle(create) len={len(castle)}")
        if captured.get("turnstile"):
            turnstile = captured["turnstile"]
        if captured.get("next_action"):
            next_action = captured["next_action"]
            client.next_action = next_action
            log(f"[pure] next-action live={next_action}")
        if captured.get("router_tree"):
            router_tree = captured["router_tree"]
            client.router_tree = router_tree
        if captured.get("conversion_id"):
            conversion_id = captured["conversion_id"]

        browser_cookies = _export_cookies(page)
        apply_browser_cookies(session, browser_cookies)

        # If browser somehow already got sso (should not when blocked), keep it.
        for c in browser_cookies:
            if c.get("name") in ("sso", "sso-rw") and c.get("value"):
                sso = str(c["value"])
                break

        # 5) protocol create_user
        if not sso:
            raise_if_cancelled(cancel_callback)
            if not client.next_action:
                client.bootstrap(html=html)
            create_user_attempted = True
            create_user = client.create_user(
                email=email,
                code=code,
                password=password,
                given_name=given,
                family_name=family,
                turnstile_token=turnstile,
                castle_token=castle,
                conversion_id=conversion_id or None,
                body_style="args2",
            )
            sso = create_user.get("sso") or ""
            if not sso:
                # one retry with args1 then raw
                for style in ("args1", "raw"):
                    raise_if_cancelled(cancel_callback)
                    log(f"[pure] retry create_user style={style}")
                    create_user_attempted = True
                    create_user = client.create_user(
                        email=email,
                        code=code,
                        password=password,
                        given_name=given,
                        family_name=family,
                        turnstile_token=turnstile,
                        castle_token=castle,
                        conversion_id=conversion_id or None,
                        body_style=style,
                    )
                    sso = create_user.get("sso") or ""
                    if sso:
                        break
    except (FastRegistrationCancelled, FastRegistrationAmbiguous):
        raise
    except Exception as exc:
        if create_user_attempted:
            raise FastRegistrationAmbiguous(
                "create_user outcome is unknown; automatic browser fallback is disabled"
            ) from exc
        raise
    finally:
        # Always kill register browser BEFORE mint so windows don't pile up
        # while waiting on mint lock / mint Chromium.
        force_close_browser(browser, tmp=browser_tmp, log=log)
        browser = None
        page = None
        # Delete only the mailbox created by this attempt.
        _drop_mailbox("finally")

    if not sso:
        kill_project_orphan_browsers(log=log)
        raise FastRegistrationAmbiguous(
            "create_user returned no session; automatic browser fallback is disabled"
        )

    log(f"[pure] protocol registration completed: {email} ({time.time()-t0:.1f}s)")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "password": password,
        "sso": sso,
        "given_name": given,
        "family_name": family,
        "profile": {"given_name": given, "family_name": family, "password": password},
        "cookies": browser_cookies,
        "elapsed_sec": round(time.time() - t0, 2),
    }

    kill_project_orphan_browsers(log=log)
    return result
