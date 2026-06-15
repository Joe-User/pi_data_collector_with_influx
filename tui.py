#!/usr/bin/env python3
"""pi-collector TUI — view sensor data and manage the collector service."""

import subprocess
import time
import tomllib
from pathlib import Path

import tomlkit
from influxdb_client import InfluxDBClient
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

CONFIG_PATH = Path("/etc/pi-collector/config.toml")
W1_BASE = Path("/sys/bus/w1/devices/")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def load_config_rw():
    """Load config with tomlkit so comments and structure are preserved on save."""
    with open(CONFIG_PATH, "r") as f:
        return tomlkit.load(f)


def save_config(doc) -> None:
    with open(CONFIG_PATH, "w") as f:
        tomlkit.dump(doc, f)


def read_1w_sensor(device_dir: Path) -> tuple:
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


def service_status() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "pi-collector"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


def service_control(action: str) -> tuple:
    try:
        r = subprocess.run(
            ["sudo", "systemctl", action, "pi-collector"],
            capture_output=True, text=True, timeout=15,
        )
        msg = (r.stderr.strip() or r.stdout.strip())
        return r.returncode == 0, msg
    except Exception as e:
        return False, str(e)


def get_service_logs(n: int = 30) -> str:
    try:
        r = subprocess.run(
            ["journalctl", "-u", "pi-collector", "-n", str(n),
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() or "(no log entries)"
    except Exception:
        return "Unable to fetch logs"


# ---------------------------------------------------------------------------
# Dashboard tab
# ---------------------------------------------------------------------------

class DashboardTab(Static):
    DEFAULT_CSS = """
    DashboardTab { height: 1fr; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="dash_svc_status")
        yield Label("LIVE SENSOR READINGS", classes="section_title")
        yield DataTable(id="live_table")

    def on_mount(self) -> None:
        t = self.query_one("#live_table", DataTable)
        t.add_columns("Location", "Sensor ID", "°F", "°C", "Read at")
        self.refresh_data()
        self.set_interval(15, self.refresh_data)

    def refresh_data(self) -> None:
        status = service_status()
        color = "green" if status == "active" else "red"
        self.query_one("#dash_svc_status", Label).update(
            f"Collector service: [{color}]{status}[/{color}]"
        )
        self._fetch_readings()

    @work(thread=True)
    def _fetch_readings(self) -> None:
        try:
            config = load_config()
        except Exception:
            return
        sensor_cfg = config.get("sensors", {})
        rows = []
        for device_dir in sorted(W1_BASE.glob("28-*")):
            sensor_id = device_dir.name
            celsius, fahrenheit = read_1w_sensor(device_dir)
            location = sensor_cfg.get(sensor_id, {}).get("name", sensor_id)
            if fahrenheit is not None:
                rows.append((location, sensor_id, f"{fahrenheit:.1f}", f"{celsius:.1f}", time.strftime("%H:%M:%S")))
            else:
                rows.append((location, sensor_id, "ERR", "ERR", time.strftime("%H:%M:%S")))
        self.app.call_from_thread(self._apply_readings, rows)

    def _apply_readings(self, rows) -> None:
        t = self.query_one("#live_table", DataTable)
        t.clear()
        for row in rows:
            t.add_row(*row)


# ---------------------------------------------------------------------------
# History tab
# ---------------------------------------------------------------------------

class HistoryTab(Static):
    DEFAULT_CSS = """
    HistoryTab { height: 1fr; padding: 1 2; }
    .range_row { height: auto; margin-bottom: 1; }
    """

    hours: reactive[int] = reactive(1)

    def compose(self) -> ComposeResult:
        yield Label("RECENT HISTORY", classes="section_title")
        with Horizontal(classes="range_row"):
            yield Button("1h",  id="h1",  variant="primary")
            yield Button("6h",  id="h6")
            yield Button("24h", id="h24")
        yield Label("", id="history_status")
        yield DataTable(id="history_table")

    def on_mount(self) -> None:
        t = self.query_one("#history_table", DataTable)
        t.add_columns("Time", "Location", "°F", "°C")
        self._fetch_history()

    def watch_hours(self, _: int) -> None:
        self._fetch_history()

    @on(Button.Pressed, "#h1")
    def _h1(self) -> None:
        self._set_range(1, "h1")

    @on(Button.Pressed, "#h6")
    def _h6(self) -> None:
        self._set_range(6, "h6")

    @on(Button.Pressed, "#h24")
    def _h24(self) -> None:
        self._set_range(24, "h24")

    def _set_range(self, hours: int, active_id: str) -> None:
        for btn_id in ["h1", "h6", "h24"]:
            self.query_one(f"#{btn_id}", Button).variant = "primary" if btn_id == active_id else "default"
        self.hours = hours

    @work(thread=True)
    def _fetch_history(self) -> None:
        self.app.call_from_thread(lambda: self.query_one("#history_status", Label).update("Loading..."))
        try:
            config = load_config()
            local = config["influx"]["local"]
            client = InfluxDBClient(url=local["url"], token=local["token"], org=local["org"])
            # Query fahrenheit only — avoids pivot which breaks on old records
            # that used different tag names (pre-pi-collector data).
            query = f"""
from(bucket: "{local["bucket"]}")
  |> range(start: -{self.hours}h)
  |> filter(fn: (r) => r._measurement == "temperature" and r._field == "fahrenheit")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 200)
"""
            tables = client.query_api().query(query, org=local["org"])
            rows = []
            for table in tables:
                for rec in table.records:
                    t = rec.get_time()
                    t_str = t.strftime("%m/%d %H:%M:%S") if t else "--"
                    f_val = rec.get_value()
                    c_val = round((f_val - 32) * 5 / 9, 1) if f_val is not None else None
                    location = rec.values.get("location") or rec.values.get("source", "unknown")
                    rows.append((
                        t_str,
                        location,
                        f"{f_val:.1f}" if f_val is not None else "--",
                        f"{c_val:.1f}" if c_val is not None else "--",
                    ))
            client.close()
            self.app.call_from_thread(self._apply_history, rows)
        except Exception as e:
            err_msg = f"[red]Query failed: {e}[/red]"
            self.app.call_from_thread(lambda: self.query_one("#history_status", Label).update(err_msg))

    def _apply_history(self, rows) -> None:
        self.query_one("#history_status", Label).update(f"{len(rows)} records")
        t = self.query_one("#history_table", DataTable)
        t.clear()
        for row in rows:
            t.add_row(*row)


# ---------------------------------------------------------------------------
# Sensors tab
# ---------------------------------------------------------------------------

class SensorsTab(Static):
    DEFAULT_CSS = """
    SensorsTab { height: 1fr; padding: 1 2; }
    .sensor_row { height: auto; margin-bottom: 1; }
    .sensor_id_col { width: 35; padding-top: 1; }
    .temp_col { width: 12; padding-top: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("SENSOR LABELS", classes="section_title")
        yield Label("Assign a friendly name to each discovered sensor.", classes="hint")
        yield Static("", id="sensors_body")
        yield Button("Save Labels", id="save_sensors", variant="success")
        yield Label("", id="sensors_status")

    def on_mount(self) -> None:
        self._build_rows()

    @work(thread=True)
    def _build_rows(self) -> None:
        try:
            config = load_config()
        except Exception:
            config = {}
        sensor_cfg = config.get("sensors", {})
        sensor_data = []
        for device_dir in sorted(W1_BASE.glob("28-*")):
            sensor_id = device_dir.name
            celsius, fahrenheit = read_1w_sensor(device_dir)
            name = sensor_cfg.get(sensor_id, {}).get("name", "")
            sensor_data.append((sensor_id, fahrenheit, celsius, name))
        self.app.call_from_thread(self._mount_rows, sensor_data)

    def _mount_rows(self, sensor_data) -> None:
        # Remove placeholder and any previous build
        for sel in ("#sensors_body", "#sensors_container"):
            try:
                self.query_one(sel).remove()
            except Exception:
                pass

        # Build rows with children in the constructor so the whole tree
        # is ready before mounting — Textual requires a widget be in the
        # DOM before you can mount into it.
        rows = []
        for sensor_id, fahrenheit, celsius, name in sensor_data:
            temp_str = (
                f"{fahrenheit:.1f}°F / {celsius:.1f}°C"
                if fahrenheit is not None
                else "read error"
            )
            rows.append(
                Horizontal(
                    Label(sensor_id, classes="sensor_id_col"),
                    Label(temp_str, classes="temp_col"),
                    Input(value=name, placeholder="location name", id=f"s_{sensor_id}"),
                    classes="sensor_row",
                )
            )
        self.mount(Vertical(*rows, id="sensors_container"), before="#save_sensors")

    @on(Button.Pressed, "#save_sensors")
    def save_sensors(self) -> None:
        try:
            doc = load_config_rw()
        except Exception as e:
            self.query_one("#sensors_status", Label).update(f"[red]Load error: {e}[/red]")
            return

        if "sensors" not in doc:
            doc.add("sensors", tomlkit.table())

        for device_dir in sorted(W1_BASE.glob("28-*")):
            sensor_id = device_dir.name
            widget_id = f"s_{sensor_id}"
            try:
                inp = self.query_one(f"#{widget_id}", Input)
            except Exception:
                continue
            name = inp.value.strip()
            if name:
                if sensor_id not in doc["sensors"]:
                    doc["sensors"].add(sensor_id, tomlkit.table())
                doc["sensors"][sensor_id]["name"] = name
            elif sensor_id in doc["sensors"]:
                del doc["sensors"][sensor_id]

        try:
            save_config(doc)
            self.query_one("#sensors_status", Label).update(
                "[green]Saved — collector picks up new names on next cycle.[/green]"
            )
        except Exception as e:
            self.query_one("#sensors_status", Label).update(f"[red]Save failed: {e}[/red]")


# ---------------------------------------------------------------------------
# Settings tab
# ---------------------------------------------------------------------------

class SettingsTab(Static):
    DEFAULT_CSS = """
    SettingsTab { height: 1fr; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        yield Label("COLLECTOR", classes="section_title")
        yield Label("Hostname tag (identifies this Pi in InfluxDB)")
        yield Input(id="cfg_hostname", placeholder="bu-twr-02")
        yield Label("Poll interval (seconds)")
        yield Input(id="cfg_interval", placeholder="15")

        yield Label("LOCAL INFLUXDB", classes="section_title")
        yield Label("URL")
        yield Input(id="cfg_local_url", placeholder="http://localhost:8086")
        yield Label("Token")
        yield Input(id="cfg_local_token", password=True)
        yield Label("Organization")
        yield Input(id="cfg_local_org", placeholder="mi8")
        yield Label("Bucket")
        yield Input(id="cfg_local_bucket")

        yield Label("REMOTE INFLUXDB — optional replication", classes="section_title")
        yield Label("Leave URL blank to disable. Fill in when your remote instance is ready.")
        yield Label("URL")
        yield Input(id="cfg_remote_url", placeholder="https://influx.example.com")
        yield Label("Token")
        yield Input(id="cfg_remote_token", password=True)
        yield Label("Organization")
        yield Input(id="cfg_remote_org")
        yield Label("Bucket")
        yield Input(id="cfg_remote_bucket")

        yield Button("Save Settings", id="save_settings", variant="success")
        yield Label("", id="settings_status")

    def on_mount(self) -> None:
        self._load_values()

    def _load_values(self) -> None:
        try:
            c = load_config()
        except Exception as e:
            self.query_one("#settings_status", Label).update(f"[red]Load error: {e}[/red]")
            return

        col = c.get("collector", {})
        self.query_one("#cfg_hostname", Input).value = col.get("hostname", "")
        self.query_one("#cfg_interval", Input).value = str(col.get("interval_seconds", 15))

        local = c.get("influx", {}).get("local", {})
        self.query_one("#cfg_local_url", Input).value = local.get("url", "")
        self.query_one("#cfg_local_token", Input).value = local.get("token", "")
        self.query_one("#cfg_local_org", Input).value = local.get("org", "")
        self.query_one("#cfg_local_bucket", Input).value = local.get("bucket", "")

        remote = c.get("influx", {}).get("remote", {})
        self.query_one("#cfg_remote_url", Input).value = remote.get("url", "")
        self.query_one("#cfg_remote_token", Input).value = remote.get("token", "")
        self.query_one("#cfg_remote_org", Input).value = remote.get("org", "")
        self.query_one("#cfg_remote_bucket", Input).value = remote.get("bucket", "")

    @on(Button.Pressed, "#save_settings")
    def save_settings(self) -> None:
        try:
            doc = load_config_rw()
        except Exception as e:
            self.query_one("#settings_status", Label).update(f"[red]Load error: {e}[/red]")
            return

        # collector section
        if "collector" not in doc:
            doc.add("collector", tomlkit.table())
        doc["collector"]["hostname"] = self.query_one("#cfg_hostname", Input).value.strip()
        try:
            doc["collector"]["interval_seconds"] = int(
                self.query_one("#cfg_interval", Input).value.strip()
            )
        except ValueError:
            self.query_one("#settings_status", Label).update(
                "[red]Interval must be a whole number of seconds.[/red]"
            )
            return

        # local influx
        if "influx" not in doc:
            doc.add("influx", tomlkit.table())
        if "local" not in doc["influx"]:
            doc["influx"].add("local", tomlkit.table())
        doc["influx"]["local"]["url"]    = self.query_one("#cfg_local_url", Input).value.strip()
        doc["influx"]["local"]["token"]  = self.query_one("#cfg_local_token", Input).value.strip()
        doc["influx"]["local"]["org"]    = self.query_one("#cfg_local_org", Input).value.strip()
        doc["influx"]["local"]["bucket"] = self.query_one("#cfg_local_bucket", Input).value.strip()

        # remote influx (optional)
        remote_url = self.query_one("#cfg_remote_url", Input).value.strip()
        if remote_url:
            if "remote" not in doc["influx"]:
                doc["influx"].add("remote", tomlkit.table())
            doc["influx"]["remote"]["url"]    = remote_url
            doc["influx"]["remote"]["token"]  = self.query_one("#cfg_remote_token", Input).value.strip()
            doc["influx"]["remote"]["org"]    = self.query_one("#cfg_remote_org", Input).value.strip()
            doc["influx"]["remote"]["bucket"] = self.query_one("#cfg_remote_bucket", Input).value.strip()
        elif "remote" in doc["influx"]:
            del doc["influx"]["remote"]

        try:
            save_config(doc)
            self.query_one("#settings_status", Label).update(
                "[green]Saved — collector reloads config each cycle automatically.[/green]"
            )
        except Exception as e:
            self.query_one("#settings_status", Label).update(f"[red]Save failed: {e}[/red]")


# ---------------------------------------------------------------------------
# Service tab
# ---------------------------------------------------------------------------

class ServiceTab(Static):
    DEFAULT_CSS = """
    ServiceTab { height: 1fr; padding: 1 2; }
    .ctrl_row { height: auto; margin-bottom: 1; }
    #svc_logs {
        background: $surface-darken-1;
        padding: 1;
        height: 20;
        overflow-y: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("SERVICE CONTROL", classes="section_title")
        yield Label("", id="svc_status")
        with Horizontal(classes="ctrl_row"):
            yield Button("Start",   id="svc_start",   variant="success")
            yield Button("Stop",    id="svc_stop",    variant="error")
            yield Button("Restart", id="svc_restart", variant="warning")
            yield Button("Refresh", id="svc_refresh")
        yield Label("", id="svc_action_msg")
        yield Label("RECENT LOGS", classes="section_title")
        yield Static("", id="svc_logs")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        status = service_status()
        color = "green" if status == "active" else "red"
        self.query_one("#svc_status", Label).update(f"Status: [{color}]{status}[/{color}]")
        self.query_one("#svc_logs", Static).update(get_service_logs(40))

    @on(Button.Pressed, "#svc_start")
    def _start(self) -> None:
        ok, msg = service_control("start")
        self.query_one("#svc_action_msg", Label).update(
            "[green]Started[/green]" if ok else f"[red]Failed: {msg}[/red]"
        )
        self._refresh()

    @on(Button.Pressed, "#svc_stop")
    def _stop(self) -> None:
        ok, msg = service_control("stop")
        self.query_one("#svc_action_msg", Label).update(
            "[green]Stopped[/green]" if ok else f"[red]Failed: {msg}[/red]"
        )
        self._refresh()

    @on(Button.Pressed, "#svc_restart")
    def _restart(self) -> None:
        ok, msg = service_control("restart")
        self.query_one("#svc_action_msg", Label).update(
            "[green]Restarted[/green]" if ok else f"[red]Failed: {msg}[/red]"
        )
        self._refresh()

    @on(Button.Pressed, "#svc_refresh")
    def _do_refresh(self) -> None:
        self._refresh()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

APP_CSS = """
Screen { background: $surface; }

.section_title {
    text-style: bold;
    color: $accent;
    margin-top: 1;
    margin-bottom: 1;
}

.hint {
    color: $text-muted;
    margin-bottom: 1;
}

Button { margin-right: 1; }

Input { margin-bottom: 1; }

TabbedContent { height: 1fr; }

TabPane { padding: 0; overflow-y: auto; }
"""


class CollectorTUI(App):
    TITLE = "pi-collector"
    CSS = APP_CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Dashboard", id="tab_dashboard"):
                yield DashboardTab()
            with TabPane("History",   id="tab_history"):
                yield HistoryTab()
            with TabPane("Sensors",   id="tab_sensors"):
                yield ScrollableContainer(SensorsTab())
            with TabPane("Settings",  id="tab_settings"):
                yield ScrollableContainer(SettingsTab())
            with TabPane("Service",   id="tab_service"):
                yield ServiceTab()
        yield Footer()


def main() -> None:
    CollectorTUI().run()


if __name__ == "__main__":
    main()
