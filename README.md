# toros-cloud

Standalone TCP bridge for the Toros Vision heat-pump Wi-Fi module. It accepts the module's cloud-protocol connection, relays frames to the vendor endpoint when reachable (or acknowledges data frames locally in `auto` mode), archives frames, and writes decoded telemetry to InfluxDB 1.x.

It requires only Python's standard library at runtime. The protocol decoder is based on captured traffic and community findings; some record IDs and counter meanings remain provisional.

## Layout

- `toros_cloud/`: standalone server, frame decoder, archive and InfluxDB line-protocol writer.
- `deploy/cloud/`: Docker Compose deployment and example configuration.
- `tests/`: captured-frame decoding, acknowledgement, relay, and allow-list tests.
- `docs/operations.md`: network path, record and field reference, operational commands and known limitations.
- `grafana/toros-heatpump.dashboard.json`: Grafana dashboard for the `hp_live` measurement.

## Configure and run

Copy `deploy/cloud/config.example.toml` to `deploy/cloud/config.toml` and set the allowed module IP, InfluxDB URL/database and archive path. The sample expects an InfluxDB 1.x server at `http://influxdb:8086`; adjust the URL, and attach the service to the Docker network that InfluxDB runs on (see the comment in `docker-compose.yml`).

```sh
cd deploy/cloud
cp config.example.toml config.toml
docker compose up -d --build
docker compose logs -f toros-cloud
```

Compose publishes TCP 7001. Use DNS to direct the module's configured server hostname to this host. Restrict access at the network firewall and set `[cloud].allow` to the module address. An empty allow-list accepts any client able to reach the port.

`mode = "auto"` attempts the configured upstream and falls back to local acknowledgements if it cannot connect. `relay` refuses connections when the upstream is unavailable. `standalone` always answers locally. Local acknowledgements preserve the module's reporting flow; they do not emulate unknown vendor commands or remote control.

## Tests

```sh
python -m unittest discover -s tests -v
```

No serial hardware, MQTT broker, or InfluxDB server is needed for the unit tests.

## Grafana

Import `grafana/toros-heatpump.dashboard.json` and pick your InfluxDB 1.x data source when prompted. The queries read the `hp_live` measurement from the database set in `influx_db`.

## Security and data notes

The Wi-Fi protocol is unencrypted. Archived frames may contain device identifiers and are not rotated by this service. InfluxDB writes currently use the InfluxDB 1.x unauthenticated write endpoint; configure network-level restrictions or adapt the writer if authentication is required. Verify record mappings and units before using counters for billing or performance calculations.
