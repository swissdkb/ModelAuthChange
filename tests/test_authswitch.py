from __future__ import annotations

from contextlib import contextmanager
import base64
import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import unittest
import uuid
from unittest.mock import patch

from authswitch.cli import list_accounts, reset_text, window_text, main, switch_from_list, show_usage, color_enabled
from authswitch.manager import Manager, find_account, fingerprint, upsert
from authswitch.providers import codex
from authswitch.rpc import CodexRPC, worker_env
from authswitch.security import AppError, DPAPI, atomic_write, json_bytes
from authswitch.vault import Vault


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + payload + ".TEST_ONLY"


def auth(who: str, revision="1", pending=False):
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"access_token": "" if pending else jwt(sub=who, exp=4102444800),
                       "id_token": jwt(sub=who, email=who + "@example.test"),
                       "refresh_token": "TEST-REFRESH-" + who + "-" + revision,
                       "account_id": "workspace-" + who},
            "last_refresh": "2026-10-19T00:00:00Z"}


class TestCipher:
    """Never used by production. Real Windows DPAPI has its own round-trip test."""
    def encrypt(self, data):
        return b"TEST-ONLY:" + base64.b64encode(data)

    def decrypt(self, data):
        return base64.b64decode(data.removeprefix(b"TEST-ONLY:"))


class Fixture(unittest.TestCase):
    def setUp(self):
        sandbox = Path(__file__).resolve().parents[1] / ".test-tmp"
        sandbox.mkdir(exist_ok=True)
        self.root = sandbox / ("case-" + uuid.uuid4().hex)
        self.root.mkdir()
        def cleanup():
            if self.root.resolve().parent != sandbox.resolve() or self.root.is_symlink():
                raise AssertionError("Unexpected test path")
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        # Keep the sandbox's inherited ACLs for fixture directories. Production
        # owner-only ACL changes cannot be exercised inside a restricted token.
        def fixture_directory(path):
            path.mkdir(parents=True, exist_ok=True)
        for target in ("authswitch.vault.private_directory", "authswitch.rpc.private_directory"):
            patcher = patch(target, fixture_directory)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.vault = Vault(self.root / "vault", TestCipher())
        self.manager = Manager(self.vault, self.home, idle_check=lambda: None)

    def seed(self, name, credential):
        with self.vault.locked():
            doc = self.vault.read()
            result = upsert(doc, name, credential, "test")
            self.vault.write(doc)
            return result

    def live(self, credential):
        atomic_write(self.home / "auth.json", json_bytes(credential))


class OrderingTests(Fixture):
    def test_named_order_persists_and_preserves_credentials_and_current_login(self):
        self.seed("backup", auth("b"))
        self.seed("main", auth("a"))
        self.seed("other", auth("c"))
        self.live(auth("b", "live"))
        before_login = (self.home / "auth.json").read_bytes()
        before_accounts = {a["id"]: a for a in self.vault.read()["accounts"]}
        self.manager.reorder(["main", "backup"])
        saved = self.vault.read()
        self.assertEqual([a["name"] for a in saved["accounts"]], ["main", "backup", "other"])
        self.assertEqual({a["id"]: a for a in saved["accounts"]}, before_accounts)
        self.assertEqual(find_account(saved, "1")["name"], "main")
        self.assertEqual((self.home / "auth.json").read_bytes(), before_login)

    def test_numbers_resolve_against_original_order(self):
        for name, who in (("main", "a"), ("backup", "b"), ("other", "c")):
            self.seed(name, auth(who))
        self.manager.reorder(["2", "1"])
        self.assertEqual([a["name"] for a in self.vault.read()["accounts"]], ["backup", "main", "other"])

    def test_duplicate_unknown_ambiguous_or_empty_order_never_writes_vault(self):
        self.seed("main", auth("a"))
        self.seed("max", auth("b"))
        before = self.vault.path.read_bytes()
        for selectors in (["main", "1"], ["main", "main"], ["missing"], ["m"], []):
            with self.assertRaises(AppError):
                self.manager.reorder(selectors)
            self.assertEqual(self.vault.path.read_bytes(), before)

    def test_partial_order_moves_only_selected_account_to_front(self):
        for name, who in (("first", "a"), ("second", "b"), ("third", "c"), ("fourth", "d")):
            self.seed(name, auth(who))
        self.manager.reorder(["third"])
        self.assertEqual([a["name"] for a in self.vault.read()["accounts"]], ["third", "first", "second", "fourth"])


class SwitchingTests(Fixture):
    def test_unique_alias_prefix_switches_case_insensitively(self):
        self.seed("main", auth("a"))
        self.seed("backup", auth("b"))
        self.live(auth("b"))
        self.manager.switch("M")
        self.assertEqual(codex.read_auth(self.home), auth("a"))

    def test_ambiguous_prefix_lists_candidates_without_switching(self):
        self.seed("main", auth("a"))
        self.seed("max", auth("b"))
        self.live(auth("a"))
        before = (self.home / "auth.json").read_bytes(), self.vault.path.read_bytes()
        with self.assertRaisesRegex(AppError, "main.*max"):
            self.manager.switch("m")
        self.assertEqual(before, ((self.home / "auth.json").read_bytes(), self.vault.path.read_bytes()))

    def test_exact_alias_takes_priority_over_longer_prefix_match(self):
        account = self.seed("main", auth("a"))
        self.seed("main-work", auth("b"))
        self.assertEqual(find_account(self.vault.read(), "main")["id"], account["id"])

    def test_short_numeric_selector_switches_account(self):
        self.seed("A", auth("a"))
        self.seed("B", auth("b"))
        self.live(auth("a"))
        self.manager.switch("2")
        self.assertEqual(codex.read_auth(self.home), auth("b"))

    def test_numeric_name_takes_priority_over_index(self):
        first = self.seed("2", auth("a"))
        self.seed("B", auth("b"))
        self.assertEqual(find_account(self.vault.read(), "2")["id"], first["id"])

    def test_out_of_range_numeric_selector_does_not_change_auth(self):
        self.seed("A", auth("a"))
        self.live(auth("a"))
        before = (self.home / "auth.json").read_bytes()
        for value in ("0", "99", "-1"):
            with self.assertRaises(AppError):
                self.manager.switch(value)
        self.assertEqual((self.home / "auth.json").read_bytes(), before)

    def test_list_selection_switches_the_displayed_account_by_id(self):
        self.seed("A", auth("a"))
        target = self.seed("B", auth("b"))
        self.live(auth("a"))
        with patch("builtins.input", side_effect=["bad", "9", "2"]), \
             patch("sys.stdout", new_callable=io.StringIO) as out, \
             patch.object(self.manager, "switch", wraps=self.manager.switch) as switch:
            self.assertEqual(switch_from_list(self.manager), 0)
        switch.assert_called_once_with(target["id"])
        self.assertEqual(codex.read_auth(self.home), auth("b"))
        self.assertIn("1. A", out.getvalue())
        self.assertIn("2. B", out.getvalue())

    def test_list_cancel_keeps_live_auth_unchanged(self):
        self.seed("A", auth("a"))
        self.live(auth("a"))
        before = (self.home / "auth.json").read_bytes()
        with patch("builtins.input", return_value=""), \
             patch("sys.stdout", new_callable=io.StringIO), \
             patch.object(self.manager, "switch") as switch:
            self.assertEqual(switch_from_list(self.manager), 0)
        switch.assert_not_called()
        self.assertEqual((self.home / "auth.json").read_bytes(), before)

    def test_list_selection_still_checks_running_clients(self):
        self.seed("B", auth("b"))
        self.live(auth("a"))
        before = (self.home / "auth.json").read_bytes()
        def blocked():
            raise AppError("running")
        self.manager.idle_check = blocked
        with patch("builtins.input", return_value="1"), \
             patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(AppError, "running"):
                switch_from_list(self.manager)
        self.assertEqual((self.home / "auth.json").read_bytes(), before)

    def test_switch_saves_rotated_outgoing_and_preserves_sessions_settings(self):
        self.seed("A", auth("a", "old"))
        self.seed("B", auth("b"))
        latest = auth("a", "rotated")
        self.live(latest)
        (self.home / "sessions").mkdir()
        transcript = self.home / "sessions/session.jsonl"
        transcript.write_bytes(b'conversation-history\n')
        config = self.home / "config.toml"
        config.write_text('model = "test-model"\n', encoding="utf-8")
        self.manager.switch("B")
        self.assertEqual(codex.read_auth(self.home), auth("b"))
        self.assertEqual(find_account(self.vault.read(), "A")["auth"], latest)
        self.assertEqual(transcript.read_bytes(), b'conversation-history\n')
        self.assertEqual(config.read_text(), 'model = "test-model"\n')
        self.manager.switch("A")
        self.assertEqual(codex.read_auth(self.home), latest)

    def test_unknown_outgoing_account_is_automatically_backed_up(self):
        self.seed("B", auth("b"))
        self.live(auth("unknown"))
        self.manager.switch("B")
        doc = self.vault.read()
        self.assertEqual(len(doc["accounts"]), 2)
        self.assertEqual(json.loads(doc["recovery"]["before"]), auth("unknown"))

    def test_running_client_blocks_before_any_write(self):
        self.seed("B", auth("b"))
        self.live(auth("a"))
        before = (self.home / "auth.json").read_bytes(), self.vault.path.read_bytes()
        def blocked():
            raise AppError("running")
        self.manager.idle_check = blocked
        with self.assertRaises(AppError):
            self.manager.switch("B")
        self.assertEqual(before, ((self.home / "auth.json").read_bytes(), self.vault.path.read_bytes()))

    def test_concurrent_auth_change_is_not_overwritten(self):
        self.seed("B", auth("b"))
        self.live(auth("a"))
        original = self.manager._prepared_auth
        def changed(account, doc):
            result = original(account, doc)
            self.live(auth("a", "concurrent"))
            return result
        with patch.object(self.manager, "_prepared_auth", changed):
            with self.assertRaisesRegex(AppError, "认证已变化"):
                self.manager.switch("B")
        self.assertEqual(codex.read_auth(self.home), auth("a", "concurrent"))

    def test_failed_replace_keeps_old_auth_and_encrypted_recovery(self):
        self.seed("B", auth("b"))
        self.live(auth("a"))
        before = (self.home / "auth.json").read_bytes()
        with patch("authswitch.manager.atomic_write", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                self.manager.switch("B")
        self.assertEqual((self.home / "auth.json").read_bytes(), before)
        self.assertEqual(json.loads(self.vault.read()["recovery"]["before"]), auth("a"))

    def test_same_account_switch_retains_latest_live_credential(self):
        self.seed("A", auth("a", "old"))
        self.live(auth("a", "new"))
        self.manager.switch("A")
        self.assertEqual(find_account(self.vault.read(), "A")["auth"], auth("a", "new"))

    def test_pending_new_login_does_not_get_overwritten_by_old_live_auth(self):
        self.live(auth("a", "old"))
        self.seed("A", auth("a", "new"))
        with self.vault.locked():
            doc = self.vault.read()
            find_account(doc, "A")["superseded_live_hash"] = fingerprint(json_bytes(auth("a", "old")))
            self.vault.write(doc)
        with self.assertRaisesRegex(AppError, "更新的登录"):
            self.manager.snapshot("A")
        self.manager.switch("A")
        self.assertEqual(codex.read_auth(self.home), auth("a", "new"))

    def test_keyring_is_rejected_without_changing_config(self):
        self.seed("B", auth("b"))
        config = self.home / "config.toml"
        config.write_text('cli_auth_credentials_store = "keyring"\n')
        with self.assertRaisesRegex(AppError, "只支持 file"):
            self.manager.switch("B")
        self.assertFalse((self.home / "auth.json").exists())


class ImportTests(Fixture):
    def source(self, values):
        path = self.root / "ccswitch.json"
        path.write_bytes(json_bytes({"version": 1, "accounts": values}))
        return path

    def entry(self, who):
        a = auth(who)
        return {"account_id": "ccswitch-internal-" + who,
                "chatgpt_account_id": a["tokens"]["account_id"],
                "email": who + "@example.test", "id_token": a["tokens"]["id_token"],
                "refresh_token": a["tokens"]["refresh_token"], "token_updated_at_ms": 1792368000000}

    def test_import_two_refresh_only_accounts_without_touching_source_or_live(self):
        self.live(auth("a"))
        source = self.source({"a": self.entry("a"), "b": self.entry("b")})
        before = source.read_bytes(), (self.home / "auth.json").read_bytes()
        self.assertEqual(self.manager.import_ccswitch(source), (2, 0))
        self.assertEqual(before, (source.read_bytes(), (self.home / "auth.json").read_bytes()))
        self.assertEqual(len(self.vault.read()["accounts"]), 2)
        self.assertFalse(self.vault.read()["accounts"][0]["auth"]["tokens"]["access_token"])

    def test_repeated_import_never_overwrites_rotated_refresh_tokens(self):
        source = self.source({"a": self.entry("a")})
        self.manager.import_ccswitch(source)
        self.seed("a@example.test", auth("a", "new"))
        self.assertEqual(self.manager.import_ccswitch(source), (0, 1))
        self.assertEqual(self.vault.read()["accounts"][0]["auth"], auth("a", "new"))

    def test_invalid_entry_aborts_entire_import(self):
        source = self.source({"a": self.entry("a"), "bad": {"refresh_token": "TEST"}})
        with self.assertRaises(AppError):
            self.manager.import_ccswitch(source)
        self.assertFalse(self.vault.path.exists())

    def test_dry_run_does_not_create_vault(self):
        source = self.source({"a": self.entry("a")})
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--data-dir", str(self.vault.root), "--codex-home", str(self.home),
                                   "import-ccswitch", "--source", str(source), "--dry-run"]), 0)
        self.assertFalse(self.vault.root.exists())


class UsageTests(Fixture):
    def test_list_refresh_queries_each_account_once_and_retains_failed_cache(self):
        first = self.seed("main", auth("a"))
        second = self.seed("backup", auth("b"))
        with self.vault.locked():
            doc = self.vault.read()
            find_account(doc, "backup")["usage"] = {"payload": {"old": 1}}
            self.vault.write(doc)
        def usage(account_id):
            if account_id == second["id"]:
                raise AppError("test failure before cache write")
            with self.vault.locked():
                doc = self.vault.read()
                find_account(doc, account_id)["usage"] = {"payload": {"fresh": 1}}
                self.vault.write(doc)
        with patch.object(self.manager, "usage", side_effect=usage) as query, \
             patch("sys.stdout", new_callable=io.StringIO) as out, \
             patch("sys.stderr", new_callable=io.StringIO):
            list_accounts(self.manager, as_json=True, refresh=True)
        self.assertEqual([c.args[0] for c in query.call_args_list], [first["id"], second["id"]])
        items = json.loads(out.getvalue())["accounts"]
        self.assertEqual(items[0]["usage"]["payload"], {"fresh": 1})
        self.assertEqual(items[1]["usage"]["payload"], {"old": 1})
        self.assertIn("test failure", items[1]["usage"]["error"])

    def test_list_command_refreshes_by_default_but_cached_skips_queries(self):
        self.seed("main", auth("a"))
        args = ["--data-dir", str(self.vault.root), "--codex-home", str(self.home), "list", "--json"]
        with patch("authswitch.cli.Manager", return_value=self.manager), \
             patch.object(self.manager, "usage", return_value={}) as query, \
             patch("sys.stdout", new_callable=io.StringIO), \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(args), 0)
            self.assertEqual(query.call_count, 1)
            query.reset_mock()
            self.assertEqual(main(args + ["--cached"]), 0)
            query.assert_not_called()

    def fake_rpc(self, calls, mutate=False, fail_limits=False):
        class Fake:
            def __init__(this, binary, home):
                this.home = home
            def __enter__(this):
                return this
            def __exit__(this, *_):
                pass
            def call(this, method, params=None):
                calls.append((method, params))
                if method == "account/read" and mutate:
                    atomic_write(this.home / "auth.json", json_bytes(auth("b", "rotated")))
                if method == "account/rateLimits/read":
                    if fail_limits:
                        raise AppError("offline")
                    return {"rateLimits": {"primary": {"usedPercent": 25,
                            "windowDurationMins": 300, "resetsAt": 1792400000}}}
                return {}
        return Fake

    def test_active_usage_uses_external_token_and_never_writes_live_auth(self):
        self.seed("A", auth("a"))
        self.live(auth("a", "new"))
        before = (self.home / "auth.json").read_bytes()
        calls = []
        with patch("authswitch.manager.codex.find_binary", return_value="test.exe"), \
             patch("authswitch.manager.CodexRPC", self.fake_rpc(calls)):
            result = self.manager.usage("A")
        self.assertEqual((self.home / "auth.json").read_bytes(), before)
        self.assertEqual(calls[0][0], "account/login/start")
        self.assertEqual(calls[0][1]["type"], "chatgptAuthTokens")
        self.assertIsNone(result["error"])
        self.assertEqual(list((self.vault.root / "jobs").iterdir()), [])

    def test_refreshed_credentials_saved_even_if_usage_fetch_fails(self):
        self.seed("B", auth("b", pending=True))
        self.live(auth("a"))
        calls = []
        with patch("authswitch.manager.codex.find_binary", return_value="test.exe"), \
             patch("authswitch.manager.CodexRPC", self.fake_rpc(calls, True, True)):
            with self.assertRaisesRegex(AppError, "offline"):
                self.manager.usage("B")
        saved = find_account(self.vault.read(), "B")
        self.assertEqual(saved["auth"], auth("b", "rotated"))
        self.assertEqual(saved["usage"]["error"], "offline")
        self.assertEqual(codex.read_auth(self.home), auth("a"))

    def test_failed_query_keeps_last_successful_cache(self):
        self.seed("A", auth("a"))
        self.live(auth("a"))
        with self.vault.locked():
            doc = self.vault.read()
            find_account(doc, "A")["usage"] = {"checked_at": "2026-10-18T00:00:00Z", "payload": {"old": 1}}
            self.vault.write(doc)
        with patch("authswitch.manager.codex.find_binary", return_value="test.exe"), \
             patch("authswitch.manager.CodexRPC", self.fake_rpc([], fail_limits=True)):
            with self.assertRaises(AppError):
                self.manager.usage("A")
        self.assertEqual(find_account(self.vault.read(), "A")["usage"]["payload"], {"old": 1})


class SecurityAndDisplayTests(Fixture):
    def test_installer_preserves_profiles_and_path_without_duplicates(self):
        if os.name != "nt":
            self.skipTest("PowerShell installation")
        shell = shutil.which("pwsh") or str(Path(os.environ["SystemRoot"]) /
                    "System32/WindowsPowerShell/v1.0/powershell.exe")
        module = str(Path(__file__).resolve().parents[1] / "setup/ProfileTools.psm1").replace("'", "''")
        command = (
            "$ErrorActionPreference='Stop'; "
            f"Import-Module '{module}' -Force; "
            "$original = \"# existing`r`nWrite-Output 'preserved'`r`n\"; "
            "$loader = \"E:\\Some Folder\\O'Neil\\enable-auth.ps1\"; "
            "$once = Get-AuthProfileContent $original $loader; "
            "$twice = Get-AuthProfileContent $once $loader; "
            "if ($once -cne $twice) { throw 'not idempotent' }; "
            "if (-not $once.StartsWith($original)) { throw 'profile overwritten' }; "
            "$null = [scriptblock]::Create($once); "
            "$updated = Get-AuthProfileContent $once 'E:\\Moved\\enable-auth.ps1'; "
            "if ($updated.Contains($loader)) { throw 'old path retained' }; "
            "$bad = $false; try { Get-AuthProfileContent '# >>> AuthSwitch managed >>>' $loader } catch { $bad=$true }; "
            "if (-not $bad) { throw 'malformed markers accepted' }; "
            "$path = 'C:\\existing;'; $bin = 'E:\\AuthSwitch\\bin'; "
            "$p = Get-AuthUserPath $path $bin; "
            "if ($p -cne ($path + $bin)) { throw 'path overwritten' }; "
            "if ((Get-AuthUserPath $p $bin.ToLower()) -cne $p) { throw 'duplicate path' }; "
            "Write-Output 'OK'"
        )
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "OK")

    def test_path_launcher_works_in_cmd_from_another_directory(self):
        if os.name != "nt":
            self.skipTest("Windows CMD launcher")
        project = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PATH"] = str(project / "bin") + os.pathsep + env["PATH"]
        env["AUTHSWITCH_HOME"] = str(self.root / "empty-vault")
        env["CODEX_HOME"] = str(self.home)
        result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", "auth list --json --cached"],
                                cwd=self.home, env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["accounts"], [])

    def test_powershell_shortcut_works_from_other_directory_and_completes_aliases(self):
        if os.name != "nt":
            self.skipTest("PowerShell shortcut")
        shell = shutil.which("pwsh") or str(Path(os.environ["SystemRoot"]) /
                    "System32/WindowsPowerShell/v1.0/powershell.exe")
        real = Vault(self.root / "completion-vault")
        with real.locked():
            doc = real.read()
            upsert(doc, "main", auth("a"), "test")
            upsert(doc, "max team", auth("b"), "test")
            real.write(doc)
        claude_vault = Vault(real.root / "claude")
        with claude_vault.locked():
            claude_vault.write({"version": 1, "accounts": [{"id": "claude-test",
                "name": "mobile", "service": "claude", "identity": {"key": "claude-test", "email": "c@example.test"},
                "auth": {"claudeAiOauth": {"accessToken": "TEST-ONLY"}}, "saved_at": "2026-10-19T00:00:00Z"}]})
        project = Path(__file__).resolve().parents[1]
        script = str(project / "enable-auth.ps1").replace("'", "''")
        command = (
            f". '{script}' 6>$null; "
            "$global:LASTEXITCODE = 37; "
            "$c = TabExpansion2 'auth use m' 10; "
            "$completionExit = $global:LASTEXITCODE; "
            "$items = @($c.CompletionMatches | ForEach-Object { $_.CompletionText }); "
            "$c2 = TabExpansion2 'auth use m claude' 10; "
            "$claudeItems = @($c2.CompletionMatches | ForEach-Object { $_.CompletionText }); "
            "$serviceLine = 'auth use main cl'; $c3 = TabExpansion2 $serviceLine $serviceLine.Length; "
            "$services = @($c3.CompletionMatches | ForEach-Object { $_.CompletionText }); "
            "$data = auth list --json --cached | ConvertFrom-Json; "
            "@{names=@($data.accounts | ForEach-Object { $_.name }); "
            "completion=$items; claudeCompletion=$claudeItems; services=$services; completionExit=$completionExit} | ConvertTo-Json -Compress"
        )
        env = os.environ.copy()
        env["AUTHSWITCH_HOME"] = str(real.root)
        env["CODEX_HOME"] = str(self.home)
        env["CLAUDE_CONFIG_DIR"] = str(self.home)
        env["PATH"] = str(project / "bin") + os.pathsep + env["PATH"]
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                cwd=self.home, env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["names"], ["main", "max team"])
        self.assertEqual(payload["completion"], ["'main'", "'max team'"])
        self.assertEqual(payload["claudeCompletion"], ["'mobile'"])
        self.assertEqual(payload["services"], ["claude"])
        self.assertEqual(payload["completionExit"], 37)

    def test_public_json_never_contains_auth_or_refresh_tokens(self):
        self.seed("A", auth("a"))
        self.live(auth("a"))
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            list_accounts(self.manager, True)
        result = out.getvalue()
        self.assertNotIn("TEST-REFRESH", result)
        self.assertNotIn("id_token", result)
        self.assertNotIn("access_token", result)
        self.assertEqual(json.loads(result)["accounts"][0]["current"], True)

    def test_windows_dpapi_real_roundtrip_and_encrypted_vault(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI")
        secret = b"TEST-SECRET-DO-NOT-STORE-IN-PLAINTEXT"
        encrypted = DPAPI().encrypt(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(DPAPI().decrypt(encrypted), secret)
        real = Vault(self.root / "real-vault")
        with real.locked():
            doc = real.read()
            upsert(doc, "A", auth("a"), "test")
            real.write(doc)
        self.assertNotIn(b"TEST-REFRESH", real.path.read_bytes())
        self.assertEqual(len(real.read()["accounts"]), 1)

    def test_invalid_vault_fails_closed(self):
        with self.vault.locked():
            self.vault.path.write_bytes(b"corrupted")
            with self.assertRaises(AppError):
                self.vault.read()

    def test_names_cannot_inject_terminal_controls(self):
        with self.assertRaises(AppError):
            self.seed("bad\x1b[31m", auth("a"))

    def test_compact_reset_uses_local_date_and_keeps_expired_state(self):
        at = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        self.assertEqual(reset_text(at.timestamp() + 7200, at), "14:00 reset")
        self.assertIn("待刷新", reset_text(at.timestamp() - 1, at))
        self.assertEqual(reset_text(None, at), "? reset")
        later = datetime.fromtimestamp(at.timestamp() + 7 * 86400).astimezone()
        self.assertEqual(reset_text(later.timestamp(), at), later.strftime("%m/%d %H:%M reset"))

    def test_compact_quota_colors_only_percentage_and_returns_to_white(self):
        at = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        for remaining, color in ((97, 32), (50, 32), (49, 33), (20, 33), (19, 31), (0, 31)):
            result = window_text({"usedPercent": 100 - remaining, "windowDurationMins": 300,
                                  "resetsAt": at.timestamp() + 7200}, color=True, at=at)
            self.assertEqual(result, f"5h \033[{color}m{remaining}%\033[37m (14:00 reset)")
        plain = window_text({"usedPercent": 3, "windowDurationMins": 10080}, color=False)
        self.assertEqual(plain, "7d 97% (? reset)")
        self.assertEqual(window_text({"usedPercent": None}, color=True), "? --% (? reset)")

    def test_usage_windows_share_one_line_and_cache_is_compact(self):
        usage = {"checked_at": "2026-10-19T01:00:00Z", "payload": {"rateLimits": {
            "primary": {"usedPercent": 3, "windowDurationMins": 300},
            "secondary": {"usedPercent": 85, "windowDurationMins": 10080}}}}
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            show_usage(usage)
        self.assertEqual(out.getvalue(), "  5h 97% (? reset) | 7d 15% (? reset)\n")
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            show_usage({**usage, "error": "offline"})
        self.assertIn("旧缓存", out.getvalue())
        self.assertIn("5h 97% (? reset) | 7d 15% (? reset)", out.getvalue())

    def test_color_disabled_for_redirection_and_no_color_environment(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(color_enabled())
        with patch.dict(os.environ, {"NO_COLOR": "1"}), \
             patch("sys.stdout.isatty", return_value=True):
            self.assertFalse(color_enabled())

    def test_usage_labels_and_reset_stay_white(self):
        usage = {"payload": {"rateLimits": {"primary": {
            "usedPercent": 3, "windowDurationMins": 300}}}}
        with patch("authswitch.cli.color_enabled", return_value=True), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            show_usage(usage)
        self.assertEqual(out.getvalue(), "  \033[37m5h \033[32m97%\033[37m (? reset)\033[0m\n")

    def test_worker_environment_does_not_inherit_alternate_auth(self):
        with patch.dict(os.environ, {"CODEX_HOME": "old", "CODEX_AUTH_JSON": "TESTSECRET",
                                     "OPENAI_API_KEY": "TESTSECRET", "PATH": "keep"}):
            env = worker_env(self.home)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_AUTH_JSON", env)
        self.assertEqual(env["CODEX_HOME"], str(self.home))
        self.assertEqual(env["PATH"], "keep")


if __name__ == "__main__":
    unittest.main()
