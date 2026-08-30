# Auta ONEX — protocol & reverse-engineering notes

How the Auta ONEX Wi-Fi system opens a door, and how this was worked out from the
Android app. Written so you can reproduce or extend it.

## The system in one paragraph

Auta ONEX is a **SIP video intercom over a cloud PBX**. The street panel(s) sit on the
Auta 2-wire bus; the indoor **monitor** is the gateway that bridges the bus to SIP and
registers to Auta's cloud **FreePBX**. Your phone app registers to the same PBX as a
separate SIP extension. When someone rings, the panel → monitor → PBX → your app rings;
during that call you press "open", which sends a **DTMF tone** the monitor turns into a
bus command that fires the door relay. The app can also start that call **on demand**
("view door"), and that is what this project reproduces.

## Cloud API — `https://onex.auta.es:8443/api/`

- Auth is a **JWT**. `POST /login` as `multipart/form-data` with fields `Email`,
  `Password`, `FirebaseToken` → `{"response": {"Token": "<jwt>", "Name": "..."}}`.
- All other calls carry `Authorization: BEARER <jwt>` (the header value is the literal
  word `BEARER`, uppercase).
- Relevant endpoints (from the app's Retrofit interface):
  - `GET /SIP` → your SIP account: `{"SIP": {"Extension", "Password", "Domain"}}`.
  - `GET /monitor` → your monitors; each has `MonitorID`, `Name` and its own
    `SIP: {Extension, Password, Domain}`.
  - `GET /plate` → your street panels; each has `PlateID`, `Number`, and its parent
    `Monitor`.
  - `PUT /monitor/{monitorId}/plate_call` with body `{"PlateCall": "<n>"}` → **arms** the
    panel for the on-demand call (it just records which panel; it does *not* place a call).
  - plus `GET /user`, `GET /file` (recordings), `POST /register`, `password/*`,
    `email/verify*`, `PUT /token` (FCM), etc.

## SIP registration & the call

- The monitor and the app register over **TLS** to the PBX. The app builds the account
  from `GET /SIP` (`sip:<ext>@<domain>`), TLS transport.
- The on-demand "view door" flow (from the app) is:
  1. `PUT /monitor/{id}/plate_call {"PlateCall": "<panel_number>"}`.
  2. SIP INVITE to **`sip:<monitor_ext>@<pbx>`** with a custom header **`X-PlateNumber: <panel_number>`**
     (in the app: `startVideoCall("<monitor_ext>?X-PlateNumber=<n>")`).
  3. The PBX bridges to the panel; the panel answers (`200 OK`), video/audio flow.

### The PBX gotcha (the source of endless `503`s)

`GET /SIP` reports the account **domain as the API host** (`onex.auta.es`, an IP). But the
monitor actually **registers to a different host** — Auta's production FreePBX
`ast-ssl.pro.auta.es` (a different IP; cert `CN=freepbx.pro.auta.es`). Calling
`sip:<monitor_ext>@onex.auta.es` returns **`503 Service Unavailable` / `Q.850;cause=34`
(no channel)** because that host doesn't route to the monitor. Register **and** call on
`ast-ssl.pro.auta.es` and it connects. This project defaults `AUTA_PBX` to that host.

> How to find your PBX host if it differs: the monitor's own SIP stack reports it. On ONEX
> monitors the indoor unit runs **baresip with an (unauthenticated) HTTP control interface**
> on port 8000; `GET http://<monitor-ip>:8000/?%2Freginfo` prints e.g.
> `sip:3641@ast-ssl.pro.auta.es`. (Don't rely on that interface for control — it's a fragile,
> old baresip; here it's only used to read the registrar name.)

## Opening the door — DTMF during the call

The app opens by sending a DTMF tone on the active call (abto SDK, `sendTone(callId, c)`):

| Tone | Action |
|---|---|
| **`1`** | **open the door** |
| `2` | change panel / video on |
| `3` / `4` | aux on / off |
| `5` / `6` | video HD / SD |
| `7` | screenshot |
| `8` / `9` | start / stop recording |
| `0` | video off |

There is **no HTTP endpoint** for opening — it only happens in-call. Here the tone is sent
via **SIP INFO** (`pjsua2` `sendDtmf` with `PJSUA_DTMF_METHOD_SIP_INFO`); RFC2833 needs a
negotiated `telephone-event`, which didn't come up in this setup, so SIP INFO is used and
works.

## Detecting a ring — incoming call on the monitor

When a visitor presses your button on the street panel, the panel places a SIP call that rings
the monitor. There's no cloud webhook for that, but the monitor's local baresip control port
(the same `:8000` used for the registrar) can list active calls **locally, with no cloud traffic**:

```
GET http://<monitor-ip>:8000/?%2Flistcalls
--- Active calls (0) ---                     # idle
--- Active calls (1) ---                     # a call is up
> [line 1]  0:00:00   INCOMING   sip:<caller>@<ip>
```

An unanswered *ringing* call already shows up here (state `0:00:00`, `INCOMING`), so you can poll
`/listcalls` to know when someone is ringing. Two things make it reliable:

- **Tell a real ring from your own open.** Your own open call also appears as `INCOMING`, but from
  *your* SIP extension (e.g. `sip:3685@…`). So treat as a ring only an `INCOMING` call whose caller
  is **not** your extension.
- **Poll strictly serially.** These monitors are old and fragile under load; issue the next poll only
  after the previous one returns, so requests never stack up. A ~1 s cadence catches a ring long
  before it times out, and a plain local `GET` is cheap (unlike SIP call attempts, which are not).

This is exactly what the bridge's optional ring watcher does (see the README) — poll `/listcalls`,
ignore your own extension, and POST the on/off edges to a Home Assistant webhook.

## How this was reverse-engineered (procedure)

1. **Locate the device & app.** Find the monitor on the network; grab the app,
   `com.auta.intercom` (*Auta ONEX Wi-Fi*), from a mirror.
2. **Decompile.** `jadx -d out auta.apk`. It's native Kotlin (Hilt, Retrofit, Gson) with
   the **abto** SIP SDK (`org.abtollc`). Read:
   - `repository/ApiInterface.kt` — every REST route (login, `/SIP`, `/monitor`, `/plate`,
     `plate_call`, …) with methods and body shapes.
   - `repository/UserRepository` — the login body (`Email`/`Password`/`FirebaseToken`
     multipart) and the `BEARER <token>` interceptor.
   - `util/SipManager` — TLS registration and `addAccount(domain, "", ext, pass, …)`.
   - `ui/call/BaseCallActivity` — `openDoor()` = `sendTone(callId, '1')` and the whole tone
     table; `startOutgoingCallByIntent()` = `startVideoCall("<ext>?X-PlateNumber=<n>")`.
   - `ui/autaonex/*` — which id/extension the outgoing call uses
     (`REMOTE_CONTACT = monitor.sip.extension`, `OUTGOING_MONITOR_ID = monitor.id`).
3. **Hit the real API.** Reproduce `login` and read `/SIP`, `/monitor`, `/plate` to get the
   real extensions, ids and panel number (all read-only).
4. **Replay the SIP call.** With a scriptable SIP stack (**pjsua2**), register the user
   extension over TLS and call `sip:<monitor_ext>@<domain>` with the `X-PlateNumber` header,
   turning on full SIP tracing. This is where the `503 / cause=34` showed up.
5. **Fix the routing.** DNS-resolve the hostnames the API and the monitor use; discover the
   monitor registers to `ast-ssl.pro.auta.es` (different host). Register/call there → the
   panel answers.
6. **Send the tone.** On `CONFIRMED`, `dialDtmf("1")` (RFC2833) failed (no `telephone-event`
   negotiated); `sendDtmf(SIP_INFO, "1")` succeeded → door opens.
7. **Package.** Wrap it (`login → plate_call → register → call → DTMF`) behind a small HTTP
   endpoint and drive it from Home Assistant.

## Gotchas learned the hard way

- **The monitor must be registered.** If it's off/asleep, everything is `503`. It's the
  gateway to the door bus.
- **Old, fragile SIP stack.** Rapid repeated calls can drop the monitor's registration
  ("monitor unavailable" in the app); a power-cycle restores it. Don't stress-test it — one
  call per open, like the app.
- **`plate_call` doesn't call.** It only records the panel selection; the SIP INVITE is what
  places the call.
