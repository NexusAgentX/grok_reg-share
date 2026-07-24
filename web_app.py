"""Web UI for grok_reg - Flask backend with SSE log streaming."""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GROK_REG_DATA_DIR", BASE_DIR)).expanduser().resolve()
CONFIG_FILE = DATA_DIR / "config.json"
CONFIG_EXAMPLE = BASE_DIR / "config.example.json"
ACCOUNTS_FILE = DATA_DIR / "accounts_cli.txt"
CPA_DIR = DATA_DIR / "cpa_auths"
sys.path.insert(0, str(BASE_DIR))

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("GROK_REG_FLASK_SECRET") or secrets.token_bytes(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")

WEB_TOKEN = os.environ.get("GROK_REG_WEB_TOKEN") or secrets.token_urlsafe(24)
_SECRET_SENTINEL = "__GROK_REG_SECRET_SET__"
_SECRET_KEY_RE = re.compile(r"password|api[_-]?key|jwt|app[_-]?key|secret", re.I)


def _is_authenticated():
    return bool(session.get("authenticated"))


@app.after_request
def security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.before_request
def require_local_login():
    if request.endpoint in {"login", "static", "favicon"}:
        return None
    if not _is_authenticated():
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="authentication required"), 401
        return redirect(url_for("login", next=request.path))
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(ok=False, error="cross-origin request rejected"), 403
    return None


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        supplied = request.form.get("token", "")
        if hmac.compare_digest(supplied, WEB_TOKEN):
            session.clear()
            session["authenticated"] = True
            next_path = request.args.get("next", "")
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = url_for("index")
            return redirect(next_path)
        error = "访问令牌无效"
    return render_template("login.html", error=error)


# --------------- SSE log bus ---------------
_log_listeners: list[queue.Queue] = []
_log_lock = threading.Lock()
_log_history: list[dict] = []
MAX_HISTORY = 500

def _broadcast(entry: dict):
    with _log_lock:
        _log_history.append(entry)
        if len(_log_history) > MAX_HISTORY:
            _log_history.pop(0)
        dead = []
        for q in _log_listeners:
            try: q.put_nowait(entry)
            except Exception: dead.append(q)
        for q in dead:
            _log_listeners.remove(q)

def _subscribe():
    q = queue.Queue(maxsize=200)
    with _log_lock:
        _log_listeners.append(q)
    return q

def _unsubscribe(q):
    with _log_lock:
        try: _log_listeners.remove(q)
        except ValueError: pass

# --------------- registration state ---------------
_run_lock = threading.Lock()
_running = False
_stats = dict(reg_success=0, reg_fail=0, mint_success=0, mint_fail=0, mint_skip=0)
_cancel_event = threading.Event()

# CPA mint queue lock
_mint_lock = threading.Lock()
_minting_emails = set()

def _is_running():
    with _run_lock:
        return _running

def _strip_comments(obj):
    return {k: v for k, v in obj.items() if not k.startswith("//") and not k.startswith("#")}


def _is_secret_key(key):
    key = str(key)
    return key in {"proxy", "cpa_proxy", "moemail_cookie"} or bool(_SECRET_KEY_RE.search(key))


def public_config():
    return {
        key: (_SECRET_SENTINEL if _is_secret_key(key) and value else value)
        for key, value in load_config().items()
    }

def load_config():
    path = CONFIG_FILE if CONFIG_FILE.exists() else CONFIG_EXAMPLE
    # 兼容编码问题：优先 utf-8，失败则回退至 gb18030
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(path, encoding="gb18030") as f:
            content = f.read()
    raw = json.loads(content)
    return _strip_comments(raw)

def save_config(data):
    if not isinstance(data, dict):
        raise ValueError("config body must be an object")
    existing = load_config()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(CONFIG_FILE, encoding="gb18030") as f:
                content = f.read()
        existing = json.loads(content)

    for k, v in data.items():
        if not isinstance(k, str) or k.startswith(("//", "#")):
            continue
        if _is_secret_key(k) and v == _SECRET_SENTINEL:
            continue
        existing[k] = v

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=CONFIG_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, CONFIG_FILE)
        os.chmod(CONFIG_FILE, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

def _cpa_path_for_email(email):
    safe = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "@._-") else "-"
        for ch in str(email).strip()
    ).strip("-")
    return CPA_DIR / f"xai-{safe}.json"


def read_accounts(include_secrets=False):
    results = []
    if not ACCOUNTS_FILE.exists():
        return results
    for i, line in enumerate(ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        email = parts[0] if len(parts) > 0 else ""
        password = parts[1] if len(parts) > 1 else ""
        sso = parts[2] if len(parts) > 2 else ""
        
        cpa_path = _cpa_path_for_email(email)
        has_cpa = cpa_path.exists()
        cpa_expiry = ""
        if has_cpa:
            try:
                cpa_data = json.loads(cpa_path.read_text(encoding="utf-8"))
                exp = cpa_data.get("expires_at") or cpa_data.get("expires") or cpa_data.get("expired")
                if exp:
                    cpa_expiry = str(exp)
            except Exception:
                pass

        is_minting = False
        with _mint_lock:
            is_minting = email in _minting_emails

        item = dict(
            id=i,
            email=email,
            has_password=bool(password),
            has_sso=bool(sso),
            has_cpa=has_cpa,
            cpa_expiry=cpa_expiry,
            is_minting=is_minting,
        )
        if include_secrets:
            item.update(password=password, sso=sso, cpa_path=cpa_path)
        results.append(item)
    return results


def get_account(account_id):
    try:
        target = int(account_id)
    except (TypeError, ValueError):
        return None
    return next((a for a in read_accounts(include_secrets=True) if a["id"] == target), None)

def cpa_file_count():
    if not CPA_DIR.exists():
        return 0
    return len(list(CPA_DIR.glob("xai-*.json")))

# --------------- CPA Mint single task thread ---------------
def _bg_mint_single(email: str, password: str, sso: str):
    def log_cb(msg):
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg=f"[CPA-Mint] [{email}] {msg}"))

    try:
        log_cb("开始单号 CPA OIDC 补签流程...")
        import cpa_export
        
        reg_cfg = load_config()
        proxy = reg_cfg.get("cpa_proxy") or reg_cfg.get("proxy") or None
        log_cb(f"正在拉起 Chromium 访问 accounts.x.ai 进行授权确认 (使用代理: {proxy or '直连'})...")
        
        r = cpa_export.export_cpa_xai_for_account(
            email=email,
            password=password,
            page=None,
            cookies=None,
            sso=sso,
            config=reg_cfg,
            log_callback=log_cb,
        )
        
        if r.get("ok") and r.get("path"):
            log_cb(f"CPA OIDC 授权生成成功! 路径: {r.get('path')}")
        else:
            log_cb(f"CPA 补签失败: {r.get('error') or r}")
    except Exception as exc:
        log_cb(f"CPA 补签发生异常: {exc}")
        log_cb(traceback.format_exc())
    finally:
        with _mint_lock:
            _minting_emails.discard(email)
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg="__DONE__"))

# --------------- batch register run ---------------
def _run_registration(extra: int, threads: int, registration_mode: str):
    global _running, _stats
    with _run_lock:
        _running = True
        _stats = dict(reg_success=0, reg_fail=0, mint_success=0, mint_fail=0, mint_skip=0)
    _cancel_event.clear()
    def log_cb(msg: str):
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg=msg))
    try:
        log_cb(
            f"[Web] 开始批量注册任务: 数量={extra}, 线程={threads}, "
            f"模式={registration_mode}"
        )
        import register_cli as cli
        import grok_register_ttk as reg
        reg.load_config()
        cli.reset_stats()
        cfg = getattr(reg, "config", {}) or {}
        registration_mode = cli.resolve_registration_mode(registration_mode, cfg)
        threads = max(1, min(threads, 10))
        mint_workers = cli.resolve_mint_workers(cli_value=-1, threads=threads, config=cfg, inline_mint=False)
        do_mint_inline = mint_workers == 0
        mint_qmax = cli.resolve_mint_queue_max(cfg, mint_workers)
        # Keep debug IO on so failed profile/email stages can dump page state.
        reg.configure_perf(
            fast=True,
            sleep_scale=0.15,
            skip_debug_io=False,
            cookie_snapshot=False,
            async_side_effects=True,
            browser_reuse=True,
            browser_recycle_every=25,
        )
        done_count = 0
        af = str(ACCOUNTS_FILE)
        if os.path.exists(af):
            with open(af) as f:
                done_count = sum(1 for line in f if line.strip())
        target_total = done_count + extra
        if cli.requires_eager_browser_pool(registration_mode):
            try:
                reg.TabPool.init(reg.create_browser_options, log_callback=log_cb)
            except Exception as exc:
                log_cb(f"[!] 浏览器初始化失败: {exc}")
                with _run_lock:
                    _running = False
                return
        task_queue = queue.Queue()
        mint_queue = queue.Queue() if not do_mint_inline else None
        if mint_queue is not None:
            mint_queue._reg_qmax = mint_qmax
        for i in range(done_count + 1, target_total + 1):
            task_queue.put(i)
        original_log = cli.log
        cli.log = lambda wid, msg: log_cb(f"[W{wid}] {msg}")
        mint_threads = []
        if mint_queue is not None and mint_workers > 0:
            for i in range(1, mint_workers + 1):
                wid = f"M{i}"
                t = threading.Thread(
                    target=cli._mint_worker,
                    args=(wid, mint_queue, cfg, _cancel_event),
                    daemon=True,
                )
                t.start()
                mint_threads.append(t)
        reg_threads = []
        for wid in range(1, threads + 1):
            t = threading.Thread(
                target=cli._register_worker,
                args=(
                    wid,
                    task_queue,
                    target_total,
                    af,
                    mint_queue,
                    False,
                    do_mint_inline,
                    _cancel_event,
                    registration_mode,
                ),
                daemon=True,
            )
            t.start()
            reg_threads.append(t)
        for t in reg_threads:
            while t.is_alive():
                if _cancel_event.is_set():
                    log_cb("[Web] 收到取消信号，正在停止浏览器任务...")
                t.join(timeout=1.0)
        if mint_queue is not None:
            log_cb("[Web] 正在等待 CPA Mint 队列结束...")
            mint_queue.join()
            for _ in mint_threads:
                mint_queue.put(cli._MINT_STOP)
            for t in mint_threads:
                t.join(timeout=120)
        try: reg.shutdown_browser()
        except Exception: pass
        with cli._stats_lock:
            _stats.update(cli._stats)
        log_cb(f"[Web] 注册已完成: 注册成功={_stats['reg_success']}, 注册失败={_stats['reg_fail']}, CPA成功={_stats['mint_success']}, CPA失败={_stats['mint_fail']}, CPA跳过={_stats['mint_skip']}")
    except Exception as exc:
        log_cb(f"[!] 任务异常: {exc}")
        log_cb(traceback.format_exc())
    finally:
        try:
            if "cli" in locals() and "original_log" in locals():
                cli.log = original_log
        except Exception:
            pass
        with _run_lock:
            _running = False
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg="__DONE__"))

# --------------- routes ---------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    accounts = read_accounts()
    return jsonify(dict(
        running=_is_running(), 
        stats=dict(_stats), 
        accounts_count=len(accounts), 
        cpa_count=cpa_file_count()
    ))

@app.route("/api/accounts")
def api_accounts():
    return jsonify(read_accounts())

@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(public_config())

@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    save_config(data)
    return jsonify(dict(ok=True))

@app.route("/api/start", methods=["POST"])
def api_start():
    global _running
    data = request.get_json(force=True) or {}
    try:
        extra = int(data.get("extra", 1))
        threads = int(data.get("threads", 1))
        import register_cli as cli
        registration_mode = cli.resolve_registration_mode(
            data.get("registration_mode"), load_config()
        )
    except (TypeError, ValueError) as exc:
        return jsonify(dict(ok=False, error=f"启动参数无效: {exc}")), 400
    if not 1 <= extra <= 100 or not 1 <= threads <= 10:
        return jsonify(dict(ok=False, error="数量需为 1-100，线程需为 1-10")), 400
    if registration_mode != "browser" and threads > 4:
        return jsonify(dict(ok=False, error="快速或自动模式的并发线程需为 1-4")), 400
    with _run_lock:
        if _running:
            return jsonify(dict(ok=False, error="当前有注册任务正在运行")), 409
        _running = True
    t = threading.Thread(
        target=_run_registration,
        args=(extra, threads, registration_mode),
        daemon=True,
    )
    try:
        t.start()
    except Exception:
        with _run_lock:
            _running = False
        raise
    return jsonify(
        dict(ok=True, extra=extra, threads=threads, registration_mode=registration_mode)
    )

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not _is_running():
        return jsonify(dict(ok=False, error="注册任务没有运行")), 400
    _cancel_event.set()
    return jsonify(dict(ok=True))

# CPA probe API
@app.route("/api/cpa/probe", methods=["POST"])
def api_cpa_probe():
    data = request.get_json(force=True) or {}
    account = get_account(data.get("account_id"))
    if not account:
        return jsonify(dict(ok=False, error="账号不存在")), 404
    email = account["email"]
    cpa_path = account["cpa_path"]
    if not cpa_path.exists():
        return jsonify(dict(ok=False, error="对应的 CPA OIDC 凭证文件不存在")), 404
        
    try:
        cpa_data = json.loads(cpa_path.read_text(encoding="utf-8"))
        access_token = cpa_data.get("access_token")
        if not access_token:
            return jsonify(dict(ok=False, error="凭证文件中无 access_token")), 400
            
        reg_cfg = load_config()
        proxy = reg_cfg.get("cpa_proxy") or reg_cfg.get("proxy") or None
        
        from cpa_xai.probe import probe_models
        t0 = time.time()
        res = probe_models(
            access_token,
            base_url=reg_cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1",
            proxy=proxy,
        )
        res["elapsed"] = round(time.time() - t0, 2)
        return jsonify(res)
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

# Single Account CPA Mint
@app.route("/api/cpa/mint_single", methods=["POST"])
def api_cpa_mint_single():
    data = request.get_json(force=True) or {}
    account = get_account(data.get("account_id"))
    if not account:
        return jsonify(dict(ok=False, error="账号不存在")), 404
    email = account["email"]
    password = account["password"]
    sso = account["sso"]
    if not password:
        return jsonify(dict(ok=False, error="账号密码缺失")), 400
        
    with _mint_lock:
        if email in _minting_emails:
            return jsonify(dict(ok=False, error="该账号已在补签任务中")), 409
        _minting_emails.add(email)

    t = threading.Thread(target=_bg_mint_single, args=(email, password, sso), daemon=True)
    try:
        t.start()
    except Exception:
        with _mint_lock:
            _minting_emails.discard(email)
        raise
    return jsonify(dict(ok=True))


@app.route("/api/accounts/<int:account_id>/secret/<kind>", methods=["POST"])
def api_account_secret(account_id, kind):
    account = get_account(account_id)
    if not account:
        return jsonify(dict(ok=False, error="账号不存在")), 404
    if kind == "password":
        value = account["password"]
    elif kind == "sso":
        value = account["sso"]
    elif kind == "cpa":
        path = account["cpa_path"]
        if not path.exists():
            return jsonify(dict(ok=False, error="CPA 凭证不存在")), 404
        value = path.read_text(encoding="utf-8")
    else:
        return jsonify(dict(ok=False, error="凭证类型无效")), 400
    response = jsonify(dict(ok=True, value=value))
    response.headers["Cache-Control"] = "no-store"
    return response

# Test email connection
@app.route("/api/test/mail", methods=["POST"])
def api_test_mail():
    try:
        import grok_register_ttk as reg
        reg.load_config()
        cfg = load_config()
        provider = (cfg.get("email_provider") or "cloudmail").strip().lower()

        t0 = time.time()
        if provider == "cloudmail":
            url = reg.get_cloudmail_url()
            admin = reg.get_cloudmail_admin_email()
            pwd = reg.get_cloudmail_password()
            if not url:
                return jsonify(dict(ok=False, error="未配置 CloudMail 地址 (cloudmail_url)")), 400
            token = reg.cloudmail_gen_public_token(url, admin, pwd)
            elapsed = round(time.time() - t0, 2)
            preview = (str(token)[:15] + "...") if token else ""
            return jsonify(dict(ok=True, provider=provider, token=preview, elapsed=elapsed))

        # 其它服务商：做基础配置完整性检查
        required = {
            "cloudflare": ["cloudflare_api_base"],
            "duckmail": ["duckmail_api_key"],
            "yyds": ["yyds_api_key"],
        }.get(provider, [])
        missing = [k for k in required if not str(cfg.get(k) or "").strip()]
        if missing:
            return jsonify(dict(ok=False, error=f"当前服务商 {provider} 缺少配置: {', '.join(missing)}")), 400
        elapsed = round(time.time() - t0, 2)
        return jsonify(dict(
            ok=True,
            provider=provider,
            token="配置项检查通过",
            elapsed=elapsed,
            note=f"已校验 {provider} 关键配置项，未实际发起收信请求"
        ))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

# Workspace diagnostics
@app.route("/api/test/sys_check", methods=["GET"])
def api_test_sys_check():
    import sys, platform, shutil
    chrome_path = ""
    for cand in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ):
        if os.path.exists(cand):
            chrome_path = cand
            break
    if not chrome_path:
        chrome_path = (
            shutil.which("google-chrome-stable")
            or shutil.which("google-chrome")
            or shutil.which("chrome")
            or shutil.which("chromium")
            or "未检测到系统 Chrome，请确认安装"
        )
        
    res = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "chrome_path": chrome_path,
        "workspace": str(BASE_DIR)
    }
    return jsonify(res)

@app.route("/api/logs/stream")
def api_logs_stream():
    q = _subscribe()
    def generate():
        with _log_lock:
            for entry in list(_log_history[-50:]):
                yield "data: " + json.dumps(entry, ensure_ascii=False) + "\n\n"
        try:
            while True:
                try:
                    entry = q.get(timeout=30)
                    yield "data: " + json.dumps(entry, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            _unsubscribe(q)
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    host = os.environ.get("GROK_REG_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("GROK_REG_WEB_PORT", "5000"))
    print(f"[*] Grok Register Web UI: http://{host}:{port}")
    print(f"[*] Runtime data: {DATA_DIR}")
    print("[*] Web login token: configured")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
