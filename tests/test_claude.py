from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from authswitch import cli
from authswitch.claude_manager import ClaudeManager
from authswitch.manager import find_account, fingerprint
from authswitch.providers import claude
from authswitch.security import AppError, atomic_write, json_bytes
from authswitch.vault import Vault
from tests.test_authswitch import Fixture, auth as codex_auth


def auth(who, revision="1"):
    return {"claudeAiOauth": {"accessToken": "TEST-ACCESS-" + who + revision,
                             "refreshToken": "TEST-REFRESH-" + who + revision,
                             "expiresAt": 4102444800000, "scopes": ["user:inference", "user:profile"],
                             "subscriptionType": "max", "rateLimitTier": "test"},
            "oauthAccount": {"accountUuid": who, "organizationUuid": "org-" + who,
                             "emailAddress": who + "@example.test", "displayName": who}}


class ClaudeFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.codex_manager = self.manager
        self.claude_home = self.root / "claude-home"
        self.claude_home.mkdir()
        self.config = self.root / ".claude.json"
        self.claude_vault = Vault(self.root / "vault/claude", self.vault.cipher)
        self.manager = ClaudeManager(self.claude_vault, self.claude_home, self.config, idle_check=lambda: None)

    def seed_claude(self, name, credential):
        with self.claude_vault.locked():
            doc = self.claude_vault.read()
            item = self.manager._store(doc, name, credential, "test")
            self.claude_vault.write(doc)
            return item

    def live_claude(self, credential):
        atomic_write(self.claude_home / ".credentials.json", json_bytes({
            "claudeAiOauth": credential["claudeAiOauth"], "mcpOAuth": {"keep": "untouched"}}))
        atomic_write(self.config, json_bytes({"oauthAccount": credential["oauthAccount"],
                                             "projects": {"test-project": {"keep": True}},
                                             "userID": "shared-local-id", "hasCompletedOnboarding": True}))


class ClaudeSwitchTests(ClaudeFixture):
    def test_snapshot_while_running_only_saves_subscription_not_mcp_or_projects(self):
        self.live_claude(auth("a"))
        before = (self.claude_home / ".credentials.json").read_bytes(), self.config.read_bytes()
        self.manager.idle_check = Mock(side_effect=AppError("running"))
        self.manager.snapshot("main")
        self.manager.idle_check.assert_not_called()
        self.assertEqual(self.claude_vault.read()["accounts"][0]["auth"], auth("a"))
        self.assertEqual(before, ((self.claude_home / ".credentials.json").read_bytes(), self.config.read_bytes()))

    def test_switch_saves_latest_tokens_preserves_metadata_and_transcripts(self):
        self.seed_claude("main", auth("a", "old"))
        self.seed_claude("backup", auth("b"))
        self.live_claude(auth("a", "latest"))
        projects = self.claude_home / "projects"
        projects.mkdir()
        transcript = projects / "session.jsonl"
        transcript.write_text("local shared history")
        self.manager.switch("b")
        self.assertEqual(self.manager.live()[0], auth("b"))
        saved = find_account(self.claude_vault.read(), "main")
        self.assertEqual(saved["auth"], auth("a", "latest"))
        self.assertEqual(json.loads(self.config.read_bytes())["projects"], {"test-project": {"keep": True}})
        self.assertEqual(json.loads(self.config.read_bytes())["userID"], "shared-local-id")
        self.assertEqual(json.loads((self.claude_home / ".credentials.json").read_bytes())["mcpOAuth"], {"keep": "untouched"})
        self.assertEqual(transcript.read_text(), "local shared history")
        self.manager.switch("main")
        self.assertEqual(self.manager.live()[0], auth("a", "latest"))

    def test_running_claude_blocks_switch_before_any_file_write(self):
        self.seed_claude("backup", auth("b"))
        self.live_claude(auth("a"))
        before = self.config.read_bytes(), self.claude_vault.path.read_bytes()
        self.manager.idle_check = Mock(side_effect=AppError("Claude running"))
        with self.assertRaisesRegex(AppError, "Claude running"):
            self.manager.switch("backup")
        self.assertEqual(before, (self.config.read_bytes(), self.claude_vault.path.read_bytes()))

    def test_second_file_failure_rolls_back_first_file_and_keeps_backup(self):
        self.seed_claude("backup", auth("b"))
        self.live_claude(auth("a"))
        # A BOM must survive rollback and the encrypted recovery record.
        original = b"\xef\xbb\xbf" + self.config.read_bytes()
        self.config.write_bytes(original)
        credentials = (self.claude_home / ".credentials.json").read_bytes()
        def write(path, data):
            if path.name == ".credentials.json":
                raise PermissionError("test failure")
            atomic_write(path, data)
        with patch("authswitch.claude_manager.atomic_write", side_effect=write):
            with self.assertRaises(PermissionError):
                self.manager.switch("backup")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual((self.claude_home / ".credentials.json").read_bytes(), credentials)
        recovery = self.claude_vault.read()["recovery"]["files"][0]
        self.assertEqual(base64.b64decode(recovery["before_base64"]), original)

    def test_external_change_between_two_writes_is_preserved(self):
        self.seed_claude("backup", auth("b"))
        self.live_claude(auth("a"))
        before_config = self.config.read_bytes()
        replacement_credentials = json_bytes({"claudeAiOauth": auth("c")["claudeAiOauth"]})
        def write(path, data):
            atomic_write(path, data)
            if path == self.config and data != before_config:
                atomic_write(self.claude_home / ".credentials.json", replacement_credentials)
        with patch("authswitch.claude_manager.atomic_write", side_effect=write):
            with self.assertRaisesRegex(AppError, "文件已变化"):
                self.manager.switch("backup")
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((self.claude_home / ".credentials.json").read_bytes(), replacement_credentials)

    def test_updated_login_applies_even_if_same_account_is_current(self):
        self.seed_claude("main", auth("a", "new"))
        self.live_claude(auth("a", "old"))
        with self.claude_vault.locked():
            doc = self.claude_vault.read()
            find_account(doc, "main")["superseded_live_hash"] = fingerprint(json_bytes(auth("a", "old")))
            self.claude_vault.write(doc)
        with self.assertRaisesRegex(AppError, "更新的登录"):
            self.manager.snapshot("main")
        self.manager.switch("main")
        self.assertEqual(self.manager.live()[0], auth("a", "new"))

    def test_codex_vault_is_unchanged_by_claude_operations(self):
        self.live(codex_auth("a"))
        self.codex_manager.snapshot("main")
        before = self.vault.path.read_bytes(), (self.home / "auth.json").read_bytes()
        self.live_claude(auth("a"))
        self.manager.snapshot("main")
        self.seed_claude("backup", auth("b"))
        self.manager.switch("backup")
        self.assertEqual(before, (self.vault.path.read_bytes(), (self.home / "auth.json").read_bytes()))
        self.assertEqual(self.vault.read()["accounts"][0]["service"], "codex")
        self.assertEqual(self.claude_vault.read()["accounts"][0]["service"], "claude")

    def test_login_runs_official_subscription_flow_in_isolation(self):
        self.live_claude(auth("a"))
        originals = self.config.read_bytes(), (self.claude_home / ".credentials.json").read_bytes()
        def official_login(command, *, cwd, env):
            self.assertEqual(command, ["test-claude.exe", "auth", "login", "--claudeai"])
            self.assertNotEqual(cwd, self.claude_home)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(cwd))
            self.assertEqual(env["USERPROFILE"], str(cwd))
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            atomic_write(cwd / ".credentials.json", json_bytes({"claudeAiOauth": auth("b")["claudeAiOauth"]}))
            atomic_write(cwd / ".claude.json", json_bytes({"oauthAccount": auth("b")["oauthAccount"]}))
            return Mock(returncode=0)
        with patch("authswitch.claude_manager.claude.find_binary", return_value="test-claude.exe"), \
             patch("authswitch.claude_manager.subprocess.run", side_effect=official_login), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "CLAUDE_CODE_OAUTH_TOKEN": "test"}):
            saved = self.manager.login("backup")
        self.assertEqual(saved["auth"], auth("b"))
        self.assertEqual(originals, (self.config.read_bytes(), (self.claude_home / ".credentials.json").read_bytes()))
        self.assertEqual(list((self.claude_vault.root / "jobs").iterdir()), [])

    def test_usage_read_does_not_write_live_credentials_or_refresh_tokens(self):
        self.seed_claude("main", auth("a", "old"))
        self.live_claude(auth("a", "latest"))
        before = self.config.read_bytes(), (self.claude_home / ".credentials.json").read_bytes()
        payload = {"rateLimitsByLimitId": {"claude": {"primary": {"usedPercent": 3}}}}
        with patch("authswitch.claude_manager.claude.query_usage", return_value=payload) as query:
            result = self.manager.usage("main")
        query.assert_called_once_with(auth("a", "latest"))
        self.assertIsNone(result["error"])
        self.assertEqual(before, (self.config.read_bytes(), (self.claude_home / ".credentials.json").read_bytes()))

    def test_failed_usage_keeps_previous_cache_and_marks_error(self):
        self.seed_claude("backup", auth("b"))
        with self.claude_vault.locked():
            doc = self.claude_vault.read()
            find_account(doc, "backup")["usage"] = {"payload": {"old": 1}, "checked_at": "2026-10-19T00:00:00Z"}
            self.claude_vault.write(doc)
        with patch("authswitch.claude_manager.claude.query_usage", side_effect=AppError("offline")):
            with self.assertRaisesRegex(AppError, "offline"):
                self.manager.usage("backup")
        cached = find_account(self.claude_vault.read(), "backup")["usage"]
        self.assertEqual(cached["payload"], {"old": 1})
        self.assertEqual(cached["error"], "offline")


class ClaudeProtocolTests(unittest.TestCase):
    def test_usage_normalizes_primary_weekly_and_model_specific_windows(self):
        result = claude.normalize_usage({"five_hour": {"utilization": 3, "resets_at": "2026-10-19T12:00:00Z"},
            "seven_day": {"utilization": 85, "resets_at": "2026-10-21T12:00:00+00:00"},
            "seven_day_sonnet": {"utilization": 12, "resets_at": None}, "extra_usage": {"used_credits": 2}})
        self.assertEqual(result["rateLimitsByLimitId"]["claude"]["primary"], {
            "usedPercent": 3, "windowDurationMins": 300,
            "resetsAt": datetime(2026, 10, 19, 12, tzinfo=timezone.utc).timestamp()})
        self.assertEqual(result["rateLimitsByLimitId"]["claude"]["secondary"]["usedPercent"], 85)
        self.assertEqual(result["rateLimitsByLimitId"]["seven_day_sonnet"]["limitName"], "sonnet")

    def test_expired_token_fails_before_network_and_without_refreshing(self):
        expired = auth("a")
        expired["claudeAiOauth"]["expiresAt"] = 1
        with patch("urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(AppError, "已过期"):
                claude.query_usage(expired)
        opener.assert_not_called()

    def test_query_uses_fixed_https_endpoint_and_disallows_redirects(self):
        response = Mock()
        response.read.return_value = json_bytes({"five_hour": {"utilization": 3}})
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch("urllib.request.build_opener", return_value=opener) as builder:
            claude.query_usage(auth("a"))
        self.assertIsInstance(builder.call_args.args[0], claude.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer TEST-ACCESS-a1")
        self.assertIsNone(claude.NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_http_errors_never_expose_response_token_material(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(claude.USAGE_URL, 429, "TEST-SECRET", {}, None)
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(AppError) as caught:
                claude.query_usage(auth("a"))
        self.assertNotIn("TEST-SECRET", str(caught.exception))
        self.assertIn("频繁", str(caught.exception))


class ServiceRoutingTests(ClaudeFixture):
    def test_trailing_service_and_help_are_parsed_before_any_auth_work(self):
        self.assertEqual(cli.service_args(["use", "main", "claude"]), ("claude", ["use", "main"]))
        self.assertEqual(cli.service_args(["list"]), ("codex", ["list"]))
        for command in ([], ["list"], ["use"], ["login"], ["refresh"]):
            outputs = []
            for suffix in ([], ["codex"], ["claude"]):
                with patch("authswitch.cli.Vault") as vault, patch("sys.stdout", new_callable=io.StringIO) as out:
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(command + ["--help"] + suffix)
                    self.assertEqual(caught.exception.code, 0)
                    outputs.append(out.getvalue())
                vault.assert_not_called()
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0], outputs[2])
            self.assertIn("codex", outputs[0])
            self.assertIn("claude", outputs[0])
            if command == ["login"]:
                self.assertIn("--device-auth", outputs[0])
                self.assertIn("仅 Codex", outputs[0])

    def test_claude_list_outputs_service_email_and_no_credentials(self):
        self.seed_claude("main", auth("a"))
        self.live_claude(auth("a"))
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.list_accounts(self.manager, as_json=True)
        result = json.loads(out.getvalue())
        self.assertEqual(result["service"], "claude")
        self.assertEqual(result["accounts"][0]["email"], "a@example.test")
        self.assertTrue(result["accounts"][0]["current"])
        self.assertNotIn("TEST-ACCESS", out.getvalue())
        self.assertNotIn("TEST-REFRESH", out.getvalue())

    def test_cli_keeps_codex_vault_location_and_selects_independent_claude_vault(self):
        with patch("authswitch.cli.ClaudeManager", return_value=self.manager) as manager, \
             patch("sys.stdout", new_callable=io.StringIO):
            cli.main(["--data-dir", str(self.vault.root), "--claude-home", str(self.claude_home),
                      "list", "--json", "--cached", "claude"])
        args = manager.call_args.args
        self.assertEqual(args[0].root, self.vault.root / "claude")
        self.assertEqual(args[2], self.claude_home / ".claude.json")


if __name__ == "__main__":
    unittest.main()
