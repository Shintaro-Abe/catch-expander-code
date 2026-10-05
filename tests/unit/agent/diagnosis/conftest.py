"""実行診断役のテストの土台。

- AC-25: ソケットでの通信と認証情報を使えない状態にする（自動適用）。
  ``socket.socket`` そのものは差し替えない（``asyncio.run`` がイベントループの内部で
  ``socketpair`` を使うため。design.md §13）。
- AC-11: テストごとに、偽の DynamoDB・S3 が受けた操作が読み取りだけであることを確かめる（自動適用）。
"""

import socket

import pytest

from . import fakes

_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "EVENTS_TABLE",
    "PROMPTS_BUCKET",
    "DIAGNOSIS_MODEL",
    "EXECUTION_DIAGNOSIS",
)


class NetworkBlockedError(AssertionError):
    """テスト中に通信しようとしたときに上がる。"""


def _blocked(*_args, **_kwargs):
    raise NetworkBlockedError("network access is forbidden in diagnosis tests (AC-25)")


@pytest.fixture(autouse=True)
def _no_network_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(autouse=True)
def _read_only_fakes():
    """AC-11: このテストで作られた偽クライアントが受けた操作は、読み取りだけである。"""
    fakes.REGISTRY.clear()
    yield
    violations = [
        (fake.kind, op.name)
        for fake in fakes.REGISTRY
        for op in fake.operations
        if op.name not in fakes.READ_OPERATIONS
    ]
    fakes.REGISTRY.clear()
    assert violations == [], f"non-read operations reached the fake clients: {violations}"
