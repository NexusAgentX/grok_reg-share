from __future__ import annotations

import os
import queue
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import grok_register_ttk as reg
import register_cli as cli
import pure_api.mail as fast_mail
import pure_api.register as fast_register
import pure_api.tokens as fast_tokens
from pure_api.client import AuthClient
from pure_api.errors import FastRegistrationAmbiguous, FastRegistrationCancelled
from pure_api.register import register_one as pure_register_one
from cpa_xai.schema import build_cpa_xai_auth
from cpa_xai.writer import write_cpa_xai_auth


class CoreBehaviorTestCase(unittest.TestCase):
    def setUp(self):
        cli.reset_stats()

    def test_user_visible_source_has_no_known_mojibake_fragments(self):
        source = Path(reg.__file__).read_text(encoding="utf-8")
        for fragment in (
            "鑾峰",
            "澶辫触",
            "閭欢",
            "宸插垱",
            "鐢ㄦ埛",
            "寮傚父",
            "鎴愬姛",
        ):
            self.assertNotIn(fragment, source)

    def test_secure_append_is_thread_safe_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.txt"
            lock = threading.Lock()
            threads = [
                threading.Thread(target=reg.secure_append, args=(str(path), f"line-{i}\n"), kwargs={"lock": lock})
                for i in range(40)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 40)
            self.assertEqual(set(lines), {f"line-{i}" for i in range(40)})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_required_mint_forces_inline_mode(self):
        workers = cli.resolve_mint_workers(
            cli_value=4,
            threads=4,
            config={"cpa_export_enabled": True, "cpa_mint_required": True},
            inline_mint=False,
        )
        self.assertEqual(workers, 0)

    def test_cancel_guard_observes_event_and_deadline(self):
        event = threading.Event()
        guard = cli.CancelGuard(event, timeout=5)
        self.assertFalse(guard())
        event.set()
        self.assertTrue(guard())

        deadline_guard = cli.CancelGuard(timeout=0.01)
        time.sleep(0.02)
        self.assertTrue(deadline_guard())

    def test_cancelled_registration_exits_without_failure_or_restart(self):
        event = threading.Event()
        event.set()
        with patch.object(cli, "_ensure_browser"), patch.object(reg, "open_signup_page", side_effect=reg.RegistrationCancelled("stop")), patch.object(reg, "restart_browser") as restart_browser, patch.object(reg, "stop_browser"):
            result = cli.register_one(1, 1, 1, "unused.txt", cancel_event=event)
        self.assertIsNone(result)
        restart_browser.assert_not_called()
        self.assertEqual(cli._stats["reg_fail"], 0)

    def test_cancelled_worker_does_not_start_browser(self):
        event = threading.Event()
        event.set()
        tasks = __import__("queue").Queue()
        tasks.put(1)
        with patch.object(reg, "stop_browser") as stop_browser, patch.object(cli, "register_one") as register_one:
            cli._register_worker(1, tasks, 1, "unused.txt", None, False, False, event)
        register_one.assert_not_called()
        stop_browser.assert_called_once()

    def test_email_signup_click_waits_for_form_transition(self):
        page = unittest.mock.Mock()
        page.run_js.return_value = True
        with patch.object(reg, "_get_page", return_value=page), patch.object(
            reg, "_email_form_is_ready", return_value=False
        ), patch.object(reg, "_wait_for_email_form", return_value=True) as wait:
            self.assertTrue(reg.click_email_signup_button(timeout=1))
        wait.assert_called_once_with(
            page,
            unittest.mock.ANY,
            cancel_callback=None,
        )

    def test_email_form_is_ready_before_mailbox_is_created(self):
        page = unittest.mock.Mock()
        with patch.object(reg, "_get_page", return_value=page), patch.object(
            reg, "_wait_for_email_form", return_value=False
        ), patch.object(reg, "get_email_and_token") as create:
            with self.assertRaisesRegex(Exception, "邮箱注册表单未在限定时间内出现"):
                reg.fill_email_and_submit(timeout=1)
        create.assert_not_called()

    def test_email_submit_accepts_scheduled_click_before_page_transition(self):
        page = unittest.mock.Mock()
        page.run_js.side_effect = ["filled", True, {"advanced": True}]
        with patch.object(reg, "_get_page", return_value=page), patch.object(
            reg, "_wait_for_email_form", return_value=True
        ), patch.object(
            reg, "_native_fill_email_and_submit", return_value=False
        ), patch.object(
            reg,
            "get_email_and_token",
            return_value=("temporary@duck.test", "mailbox-token"),
        ), patch.object(reg, "_start_email_submit_probe"), patch.object(
            reg, "_stop_email_submit_probe"
        ), patch.object(reg, "human_sleep"), patch.object(
            reg, "dump_state"
        ), patch.object(reg, "take_screenshot"):
            result = reg.fill_email_and_submit(timeout=1)
        self.assertEqual(result, ("temporary@duck.test", "mailbox-token"))

    def test_email_submit_timeout_diagnostics_omit_email_value(self):
        page = unittest.mock.Mock()
        page.run_js.return_value = {
            "path": "/sign-up",
            "emailVisible": True,
            "emailValid": True,
            "errors": ["Something went wrong"],
        }
        with patch.object(
            reg, "_collect_email_submit_network", return_value=[]
        ), patch.object(
            reg, "_collect_email_submit_console", return_value=[]
        ), patch.object(
            reg, "_capture_email_submit_failure_screenshot", return_value=None
        ), patch.object(
            reg, "_write_email_submit_diagnostic", return_value=None
        ), patch.object(reg, "_stop_email_submit_probe"):
            message = str(reg._email_submit_timeout_error(page))
        self.assertIn("Something went wrong", message)
        self.assertNotIn("temporary@duck.test", message)

        redacted = reg._redact_diagnostic_text(
            "failed for temporary@duck.test token abcdefghijklmnopqrstuvwxyz1234567890"
        )
        self.assertNotIn("temporary@duck.test", redacted)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz1234567890", redacted)

    def test_native_email_submit_uses_browser_input_and_request_submit(self):
        email = "temporary@duck.test"
        email_input = unittest.mock.Mock()
        email_input.states.is_displayed = True
        email_input.attr.side_effect = lambda name: {
            "disabled": None,
            "aria-disabled": None,
        }.get(name)
        email_input.property.return_value = email
        submit = unittest.mock.Mock()
        submit.states.is_displayed = True
        submit.attr.side_effect = lambda name: {
            "disabled": None,
            "aria-disabled": None,
        }.get(name)
        submit.run_js.return_value = True
        page = unittest.mock.Mock()
        page.ele.side_effect = [email_input, submit]
        with patch.object(reg, "human_sleep"):
            self.assertTrue(reg._native_fill_email_and_submit(page, email))
        email_input.clear.assert_called_once()
        email_input.input.assert_called_once_with(email)
        submit.run_js.assert_called_once()
        submit.click.assert_not_called()

    def test_cpa_writer_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = build_cpa_xai_auth(
                email="user@example.com",
                access_token="not-a-jwt",
                refresh_token="refresh-token",
            )
            path = write_cpa_xai_auth(tmp, payload)
            self.assertEqual(path.name, "xai-user@example.com.json")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_grok2api_remote_import_uses_configured_retries(self):
        previous = dict(reg.config)
        reg.config = {
            "grok2api_auto_add_local": False,
            "grok2api_auto_add_remote": True,
            "grok2api_import_retries": 3,
            "grok2api_import_retry_delay": 0,
            "enable_nsfw": False,
        }
        try:
            with patch.object(
                reg,
                "add_token_to_grok2api_remote_pool",
                side_effect=[RuntimeError("one"), RuntimeError("two"), True],
            ) as remote:
                reg._add_token_to_grok2api_pools_sync("token")
            self.assertEqual(remote.call_count, 3)
        finally:
            reg.config = previous

    def test_xai_alphanumeric_code_prefers_subject_and_rejects_css_tokens(self):
        html = "<style>.per-100{width:100%}</style><p>Confirmation code: LW4-9C8</p>"
        self.assertEqual(
            reg.extract_verification_code(
                html,
                "SpaceXAI confirmation code: LW4-9C8",
            ),
            "LW4-9C8",
        )
        self.assertEqual(
            reg.extract_verification_code(
                "Verification code is ab1-2cd",
                "",
            ),
            "AB1-2CD",
        )
        self.assertIsNone(
            reg.extract_verification_code(
                "<style>.per-100{width:100%}</style>",
                "Newsletter",
            )
        )

    def test_cloudflare_worker_create_and_cleanup_use_scoped_bearer_tokens(self):
        previous = dict(reg.config)
        reg.config = {
            "email_provider": "cloudflare",
            "cloudflare_api_base": "https://mail-api.kagari.test",
            "cloudflare_api_key": "shared-api-secret",
            "cloudflare_auth_mode": "bearer",
            "defaultDomains": "kagari.test",
            "mail_cleanup_retries": 1,
        }
        created = unittest.mock.Mock(status_code=201)
        created.json.return_value = {
            "address": "temporary@kagari.test",
            "jwt": "mailbox-token",
        }
        deleted = unittest.mock.Mock(status_code=204)
        try:
            with patch.object(reg, "http_post", return_value=created) as post:
                result = reg.cloudflare_create_temp_address(
                    "https://mail-api.kagari.test"
                )
            self.assertEqual(result, ("temporary@kagari.test", "mailbox-token"))
            post.assert_called_once_with(
                "https://mail-api.kagari.test/api/new_address",
                json={"domain": "kagari.test"},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer shared-api-secret",
                },
                params={},
            )

            with patch.object(reg, "http_delete", return_value=deleted) as delete:
                self.assertTrue(reg.release_email("mailbox-token"))
            delete.assert_called_once_with(
                "https://mail-api.kagari.test/api/mailbox",
                headers={"Authorization": "Bearer mailbox-token"},
            )
        finally:
            reg.config = previous

    def test_duckmail_domain_selection_skips_rejected_domains(self):
        previous = dict(reg.config)
        reg.config = {"duckmail_excluded_domains": "duckmail.sbs"}
        try:
            domains = [
                {"domain": "duckmail.sbs", "isVerified": True, "ownerId": None},
                {"domain": "baldur.edu.kg", "isVerified": True, "ownerId": None},
            ]
            with patch.object(reg, "get_domains", return_value=domains):
                self.assertEqual(reg.pick_domain(), "baldur.edu.kg")

            reg.config["duckmail_excluded_domains"] = "duckmail.sbs,baldur.edu.kg"
            with patch.object(reg, "get_domains", return_value=domains):
                with self.assertRaisesRegex(Exception, "当前所有公共域名均已被 xAI 拒绝"):
                    reg.pick_domain()
        finally:
            reg.config = previous

    def test_duckmail_rejection_rotates_domain_in_same_registration(self):
        previous = dict(reg.config)
        reg.config = {
            "email_provider": "duckmail",
            "email_form_timeout": 1,
            "email_submit_confirm_timeout": 1,
        }
        page = unittest.mock.Mock()
        created = unittest.mock.Mock()
        try:
            with patch.object(reg, "_get_page", return_value=page), patch.object(
                reg, "_wait_for_email_form", return_value=True
            ), patch.object(
                reg,
                "get_email_and_token",
                side_effect=[
                    ("first@duckmail.sbs", "first-token"),
                    ("second@baldur.edu.kg", "second-token"),
                ],
            ) as get_mail, patch.object(
                reg, "_native_fill_email_and_submit", return_value=True
            ), patch.object(
                reg,
                "_wait_for_email_submission",
                side_effect=["domain-rejected", True],
            ), patch.object(
                reg, "release_email", return_value=True
            ) as release, patch.object(
                reg, "_start_email_submit_probe"
            ), patch.object(
                reg, "_stop_email_submit_probe"
            ), patch.object(reg, "dump_state"), patch.object(
                reg, "take_screenshot"
            ):
                result = reg.fill_email_and_submit(timeout=1, on_created=created)
            self.assertEqual(result, ("second@baldur.edu.kg", "second-token"))
            release.assert_called_once_with(
                "first-token",
                log_callback=None,
            )
            self.assertEqual(created.call_count, 2)
            self.assertEqual(get_mail.call_count, 2)
            second_excluded = get_mail.call_args_list[1].kwargs[
                "duckmail_excluded_domains"
            ]
            self.assertIn("duckmail.sbs", second_excluded)
        finally:
            reg.config = previous

    def test_duckmail_accounts_expire_and_are_deleted_after_use(self):
        previous = dict(reg.config)
        reg.config = {
            "email_provider": "duckmail",
            "duckmail_api_key": "",
            "duckmail_expiry_seconds": 86400,
            "mail_cleanup_retries": 1,
        }
        try:
            with patch.object(reg, "pick_domain", return_value="public.duck.test"), patch.object(
                reg, "generate_username", return_value="temporary-user"
            ), patch.object(reg, "create_account") as create, patch.object(
                reg, "get_token", return_value="mailbox-token"
            ):
                email, token = reg.get_email_and_token()
            self.assertEqual(email, "temporary-user@public.duck.test")
            self.assertEqual(token, "mailbox-token")
            create.assert_called_once_with(
                email,
                unittest.mock.ANY,
                api_key="",
                expires_in=86400,
            )

            me = unittest.mock.Mock(status_code=200)
            me.json.return_value = {"id": "duck-account-id"}
            deleted = unittest.mock.Mock(status_code=204)
            with patch.object(reg, "http_get", return_value=me) as get, patch.object(
                reg, "http_delete", return_value=deleted
            ) as delete:
                self.assertTrue(reg.release_email(token))
            get.assert_called_once_with(
                f"{reg.DUCKMAIL_API_BASE}/me",
                headers={"Authorization": "Bearer mailbox-token"},
            )
            delete.assert_called_once_with(
                f"{reg.DUCKMAIL_API_BASE}/accounts/duck-account-id",
                headers={"Authorization": "Bearer mailbox-token"},
            )
        finally:
            reg.config = previous

    def test_moemail_is_available_to_browser_registration(self):
        previous = dict(reg.config)
        reg.config = {
            "email_provider": "moemail",
            "moemail_api_key": "test-key",
            "mail_cleanup_retries": 1,
        }
        cancel = lambda: False
        try:
            with patch.object(
                fast_mail,
                "create_mailbox",
                return_value=("browser@moemail.app", "mailbox-id"),
            ) as create, patch.object(
                fast_mail, "poll_code", return_value="ABC-123"
            ) as poll, patch.object(
                fast_mail, "delete_mailbox", return_value=True
            ) as delete:
                email, token = reg.get_email_and_token(
                    log_callback=lambda _msg: None,
                    cancel_callback=cancel,
                )
                code = reg.get_oai_code(
                    token,
                    email,
                    timeout=30,
                    poll_interval=0.1,
                    cancel_callback=cancel,
                )
                released = reg.release_email(token)
            self.assertEqual((email, token, code), ("browser@moemail.app", "mailbox-id", "ABC-123"))
            self.assertTrue(released)
            create.assert_called_once()
            poll.assert_called_once()
            delete.assert_called_once()

            reg.config["email_provider"] = "cloudmail"
            self.assertIsNone(reg.release_email("not-provider-owned"))
            reg.config["email_provider"] = "moemail"
            with patch.object(
                fast_mail,
                "create_mailbox",
                side_effect=FastRegistrationCancelled("stop"),
            ):
                with self.assertRaises(reg.RegistrationCancelled):
                    reg.get_email_and_token(cancel_callback=lambda: True)
        finally:
            reg.config = previous

    def test_browser_registration_releases_moemail_when_form_fails_early(self):
        previous = dict(reg.config)
        reg.config = {
            "email_provider": "moemail",
            "mail_retry_count": 1,
            "account_hard_timeout": 30,
        }

        def create_then_fail(*, on_created, **_kwargs):
            on_created("early@moemail.app", "early-mailbox-id")
            raise RuntimeError("email form failed")

        try:
            with patch.object(cli, "_ensure_browser"), patch.object(
                reg, "open_signup_page"
            ), patch.object(
                reg, "fill_email_and_submit", side_effect=create_then_fail
            ), patch.object(
                reg, "release_email", return_value=True
            ) as release, patch.object(
                reg, "restart_browser"
            ), patch("register_cli.traceback.print_exc"):
                result = cli.register_one(
                    1,
                    1,
                    1,
                    "unused.txt",
                    registration_mode="browser",
                )
            self.assertIsNone(result)
            release.assert_called_once_with(
                "early-mailbox-id",
                log_callback=unittest.mock.ANY,
            )
        finally:
            reg.config = previous

    def test_browser_registration_releases_moemail_after_code(self):
        previous = dict(reg.config)
        reg.config = {
            "email_provider": "moemail",
            "mail_retry_count": 1,
            "account_hard_timeout": 30,
        }
        try:
            with patch.object(cli, "_ensure_browser"), patch.object(
                reg, "open_signup_page"
            ), patch.object(
                reg,
                "fill_email_and_submit",
                return_value=("browser@moemail.app", "mailbox-id"),
            ), patch.object(
                reg, "fill_code_and_submit", return_value="ABC-123"
            ), patch.object(
                reg, "release_email", return_value=True
            ) as release, patch.object(
                reg, "fill_profile_and_submit", side_effect=RuntimeError("profile failed")
            ), patch.object(reg, "mark_error"), patch.object(
                reg, "restart_browser"
            ), patch("register_cli.traceback.print_exc"):
                result = cli.register_one(
                    1,
                    1,
                    1,
                    "unused.txt",
                    registration_mode="browser",
                )
            self.assertIsNone(result)
            release.assert_called_once_with(
                "mailbox-id",
                log_callback=unittest.mock.ANY,
            )
        finally:
            reg.config = previous

    def test_registration_modes_and_auto_fallback(self):
        self.assertEqual(cli.resolve_registration_mode(None, {}), "browser")
        self.assertEqual(cli.resolve_registration_mode("protocol", {}), "fast")
        self.assertTrue(cli.requires_eager_browser_pool("browser"))
        self.assertFalse(cli.requires_eager_browser_pool("fast"))
        self.assertFalse(cli.requires_eager_browser_pool("auto"))
        with self.assertRaises(ValueError):
            cli.resolve_registration_mode("unknown", {})

        previous = dict(reg.config)
        reg.config = {"account_hard_timeout": 30}
        try:
            with patch.object(cli, "_run_fast_protocol", side_effect=RuntimeError("protocol changed")), patch.object(
                cli, "_register_one_browser", return_value={"registration_mode": "browser"}
            ) as browser:
                result = cli.register_one(1, 1, 1, "unused.txt", registration_mode="auto")
            self.assertEqual(result["registration_mode"], "browser")
            browser.assert_called_once()

            with patch.object(
                cli,
                "_run_fast_protocol",
                side_effect=FastRegistrationAmbiguous("unknown outcome"),
            ), patch.object(cli, "_register_one_browser") as browser:
                result = cli.register_one(
                    1, 1, 1, "unused.txt", registration_mode="auto"
                )
            self.assertTrue(result["terminal_failure"])
            browser.assert_not_called()
        finally:
            reg.config = previous

    def test_fast_result_uses_shared_private_ledger_and_mint_queue(self):
        previous = dict(reg.config)
        reg.config = {"cpa_mint_required": False}
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(reg, "mark_used"), patch.object(
                reg, "add_token_to_grok2api_pools"
            ):
                path = Path(tmp) / "accounts.txt"
                mint_queue = queue.Queue()
                result = cli._finalize_fast_registration(
                    1,
                    1,
                    str(path),
                    {
                        "email": "fast@example.com",
                        "password": "password-value",
                        "sso": "sso-value",
                        "profile": {"password": "password-value"},
                        "cookies": [],
                    },
                    reg.config,
                    do_mint_inline=False,
                    mint_queue=mint_queue,
                    cancel_event=None,
                    cancel=cli.CancelGuard(),
                )
                self.assertEqual(result["registration_mode"], "fast")
                self.assertEqual(mint_queue.get_nowait()["email"], "fast@example.com")
                self.assertIn("sso-value", path.read_text(encoding="utf-8"))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            reg.config = previous

    def test_fast_browser_keeps_sandbox_by_default(self):
        created = []

        class FakeOptions:
            def __init__(self):
                self.arguments = []
                self.extensions = []
                created.append(self)

            def auto_port(self): pass
            def set_timeouts(self, **_kwargs): pass
            def set_argument(self, value): self.arguments.append(value)
            def headless(self, _value): pass
            def set_browser_path(self, _value): pass
            def add_extension(self, value): self.extensions.append(value)
            def set_tmp_path(self, _value): pass

        class FakeChromium:
            def __init__(self, _options): self.latest_tab = object()

        fake_module = types.SimpleNamespace(
            Chromium=FakeChromium, ChromiumOptions=FakeOptions
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"DrissionPage": fake_module}
        ), patch.dict(os.environ, {"GROK_REG_DATA_DIR": tmp}):
            fast_tokens._create_browser(config={})
            fast_tokens._create_browser(config={"chromium_no_sandbox": True})
            _, _, proxy_tmp = fast_tokens._create_browser(
                proxy="http://proxy-user:proxy%20pass@127.0.0.1:8080",
                config={},
            )
            auth_dir = proxy_tmp / "proxy-auth"
            self.assertEqual(stat.S_IMODE(auth_dir.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((auth_dir / "background.js").stat().st_mode), 0o600
            )
            worker = (auth_dir / "background.js").read_text(encoding="utf-8")
            self.assertIn('username: "proxy-user"', worker)
            self.assertIn('password: "proxy pass"', worker)
        self.assertNotIn("--no-sandbox", created[0].arguments)
        self.assertIn("--no-sandbox", created[1].arguments)
        self.assertTrue(any(path.endswith("proxy-auth") for path in created[2].extensions))

    def test_fast_turnstile_uses_real_cdp_pointer_events(self):
        page = unittest.mock.Mock()
        page.run_js.return_value = {"x": 10, "y": 20, "width": 100, "height": 60}
        self.assertTrue(fast_register._click_turnstile(page, lambda _msg: None))
        self.assertEqual(page.run_cdp.call_count, 3)
        self.assertEqual(
            [call.kwargs["type"] for call in page.run_cdp.call_args_list],
            ["mouseMoved", "mousePressed", "mouseReleased"],
        )

    def test_moemail_creation_cancellation_deletes_created_mailbox(self):
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {
            "email": "cancelled@moemail.app",
            "id": "cancelled-mailbox-id",
        }
        cancel = unittest.mock.Mock(side_effect=[False, True])
        with patch.object(fast_mail.requests, "post", return_value=response), patch.object(
            fast_mail, "delete_mailbox", return_value=True
        ) as delete:
            with self.assertRaises(FastRegistrationCancelled):
                fast_mail.create_mailbox(
                    {"moemail_api_key": "test-key"},
                    cancel_callback=cancel,
                )
        delete.assert_called_once()

    def test_pure_registration_can_cancel_before_network(self):
        with patch("pure_api.register.make_session") as make_session:
            with self.assertRaises(FastRegistrationCancelled):
                pure_register_one({}, cancel_callback=lambda: True)
        make_session.assert_not_called()

    def test_fast_client_requires_live_next_action_before_create_request(self):
        session = unittest.mock.Mock()
        client = AuthClient(session, bootstrap_html="<html></html>")
        with self.assertRaisesRegex(RuntimeError, "Next-Action"):
            client.create_user(
                email="user@example.com",
                code="ABC-123",
                password="password-value",
                given_name="Given",
                family_name="Family",
                turnstile_token="t" * 100,
                castle_token="c" * 100,
            )
        session.post.assert_not_called()

    def test_fast_rpc_rejects_non_success_http_without_retaining_raw_body(self):
        response = unittest.mock.Mock(status_code=407, content=b"proxy auth required")
        session = unittest.mock.Mock()
        session.post.return_value = response
        client = AuthClient(session)
        with self.assertRaisesRegex(RuntimeError, "HTTP 407"):
            client._rpc("https://accounts.x.ai/test", b"request")

        response.status_code = 200
        response.content = b""
        parsed = client._rpc("https://accounts.x.ai/test", b"request")
        self.assertNotIn("raw", parsed)

    def test_moemail_limit_does_not_purge_existing_mailboxes(self):
        response = unittest.mock.Mock(status_code=403, text="limit")
        with patch.object(fast_mail.requests, "post", return_value=response), patch.object(
            fast_mail, "delete_mailbox"
        ) as delete:
            with self.assertRaisesRegex(RuntimeError, "不会自动删除"):
                fast_mail.create_mailbox(
                    {"moemail_api_key": "key", "moemail_api_base": "https://mail.test"}
                )
        delete.assert_not_called()

    def test_proxy_failure_is_fail_closed_by_default(self):
        previous = dict(reg.config)
        reg.config = {"proxy": "http://127.0.0.1:7890", "allow_direct_fallback": False}
        try:
            with patch.object(reg.requests, "get", side_effect=RuntimeError("Could not connect to server")) as get:
                with self.assertRaises(RuntimeError):
                    reg.http_get("https://example.test")
            self.assertEqual(get.call_count, 1)
            with patch.object(
                reg.requests,
                "delete",
                side_effect=RuntimeError("Could not connect to server"),
            ) as delete:
                with self.assertRaises(RuntimeError):
                    reg.http_delete("https://example.test/resource")
            self.assertEqual(delete.call_count, 1)
        finally:
            reg.config = previous


if __name__ == "__main__":
    unittest.main()
