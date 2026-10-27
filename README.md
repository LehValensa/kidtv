# kidtv - Chrome TV time limiter for Ubuntu

Limits how long a child can watch video in Chrome (YouTube, YouTube Kids, anything that plays media) on an Ubuntu machine, without touching the network. The machine can keep working as a media server while the child's playback is paused when time is up.

- **Daily allowance** (default 2 hours), counted only while video is actually playing.
- **Quiet windows** when TV is not allowed (default 21:00-07:00 sleep and 13:00-15:00 nap).
- **Easy extension** from a phone: add time, or lift the quiet windows for a while.
- **Web page** for status, daily actions and all settings. No password (see [Security](#security)).
- Runs as a root `systemd` service, so the child's account cannot stop it or change its settings.

## How it works

Chrome publishes its media player on the desktop D-Bus using the standard MPRIS interface. `kidtv` polls it every 2 seconds through `playerctl`, running as the child's user:

1. If the player says **Playing** and playback is allowed, the elapsed seconds are added to today's counter.
2. If the player says **Playing** and playback is *not* allowed (quiet window, or allowance used up), it is **paused** immediately and a short notification is shown on screen.
3. Optionally, if the child keeps pressing play (5 pauses within a minute by default), Chrome is closed. This is **off by default**.

Nothing else on the machine is affected: other users, services and the network are untouched.

## Requirements

- Ubuntu (any version with systemd and a desktop session for the child's account)
- Python 3 (standard library only)
- `playerctl` and `libnotify-bin` (for `notify-send`)
- Chrome or Chromium running in the child's own desktop session
- The child's account is a normal user **without `sudo`**

## Install

Copy the files to the machine, then:

```bash
sudo apt install playerctl libnotify-bin

sudo install -m 755 kidtv.py /usr/local/bin/kidtv
sudo cp kidtv.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now kidtv
```

Check that it runs:

```bash
systemctl status kidtv
journalctl -u kidtv -f
kidtv status
```

Then open `http://<server-ip>:8731/` on your phone.

### Check that Chrome is visible first

Log in as the child, play a YouTube video, and run:

```bash
playerctl -l                  # should list chromium.instanceNNNN
playerctl -p chromium status  # Playing / Paused
playerctl -p chromium pause
```

If the player name does not start with `chrome` or `chromium`, `kidtv` will not see it.

### Update to a new version

```bash
sudo install -m 755 kidtv.py /usr/local/bin/kidtv
sudo cp kidtv.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart kidtv
```

Settings and today's counters are kept.

### Uninstall

```bash
sudo systemctl disable --now kidtv
sudo rm /etc/systemd/system/kidtv.service /usr/local/bin/kidtv
sudo rm -r /var/lib/kidtv
sudo systemctl daemon-reload
```

## Using the web page

Open `http://<server-ip>:8731/` and bookmark it on your phone. It refreshes every 5 seconds.

| Section | What it does |
|---|---|
| Status card | Time left today, progress bar, whether TV is allowed or blocked (and why), whether something is playing now |
| Add time today | +15 / +30 / +60 minutes, or a custom number to add or remove |
| Sleep / nap times | Lift the quiet windows for 30 min, 1 h or 2 h; switch them back on; reset today's used time |
| Settings | All options from the table below |

"Add time" bonuses and "lift quiet windows" apply to **today only**. Used time and bonuses reset at midnight.

## Command line

```bash
kidtv status            # used / left today, blocked or not
sudo kidtv extend 60    # +60 minutes today (a negative number removes time)
sudo kidtv unlock 90    # ignore the quiet windows for 90 minutes
sudo kidtv lock         # cancel unlock
sudo kidtv reset        # set today's used time back to 0
```

The command line shows and changes the same state as the web page.

## Settings

Changed on the web page (stored in `/var/lib/kidtv/config.json`). The daemon picks changes up within about 2 seconds, with no restart.

| Setting | Default | Notes |
|---|---|---|
| Kid's Linux account | `sofy` | Must exist, cannot be `root`. The first-run default comes from `KIDTV_USER` in the service file |
| Daily allowance (minutes) | `120` | 0-1440 |
| Quiet windows | `21:00-07:00`, `13:00-15:00` | One per line, `HH:MM-HH:MM`. A window may cross midnight |
| Warn when minutes left | `10, 2` | A short on-screen notification. Empty for no warnings |
| Close Chrome if the kid keeps pressing play | **off** | If on, Chrome is closed after the pauses below |
| ...after this many pauses within a minute | `5` | 2-50 |
| Chrome process name | `chrome` | `chrome` for Google Chrome, `chromium` for Chromium. Only used when closing Chrome |

Defaults apply only when `config.json` does not exist yet. An existing install keeps the values it already has: change them on the web page, or delete `/var/lib/kidtv/config.json` and restart to go back to the defaults.

### Service environment variables

Set in `kidtv.service` (run `sudo systemctl daemon-reload && sudo systemctl restart kidtv` after changing).

| Variable | Default | Meaning |
|---|---|---|
| `KIDTV_USER` | `sofy` | First-run default for the kid's account |
| `KIDTV_PORT` | `8731` | Web page port |
| `KIDTV_BIND` | `0.0.0.0` | Web page address. Use a LAN address or `127.0.0.1` to restrict it |
| `KIDTV_STATE` | `/var/lib/kidtv/state.json` | Daily counters |
| `KIDTV_CONFIG` | `/var/lib/kidtv/config.json` | Settings |

## Home Assistant

The web page has a small JSON API, so Home Assistant can add time with a button or automation:

```yaml
rest_command:
  kidtv_extend_60:
    url: "http://<server-ip>:8731/api/extend"
    method: POST
    content_type: "application/json"
    payload: '{"minutes": 60}'
```

Other endpoints:

| Endpoint | Method | Body |
|---|---|---|
| `/api/status` | GET | |
| `/api/config` | GET | |
| `/api/config` | POST | any subset of the settings, e.g. `{"daily_allowance_min": 90}` |
| `/api/extend` | POST | `{"minutes": 30}` (negative removes time) |
| `/api/unlock` | POST | `{"minutes": 60}` |
| `/api/lock` | POST | `{}` |
| `/api/reset` | POST | `{}` |

POST requests must have `Content-Type: application/json`.

## Files

| Path | Purpose |
|---|---|
| `/usr/local/bin/kidtv` | The program (daemon, web page and command line in one file) |
| `/etc/systemd/system/kidtv.service` | systemd unit |
| `/var/lib/kidtv/config.json` | Settings (root-owned) |
| `/var/lib/kidtv/state.json` | Today's used time and bonuses (root-owned) |

## Troubleshooting

**Playback is not paused.** Run `journalctl -u kidtv -f` while a video plays. If you never see `paused ...` lines when blocked, check `playerctl -l` as the child (see [above](#check-that-chrome-is-visible-first)). The child must be logged in to a desktop session: the service needs `/run/user/<uid>/bus`.

**Time is not counted.** `kidtv status` shows "Used today". Only seconds with status *Playing* count. A paused or buffering tab does not.

**No notifications appear.** Install `libnotify-bin`. Pausing works without it. GNOME may show a notification for less than 10 seconds because it uses its own timeout. This is normal.

**The web page does not open.** Check `ss -ltn | grep 8731` (is something else on the port?), `journalctl -u kidtv` for "web interface NOT started", and the firewall: `sudo ufw allow from 192.168.0.0/16 to any port 8731` (adjust the range to your network).

**"No such Linux user" when saving settings.** The account name must match `getent passwd <name>` exactly.

**Two videos at once.** Chrome reports one active media session per player, so `kidtv` acts on every Chrome player it can see, but a second tab may be missed if Chrome does not list it.

## Security

This is designed for a small child, not a determined teenager.

- The web page has **no password**. Anyone on your network who knows the address can use it. Keep the address off the child's devices.
- The child's account must be a **normal user without `sudo`**, otherwise it could stop the service.
- `kidtv` controls Chrome's media playback only. It does not block other browsers or players. Remove them or restrict the child's account if that matters.
- The service runs as root so the settings and counters cannot be edited by the child. It only runs `playerctl`, `notify-send` and (optionally) `pkill` as the configured user.

## Limits

- It pauses; it cannot stop a child from pressing play again. Enable *Close Chrome if the kid keeps pressing play* if that happens.
- Time is counted per day on this machine. Other devices are not counted.
- Counting needs the service to be running. If the machine is off, nothing is counted.
