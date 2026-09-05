"""Open an Auta ONEX door over SIP with pjsua2 (invoked by bridge.py).

Registers the user's SIP extension over TLS, calls the monitor's extension with the
X-PlateNumber header (exactly like the app), and on answer sends DTMF '1' (open).

Env: AUTA_SIP_EXTENSION, AUTA_SIP_PASSWORD, AUTA_SIP_DOMAIN, TARGET (destination ext),
PLATE (panel number), DTMF (default '1'), SEND_DTMF ('1' to send, '0' to only test the call).
Optional: TARGET_URI (call this URI verbatim), DO_REGISTER ('1'/'0'), AUTA_PJSIP_LOG (log file).
"""
import os
import sys
import time

import pjsua2 as pj

EXT = os.environ["AUTA_SIP_EXTENSION"]
PW = os.environ["AUTA_SIP_PASSWORD"]
DOM = os.environ["AUTA_SIP_DOMAIN"]
TARGET = os.environ.get("TARGET", "3641")
TARGET_URI = os.environ.get("TARGET_URI", "")  # if set, this URI is called as-is
DO_REGISTER = os.environ.get("DO_REGISTER", "1") == "1"
PLATE = os.environ.get("PLATE", "1")
DTMF = os.environ.get("DTMF", "1")
SEND_DTMF = os.environ.get("SEND_DTMF", "1") == "1"
# Offer a video m-line in the INVITE (needed so V4.1+ monitors auto-answer). On by default;
# set AUTA_SIP_VIDEO=0 only if you run a pjsip built without video (older audio-only image).
VIDEO = os.environ.get("AUTA_SIP_VIDEO", "1") == "1"
PJSIP_LOG = os.environ.get("AUTA_PJSIP_LOG", "")  # optional full SIP trace to a file


def log(*a):
    print("[open]", *a, flush=True)


state = {"confirmed": False, "code": None, "done": False, "dtmf_sent": False}


def send_dtmf(call, digit):
    """Send DTMF; try RFC2833 first and fall back to SIP INFO."""
    errs = []
    try:
        call.dialDtmf(digit)
        return "rfc2833"
    except Exception as e:  # noqa: BLE001
        errs.append(f"rfc2833:{e!r}")
    try:
        p = pj.CallSendDtmfParam()
        p.method = pj.PJSUA_DTMF_METHOD_SIP_INFO
        p.digits = digit
        call.sendDtmf(p)
        return "sipinfo"
    except Exception as e:  # noqa: BLE001
        errs.append(f"sipinfo:{e!r}")
    log("DTMF failed:", "; ".join(errs))
    return None


class Call(pj.Call):
    def onCallState(self, prm):
        ci = self.getInfo()
        log(f"call state={ci.stateText} lastCode={ci.lastStatusCode} reason={ci.lastReason}")
        state["code"] = ci.lastStatusCode
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            state["confirmed"] = True
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            state["done"] = True

    def onCallMediaState(self, prm):
        # As soon as media is up, send the open tone right away (the call ends quickly).
        log("media state changed")
        if SEND_DTMF and state["confirmed"] and not state["dtmf_sent"]:
            m = send_dtmf(self, DTMF)
            if m:
                state["dtmf_sent"] = True
                log(f"DTMF '{DTMF}' sent via {m} (open)")


class Account(pj.Account):
    def onRegState(self, prm):
        log(f"reg state code={prm.code} reason={prm.reason}")


def main():
    ep = pj.Endpoint()
    ep.libCreate()
    ec = pj.EpConfig()
    ec.uaConfig.userAgent = "AutaONEX/1.6.0"
    ec.logConfig.level = 5
    ec.logConfig.consoleLevel = 2
    if PJSIP_LOG:
        ec.logConfig.filename = PJSIP_LOG
    ep.libInit(ec)

    tc = pj.TransportConfig()
    tc.tlsConfig.method = pj.PJSIP_TLSV1_2_METHOD
    tc.tlsConfig.verifyServer = False
    tc.tlsConfig.verifyClient = False
    ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, tc)
    ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, pj.TransportConfig())  # for a direct on-LAN INVITE
    ep.libStart()
    ep.audDevManager().setNullDev()  # no sound card in the container

    # Offer VIDEO in the INVITE. The ONEX monitor is a *video* intercom, and since firmware
    # V4.1 (2026-09) OnexGuiApp only auto-answers the on-demand "view door" call when the
    # INVITE carries a video m-line (an audio-only offer stays at 180 Ringing forever). We
    # never render or watch the stream — the offer alone is what makes the panel answer, and
    # then the DTMF opens. Use the built-in colorbar generator as the capture device so pjsip
    # has a source to (nominally) send without a real camera; no hardware needed.
    cap_dev = -1
    if VIDEO:
        try:
            vdm = ep.vidDevManager()
            cap_dev = next((i for i in range(vdm.getDevCount())
                            if vdm.getDevInfo(i).driver.lower().startswith("colorbar")), -1)
            log(f"endpoint started (TLS+UDP, null audio, video offer; colorbar dev {cap_dev})")
        except Exception as e:  # noqa: BLE001
            log("video device probe failed (video still offered):", repr(e))
    else:
        log("endpoint started (TLS+UDP, null audio, audio-only offer)")

    acfg = pj.AccountConfig()
    acfg.idUri = f"sip:{EXT}@{DOM}"
    acfg.regConfig.registrarUri = f"sip:{DOM};transport=tls"
    acfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", EXT, 0, PW))
    acfg.regConfig.registerOnAdd = DO_REGISTER
    if VIDEO:
        # Transmit our colorbar source, never pop up the incoming stream (headless container).
        # pjsip logs a benign "PJMEDIA_EVID_NODEFDEV" when it wires up the negotiated video with
        # no render device — harmless: the call still CONFIRMs and the DTMF (SIP INFO) still opens.
        acfg.videoConfig.autoTransmitOutgoing = True
        acfg.videoConfig.autoShowIncoming = False
        if cap_dev >= 0:
            try:
                acfg.videoConfig.defaultCaptureDevice = cap_dev
            except Exception as e:  # noqa: BLE001
                log("could not pin capture device (video still offered):", repr(e))
    acc = Account()
    acc.create(acfg)

    if DO_REGISTER:
        for _ in range(30):
            if acc.getInfo().regIsActive:
                break
            ep.libHandleEvents(200)
        log("registered:", acc.getInfo().regIsActive)
    else:
        for _ in range(5):
            ep.libHandleEvents(100)
        log("no registration (direct local call)")

    call = Call(acc)
    prm = pj.CallOpParam(True)
    if VIDEO:
        prm.opt.audioCount = 1
        prm.opt.videoCount = 1  # emit the video m-line V4.1 requires to auto-answer
    h = pj.SipHeader()
    h.hName = "X-PlateNumber"
    h.hValue = str(PLATE)
    prm.txOption.headers.append(h)
    target = TARGET_URI if TARGET_URI else f"sip:{TARGET}@{DOM};transport=tls"
    log(f"calling {target} with X-PlateNumber: {PLATE}")
    call.makeCall(target, prm)

    # wait for the call to connect (up to 25 s)
    t0 = time.time()
    while time.time() - t0 < 25 and not state["confirmed"] and not state["done"]:
        ep.libHandleEvents(100)

    if state["confirmed"]:
        log("CALL CONFIRMED (answered).")
        if SEND_DTMF:
            # in case onCallMediaState didn't send it, retry a few times
            for _ in range(6):
                if state["dtmf_sent"] or state["done"]:
                    break
                m = send_dtmf(call, DTMF)
                if m:
                    state["dtmf_sent"] = True
                    log(f"DTMF '{DTMF}' sent via {m} (open)")
                ep.libHandleEvents(250)
            # keep the call up a little after the tone
            t1 = time.time()
            while time.time() - t1 < 4 and not state["done"]:
                ep.libHandleEvents(100)
        else:
            log("SEND_DTMF=0: not sending the tone, only checking the connection")
            time.sleep(1)
    else:
        log(f"NOT answered. last SIP code={state['code']}")

    try:
        call.hangup(pj.CallOpParam(True))
    except Exception:
        pass
    t2 = time.time()
    while time.time() - t2 < 3 and not state["done"]:
        ep.libHandleEvents(100)

    log(f"RESULT confirmed={state['confirmed']} dtmf_sent={state['dtmf_sent']} last_code={state['code']}")
    ep.libDestroy()
    sys.exit(0 if state["confirmed"] else 2)


if __name__ == "__main__":
    main()
