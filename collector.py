#!/usr/bin/env python3
"""1-Wire temperature collector daemon.

Reads all 28-* sensors from the w1 bus, writes to local InfluxDB,
and optionally replicates to a remote InfluxDB instance.
Config is loaded fresh each cycle so edits take effect without a restart.
"""

import logging
import sys
import time
import tomllib
from pathlib import Path

import influxdb_client
from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS

CONFIG_PATH = Path("/etc/pi-collector/config.toml")
W1_BASE = Path("/sys/bus/w1/devices/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def make_write_api(cfg: dict):
    client = influxdb_client.InfluxDBClient(
        url=cfg["url"],
        token=cfg["token"],
        org=cfg["org"],
    )
    return client, client.write_api(write_options=SYNCHRONOUS)


def read_1w_sensor(device_dir: Path) -> tuple[float, float] | tuple[None, None]:
    slave_file = device_dir / "w1_slave"
    for _ in range(3):
        try:
            lines = slave_file.read_text().splitlines()
            if len(lines) >= 2 and lines[0].strip().endswith("YES"):
                pos = lines[1].find("t=")
                if pos != -1:
                    celsius = round(int(lines[1][pos + 2:]) / 1000.0, 2)
                    fahrenheit = round(celsius * 9.0 / 5.0 + 32.0, 2)
                    return celsius, fahrenheit
        except OSError:
            pass
        time.sleep(0.1)
    return None, None


def collect(config: dict) -> list[Point]:
    sensor_cfg = config.get("sensors", {})
    hostname = config["collector"].get("hostname", "pi")
    points = []

    for device_dir in sorted(W1_BASE.glob("28-*")):
        sensor_id = device_dir.name
        celsius, fahrenheit = read_1w_sensor(device_dir)
        if fahrenheit is None:
            log.warning("Sensor %s: no valid reading after 3 attempts, skipping", sensor_id)
            continue
        location = sensor_cfg.get(sensor_id, {}).get("name", sensor_id)
        point = (
            Point("temperature")
            .tag("host", hostname)
            .tag("sensor_id", sensor_id)
            .tag("location", location)
            .field("celsius", celsius)
            .field("fahrenheit", fahrenheit)
        )
        points.append(point)
        log.info("%s (%s): %.2f°F / %.2f°C", location, sensor_id, fahrenheit, celsius)

    return points


def write_points(write_api, cfg: dict, points: list[Point], label: str) -> None:
    try:
        write_api.write(bucket=cfg["bucket"], org=cfg["org"], record=points)
    except Exception as e:
        log.error("Write to %s InfluxDB failed: %s", label, e)


def main() -> None:
    log.info("pi-collector starting, config: %s", CONFIG_PATH)

    try:
        config = load_config()
    except Exception as e:
        log.critical("Cannot load config: %s", e)
        sys.exit(1)

    interval = config["collector"].get("interval_seconds", 15)
    local_cfg = config["influx"]["local"]
    local_client, local_api = make_write_api(local_cfg)

    remote_client = None
    remote_api = None
    remote_cfg = config["influx"].get("remote")
    if remote_cfg and remote_cfg.get("url") and remote_cfg.get("token"):
        remote_client, remote_api = make_write_api(remote_cfg)
        log.info("Remote replication enabled: %s", remote_cfg["url"])
    else:
        log.info("Remote replication not configured, running local-only")

    log.info("Polling every %ds", interval)

    try:
        while True:
            # Reload config each cycle so sensor renames and interval changes apply live
            try:
                config = load_config()
                interval = config["collector"].get("interval_seconds", 15)
            except Exception as e:
                log.warning("Config reload failed, using previous config: %s", e)

            points = collect(config)
            if points:
                write_points(local_api, local_cfg, points, "local")
                if remote_api and remote_cfg:
                    write_points(remote_api, remote_cfg, points, "remote")
            else:
                log.warning("No sensor readings this cycle")

            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("Stopped by interrupt")
    finally:
        local_client.close()
        if remote_client:
            remote_client.close()


if __name__ == "__main__":
    main()
