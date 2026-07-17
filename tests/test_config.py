from pathlib import Path

from shelly_ha_extras.config import load_config


def test_precedence_and_secret_expansion(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
data_directory = "/toml"
[polling]
device_seconds = 60
[auth]
password = "${DEVICE_PASSWORD}"
""")
    cfg = load_config(
        config_file,
        env={
            "DEVICE_PASSWORD": "secret",
            "SHELLY_HA_EXTRAS_POLLING_DEVICE_SECONDS": "12",
        },
        cli={"data_directory": "/cli"},
    )
    assert cfg.data_directory == Path("/cli")
    assert cfg.cache.directory == Path("/cli/cache")
    assert cfg.polling.device_seconds == 12
    assert cfg.auth.password == "secret"


def test_defaults() -> None:
    cfg = load_config(env={})
    assert cfg.polling.device_seconds == 300
    assert cfg.polling.metadata_seconds == 21600
    assert cfg.discovery.stale_cleanup_days == 7
    assert cfg.data_directory == Path("/data")
    assert cfg.cache.directory == Path("/data/cache")


def test_all_environment_sections_are_addressable() -> None:
    cfg = load_config(
        env={
            "SHELLY_HA_EXTRAS_DATA_DIRECTORY": "/runtime",
            "SHELLY_HA_EXTRAS_MQTT_PORT": "1884",
            "SHELLY_HA_EXTRAS_DISCOVERY_INTERVAL_SECONDS": "12",
            "SHELLY_HA_EXTRAS_POLLING_METADATA_SECONDS": "13",
            "SHELLY_HA_EXTRAS_CACHE_DIRECTORY": "/cache",
            "SHELLY_HA_EXTRAS_TLS_ALLOWED_HOSTS": "[\"updates.example\"]",
            "SHELLY_HA_EXTRAS_AUTH_PASSWORDS": "{\"device\":\"secret\"}",
            "SHELLY_HA_EXTRAS_LOGGING_LEVEL": "DEBUG",
        }
    )
    assert cfg.data_directory == Path("/runtime")
    assert cfg.mqtt.port == 1884
    assert cfg.discovery.interval_seconds == 12
    assert cfg.polling.metadata_seconds == 13
    assert cfg.cache.directory == Path("/cache")
    assert cfg.tls.allowed_hosts == ("updates.example",)
    assert cfg.auth.passwords == {"device": "secret"}
    assert cfg.logging.level == "DEBUG"


def test_config_file_environment_fallback_and_cli_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / "env.toml"
    env_file.write_text('data_directory = "/from-env-file"\n')
    cli_file = tmp_path / "cli.toml"
    cli_file.write_text('data_directory = "/from-cli-file"\n')
    from_env = load_config(env={"SHELLY_HA_EXTRAS_CONFIG_FILE": str(env_file)})
    from_cli = load_config(
        cli_file,
        env={"SHELLY_HA_EXTRAS_CONFIG_FILE": str(env_file)},
    )
    assert from_env.data_directory == Path("/from-env-file")
    assert from_cli.data_directory == Path("/from-cli-file")


def test_explicit_cache_directory_is_not_derived(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('data_directory = "/data-root"\n[cache]\ndirectory = "/cache-root"\n')
    cfg = load_config(config_file, cli={"data_directory": "/cli"})
    assert cfg.data_directory == Path("/cli")
    assert cfg.cache.directory == Path("/cache-root")
