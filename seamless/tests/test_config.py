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
