import re
from pathlib import Path

import pytest

from seamless_migrate.config import Settings


def test_settings_defaults_match_sdd():
    s = Settings.from_env({})
    assert s.data_dir == Path("./data")
    assert s.db_url == f"sqlite:///{Path('./data') / 'seamless.db'}"
    assert (s.host, s.port) == ("127.0.0.1", 8080)
    assert (s.demo, s.demo_speed, s.demo_seed, s.demo_failure_rate) == (False, 60.0, 42, 0.1)
    assert (s.auth_disabled, s.tokens_file) == (False, None)
    assert s.cors_origins == []
    assert s.clouds_yaml is None
    assert s.secrets_dir == Path("/var/run/secrets/seamless")
    assert s.ansible_playbook == "ansible-playbook"
    assert (s.collection_root / "galaxy.yml").exists(), "defaults to the repository root"
    assert (s.max_concurrent_migrations, s.max_concurrent_cutovers) == (10, 3)
    assert (s.tick_s, s.max_step_retries) == (1.0, 2)
    assert s.jev_mode == "off"
    assert s.jev_command == "npx -y @jkudish/jev-mcp@0.14.1"
    assert (s.jev_url, s.jev_token) == (None, None)
    assert (s.jev_timeout_s, s.jev_min_confidence) == (20.0, 0.6)
    # ADV-06 (SDD §13.4): remote by default, so the console excerpt stays local by default
    assert (s.jev_deployment, s.jev_endpoint_host, s.console_screening_send_to_remote) == (
        "remote",
        None,
        False,
    )
    assert (s.memory_url, s.memory_secret) == (None, None)
    assert s.memory_project == "seamless-migrate"
    assert s.memory_redact_names is False
    assert s.metrics_public is False
    assert s.log_level == "INFO"
    assert s.log_json is False
    assert s.step_timeout_s == 0.0
    assert s.leader_lease_s == 30.0  # SDD §15.1, §25.3 (plan task HA2)


def test_settings_pg_url_from_env(tmp_path):
    url = "postgresql+psycopg://seamless:secret@db.example:5432/seamless"
    s = Settings.from_env({"SEAMLESS_DB_URL": url, "SEAMLESS_DATA_DIR": str(tmp_path)})
    assert s.db_url == url
    assert s.data_dir == tmp_path
    # secrets never show up in repr
    shown = repr(
        Settings.from_env(
            {"SEAMLESS_JEV_TOKEN": "tok-s3nt1nel", "SEAMLESS_MEMORY_SECRET": "mem-s3nt1nel"}
        )
    )
    assert "s3nt1nel" not in shown


def test_settings_parses_types(tmp_path):
    s = Settings.from_env(
        {
            "SEAMLESS_DATA_DIR": str(tmp_path),
            "SEAMLESS_PORT": "9090",
            "SEAMLESS_DEMO": "true",
            "SEAMLESS_DEMO_SPEED": "120",
            "SEAMLESS_AUTH_DISABLED": "1",
            "SEAMLESS_CORS_ORIGINS": "http://a.example, http://b.example",
            "SEAMLESS_MAX_CONCURRENT_CUTOVERS": "5",
            "SEAMLESS_JEV_MODE": "stdio",
            "SEAMLESS_JEV_MIN_CONFIDENCE": "0.7",
            "SEAMLESS_MEMORY_URL": "http://localhost:3111",
            "SEAMLESS_MEMORY_REDACT_NAMES": "yes",
            "SEAMLESS_METRICS_PUBLIC": "true",
            "SEAMLESS_DASHBOARD_DIR": str(tmp_path / "dist"),
        }
    )
    assert s.db_url == f"sqlite:///{tmp_path / 'seamless.db'}"
    assert s.port == 9090 and s.demo and s.demo_speed == 120.0 and s.auth_disabled
    assert s.cors_origins == ["http://a.example", "http://b.example"]
    assert s.max_concurrent_cutovers == 5
    assert s.jev_mode == "stdio" and s.jev_min_confidence == 0.7
    assert s.memory_url == "http://localhost:3111" and s.memory_redact_names
    assert s.metrics_public
    assert s.dashboard_dir == tmp_path / "dist"


def test_settings_ansible_host_key_checking_is_on_by_default_and_can_be_turned_off():
    """SDD §15.1, §26.8 (plan task ST7; audit B-02): every hop of the data path is pinned since
    plan task ST6, so the conversion hosts' keys are checked by default - the run's
    ANSIBLE_HOST_KEY_CHECKING and every hop's StrictHostKeyChecking=yes -, and `false` stays the
    way out; never the control plane's own environment."""
    assert Settings.from_env({}).ansible_host_key_checking is True
    assert Settings().ansible_host_key_checking is True
    on = Settings.from_env({"SEAMLESS_ANSIBLE_HOST_KEY_CHECKING": "true"})
    assert on.ansible_host_key_checking is True
    off = Settings.from_env({"SEAMLESS_ANSIBLE_HOST_KEY_CHECKING": "false"})
    assert off.ansible_host_key_checking is False


@pytest.mark.parametrize(
    ("configured", "browser_sends"),
    [
        ("https://Dashboard.Example.com", "https://dashboard.example.com"),
        ("https://dashboard.example.com:443", "https://dashboard.example.com"),
        ("http://dashboard.example.com:80", "http://dashboard.example.com"),
        ("http://[0:0:0:0:0:0:0:1]:5173", "http://[::1]:5173"),
        ("https://b\u00fccher.example", "https://xn--bcher-kva.example"),
        # the URL Standard's host parser (review RV31-03): a percent-encoded host is decoded, a
        # host ending in a number is an IPv4 address in any of its forms, written dotted, and an
        # IPv6 address is written in hex pieces - never dotted, as Python 3.13 writes ::ffff:a.b.c.d
        ("https://ex%61mple.com", "https://example.com"),
        ("http://127.1:5173", "http://127.0.0.1:5173"),
        ("http://2130706433:5173", "http://127.0.0.1:5173"),
        ("http://0x7f.0.0.1:5173", "http://127.0.0.1:5173"),
        ("http://0177.0.0.1:5173", "http://127.0.0.1:5173"),
        ("http://127.0.0.1.:5173", "http://127.0.0.1:5173"),
        ("http://[::ffff:127.0.0.1]:5173", "http://[::ffff:7f00:1]:5173"),
        ("http://[2001:db8:0:0:1:0:0:1]", "http://[2001:db8::1:0:0:1]"),
    ],
)
def test_a_cors_origin_is_written_as_a_browser_sends_it(configured, browser_sends):
    """SDD §15.1 (review RV27-08): each entry of SEAMLESS_CORS_ORIGINS is one origin as a browser
    sends it - the host lower-cased and in punycode, an IPv6 address written canonically, no
    default port - since the comparison is exact: any other spelling would start the server and
    silently allow nothing. It is refused, naming the spelling the browser sends; that one is
    taken."""
    with pytest.raises(ValueError, match=re.escape(f"write '{browser_sends}'")):
        Settings(cors_origins=[configured])
    assert Settings(cors_origins=[browser_sends]).cors_origins == [browser_sends]


@pytest.mark.parametrize(
    ("host", "browser_sends"),
    [
        ("stra\u00dfe.example", "https://xn--strae-oqa.example"),
        ("\u03c2a.example", "https://xn--a-xmb.example"),
    ],
)
def test_a_cors_origin_whose_host_python_writes_otherwise_than_browsers_is_refused(
    host, browser_sends
):
    """SDD §15.1 (review RV31-03): Python's IDNA 2003 writes a host holding \u00df, \u03c2 or a
    zero-width joiner otherwise than browsers do (UTS #46 keeps them: xn--strae-oqa, where Python
    writes strasse - another domain). Such an entry is refused, asking for the xn-- form the browser
    sends - never naming another domain; that form is taken."""
    with pytest.raises(ValueError) as refused:
        Settings(cors_origins=[f"https://{host}"])
    # no spelling of its own: Python's would be another domain (strasse, xn--a-zmb)
    assert "xn--" in str(refused.value) and "write '" not in str(refused.value)
    assert Settings(cors_origins=[browser_sends]).cors_origins == [browser_sends]


@pytest.mark.parametrize(
    "origin",
    ["http://256.0.0.1", "http://1.2.3.4.5", "http://[fe80::1%25en0]", "https://a%20b.example"],
)
def test_a_cors_origin_no_browser_sends_is_refused(origin):
    """SDD §15.1 (review RV31-03): a host the URL Standard rejects - an IPv4 part out of range, five
    parts ending in a number, an IPv6 zone, a character it forbids once decoded - is no origin a
    browser sends: refused, with no spelling to write instead."""
    with pytest.raises(ValueError, match="is not an origin a browser sends") as refused:
        Settings(cors_origins=[origin])
    assert "write '" not in str(refused.value)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SEAMLESS_PORT", "0"),
        ("SEAMLESS_PORT", "65536"),
        ("SEAMLESS_TICK_S", "0"),
        ("SEAMLESS_DEMO_SPEED", "0"),
        ("SEAMLESS_JEV_TIMEOUT_S", "0"),
        ("SEAMLESS_MAX_CONCURRENT_MIGRATIONS", "0"),
        ("SEAMLESS_MAX_CONCURRENT_CUTOVERS", "0"),
        ("SEAMLESS_MAX_STEP_RETRIES", "-1"),
        ("SEAMLESS_STEP_TIMEOUT_S", "-1"),
        ("SEAMLESS_SOURCE_WATCH_S", "-1"),
        ("SEAMLESS_AUTH_LOCKOUT_PER_MINUTE", "-1"),
        ("SEAMLESS_DEMO_FAILURE_RATE", "-0.1"),
        ("SEAMLESS_DEMO_FAILURE_RATE", "1.1"),
        ("SEAMLESS_JEV_MIN_CONFIDENCE", "1.1"),
        # a renew deadline of two thirds of it, and the retry period of 2 s (SDD §25.3)
        ("SEAMLESS_LEADER_LEASE_S", "9.9"),
        ("SEAMLESS_LEADER_LEASE_S", "0"),
        # every page could call the API (Security.md C1-12, audit AP-10)
        ("SEAMLESS_CORS_ORIGINS", "*"),
        ("SEAMLESS_CORS_ORIGINS", "https://dashboard.example,*"),
        # no origin a browser sends: it would never match, silently
        ("SEAMLESS_CORS_ORIGINS", "dashboard.example"),
        ("SEAMLESS_CORS_ORIGINS", "https://dashboard.example/"),
        ("SEAMLESS_CORS_ORIGINS", "https://dashboard.example/app"),
        ("SEAMLESS_CORS_ORIGINS", "ftp://dashboard.example"),
        ("SEAMLESS_CORS_ORIGINS", "https://user@dashboard.example"),
        ("SEAMLESS_CORS_ORIGINS", "https://dashboard.example:99999"),
        ("SEAMLESS_CORS_ORIGINS", "HTTPS://dashboard.example"),
        ("SEAMLESS_CORS_ORIGINS", "https://dashboard.example#"),
        # an entry Python's urlsplit itself refuses, first or not (review RV32B-08)
        ("SEAMLESS_CORS_ORIGINS", "http://[127.0.0.1]:5173"),
        ("SEAMLESS_CORS_ORIGINS", "http://[::1:5173"),
        ("SEAMLESS_CORS_ORIGINS", "https://dashboard.example,http://[::1:5173"),
    ],
)
def test_settings_refuse_a_value_outside_its_bounds(variable, value):
    """Audit B-08 (SDD §15.1): a tick of 0 would spin and a negative retry count would silently
    disable the retries - a value outside a setting's bounds is refused, naming the setting. A CORS
    origin is one origin, never "*" (audit AP-10)."""
    with pytest.raises(ValueError, match=variable.removeprefix("SEAMLESS_").lower()):
        Settings.from_env({variable: value})


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SEAMLESS_PORT", "1"),
        ("SEAMLESS_PORT", "65535"),
        ("SEAMLESS_TICK_S", "0.001"),
        ("SEAMLESS_MAX_CONCURRENT_MIGRATIONS", "1"),
        ("SEAMLESS_MAX_CONCURRENT_CUTOVERS", "1"),
        ("SEAMLESS_MAX_STEP_RETRIES", "0"),
        ("SEAMLESS_STEP_TIMEOUT_S", "0"),
        ("SEAMLESS_SOURCE_WATCH_S", "0"),
        ("SEAMLESS_AUTH_LOCKOUT_PER_MINUTE", "0"),
        ("SEAMLESS_DEMO_FAILURE_RATE", "0"),
        ("SEAMLESS_DEMO_FAILURE_RATE", "1"),
        ("SEAMLESS_JEV_MIN_CONFIDENCE", "0"),
        ("SEAMLESS_JEV_MIN_CONFIDENCE", "1"),
        ("SEAMLESS_LEADER_LEASE_S", "10"),
        ("SEAMLESS_CORS_ORIGINS", ""),
        ("SEAMLESS_CORS_ORIGINS", "https://dashboard.example"),
        ("SEAMLESS_CORS_ORIGINS", "http://127.0.0.1:5173,https://[2001:db8::1]:8443"),
    ],
)
def test_settings_accept_the_edge_of_their_bounds(variable, value):
    """SDD §15.1: 0 keeps the meaning its row gives it (off, unbounded, once); the edges hold."""
    Settings.from_env({variable: value})


def test_every_setting_is_documented_in_sdd_15_1():
    """Every SEAMLESS_* variable Settings.from_env reads appears in SDD §15.1, and every
    variable the section lists is read (the dashboard-only SEAMLESS_MOCK aside)."""
    import inspect
    import re

    from seamless_migrate.config import Settings, find_repo_root

    source = inspect.getsource(Settings.from_env)
    read = {f"SEAMLESS_{name}" for name in re.findall(r'"([A-Z0-9_]+)"\s*:', source)}
    read |= {f"SEAMLESS_{name}" for name in re.findall(r'get\("([A-Z0-9_]+)"\)', source)}
    sdd = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    start = sdd.index("### 15.1")
    end = sdd.index("\n## 16", start)
    documented = set(re.findall(r"SEAMLESS_[A-Z0-9_]+", sdd[start:end])) - {"SEAMLESS_MOCK"}
    assert read - documented == set(), "read but not documented in SDD §15.1"
    assert documented - read == set(), "documented in SDD §15.1 but never read"
    assert len(read) >= 30


def test_settings_read_where_the_model_runs_and_whether_the_console_may_go_there():
    s = Settings.from_env(
        {
            "SEAMLESS_JEV_DEPLOYMENT": "ON_PREM",
            "SEAMLESS_JEV_ENDPOINT_HOST": " jev.dc1.example.internal ",
            "SEAMLESS_CONSOLE_SCREENING_SEND_TO_REMOTE": "true",
        }
    )
    assert (s.jev_deployment, s.jev_endpoint_host, s.console_screening_send_to_remote) == (
        "on_prem",
        "jev.dc1.example.internal",
        True,
    )
    with pytest.raises(ValueError):
        Settings.from_env({"SEAMLESS_JEV_DEPLOYMENT": "somewhere"})


def _sdd_15_1_rows() -> list[tuple[list[str], list[str]]]:
    """(variables, defaults) of each row of SDD §15.1, the binding environment table."""
    import re

    from seamless_migrate.config import find_repo_root

    sdd = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    section = sdd[sdd.index("### 15.1") :]
    section = section[: section.index("\n---")]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        names = re.findall(r"`(SEAMLESS_[A-Z0-9_]+)`", cells[0]) if len(cells) >= 2 else []
        if names:
            rows.append((names, [d.strip() for d in re.split(r",\s|\s/\s", cells[1])]))
    return rows


def test_settings_read_exactly_the_variables_sdd_15_1_documents():
    """Settings.from_env reads every variable of SDD §15.1 and no other (the §13.3 secret
    variables are read by the secret store, not here)."""
    read: set[str] = set()

    class Recording(dict):
        def get(self, key, default=None):
            read.add(key)
            return super().get(key, default)

    Settings.from_env(Recording())
    documented = {name for names, _ in _sdd_15_1_rows() for name in names}
    assert read == documented


def test_each_literal_default_of_sdd_15_1_is_the_effective_one():
    """Setting a variable to the default SDD §15.1 documents changes nothing."""
    import re

    baseline = Settings.from_env({})
    checked = 0
    for names, defaults in _sdd_15_1_rows():
        assert len(names) == len(defaults), (names, defaults)
        for name, default in zip(names, defaults, strict=True):
            literal = re.match(r"`([^`]*)`", default)
            if literal is None or "{" in literal.group(1):
                continue  # unset, empty, auto, repo root, a template: no literal to compare
            assert Settings.from_env({name: literal.group(1)}) == baseline, name
            checked += 1
    assert checked >= 25
