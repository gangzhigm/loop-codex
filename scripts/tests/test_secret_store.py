from __future__ import annotations

# 中文排查：覆盖 SecretStore 校验、后端能力、账户隔离以及 secretctl 的人工命令分支。
# 失败时先区分输入错误、后端不可用、权限不足和密钥不存在四类异常。
# 全部值使用内存 FakeKeyring，禁止在测试中接触本机真实系统密钥库内容。

import argparse
import json
import threading
import unittest
from typing import Any
from unittest.mock import patch

from _bootstrap import REPOSITORY_ROOT

from loopdb import load_initialization_config
from loop_agent.secrets.store import (
    EnvironmentBackend,
    SecretAccessDenied,
    SecretNotFound,
    SecretOperationUnsupported,
    SecretStore,
    SecretStoreUnavailable,
    SecretValidationError,
    create_secret_store,
)
from roles.operator.secretctl import execute, parser


class FakeKeyringBackend:
    priority = 1


class FakeKeyring:
    def __init__(self) -> None:
        self.backend = FakeKeyringBackend()
        self.values: dict[tuple[str, str], str] = {}
        self.lock = threading.Lock()
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    def get_keyring(self) -> FakeKeyringBackend:
        return self.backend

    def get_password(self, service: str, secret_ref: str) -> str | None:
        if self.read_error:
            raise self.read_error
        with self.lock:
            return self.values.get((service, secret_ref))

    def set_password(self, service: str, secret_ref: str, value: str) -> None:
        if self.write_error:
            raise self.write_error
        with self.lock:
            self.values[(service, secret_ref)] = value

    def delete_password(self, service: str, secret_ref: str) -> None:
        with self.lock:
            self.values.pop((service, secret_ref), None)


class SecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = FakeKeyring()
        self.config: dict[str, Any] = {
            "secret_management": {
                "backend": "os_keyring",
                "service": "Loop Tests",
                "access_account": "test-account",
            },
            "deepseek": {"secret_ref": "DEEPSEEK_API_KEY"},
        }
        self.store = create_secret_store(
            self.config, keyring_module=self.keyring, current_account="test-account"
        )

    def test_set_get_status_verify_and_delete_never_return_secret_metadata(self) -> None:
        token = "test-only-secret-token"
        result = self.store.set("DEEPSEEK_API_KEY", token)
        self.assertTrue(result.changed)
        self.assertEqual(self.store.get("DEEPSEEK_API_KEY"), token)
        status = self.store.status("DEEPSEEK_API_KEY").as_dict()
        verification = self.store.verify("DEEPSEEK_API_KEY").as_dict()
        serialized = json.dumps([result.as_dict(), status, verification])
        self.assertEqual(status["state"], "ready")
        self.assertNotIn(token, serialized)
        self.assertNotIn(token[-4:], serialized)
        deleted = self.store.delete("DEEPSEEK_API_KEY")
        self.assertTrue(deleted.changed)
        self.assertEqual(self.store.status("DEEPSEEK_API_KEY").state, "missing")

    def test_missing_secret_and_invalid_values_fail_closed(self) -> None:
        with self.assertRaises(SecretNotFound):
            self.store.get("DEEPSEEK_API_KEY")
        for value in ("short", " leading-secret", "secret-token\n"):
            with self.subTest(value=repr(value)), self.assertRaises(SecretValidationError):
                self.store.set("DEEPSEEK_API_KEY", value)

    def test_set_cannot_replace_an_existing_value(self) -> None:
        self.store.set("DEEPSEEK_API_KEY", "original-test-secret")
        with self.assertRaises(SecretOperationUnsupported):
            self.store.set("DEEPSEEK_API_KEY", "replacement-test-secret")
        self.assertEqual(self.store.get("DEEPSEEK_API_KEY"), "original-test-secret")

    def test_utf8_secret_round_trip_uses_byte_safe_comparison(self) -> None:
        value = "测试-secret-value"
        self.store.set("UNICODE_SECRET", value)
        self.assertEqual(self.store.get("UNICODE_SECRET"), value)

    def test_account_mismatch_and_permission_error_are_sanitized(self) -> None:
        mismatch = create_secret_store(
            self.config, keyring_module=self.keyring, current_account="other-account"
        )
        self.assertEqual(mismatch.status("DEEPSEEK_API_KEY").state, "account_mismatch")
        with self.assertRaises(SecretAccessDenied):
            mismatch.get("DEEPSEEK_API_KEY")

        leaked = "test-only-secret-token"
        self.keyring.read_error = PermissionError(leaked)
        with self.assertRaises(SecretAccessDenied) as captured:
            self.store.get("DEEPSEEK_API_KEY")
        self.assertNotIn(leaked, str(captured.exception))

    def test_unavailable_keyring_is_reported_without_plaintext_fallback(self) -> None:
        self.keyring.backend.priority = 0
        status = self.store.status("DEEPSEEK_API_KEY")
        self.assertEqual(status.state, "backend_unavailable")
        with self.assertRaises(SecretStoreUnavailable):
            self.store.set("DEEPSEEK_API_KEY", "test-only-secret-token")

    def test_rotation_validates_staged_value_and_rolls_back_on_failure(self) -> None:
        old = "old-test-secret-token"
        candidate = "new-test-secret-token"
        self.store.set("DEEPSEEK_API_KEY", old)
        observed: list[str] = []
        result = self.store.rotate(
            "DEEPSEEK_API_KEY", candidate, verifier=lambda value: observed.append(value)
        )
        self.assertTrue(result.changed)
        self.assertEqual(observed, [candidate])
        self.assertEqual(self.store.get("DEEPSEEK_API_KEY"), candidate)
        self.assertFalse(any(".candidate." in ref for _, ref in self.keyring.values))

        rejected = "rejected-test-secret"
        with self.assertRaises(SecretValidationError) as captured:
            self.store.rotate(
                "DEEPSEEK_API_KEY",
                rejected,
                verifier=lambda _value: (_ for _ in ()).throw(RuntimeError(rejected)),
            )
        self.assertEqual(self.store.get("DEEPSEEK_API_KEY"), candidate)
        self.assertNotIn(rejected, str(captured.exception))
        self.assertFalse(any(".candidate." in ref for _, ref in self.keyring.values))

    def test_concurrent_store_instances_serialize_rotation(self) -> None:
        self.store.set("DEEPSEEK_API_KEY", "initial-test-secret")
        second = create_secret_store(
            self.config, keyring_module=self.keyring, current_account="test-account"
        )
        errors: list[Exception] = []

        def rotate(store: SecretStore, prefix: str) -> None:
            try:
                for index in range(10):
                    store.rotate("DEEPSEEK_API_KEY", f"{prefix}-test-secret-{index}")
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=rotate, args=(self.store, "first")),
            threading.Thread(target=rotate, args=(second, "second")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertRegex(self.store.get("DEEPSEEK_API_KEY"), r"^(first|second)-test-secret-9$")

    def test_environment_backend_is_explicit_and_process_only(self) -> None:
        config = {
            "secret_management": {"backend": "environment", "service": "Loop Tests"},
            "deepseek": {"secret_ref": "DEEPSEEK_API_KEY"},
        }
        environment = {"DEEPSEEK_API_KEY": "injected-test-secret"}
        store = create_secret_store(config, environment=environment)
        self.assertFalse(store.capabilities.persistent)
        self.assertEqual(store.get("DEEPSEEK_API_KEY"), "injected-test-secret")
        store.rotate("DEEPSEEK_API_KEY", "rotated-test-secret")
        self.assertEqual(environment["DEEPSEEK_API_KEY"], "rotated-test-secret")

    def test_unknown_external_backend_is_not_reported_as_implemented(self) -> None:
        config = {
            "secret_management": {"backend": "external_vault", "service": "Loop Tests"}
        }
        with self.assertRaises(SecretOperationUnsupported):
            create_secret_store(config)

    def test_cli_uses_hidden_input_and_emits_non_sensitive_audit(self) -> None:
        args = argparse.Namespace(command="set", provider="deepseek", connect=False)
        entries = iter(["cli-test-secret-token", "cli-test-secret-token"])
        result = execute(
            args,
            config=self.config,
            store=self.store,
            secret_input=lambda _prompt: next(entries),
        )
        encoded = json.dumps(result)
        self.assertEqual(result["outcome"], "COMPLETED")
        self.assertNotIn("cli-test-secret-token", encoded)
        self.assertNotIn("oken", encoded)
        option_strings = {
            option
            for action in parser()._actions
            for option in getattr(action, "option_strings", [])
        }
        self.assertNotIn("--value", option_strings)

    def test_cli_requires_confirmation_for_rotation_and_delete(self) -> None:
        self.store.set("DEEPSEEK_API_KEY", "old-cli-test-secret")
        rotate_args = argparse.Namespace(command="rotate", provider="deepseek", connect=False)
        with self.assertRaisesRegex(Exception, "confirmed"):
            execute(
                rotate_args,
                config=self.config,
                store=self.store,
                input_fn=lambda _prompt: "NO",
                secret_input=lambda _prompt: "unused-test-secret",
            )
        delete_args = argparse.Namespace(command="delete", provider="deepseek")
        with self.assertRaisesRegex(Exception, "confirmed"):
            execute(
                delete_args,
                config=self.config,
                store=self.store,
                input_fn=lambda _prompt: "NO",
            )
        self.assertEqual(self.store.get("DEEPSEEK_API_KEY"), "old-cli-test-secret")

    def test_cli_fails_before_hidden_input_when_backend_is_unavailable(self) -> None:
        self.keyring.backend.priority = 0
        args = argparse.Namespace(command="set", provider="deepseek", connect=False)
        with self.assertRaisesRegex(Exception, "not ready"):
            execute(
                args,
                config=self.config,
                store=self.store,
                secret_input=lambda _prompt: self.fail("hidden input must not be requested"),
            )

    def test_cli_connect_verification_requires_confirmation_and_uses_store_value(self) -> None:
        token = "connect-cli-test-secret"
        self.store.set("DEEPSEEK_API_KEY", token)
        args = argparse.Namespace(command="verify", provider="deepseek", connect=True)
        config = load_initialization_config()
        with patch(
            "roles.operator.secretctl.verify_deepseek_credential",
            return_value=True,
        ) as verifier:
            result = execute(
                args,
                config=config,
                store=self.store,
                input_fn=lambda _prompt: "CONNECT",
            )
        self.assertEqual(result["operation"], "verify")
        verifier.assert_called_once()
        self.assertEqual(verifier.call_args.args[0], token)
        self.assertNotIn(token, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
