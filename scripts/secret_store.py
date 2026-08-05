"""Provider-neutral secret storage with fail-closed platform backends."""

from __future__ import annotations

import ctypes
import getpass
import hmac
import importlib
import os
import re
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, MutableMapping, Protocol
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SECRET_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class SecretStoreError(RuntimeError):
    """Credential-free public failure from the secret boundary."""


class SecretStoreUnavailable(SecretStoreError):
    pass


class SecretAccessDenied(SecretStoreError):
    pass


class SecretNotFound(SecretStoreError):
    pass


class SecretValidationError(SecretStoreError):
    pass


class SecretOperationUnsupported(SecretStoreError):
    pass


@dataclass(frozen=True)
class SecretStoreCapabilities:
    backend: str
    implementation: str
    persistent: bool
    mutable: bool
    staged_rotation: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SecretStatus:
    backend: str
    secret_ref: str
    state: str
    available: bool
    account_match: bool
    persistent: bool
    mutable: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SecretOperationResult:
    operation: str
    backend: str
    secret_ref: str
    changed: bool
    verified: bool
    at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SecretBackendAdapter(Protocol):
    """Stable adapter contract for future external Secret Manager backends."""

    capabilities: SecretStoreCapabilities

    def check_available(self) -> None: ...

    def read(self, service: str, secret_ref: str) -> str | None: ...

    def write(self, service: str, secret_ref: str, value: str) -> None: ...

    def delete(self, service: str, secret_ref: str) -> None: ...


SecretVerifier = Callable[[str], bool | None]


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(backend: str, service: str, secret_ref: str) -> threading.RLock:
    identity = f"{backend}\0{service}\0{secret_ref}"
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(identity, threading.RLock())


def _timestamp() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="milliseconds")


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def validate_secret_ref(secret_ref: str) -> str:
    if not isinstance(secret_ref, str) or not SECRET_REF_PATTERN.fullmatch(secret_ref):
        raise SecretValidationError("secret_ref is invalid")
    if ".." in secret_ref or secret_ref.endswith(("/", ":")):
        raise SecretValidationError("secret_ref is invalid")
    return secret_ref


def validate_secret_value(value: str) -> None:
    if not isinstance(value, str) or not 8 <= len(value) <= 8192:
        raise SecretValidationError("secret value does not meet the configured format")
    if value != value.strip() or CONTROL_CHARACTERS.search(value):
        raise SecretValidationError("secret value does not meet the configured format")


class SecretStore:
    """High-level store contract shared by initialization, runtimes, and providers."""

    def __init__(
        self,
        adapter: SecretBackendAdapter,
        service: str,
        *,
        expected_account: str | None = None,
        current_account: str | None = None,
    ) -> None:
        if not isinstance(service, str) or not service.strip() or CONTROL_CHARACTERS.search(service):
            raise SecretValidationError("secret service name is invalid")
        self.adapter = adapter
        self.service = service.strip()
        self.expected_account = expected_account.strip() if isinstance(expected_account, str) else None
        self.current_account = current_account or getpass.getuser()

    @property
    def capabilities(self) -> SecretStoreCapabilities:
        return self.adapter.capabilities

    def check_access(self) -> SecretStoreCapabilities:
        """Check account and backend availability without reading a secret value."""
        self._ensure_access()
        return self.capabilities

    def _account_matches(self) -> bool:
        if not self.expected_account:
            return True
        expected = self.expected_account
        current = self.current_account
        if sys.platform == "win32":
            expected = expected.rsplit("\\", 1)[-1].casefold()
            current = current.rsplit("\\", 1)[-1].casefold()
        return _constant_time_equal(expected, current)

    def _ensure_access(self) -> None:
        if not self._account_matches():
            raise SecretAccessDenied("current process account does not match secret_management.access_account")
        self.adapter.check_available()

    def _read_optional(self, secret_ref: str) -> str | None:
        self._ensure_access()
        try:
            value = self.adapter.read(self.service, secret_ref)
        except SecretStoreError:
            raise
        except Exception as error:
            raise _adapter_error(error) from None
        if value is not None and not isinstance(value, str):
            raise SecretStoreUnavailable("secret backend returned an invalid value type")
        return value

    def status(self, secret_ref: str) -> SecretStatus:
        secret_ref = validate_secret_ref(secret_ref)
        capabilities = self.capabilities
        if not self._account_matches():
            return SecretStatus(
                capabilities.backend, secret_ref, "account_mismatch", False, False,
                capabilities.persistent, capabilities.mutable,
                "current process account does not match the configured secret owner",
            )
        try:
            value = self._read_optional(secret_ref)
        except SecretAccessDenied:
            state, reason = "access_denied", "secret backend denied access for the current account"
        except SecretStoreUnavailable:
            state, reason = "backend_unavailable", "secret backend is unavailable"
        else:
            state = "ready" if value is not None else "missing"
            reason = None if value is not None else "secret_ref is not initialized"
        return SecretStatus(
            capabilities.backend, secret_ref, state, state in {"ready", "missing"}, True,
            capabilities.persistent, capabilities.mutable, reason,
        )

    def get(self, secret_ref: str) -> str:
        secret_ref = validate_secret_ref(secret_ref)
        with _lock_for(self.capabilities.backend, self.service, secret_ref):
            value = self._read_optional(secret_ref)
            if value is None:
                raise SecretNotFound("secret_ref is not initialized")
            validate_secret_value(value)
            return value

    def set(
        self,
        secret_ref: str,
        value: str,
        *,
        verifier: SecretVerifier | None = None,
    ) -> SecretOperationResult:
        secret_ref = validate_secret_ref(secret_ref)
        with _lock_for(self.capabilities.backend, self.service, secret_ref):
            old_value = self._read_optional(secret_ref)
            if old_value is not None:
                raise SecretOperationUnsupported(
                    "secret_ref already exists; use rotate for replacement"
                )
            self._replace(secret_ref, value, old_value, verifier)
            return self._result("set", secret_ref, old_value != value, True)

    def verify(
        self,
        secret_ref: str,
        *,
        verifier: SecretVerifier | None = None,
    ) -> SecretOperationResult:
        secret_ref = validate_secret_ref(secret_ref)
        with _lock_for(self.capabilities.backend, self.service, secret_ref):
            value = self.get(secret_ref)
            self._run_verifier(value, verifier)
            return self._result("verify", secret_ref, False, True)

    def rotate(
        self,
        secret_ref: str,
        candidate: str,
        *,
        verifier: SecretVerifier | None = None,
    ) -> SecretOperationResult:
        secret_ref = validate_secret_ref(secret_ref)
        with _lock_for(self.capabilities.backend, self.service, secret_ref):
            old_value = self._read_optional(secret_ref)
            if old_value is None:
                raise SecretNotFound("secret_ref must be initialized before rotation")
            self._replace(secret_ref, candidate, old_value, verifier)
            return self._result("rotate", secret_ref, old_value != candidate, True)

    def delete(self, secret_ref: str) -> SecretOperationResult:
        secret_ref = validate_secret_ref(secret_ref)
        with _lock_for(self.capabilities.backend, self.service, secret_ref):
            existing = self._read_optional(secret_ref)
            if existing is None:
                raise SecretNotFound("secret_ref is not initialized")
            try:
                self.adapter.delete(self.service, secret_ref)
            except SecretStoreError:
                raise
            except Exception as error:
                raise _adapter_error(error) from None
            if self._read_optional(secret_ref) is not None:
                raise SecretStoreUnavailable("secret backend did not confirm deletion")
            return self._result("delete", secret_ref, True, False)

    def _replace(
        self,
        secret_ref: str,
        candidate: str,
        old_value: str | None,
        verifier: SecretVerifier | None,
    ) -> None:
        validate_secret_value(candidate)
        self._ensure_mutable()
        stage_ref = f"{secret_ref}.candidate.{uuid.uuid4().hex}"
        staged = self.capabilities.staged_rotation
        committed = False
        try:
            if staged:
                self.adapter.write(self.service, stage_ref, candidate)
                staged_value = self.adapter.read(self.service, stage_ref)
                if not isinstance(staged_value, str) or not _constant_time_equal(staged_value, candidate):
                    raise SecretValidationError("secret backend could not verify the staged candidate")
                validate_secret_value(staged_value)
                self._run_verifier(staged_value, verifier)
            else:
                self._run_verifier(candidate, verifier)
            self.adapter.write(self.service, secret_ref, candidate)
            committed = True
            stored = self.adapter.read(self.service, secret_ref)
            if not isinstance(stored, str) or not _constant_time_equal(stored, candidate):
                raise SecretValidationError("secret backend could not verify the committed value")
            validate_secret_value(stored)
        except SecretStoreError:
            if committed:
                self._restore(secret_ref, old_value)
            raise
        except Exception as error:
            if committed:
                self._restore(secret_ref, old_value)
            raise _adapter_error(error) from None
        finally:
            if staged:
                try:
                    self.adapter.delete(self.service, stage_ref)
                except Exception:
                    raise SecretStoreUnavailable(
                        "secret backend could not remove the staged candidate"
                    ) from None

    def _restore(self, secret_ref: str, old_value: str | None) -> None:
        try:
            if old_value is None:
                self.adapter.delete(self.service, secret_ref)
            else:
                self.adapter.write(self.service, secret_ref, old_value)
        except Exception as error:
            raise SecretStoreUnavailable("secret rotation rollback could not be confirmed") from None

    def _ensure_mutable(self) -> None:
        if not self.capabilities.mutable:
            raise SecretOperationUnsupported("secret backend is read-only")

    @staticmethod
    def _run_verifier(value: str, verifier: SecretVerifier | None) -> None:
        if verifier is None:
            return
        try:
            accepted = verifier(value)
        except SecretStoreError:
            raise
        except Exception:
            raise SecretValidationError("external secret validation failed") from None
        if accepted is False:
            raise SecretValidationError("external secret validation failed")

    def _result(
        self, operation: str, secret_ref: str, changed: bool, verified: bool
    ) -> SecretOperationResult:
        return SecretOperationResult(
            operation, self.capabilities.backend, secret_ref, changed, verified, _timestamp()
        )


class EnvironmentBackend:
    def __init__(self, environment: MutableMapping[str, str] | None = None) -> None:
        self.environment = environment if environment is not None else os.environ
        self.capabilities = SecretStoreCapabilities(
            "environment", "process_environment", False, True, False
        )

    def check_available(self) -> None:
        if not isinstance(self.environment, MutableMapping):
            raise SecretStoreUnavailable("environment backend is unavailable")

    def read(self, _service: str, secret_ref: str) -> str | None:
        return self.environment.get(secret_ref)

    def write(self, _service: str, secret_ref: str, value: str) -> None:
        self.environment[secret_ref] = value

    def delete(self, _service: str, secret_ref: str) -> None:
        self.environment.pop(secret_ref, None)


class PythonKeyringBackend:
    def __init__(self, keyring_module: Any, *, enforce_platform_backend: bool = True) -> None:
        self.keyring = keyring_module
        backend = keyring_module.get_keyring()
        implementation = f"{type(backend).__module__}.{type(backend).__name__}"
        self._enforce_platform_backend = enforce_platform_backend
        self.capabilities = SecretStoreCapabilities(
            "os_keyring", implementation, True, True, True
        )

    def check_available(self) -> None:
        backend = self.keyring.get_keyring()
        priority = getattr(backend, "priority", 0)
        if not isinstance(priority, (int, float)) or priority <= 0:
            raise SecretStoreUnavailable("operating-system keyring backend is unavailable")
        if type(backend).__module__.startswith("keyring.backends.fail"):
            raise SecretStoreUnavailable("operating-system keyring backend is unavailable")
        if self._enforce_platform_backend:
            implementation = f"{type(backend).__module__}.{type(backend).__name__}".casefold()
            if sys.platform == "darwin" and not any(
                marker in implementation for marker in ("macos", "keychain")
            ):
                raise SecretStoreUnavailable("macOS Keychain backend is unavailable")
            if sys.platform.startswith("linux") and not any(
                marker in implementation for marker in ("secretservice", "libsecret")
            ):
                raise SecretStoreUnavailable("Linux Secret Service backend is unavailable")

    def read(self, service: str, secret_ref: str) -> str | None:
        try:
            return self.keyring.get_password(service, secret_ref)
        except Exception as error:
            raise _adapter_error(error) from None

    def write(self, service: str, secret_ref: str, value: str) -> None:
        try:
            self.keyring.set_password(service, secret_ref, value)
        except Exception as error:
            raise _adapter_error(error) from None

    def delete(self, service: str, secret_ref: str) -> None:
        try:
            self.keyring.delete_password(service, secret_ref)
        except Exception as error:
            if type(error).__name__ not in {"PasswordDeleteError", "KeyError"}:
                raise _adapter_error(error) from None


class WindowsCredentialBackend:
    """Windows Credential Manager adapter without third-party dependencies."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretStoreUnavailable("Windows Credential Manager is unavailable")
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._credential_type = Credential
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self._advapi.CredWriteW.restype = wintypes.BOOL
        self._advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(Credential)),
        ]
        self._advapi.CredReadW.restype = wintypes.BOOL
        self._advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._advapi.CredDeleteW.restype = wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self.capabilities = SecretStoreCapabilities(
            "os_keyring", "windows_credential_manager", True, True, True
        )

    @staticmethod
    def _target(service: str, secret_ref: str) -> str:
        return f"{service}:{secret_ref}"

    def check_available(self) -> None:
        if not self._advapi:
            raise SecretStoreUnavailable("Windows Credential Manager is unavailable")

    def read(self, service: str, secret_ref: str) -> str | None:
        pointer = ctypes.POINTER(self._credential_type)()
        if not self._advapi.CredReadW(
            self._target(service, secret_ref), self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error_code = ctypes.get_last_error()
            if error_code == self.ERROR_NOT_FOUND:
                return None
            raise _windows_error(error_code)
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                raise SecretStoreUnavailable("stored credential is not valid UTF-8") from None
        finally:
            self._advapi.CredFree(pointer)

    def write(self, service: str, secret_ref: str, value: str) -> None:
        encoded = value.encode("utf-8")
        if len(encoded) > 512:
            raise SecretValidationError("secret value exceeds the Windows credential size limit")
        blob = ctypes.create_string_buffer(encoded)
        credential = self._credential_type()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = self._target(service, secret_ref)
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = secret_ref
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise _windows_error(ctypes.get_last_error())

    def delete(self, service: str, secret_ref: str) -> None:
        if not self._advapi.CredDeleteW(
            self._target(service, secret_ref), self.CRED_TYPE_GENERIC, 0
        ):
            error_code = ctypes.get_last_error()
            if error_code != self.ERROR_NOT_FOUND:
                raise _windows_error(error_code)


def _adapter_error(error: Exception) -> SecretStoreError:
    name = type(error).__name__
    if name in {"NoKeyringError", "InitError", "BackendNotAvailableError"}:
        return SecretStoreUnavailable("operating-system keyring backend is unavailable")
    if name in {"PermissionError", "AccessDenied", "KeyringLocked"}:
        return SecretAccessDenied("secret backend denied access for the current account")
    return SecretStoreUnavailable("secret backend operation failed")


def _windows_error(error_code: int) -> SecretStoreError:
    if error_code in {5, 1314}:
        return SecretAccessDenied("Windows Credential Manager denied access for the current account")
    if error_code in {1312, 1326}:
        return SecretStoreUnavailable("Windows logon session cannot access Credential Manager")
    return SecretStoreUnavailable("Windows Credential Manager operation failed")


def _os_keyring_backend(keyring_module: Any | None = None) -> SecretBackendAdapter:
    if keyring_module is not None:
        return PythonKeyringBackend(keyring_module, enforce_platform_backend=False)
    if sys.platform == "win32":
        return WindowsCredentialBackend()
    try:
        module = importlib.import_module("keyring")
    except ImportError:
        raise SecretStoreUnavailable(
            "the operating-system keyring adapter is not installed for this platform"
        ) from None
    return PythonKeyringBackend(module, enforce_platform_backend=True)


def create_secret_store(
    config: Mapping[str, Any],
    *,
    environment: MutableMapping[str, str] | None = None,
    keyring_module: Any | None = None,
    current_account: str | None = None,
) -> SecretStore:
    raw = config.get("secret_management")
    if not isinstance(raw, Mapping):
        raise SecretStoreUnavailable("secret_management configuration is missing")
    backend_name = raw.get("backend")
    service = raw.get("service")
    expected_account = raw.get("access_account")
    if not isinstance(service, str) or not service.strip():
        raise SecretValidationError("secret_management.service is invalid")
    if expected_account is not None and not isinstance(expected_account, str):
        raise SecretValidationError("secret_management.access_account is invalid")
    if backend_name == "os_keyring":
        adapter = _os_keyring_backend(keyring_module)
    elif backend_name == "environment":
        if environment is None:
            environment = os.environ
        adapter = EnvironmentBackend(environment)
    else:
        raise SecretOperationUnsupported(
            "configured external Secret Manager backend has no installed adapter"
        )
    return SecretStore(
        adapter, service, expected_account=expected_account, current_account=current_account
    )
