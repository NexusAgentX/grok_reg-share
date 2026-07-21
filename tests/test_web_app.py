from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_app


class WebAppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config_file = root / "config.json"
        self.accounts_file = root / "accounts_cli.txt"
        self.cpa_dir = root / "cpa_auths"
        self.cpa_dir.mkdir()
        self.patches = [
            patch.object(web_app, "CONFIG_FILE", self.config_file),
            patch.object(web_app, "ACCOUNTS_FILE", self.accounts_file),
            patch.object(web_app, "CPA_DIR", self.cpa_dir),
        ]
        for p in self.patches:
            p.start()
        web_app.app.config.update(TESTING=True)
        web_app._cancel_event.clear()
        with web_app._run_lock:
            web_app._running = False
        with web_app._mint_lock:
            web_app._minting_emails.clear()
        self.client = web_app.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def login(self):
        response = self.client.post("/login", data={"token": web_app.WEB_TOKEN})
        self.assertEqual(response.status_code, 302)

    def test_login_required_and_security_headers(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

        self.login()
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_config_secrets_are_masked_and_preserved(self):
        self.config_file.write_text(
            json.dumps({
                "proxy": "http://proxy-user:proxy-pass@127.0.0.1:7890",
                "cloudmail_password": "top-secret",
                "moemail_cookie": "cookie-secret",
            }),
            encoding="utf-8",
        )
        self.login()

        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["cloudmail_password"], web_app._SECRET_SENTINEL)
        self.assertEqual(body["proxy"], web_app._SECRET_SENTINEL)
        self.assertEqual(body["moemail_cookie"], web_app._SECRET_SENTINEL)
        self.assertNotIn("top-secret", response.get_data(as_text=True))
        self.assertNotIn("cookie-secret", response.get_data(as_text=True))

        body["proxy"] = "http://127.0.0.1:8888"
        response = self.client.post("/api/config", json=body)
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["cloudmail_password"], "top-secret")
        self.assertEqual(saved["moemail_cookie"], "cookie-secret")
        self.assertEqual(saved["proxy"], "http://127.0.0.1:8888")
        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)

    def test_first_save_preserves_masked_example_secrets(self):
        self.login()
        body = self.client.get("/api/config").get_json()
        self.assertEqual(body["proxy"], web_app._SECRET_SENTINEL)
        self.assertEqual(body["cpa_proxy"], web_app._SECRET_SENTINEL)

        response = self.client.post("/api/config", json=body)
        self.assertEqual(response.status_code, 200)
        saved = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(saved["cpa_proxy"], "http://127.0.0.1:7890")

    def test_cross_origin_mutation_is_rejected(self):
        self.login()
        response = self.client.post(
            "/api/config",
            json={"proxy": ""},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_account_list_omits_secrets_and_secret_endpoint_is_explicit(self):
        self.accounts_file.write_text(
            "user@example.com----password-value----sso-value\n",
            encoding="utf-8",
        )
        cpa_file = self.cpa_dir / "xai-user@example.com.json"
        cpa_file.write_text(json.dumps({"access_token": "access", "refresh_token": "refresh"}), encoding="utf-8")
        self.login()

        response = self.client.get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        account = response.get_json()[0]
        self.assertNotIn("password", account)
        self.assertNotIn("sso", account)
        self.assertNotIn("cpa_path", account)
        self.assertNotIn("password-value", response.get_data(as_text=True))

        response = self.client.post("/api/accounts/1/secret/password")
        self.assertEqual(response.get_json()["value"], "password-value")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/api/accounts/1/secret/password").status_code, 405)

        response = self.client.post("/api/accounts/1/secret/cpa")
        self.assertIn("refresh", response.get_json()["value"])

    def test_start_validates_and_passes_registration_mode(self):
        self.login()
        response = self.client.post(
            "/api/start",
            json={"extra": 1, "threads": 5, "registration_mode": "fast"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(web_app._is_running())

        with patch.object(web_app.threading, "Thread") as thread:
            response = self.client.post(
                "/api/start",
                json={"extra": 1, "threads": 2, "registration_mode": "auto"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["registration_mode"], "auto")
        self.assertEqual(thread.call_args.kwargs["args"], (1, 2, "auto"))
        thread.return_value.start.assert_called_once()
        with web_app._run_lock:
            web_app._running = False

    def test_invalid_start_does_not_leave_running_state(self):
        self.login()
        response = self.client.post("/api/start", json={"extra": 0, "threads": 1})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(web_app._is_running())


if __name__ == "__main__":
    unittest.main()
