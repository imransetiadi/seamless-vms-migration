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


@pytest.mark.parametrize(
    "name",
    ["system", " System ", "cli", "anonymous", "unauthenticated", "user:alice", "policy:pol-1"],
)
def test_token_store_refuses_reserved_actor_names_and_colons(tmp_path, name):
    """Audit B-10 (SDD §13.1): a hand-edited tokens file cannot name a token after an actor the
    control plane records itself, nor in the advisor's `kind:id` form - what `token create` and
    POST /users refuse, the file refuses too, so no token's audit events pass for theirs."""
    path = tmp_path / "tokens.yaml"
    path.write_text(f"tokens:\n  - {{name: '{name}', role: admin, sha256: {'a' * 64}}}\n")
    with pytest.raises(AuthConfigError, match="actor"):
        TokenStore.from_file(path)


@pytest.mark.parametrize(
    "escaped",
    [
        "sys\\0tem",
        "ops\\tlead",
        "ops\\nlead",
        "a\\x85b",
        "a\\u202eb",
        "a\\u200bb",
        "a\\u00a0b",
        "a\\ud800b",
    ],
)
def test_token_store_refuses_a_name_that_is_not_printable(tmp_path, escaped):
    """SDD §13.1 (review RV48-05): the file checked a name's length, the reserved actors and the
    colon, never its characters - a hand-edited "sys\\0tem" is not "system", so its audit events
    passed for the control plane's once the store stripped the NUL, and a name with a control or
    format character reads as another in the dashboard. A name that is not printable (Python's
    str.isprintable: a control or format character, a lone surrogate, a separator but the space)
    is refused at start-up."""
    path = tmp_path / "tokens.yaml"
    path.write_text(f'tokens:\n  - {{name: "{escaped}", role: admin, sha256: {"a" * 64}}}\n')
    with pytest.raises(AuthConfigError, match=r"^token #0: .* holds a character that is not "):
        TokenStore.from_file(path)


@pytest.mark.parametrize("name", ["Ops Lead", "Ñandú-ops", "运维", "ops 😀"])
def test_token_store_keeps_a_printable_name_whatever_its_script(tmp_path, name):
    """SDD §13.1: printable is all the file asks of a name's characters - a space, letters of any
    script and symbols load as they are."""
    path = tmp_path / "tokens.yaml"
    path.write_text(
        f'tokens:\n  - {{name: "{name}", role: admin, sha256: {"a" * 64}}}\n', encoding="utf-8"
    )
    assert [e.name for e in TokenStore.from_file(path).entries] == [name]


def test_missing_tokens_file_is_an_error(tmp_path):
    with pytest.raises(AuthConfigError):
        TokenStore.from_file(tmp_path / "absent.yaml")


def test_auth_disabled_only_on_loopback():
    for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
        assert is_loopback(host) and auth_disabled_allowed(host), host
    for host in ("0.0.0.0", "::", "10.0.0.5", "192.168.1.10", "seamless.example.com", ""):
        assert not auth_disabled_allowed(host), host
