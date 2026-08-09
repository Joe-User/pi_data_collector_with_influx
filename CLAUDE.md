# pi_data_collector_with_influx

> This file exists to carry the standards notice below. It does not yet describe the project. When you next work here, add what a newcomer would need: what it is, how to run it, and the traps. Start from `templates/repo-CLAUDE.md` in the standards repo, and record only what is NOT true of every other repo.

---

## Standards adoption

Universal standards live in [`engineering-standards`](https://github.com/Joe-User/engineering-standards) and are in force here. The shared pre-commit baseline is **already installed** in this repo (secrets, text hygiene, commit format).

**Outstanding for this repo.** Pick these up the next time you work here rather than as a separate campaign:

- [ ] Apply the **python** kit (CI is not present yet). Copy `kits/python/files/` and follow each kit's README.
- [ ] Write `docs/DEPLOYMENT.md` from `templates/DEPLOYMENT.md`, answering the seven requirements for the **systemd-raspberry-pi** target. Playbook: `standards/deployment/systemd-raspberry-pi.md`.
- [ ] **This repo is PUBLIC** while every other repo in the portfolio is private. Confirm that is intentional.
- [ ] **Revoke the GitHub PAT** that was embedded in the remote URL until 2026-08-08.
- [ ] `setup.sh` scrapes an InfluxDB token out of a legacy `temperature_daemon.py`. A secret should be supplied at install, not recovered from a prior install.
- [ ] `ruff` is configured in `pyproject.toml` but nothing runs it.
