# Shelly Home Assistant Extras

`shelly-ha-extras` is a companion to Home Assistant's native Shelly integration
for installations where Shelly devices live on an isolated IoT VLAN. It
provides:

- Firmware updates in Home Assistant for Shelly devices behind isolated IoT
  VLANs.
- Schedule controls in Home Assistant, including enabling, disabling, and
  editing trigger times.

It discovers Shelly Gen2+ devices, checks the official stable firmware feed,
uploads firmware through the device's WebSocket RPC API, and publishes the
firmware and schedule entities through MQTT discovery.

It complements rather than replaces Home Assistant's native Shelly integration.
MQTT discovery is independent: the discovered device and
entities are usable before the native integration is configured, and can later
be associated with the same physical Shelly through its canonical ID and MAC.

Firmware updates use a service-pushed path. This service downloads and verifies
the official archive, then uploads it over the LAN to the Shelly. The built-in
Shelly integration invokes the device's own OTA update flow instead. Devices
can remain isolated from the Internet/CDN as long as the service can reach the
IoT VLAN and the official update servers.

The updater is deliberately conservative:

- only `https://updates.shelly.cloud` stable ZIPs are accepted; beta,
  downgrade, custom URL, malformed ZIP, length mismatch, and checksum mismatch
  are rejected.
- CDN TLS uses hostname allow-listing and the supplied Shelly CA file.
- firmware is sent directly to `ws://TARGET/rpc`; the container does not expose
  an HTTP firmware server. A failed upload is aborted best-effort and never
  automatically restarted.
- no installation occurs merely because an update exists. CLI `firmware install`
  asks for confirmation unless `--yes` is supplied; MQTT accepts only the
  exact Home Assistant install payload `{"version":"latest"}`.

## Configuration

Configuration is read in this order: defaults, TOML, `SHELLY_HA_EXTRAS_*`
environment variables, then CLI options. Secret values in TOML may use exact
`${ENV_VAR}` expansion. A minimal file is:

```toml
data_directory = "/data"

[auth]
username = "admin"
password = "..."

[mqtt]
host = "mqtt.example"
port = 1883
username = "homeassistant"
password = "..."
```

Use `[auth.passwords]` for per-device passwords. The updater redacts
passwords, digest values, and firmware data from logs. The default polling
intervals are device 5 minutes, metadata 6 hours, verification 5 minutes, and
stale known-device cleanup 7 days.

Every configuration field can be overridden with an environment variable. The
full mapping is:

Set `SHELLY_HA_EXTRAS_CONFIG_FILE` to load a TOML file by default. The CLI
`--config` option takes precedence when both are present.

| Configuration field | Environment variable |
| --- | --- |
| `data_directory` | `SHELLY_HA_EXTRAS_DATA_DIRECTORY` |
| `mqtt.host`, `.port`, `.username`, `.password`, `.topic_prefix`, `.discovery_prefix`, `.client_id` | `SHELLY_HA_EXTRAS_MQTT_HOST`, `_PORT`, `_USERNAME`, `_PASSWORD`, `_TOPIC_PREFIX`, `_DISCOVERY_PREFIX`, `_CLIENT_ID` |
| `discovery.interval_seconds`, `.probe_seconds`, `.stale_cleanup_days` | `SHELLY_HA_EXTRAS_DISCOVERY_INTERVAL_SECONDS`, `_PROBE_SECONDS`, `_STALE_CLEANUP_DAYS` |
| `polling.device_seconds`, `.metadata_seconds`, `.verification_seconds` | `SHELLY_HA_EXTRAS_POLLING_DEVICE_SECONDS`, `_METADATA_SECONDS`, `_VERIFICATION_SECONDS` |
| `cache.directory` | `SHELLY_HA_EXTRAS_CACHE_DIRECTORY` |
| `tls.allowed_hosts`, `.ca_file` | `SHELLY_HA_EXTRAS_TLS_ALLOWED_HOSTS`, `_CA_FILE` |
| `auth.username`, `.password`, `.passwords` | `SHELLY_HA_EXTRAS_AUTH_USERNAME`, `_PASSWORD`, `_PASSWORDS` |
| `logging.level`, `.json` | `SHELLY_HA_EXTRAS_LOGGING_LEVEL`, `_JSON` |

`SHELLY_HA_EXTRAS_AUTH_PASSWORDS` and `SHELLY_HA_EXTRAS_TLS_ALLOWED_HOSTS`
accept JSON objects and arrays respectively. Environment variables override
TOML; CLI options override both.

## Container and MQTT

Create a deployment directory, clone the repository into its `src` subdirectory,
then copy the runtime files into the deployment directory:

```sh
mkdir shelly-ha-extras
cd shelly-ha-extras
git clone https://github.com/jasoncodes/shelly-ha-extras.git src
cp src/config.example.toml config.toml
cp src/compose.example.yml compose.yaml
```

The example Compose file uses host networking so Zeroconf multicast can reach
the server's IoT interface. The image runs as a non-root user and exposes no
firmware HTTP port. It needs outbound access to the IoT VLAN, the MQTT broker,
and the Shelly CDN.

```sh
docker compose up --build
```

Add `-d` to run detached after confirming the interactive logs look healthy.

The MQTT service publishes retained discovery, state, and availability topics,
a service LWT, installed/latest versions, progress, and device metadata. It
republishes discovery and state after the Home Assistant birth topic. Known
devices are persisted in `/data/known-devices.json` and removed after seven
days with no observation.

## CLI manual testing

Install Python 3.12+ and uv, then:

```sh
uv sync --dev
uv run shelly-ha-extras devices discover
uv run shelly-ha-extras devices discover --check-updates --json
uv run shelly-ha-extras firmware check TARGET
uv run shelly-ha-extras firmware install TARGET --dry-run
uv run shelly-ha-extras firmware install TARGET
uv run shelly-ha-extras schedules list TARGET
uv run shelly-ha-extras schedules update TARGET JOB_ID --enable
uv run shelly-ha-extras schedules eval TARGET TIMESPEC
```

## Development

```sh
uv sync --dev
uv run pytest
uv run ruff check src tests
uv run mypy src
docker build -t shelly-ha-extras .
```

Tests use fakes for Zeroconf, HTTP/CDN, WebSocket RPC, and MQTT.

## License

MIT
