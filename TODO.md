# TODO

## Features

- [x] `/api/v2` for OST_OCS (ost_ocs ADR 0008): sessions, status as heartbeat, preview, instance tiles (2026-11-20)
- [ ] `/api/v2`: end a session by hand in the dashboard (ADR 0008: "Beendet wird eine Sitzung nur vom OCS-Server
  oder von Hand im Dashboard"), with login + CSRF
- [ ] `/api/v2`: notification when an instance with a running session or planned night is disturbed (e.g. mail)
- [ ] Apache: allow POSTs of mobile setups (other hosts than the OMS) once they exist
- [ ] Agents stay silent while the OST_OCS server runs (`GET http://localhost:5148/api/health`, DSH-03), **step by step**:
  - [x] `telescope_agent.ps1`: silent while `http://localhost:5148/api/health` answers (override: `OSTOCS_HEALTH_URL`),
    because OST_OCS reports the mount via `/api/v2/status` (2026-11-20)
  - `autostart_client_prompt.ps1` (session prompt): only from ost_ocs stage 2, when OST_OCS runs the sessions
  - `host_status_agent.ps1`: stays until OST_OCS reports CPU/RAM/disk as well

- [ ] Detect the connected camera.
- [ ] Get the coordinates from the mount while it is running:
  - check whether the coordinates change
  - get and show an image of the observed region

## Rename internals to ost_dashboard

The repository and local folder are already called `ost_dashboard` (2026-11); the app itself
still uses `observatory_presence` everywhere.

- [ ] Rename `observatory_presence` to `ost_dashboard`:
  - Python module `observatory_presence.py` (gunicorn target, imports in `tests/`)
  - systemd unit, env file, `RuntimeDirectory` / socket path `/run/observatory_presence`
  - install dir `/mnt/data/observatory_presence`, state dir `/var/lib/observatory_presence`
    (`deploy/scripts/setup_data_dir.sh`, `DATA_FILE`)
  - Apache config, fail2ban filter and jail (`deploy/`)
  - README (setup paths, `systemctl` commands, Windows install path `C:\observatory_presence`)
  - Plan the server migration (stop service, move dirs, install new unit/config, disable old
    ones, short downtime).
- [ ] Serve the dashboard under `/ost_dashboard` **in addition to** `/ost_status`.
  `/ost_status` stays for now (bookmarks, QR code on the camera notice, Windows agents).
  - The app currently has a single, process-wide prefix (`BASE_PATH`), which also sets
    `static_url_path` and `SESSION_COOKIE_PATH`. Decide between:
    - one canonical prefix (`/ost_dashboard`) with Apache redirects from `/ost_status`
      (simple, but redirects are not reliable for the agents' POSTs; switch the agents to the
      new URL first, or keep the agent endpoints proxied under `/ost_status`), or
    - deriving the prefix per request (e.g. `ProxyFix(x_prefix=1)` + `X-Forwarded-Prefix` set
      per `Location` in Apache) so both prefixes are served directly.
  - Apache: `Alias`, `ProxyPass` and `Location` blocks (campus / VPN allowlist, public
    exceptions for `datenschutz`, `privacy`, CSS/fonts, login) for both prefixes.
  - fail2ban filter must match failed requests under both prefixes.
  - Session cookie (`ost_status_session`, path = prefix): users signed in under one prefix are
    not signed in under the other.
  - Update `deploy/tests/*.sh`, README URLs, the central privacy policy if it names the URL
    (`ost_landing_page`, `static/datenschutz.html`) and later the agents' `OBS_PRESENCE_SERVER`.

## Data protection / retention

- [x] Observing session log (`SESSION_LOG_FILE`): **kept permanently on purpose** (decided 2026-09).
  Observers have consented to the log in its current form; it records who made which
  observation so they can be credited when the data is reused (e.g. in papers). Legal basis in
  the central privacy policy (`#status` / `#en-status`): consent, withdrawal removes the name
  from the entries concerned (by hand in `SESSION_LOG_FILE`).
