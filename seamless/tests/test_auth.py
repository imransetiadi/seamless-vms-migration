import hashlib

import pytest

from seamless_migrate.domain.enums import Role
from seamless_migrate.security.auth import (
    AuthConfigError,
    TokenStore,
    auth_disabled_allowed,
    generate_token,
    hash_token,
    is_loopback,
)


def test_generate_and_hash_token():
    token = generate_token()
    assert token.startswith("smg_") and len(token) >= 4 + 43  # 32 random bytes, url-safe
    assert generate_token() != token
    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()


def test_token_store_from_file(tmp_path):
    admin, viewer = generate_token(), generate_token()
    path = tmp_path / "tokens.yaml"
    path.write_text(
        "tokens:\n"
        f"  - {{name: alice, role: admin, sha256: {hash_token(admin)}}}\n"
        f"  - {{name: bob, role: viewer, sha256: {hash_token(viewer).upper()}}}\n"
    )
    store = TokenStore.from_file(path)
    assert store.authenticate(admin).name == "alice"
    assert store.authenticate(admin).role == Role.admin
    assert store.authenticate(viewer).role == Role.viewer  # hex case does not matter
    assert store.authenticate("smg_wrong") is None
    assert store.authenticate("") is None
    assert len(store) == 2

    entry = TokenStore.yaml_entry("carol", Role.approver, admin)
    assert "carol" in entry and hash_token(admin) in entry and admin not in entry


@pytest.mark.parametrize(
    "content",
    [
        "tokens:\n  - {name: x, role: superuser, sha256: " + "a" * 64 + "}\n",
        "tokens:\n  - {name: x, role: admin, sha256: nothex}\n",
        "tokens: nope\n",
        "tokens:\n  - {role: admin, sha256: " + "a" * 64 + "}\n",
        "tokens:\n  - {name: " + "n" * 129 + ", role: admin, sha256: " + "a" * 64 + "}\n",
    ],
)
def test_token_store_rejects_bad_files(tmp_path, content):
    path = tmp_path / "tokens.yaml"
    path.write_text(content)
    with pytest.raises(AuthConfigError):
        TokenStore.from_file(path)


def test_missing_tokens_file_is_an_error(tmp_path):
    with pytest.raises(AuthConfigError):
        TokenStore.from_file(tmp_path / "absent.yaml")


def test_auth_disabled_only_on_loopback():
    for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
        assert is_loopback(host) and auth_disabled_allowed(host), host
    for host in ("0.0.0.0", "::", "10.0.0.5", "192.168.1.10", "seamless.example.com", ""):
        assert not auth_disabled_allowed(host), host
