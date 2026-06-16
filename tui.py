#!/usr/bin/env python3
"""pi-collector TUI — view sensor data and manage the collector service."""

import re
import subprocess
import threading
import time
import tomllib
from pathlib import Path

import tomlkit
from influxdb_client import InfluxDBClient
from rich.text import Text
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

try:
    import plotext as _plt
    _PLOTEXT = True
except ImportError:
    _PLOTEXT = False

CONFIG_PATH = Path("/etc/pi-collector/config.toml")
W1_BASE = Path("/sys/bus/w1/devices/")

# plotext mutates global state; serialize chart renders across threads
_plot_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def load_config_rw():
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
        return r.returncode == 0, (r.stderr.strip() or r.stdout.strip())
    except Exception as e:
        return False, str(e)


def get_service_logs(n: int = 40) -> str:
    try:
        r = subprocess.run(
            ["journalctl", "-u", "pi-collector", "-n", str(n),
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() or "(no log entries)"
    except Exception:
        return "Unable to fetch logs"


def render_line_chart(source: str, values: list, width: int = 72, height: int = 10) -> Text:
    """Render a plotext line chart to Rich Text (ANSI-parsed)."""
    if not _PLOTEXT or len(values) < 2:
        if values:
            return Text(
                f"{source}  —  min {min(values):.1f}  max {max(values):.1f}"
                f"  now {values[-1]:.1f} °F  ({len(values)} pts)"
            )
        return Text(f"{source}  —  no data")

    with _plot_lock:
        _plt.clear_figure()
        _plt.plot(values, marker="hd")
        vmin, vmax, vnow = min(values), max(values), values[-1]
        _plt.title(f"{source}   {vmin:.1f} min / {vmax:.1f} max / {vnow:.1f} now  (°F)")
        _plt.plotsize(width, height)
        _plt.canvas_color("none")
        _plt.axes_color("none")
        _plt.ticks_color("white")
        chart_str = _plt.build()
        return Text.from_ansi(chart_str)


def build_charts(data: dict, width: int = 72) -> dict:
    """Return {source: rich.Text} for each source that has enough data."""
    return {
        src: render_line_chart(src, vals, width=width)
        for src, vals in sorted(data.items())
        if len(vals) >= 2
    }


def fetch_chart_data(local: dict, hours: int, sources: list = None) -> dict:
    """Query InfluxDB and return {source: [fahrenheit values asc]}.

    sources: if provided, only these source names are queried.
    """
    every = "5m" if hours >= 6 else "1m"
    client = InfluxDBClient(url=local["url"], token=local["token"], org=local["org"])
    src_filter = ""
    if sources:
        conditions = " or ".join(f'r.source == "{s}"' for s in sources)
        src_filter = f" and ({conditions})"
    query = (
        f'from(bucket: "{local["bucket"]}")'
        f' |> range(start: -{hours}h)'
        f' |> filter(fn: (r) => r._measurement == "temperature" and r._field == "fahrenheit"{src_filter})'
        f' |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)'
        f' |> sort(columns: ["_time"])'
    )
    tables = client.query_api().query(query, org=local["org"])
    data = {}
    for table in tables:
        for rec in table.records:
            src = rec.values.get("source", "unknown")
            val = rec.get_value()
            if val is not None:
                data.setdefault(src, []).append(val)
    client.close()
    return data


def mount_charts(container, charts: dict) -> None:
    """Replace all children of a container with one Static chart per source."""
    container.remove_children()
    if not charts:
        container.mount(Static("No trend data available."))
        return
    for chart_text in charts.values():
        container.mount(Static(chart_text, classes="chart_block"))


# ---------------------------------------------------------------------------
# Dashboard tab
# ---------------------------------------------------------------------------

def _safe_btn_id(src: str) -> str:
    return "sf_" + re.sub(r"[^a-zA-Z0-9_-]", "_", src)


class DashboardTab(Static):
    DEFAULT_CSS = """
    DashboardTab { height: 1fr; padding: 1 2; }
    .chart_block  { height: 12; margin-bottom: 1; }
    .controls_row { height: auto; margin-bottom: 1; }
    """

    hours: reactive[int] = reactive(1)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selected_sources: set = set()
        self._btn_id_to_src: dict = {}

    def compose(self) -> ComposeResult:
        yield Label("", id="dash_svc_status")
        yield Label("TIME RANGE", classes="section_title")
        with Horizontal(classes="controls_row"):
            yield Button("1h",  id="d_h1",  variant="primary")
            yield Button("6h",  id="d_h6")
            yield Button("24h", id="d_h24")
        yield Label("SENSORS", classes="section_title")
        yield Horizontal(id="sensor_btns", classes="controls_row")
        yield Label("LIVE READINGS", classes="section_title")
        yield DataTable(id="live_table")
        yield Label("TEMPERATURE TRENDS (1h)", id="trends_title", classes="section_title")
        yield Vertical(id="trends_container")

    def on_mount(self) -> None:
        t = self.query_one("#live_table", DataTable)
        t.add_columns("Source", "Sensor ID", "°F", "°C", "Read at")
        self._load_sensor_buttons()
        self._fetch_live()
        self._fetch_charts()
        self.set_interval(15, self._fetch_live)
        self.set_interval(60, self._fetch_charts)

    def _load_sensor_buttons(self) -> None:
        try:
            config = load_config()
            sensor_cfg = config.get("sensors", {})
            sources = sorted(cfg.get("name", sid) for sid, cfg in sensor_cfg.items())
        except Exception:
            sources = []
        if not sources:
            sources = sorted(d.name for d in W1_BASE.glob("28-*"))

        self._selected_sources = set(sources)
        row = self.query_one("#sensor_btns", Horizontal)
        for src in sources:
            btn_id = _safe_btn_id(src)
            self._btn_id_to_src[btn_id] = src
            row.mount(Button(src, id=btn_id, variant="primary", classes="sensor_filter_btn"))

    # --- Time range ---

    @on(Button.Pressed, "#d_h1")
    def _d_h1(self) -> None:
        self._set_range(1, "d_h1")

    @on(Button.Pressed, "#d_h6")
    def _d_h6(self) -> None:
        self._set_range(6, "d_h6")

    @on(Button.Pressed, "#d_h24")
    def _d_h24(self) -> None:
        self._set_range(24, "d_h24")

    def _set_range(self, hours: int, active_id: str) -> None:
        for btn_id in ("d_h1", "d_h6", "d_h24"):
            self.query_one(f"#{btn_id}", Button).variant = (
                "primary" if btn_id == active_id else "default"
            )
        self.hours = hours

    def watch_hours(self, h: int) -> None:
        try:
            self.query_one("#trends_title", Label).update(f"TEMPERATURE TRENDS ({h}h)")
        except Exception:
            pass
        self._fetch_charts()

    # --- Sensor filter ---

    @on(Button.Pressed, ".sensor_filter_btn")
    def _toggle_sensor(self, event: Button.Pressed) -> None:
        btn = event.button
        src = self._btn_id_to_src.get(btn.id, "")
        if not src:
            return
        if src in self._selected_sources:
            self._selected_sources.discard(src)
            btn.variant = "default"
        else:
            self._selected_sources.add(src)
            btn.variant = "primary"
        self._fetch_charts()

    # --- Workers ---

    def refresh_status(self) -> None:
        status = service_status()
        color = "green" if status == "active" else "red"
        self.query_one("#dash_svc_status", Label).update(
            f"Collector service: [{color}]{status}[/{color}]"
        )

    @work(thread=True)
    def _fetch_live(self) -> None:
        self.app.call_from_thread(self.refresh_status)
        try:
            config = load_config()
        except Exception:
            return
        sensor_cfg = config.get("sensors", {})
        rows = []
        for device_dir in sorted(W1_BASE.glob("28-*")):
            sensor_id = device_dir.name
            celsius, fahrenheit = read_1w_sensor(device_dir)
            source = sensor_cfg.get(sensor_id, {}).get("name", sensor_id)
            if fahrenheit is not None:
                rows.append((source, sensor_id, f"{fahrenheit:.1f}", f"{celsius:.1f}", time.strftime("%H:%M:%S")))
            else:
                rows.append((source, sensor_id, "ERR", "ERR", time.strftime("%H:%M:%S")))
        self.app.call_from_thread(self._apply_live, rows)

    def _apply_live(self, rows) -> None:
        t = self.query_one("#live_table", DataTable)
        t.clear()
        for row in rows:
            t.add_row(*row)

    @work(thread=True)
    def _fetch_charts(self) -> None:
        selected = set(self._selected_sources)  # snapshot; avoid races
        if not selected:
            self.app.call_from_thread(
                lambda: mount_charts(self.query_one("#trends_container", Vertical), {})
            )
            return
        try:
            config = load_config()
            data = fetch_chart_data(
                config["influx"]["local"],
                hours=self.hours,
                sources=list(selected),
            )
            charts = build_charts(data)
            self.app.call_from_thread(self._apply_charts, charts)
        except Exception:
            pass

    def _apply_charts(self, charts: dict) -> None:
        mount_charts(self.query_one("#trends_container", Vertical), charts)


# ---------------------------------------------------------------------------
# History tab
# ---------------------------------------------------------------------------

class HistoryTab(Static):
    DEFAULT_CSS = """
    HistoryTab { height: 1fr; padding: 1 2; }
    .range_row { height: auto; margin-bottom: 1; }
    .chart_block { height: 12; margin-bottom: 1; }
    """

    hours: reactive[int] = reactive(1)

    def compose(self) -> ComposeResult:
        yield Label("HISTORY", classes="section_title")
        with Horizontal(classes="range_row"):
            yield Button("1h",  id="h1",  variant="primary")
            yield Button("6h",  id="h6")
            yield Button("24h", id="h24")
        yield Label("", id="history_status")
        yield Label("TRENDS", classes="section_title")
        yield Vertical(id="hist_trends_container")
        yield DataTable(id="history_table")

    def on_mount(self) -> None:
        t = self.query_one("#history_table", DataTable)
        t.add_columns("Time", "Source", "°F", "°C")
        self._fetch_all()

    def watch_hours(self, _: int) -> None:
        self._fetch_all()

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
        for btn_id in ("h1", "h6", "h24"):
            self.query_one(f"#{btn_id}", Button).variant = (
                "primary" if btn_id == active_id else "default"
            )
        self.hours = hours

    @work(thread=True)
    def _fetch_all(self) -> None:
        self.app.call_from_thread(
            lambda: self.query_one("#history_status", Label).update("Loading...")
        )
        try:
            config = load_config()
            local = config["influx"]["local"]
            client = InfluxDBClient(url=local["url"], token=local["token"], org=local["org"])

            # Recent readings for the table (newest first, capped at 200)
            table_query = (
                f'from(bucket: "{local["bucket"]}")'
                f' |> range(start: -{self.hours}h)'
                f' |> filter(fn: (r) => r._measurement == "temperature" and r._field == "fahrenheit")'
                f' |> sort(columns: ["_time"], desc: true)'
                f' |> limit(n: 200)'
            )
            tables = client.query_api().query(table_query, org=local["org"])
            rows = []
            for table in tables:
                for rec in table.records:
                    t = rec.get_time()
                    t_str = t.strftime("%m/%d %H:%M:%S") if t else "--"
                    f_val = rec.get_value()
                    c_val = round((f_val - 32) * 5 / 9, 1) if f_val is not None else None
                    source = rec.values.get("source", "unknown")
                    rows.append((
                        t_str, source,
                        f"{f_val:.1f}" if f_val is not None else "--",
                        f"{c_val:.1f}" if c_val is not None else "--",
                    ))

            client.close()

            # Aggregated data for charts (separate query covers the full range)
            chart_data = fetch_chart_data(local, self.hours)
            charts = build_charts(chart_data)

            self.app.call_from_thread(self._apply_all, rows, charts)
        except Exception as e:
            err = f"[red]Query failed: {e}[/red]"
            self.app.call_from_thread(
                lambda: self.query_one("#history_status", Label).update(err)
            )

    def _apply_all(self, rows, charts: dict) -> None:
        self.query_one("#history_status", Label).update(
            f"{len(rows)} records (most recent first)"
        )
        mount_charts(self.query_one("#hist_trends_container", Vertical), charts)
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
    .temp_col { width: 16; padding-top: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("SENSOR NAMES", classes="section_title")
        yield Label("Names are written as the 'source' tag in InfluxDB.", classes="hint")
        yield Static("", id="sensors_body")
        yield Button("Save Names", id="save_sensors", variant="success")
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
        for sel in ("#sensors_body", "#sensors_container"):
            try:
                self.query_one(sel).remove()
            except Exception:
                pass

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
                    Input(value=name, placeholder="source name", id=f"s_{sensor_id}"),
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
            try:
                inp = self.query_one(f"#s_{sensor_id}", Input)
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
        yield Label("Leave URL blank to disable.")
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
        self.query_one("#cfg_local_url", Input).value    = local.get("url", "")
        self.query_one("#cfg_local_token", Input).value  = local.get("token", "")
        self.query_one("#cfg_local_org", Input).value    = local.get("org", "")
        self.query_one("#cfg_local_bucket", Input).value = local.get("bucket", "")

        remote = c.get("influx", {}).get("remote", {})
        self.query_one("#cfg_remote_url", Input).value    = remote.get("url", "")
        self.query_one("#cfg_remote_token", Input).value  = remote.get("token", "")
        self.query_one("#cfg_remote_org", Input).value    = remote.get("org", "")
        self.query_one("#cfg_remote_bucket", Input).value = remote.get("bucket", "")

    @on(Button.Pressed, "#save_settings")
    def save_settings(self) -> None:
        try:
            doc = load_config_rw()
        except Exception as e:
            self.query_one("#settings_status", Label).update(f"[red]Load error: {e}[/red]")
            return

        if "collector" not in doc:
            doc.add("collector", tomlkit.table())
        doc["collector"]["hostname"] = self.query_one("#cfg_hostname", Input).value.strip()
        try:
            doc["collector"]["interval_seconds"] = int(
                self.query_one("#cfg_interval", Input).value.strip()
            )
        except ValueError:
            self.query_one("#settings_status", Label).update(
                "[red]Interval must be a whole number.[/red]"
            )
            return

        if "influx" not in doc:
            doc.add("influx", tomlkit.table())
        if "local" not in doc["influx"]:
            doc["influx"].add("local", tomlkit.table())
        doc["influx"]["local"]["url"]    = self.query_one("#cfg_local_url", Input).value.strip()
        doc["influx"]["local"]["token"]  = self.query_one("#cfg_local_token", Input).value.strip()
        doc["influx"]["local"]["org"]    = self.query_one("#cfg_local_org", Input).value.strip()
        doc["influx"]["local"]["bucket"] = self.query_one("#cfg_local_bucket", Input).value.strip()

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
                "[green]Saved — collector reloads config automatically.[/green]"
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

.hint { color: $text-muted; margin-bottom: 1; }

Button { margin-right: 1; }
Input  { margin-bottom: 1; }

TabbedContent { height: 1fr; }
TabPane { padding: 0; overflow-y: auto; }
"""


class CollectorTUI(App):
    TITLE = "pi-collector"
    CSS = APP_CSS
    BINDINGS = [Binding("q", "quit", "Quit")]

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
