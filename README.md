# Auta ONEX door opener

Open an **Auta ONEX** IP intercom door from **Home Assistant / HomeKit / Siri**, using
**your own account** — no changes to the physical monitor or street panel, no extra
hardware, no cloud middleman other than Auta's own.

It's a tiny HTTP service that reproduces exactly what the official *Auta ONEX Wi-Fi* app
does when you open the door from an on-demand "view door" call: it logs in with your
account, tells the panel to get ready, places a SIP call to the panel through Auta's
cloud PBX, and sends the DTMF tone that releases the door. Home Assistant then exposes a
button/switch you can press or say *"Hey Siri, turn on Open Door"*.

> Only works with your own account and your own installation. It uses the same private
> API and SIP flow the app uses. It is meant for interoperability/home‑automation of an
> intercom **you are entitled to open**. See [Notes & safety](#notes--safety).

---

## How it works

```
At startup (once, then cached):
  auta-bridge ── login → JWT  +  auto-discover  GET /SIP, /monitor, /plate   (onex.auta.es API)
                 └ cached & warmed; JWT refreshed on 401, discovery re-run if it goes stale

On each open:
Home Assistant ──POST /open──▶ auta-bridge (this service)
                                   │ 1. PUT plate_call    arm the panel        (cached JWT)
                                   │ 2. SIP REGISTER + CALL sip:<monitor_ext>@PBX
                                   │    with header  X-PlateNumber: <n>
                                   │ 3. on answer → DTMF '1'  (open)      via SIP INFO
                                   ▼
                          Auta cloud PBX (ast-ssl.pro.auta.es) ──▶ your monitor ──bus──▶ door relay
```

Key facts that make it work (and that took some digging, see
[PROTOCOL.md](PROTOCOL.md)):

- The door is opened by **DTMF `1` during an active SIP call** to the panel. There is **no
  HTTP "open" endpoint**; the app does it in-call.
- The call must go to the **real PBX `ast-ssl.pro.auta.es`**, *not* the host the API reports
  as your SIP "domain" (that host is the API front-end and returns `503`).
- The panel is selected with a custom SIP header **`X-PlateNumber`** on the INVITE.
- The tone is sent over **SIP INFO** (pjsua2's RFC2833 path isn't negotiated here).
- Your monitor must be **online and registered** on the PBX (it's the SIP↔door-bus gateway).

The SIP part runs in an isolated subprocess (`sip_open.py`, using
[pjsua2](https://github.com/pjsip/pjproject)) so a SIP hiccup can't crash the service.

**Caching & resilience.** Login and discovery run **once at startup** and are cached, so a
normal open is just `plate_call` + the SIP call — no login/discovery round-trips. The JWT is
re-fetched automatically if it expires (a `401`), and discovery is re-run if it goes stale
(the monitor/panel changed). If the bridge starts while the monitor is offline it still
caches fine (login and discovery are cloud calls, independent of the monitor); opens then
fail cleanly with `503` until the monitor is back, and work again with no restart.

---

## Requirements

- Docker + Docker Compose.
- Home Assistant on the **same host** (the bridge listens on `127.0.0.1:8092`), or adjust
  `AUTA_BRIDGE_HOST`/networking to reach it.
- Your **Auta ONEX / MyOnex app credentials** (email + password).
- Your **monitor powered on and connected** (it must be registered on Auta's PBX — check the
  app shows the monitor as *available*).

## Setup

```bash
git clone <this-repo> auta-onex-opener
cd auta-onex-opener
cp .env.example .env
$EDITOR .env            # set AUTA_USER and AUTA_PASS
docker compose up -d --build
```

The first build compiles pjsip (a few minutes). Then check it's up:

```bash
curl -s http://127.0.0.1:8092/health          # {"ok": true}
curl -s -X POST 'http://127.0.0.1:8092/open?dry=1'   # connects to the panel, does NOT open
```

`dry=1` should return `"connected": true`. If it returns `"connected": false`, your monitor
is probably offline/unregistered (open the app and confirm it's *available*), or the PBX
host is wrong for your region — see [Troubleshooting](#troubleshooting).

Open for real:

```bash
curl -s -X POST http://127.0.0.1:8092/open     # 202 immediately; opens in the background
```

### Configuration (`.env`)

| Variable | Required | Description |
|---|---|---|
| `AUTA_USER` | ✅ | Your app account email. |
| `AUTA_PASS` | ✅ | Your app account password. |
| `AUTA_PBX` | – | SIP registrar the monitor/panel use. Default `ast-ssl.pro.auta.es` (Auta's production FreePBX). |
| `AUTA_SIP_EXTENSION`, `AUTA_SIP_PASSWORD` | – | Your SIP creds. Auto-discovered from `GET /SIP` if unset. |
| `AUTA_MONITOR_ID`, `AUTA_MONITOR_EXT` | – | Monitor DB id and SIP extension. Auto-discovered from `GET /monitor` if unset. |
| `AUTA_PLATE_NUMBER` | – | Street-panel number. Auto-discovered from `GET /plate` if unset. |
| `AUTA_BRIDGE_HOST`/`AUTA_BRIDGE_PORT` | – | Where the bridge listens. Default `127.0.0.1:8092`. |
| `AUTA_MONITOR_IP` | – | Monitor's LAN IP. Enables the optional keepalive / self-heal ([see below](#monitor-keeps-going-unavailable-optional-keepalive)). Off if unset. |
| `AUTA_KEEPALIVE_INTERVAL` | – | Seconds between keepalive nudges when `AUTA_MONITOR_IP` is set. Default `120`. |

If you have **several monitors or panels**, discovery picks the first of each; set the
`AUTA_MONITOR_*`/`AUTA_PLATE_NUMBER` overrides to choose.

### HTTP API

| Method / path | Does |
|---|---|
| `GET /health` | liveness — `{"ok": true}` |
| `POST /open` | fire-and-forget open; returns `202 {"started": true}` at once |
| `POST /open?wait=1` | open and wait; returns `{"opened": true/false, "connected": …, "took_s": …}` |
| `POST /open?dry=1` | connect to the panel but **do not** send the open tone |

## Home Assistant integration

1. Enable packages (in `configuration.yaml`):

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Copy [`homeassistant/packages/auta.yaml`](homeassistant/packages/auta.yaml) into
   `config/packages/` and **restart** Home Assistant.

You get:

- `button.open_door` — press to open.
- `switch.open_door` — a **momentary** switch (opens, then pulses off in ~1 s so the HomeKit
  tile never sticks "on"). Expose this one in the **HomeKit bridge** to say *"Hey Siri, turn
  on Open Door"*.
- `rest_command.auta_open_door` / `auta_test_door` for automations.

## Troubleshooting

- **`dry` returns `connected: false` / real open does nothing** → the monitor isn't
  registered. Open the Auta app: if it shows *"monitor unavailable"*, power-cycle the monitor
  (unplug ~30 s) and retry once it's available.
- **HomeKit tile stays "on"** → make sure you exposed `switch.open_door` (the pulsed one), not
  a plain `rest_command` switch, and that the bridge answers `/open` instantly (it does by
  default). Reset the accessory in the Home app if a stale state is cached.
- **Everything returns `503`** and even `dry` fails right away → your PBX host differs. The
  monitor's registrar is the value in `AUTA_PBX`; for most installs it's `ast-ssl.pro.auta.es`.

### Monitor keeps going "unavailable" (optional keepalive)

Some ONEX monitors run an old firmware whose registration to the cloud PBX goes **stale after a
few idle minutes**: the monitor still *thinks* it's registered (the app and its own status say so),
but the PBX can no longer reach it, so opens — and the **official app** too — start failing until the
monitor is rebooted. A reboot only helps because it forces a fresh SIP `REGISTER`.

If you set **`AUTA_MONITOR_IP`** (your monitor's LAN IP), the bridge fixes this itself, no reboots:

- a background **keepalive** nudges the monitor every `AUTA_KEEPALIVE_INTERVAL` seconds (default 120)
  — it makes the monitor's own SIP client send a tiny `OPTIONS` to the registrar, which keeps the
  PBX route warm (this also keeps the **app** working);
- and on a failed open it **self-heals**: it nudges the monitor and retries the open once.

Give the monitor a **static or DHCP-reserved IP** before you set this, so `AUTA_MONITOR_IP` stays
valid — if the monitor's address later changes, the nudges silently miss and the route goes stale
again. (Some monitors also need a reboot to rebind after their IP changes.)

This works by talking to the monitor's **local baresip control port** (`:8000`), which is **open and
unauthenticated** on these monitors. It's off unless you set `AUTA_MONITOR_IP`. Only enable it on a
network you trust, and keep the intercom on an isolated IoT VLAN.

## Notes & safety

- This talks to Auta's private cloud API/PBX **as you**, with your credentials. Use it only
  for your own intercom.
- **Don't hammer it.** Older ONEX monitors run an old SIP stack; many rapid calls can knock
  the monitor's registration offline (it recovers with a power-cycle). One press when you
  need it — like the app — is fine.
- Credentials live only in your local `.env` (git-ignored). Nothing is sent anywhere except
  Auta's own servers.
- If you want a rock-solid, Auta-independent opener, wiring a **dry-contact relay** (e.g. a
  Shelly Plus 1) across your monitor's "open" button is the sturdier route; this project is
  the software-only alternative.

See [PROTOCOL.md](PROTOCOL.md) for the full protocol and how it was reverse-engineered.
