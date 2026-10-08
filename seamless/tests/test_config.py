from pathlib import Path

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
    assert (s.memory_url, s.memory_secret) == (None, None)
    assert s.memory_project == "seamless-migrate"
    assert s.memory_redact_names is False
    assert s.metrics_public is False
    assert s.log_level == "INFO"
    assert s.log_json is False


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
