"""Command-line entry point for the standalone Toros cloud bridge."""

import argparse
import asyncio
import logging
import tomllib

from .cloud import Archive, Bridge, InfluxSink


def load_config(path):
    with open(path, "rb") as handle:
        config = tomllib.load(handle).get("cloud", {})
    defaults = {
        "listen": "0.0.0.0",
        "port": 7001,
        "mode": "auto",
        "upstream": "20.238.153.143:7001",
        "allow": [],
        "connect_timeout": 10.0,
        "idle_timeout": 900,
        "influx_url": "",
        "influx_db": "toros",
        "measurement": "hp_live",
        "archive_directory": "",
    }
    unknown = set(config) - set(defaults)
    if unknown:
        raise ValueError("unknown [cloud] option(s): " + ", ".join(sorted(unknown)))
    defaults.update(config)
    if defaults["mode"] not in ("auto", "relay", "standalone"):
        raise ValueError('[cloud] mode must be "auto", "relay", or "standalone"')
    if not 1 <= defaults["port"] <= 65535:
        raise ValueError("[cloud] port must be between 1 and 65535")
    if not isinstance(defaults["allow"], list) or not all(isinstance(ip, str) for ip in defaults["allow"]):
        raise ValueError("[cloud] allow must be a list of IP address strings")
    return defaults


async def serve(config):
    sink = InfluxSink(config["influx_url"], config["influx_db"]) if config["influx_url"] else None
    server = await Bridge(config, sink, Archive(config["archive_directory"])).start()
    async with server:
        await server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bridge Toros Wi-Fi telemetry to InfluxDB")
    parser.add_argument("-c", "--config", default="/config/config.toml", help="TOML configuration file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(serve(load_config(args.config)))
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()