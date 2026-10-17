# pi_data_collector_with_influx

**Superseded by `DataCollector` and archived (owner, 2026-10-17).** Do not build
on this repository. It was an early 1-Wire temperature collector for a Pi
(daemon, Textual TUI, InfluxDB writer) from before DataCollector was overhauled.

Two of its features are on DataCollector's backlog, and this code is the
reference for them: humidity reads (`read_sensor_humidity` in `collector.py`)
and writing to a local plus a remote InfluxDB in one cycle (`write_points`).
The checklist below is not being worked.

---

## Standards adoption

Universal standards live in [`engineering-standards`](https://github.com/Joe-User/engineering-standards) and are in force here. The shared pre-commit baseline is **already installed** in this repo (secrets, text hygiene, commit format).

**Outstanding for this repo.** Pick these up the next time you work here rather than as a separate campaign:

- [x] Apply the **python** kit. Copy `kits/python/files/` and follow each kit's README.
- [ ] Write `docs/DEPLOYMENT.md` from `templates/DEPLOYMENT.md`, answering the seven requirements for the **systemd-raspberry-pi** target. Playbook: `standards/deployment/systemd-raspberry-pi.md`.
- [ ] **This repo is PUBLIC** while every other repo in the portfolio is private. Confirm that is intentional.
- [ ] **Revoke the GitHub PAT** that was embedded in the remote URL until 2026-08-08.
- [ ] `setup.sh` scrapes an InfluxDB token out of a legacy `temperature_daemon.py`. A secret should be supplied at install, not recovered from a prior install.
- [x] `ruff` is configured in `pyproject.toml` and now runs in pre-commit and CI.
