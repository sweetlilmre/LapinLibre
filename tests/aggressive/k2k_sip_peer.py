# SPDX-License-Identifier: MIT
"""k2k-daemon: SIP calls on the wire, with a SIP peer on the loopback interface.

The set-up is k2k_daemon's, but with /dev/dsp and /dev/mixer as regular files. A minimal SIP peer (this file, run with "peer") runs inside the daemon's
user and network namespaces (nsenter, as voice_unanswered does), brings the loopback interface up
and listens on 127.0.0.1:5070; the daemon's own transport is on port 5060. A fake controller says
the rabbit is available (the daemon starts busy without one). Then:

- outgoing: k2k_call("sip:bob@127.0.0.1:5070"); the peer answers 180 Ringing, then 486 Busy Here.
  The daemon must send an INVITE with an SDP offer, acknowledge the 486 and signal finished_call;
- declined: the peer calls the rabbit; the daemon signals incoming_call, k2k_answer(false)
  declines it with a final response of 400 or more, which the peer acknowledges;
- answered: the peer calls again; k2k_answer(true) answers 200 OK with SDP, the peer acknowledges
  it, the daemon signals beginning_call; the peer hangs up (BYE), which the daemon answers 200
  and signals finished_call.

What the peer received and sent is logged and kept in the record; check() lists what's missing.
ARM only (the daemon's SIP threads need qemu-user's NPTL support).
"""

import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

# Also when run as the peer (python -I, so tests/ isn't on the path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenarios import k2k_daemon as k2k  # noqa: E402

DAEMON = k2k.DAEMON
DEVICES = k2k.DEVICES
FAKE_IOCTL_TYPES = k2k.FAKE_IOCTL_TYPES
# Regular files, not k2k_daemon's FIFOs: a call opens /dev/dsp for its tones and media, and a FIFO
# without a reader would block the daemon there.
REGULAR_FILE_DEVICES = ["dsp", "mixer"]

PEER = ("127.0.0.1", 5070)
RABBIT = ("127.0.0.1", 5060)
SDP = ("v=0\r\no=eve 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
       "m=audio 7078 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
AVAILABLE = 16


# --- the peer (runs inside the daemon's network namespace) ---------------------------------------

def headers(text):
    """The first line and the headers of a SIP message: (line, {lower-case name: [values]}, body)."""
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    fields = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        fields.setdefault(name.strip().lower(), []).append(value.strip())
    return lines[0], fields, body


def response(request, code, reason, tag, body=""):
    """A response to `request` (its text), with a To tag and an optional SDP body."""
    _, fields, _ = headers(request)
    to = fields["to"][0] + ("" if ";tag=" in fields["to"][0] else f";tag={tag}")
    lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in fields["via"]] + [
        f"From: {fields['from'][0]}", f"To: {to}", f"Call-ID: {fields['call-id'][0]}",
        f"CSeq: {fields['cseq'][0]}", f"Contact: <sip:bob@{PEER[0]}:{PEER[1]}>"]
    if body:
        lines.append("Content-Type: application/sdp")
    lines.append(f"Content-Length: {len(body)}")
    return "\r\n".join(lines) + "\r\n\r\n" + body


def peer(ctrl):
    subprocess.run(["ip", "link", "set", "lo", "up"], capture_output=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(PEER)
    log = open(os.path.join(ctrl, "sip.log"), "a", buffering=1)
    dialog = {}

    def send(text, address):
        sock.sendto(text.encode(), address)
        log.write("-> " + text.split("\r\n", 1)[0] + "\n")

    def request(method, uri, cseq, branch, to_tag=""):
        body = SDP if method == "INVITE" else ""
        lines = [f"{method} {uri} SIP/2.0", f"Via: SIP/2.0/UDP {PEER[0]}:{PEER[1]};branch={branch};rport",
                 "Max-Forwards: 70", f"From: <sip:eve@{PEER[0]}:{PEER[1]}>;tag={dialog['tag']}",
                 f"To: <sip:rabbit@{RABBIT[0]}:{RABBIT[1]}>" + (f";tag={to_tag}" if to_tag else ""),
                 f"Call-ID: {dialog['call_id']}", f"CSeq: {cseq} {method}",
                 f"Contact: <sip:eve@{PEER[0]}:{PEER[1]}>"]
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
        send("\r\n".join(lines) + "\r\n\r\n" + body, RABBIT)

    def receive():
        while True:
            data, address = sock.recvfrom(65535)
            text = data.decode("utf-8", "replace")
            line, fields, body = headers(text)
            log.write(f"<- {line}" + (" (sdp: m=audio)" if "m=audio" in body else "") + "\n")
            if line.startswith("INVITE "):
                send(response(text, 180, "Ringing", "peer1"), address)
                send(response(text, 486, "Busy Here", "peer1"), address)
            elif line.startswith(("BYE ", "OPTIONS ", "CANCEL ")):
                send(response(text, 200, "OK", "peer1"), address)
            elif line.startswith("SIP/2.0 "):
                code = int(line.split()[1])
                cseq = fields.get("cseq", [""])[0]
                if cseq.endswith("INVITE") and code >= 200:
                    to_tag = re.search(r";tag=([^;>\s]+)", fields["to"][0])
                    dialog["to_tag"] = to_tag.group(1) if to_tag else ""
                    # A 2xx is acknowledged in a new transaction, a failure in the INVITE's own.
                    branch = dialog["branch"] if code >= 300 else "z9hG4bK" + str(random.getrandbits(40))
                    request("ACK", f"sip:rabbit@{RABBIT[0]}:{RABBIT[1]}", cseq.split()[0], branch,
                            dialog["to_tag"])

    threading.Thread(target=receive, daemon=True).start()
    with open(os.path.join(ctrl, "ready"), "w") as f:
        f.write("ready\n")
    for command in sys.stdin:
        command = command.strip()
        if command == "invite":
            dialog.update(tag=str(random.getrandbits(32)), call_id=f"{random.getrandbits(64)}@{PEER[0]}",
                          branch="z9hG4bK" + str(random.getrandbits(40)))
            request("INVITE", f"sip:rabbit@{RABBIT[0]}:{RABBIT[1]}", 1, dialog["branch"])
        elif command == "bye":
            request("BYE", f"sip:rabbit@{RABBIT[0]}:{RABBIT[1]}", 2, "z9hG4bK" + str(random.getrandbits(40)),
                    dialog.get("to_tag", ""))
        log.write(f"== {command}\n")


# --- the test -----------------------------------------------------------------------------------

def wait_signal(bus, needle, count=1, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(needle in s for s in bus.signals()) >= count:
            return True
        time.sleep(0.2)
    return False


def drive(d, bus):
    record = [f"== registered: {bus.wait_for_name(k2k.SERVICE)}"]
    ctl = subprocess.Popen([sys.executable, "-c", k2k.CONTROLLER], env=bus.env(), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True)
    ctl.stdout.readline()
    ctl.stdin.write(f"{AVAILABLE}\n")
    ctl.stdin.flush()
    ctl.stdout.readline()
    time.sleep(1)
    ctrl = tempfile.mkdtemp(prefix="karotz-k2k-peer-")
    peer_proc = subprocess.Popen(["nsenter", "-t", str(d.proc.pid), "-U", "-n", "--preserve-credentials", "--",
                                  sys.executable, "-I", os.path.abspath(__file__), "peer", ctrl],
                                 stdin=subprocess.PIPE, text=True, stdout=subprocess.DEVNULL,
                                 stderr=open(os.path.join(ctrl, "err"), "w"))

    def command(text):
        peer_proc.stdin.write(text + "\n")
        peer_proc.stdin.flush()

    try:
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(os.path.join(ctrl, "ready")):
            time.sleep(0.1)
        record.append(f"== peer ready: {os.path.exists(os.path.join(ctrl, 'ready'))}")

        record.append("== outgoing")
        record.append(k2k.call(bus, "k2k_call", f"string:sip:bob@{PEER[0]}:{PEER[1]}"))
        record.append(f"finished_call seen: {wait_signal(bus, 'finished_call')}")

        record.append("== declined")
        command("invite")
        record.append(f"incoming_call seen: {wait_signal(bus, 'incoming_call')}")
        record.append(k2k.call(bus, "k2k_answer", "boolean:false"))
        record.append(f"finished_call seen: {wait_signal(bus, 'finished_call', 2)}")

        record.append("== answered")
        command("invite")
        record.append(f"incoming_call seen: {wait_signal(bus, 'incoming_call', 2)}")
        record.append(k2k.call(bus, "k2k_answer", "boolean:true"))
        record.append(f"beginning_call seen: {wait_signal(bus, 'beginning_call')}")
        time.sleep(2)
        command("bye")
        record.append(f"finished_call seen: {wait_signal(bus, 'finished_call', 3)}")
        time.sleep(1)
    finally:
        peer_proc.stdin.close()
        peer_proc.kill()
        peer_proc.wait()
        ctl.stdin.close()
        ctl.wait()
        log = os.path.join(ctrl, "sip.log")
        record.append("== the peer's SIP log")
        record.extend(open(log).read().splitlines() if os.path.exists(log) else ["(none)"])
        err = open(os.path.join(ctrl, "err")).read().strip()
        if err:
            record.append("== the peer's errors")
            record.extend(err.splitlines()[-10:])
        shutil.rmtree(ctrl, ignore_errors=True)
    return record


def section(record, name):
    """The SIP log lines of one phase (between its '== command' markers)."""
    log = record[record.index("== the peer's SIP log") + 1:] if "== the peer's SIP log" in record else []
    phases, current = {"outgoing": []}, "outgoing"
    invites = 0
    for line in log:
        if line == "== invite":
            invites += 1
            current = "declined" if invites == 1 else "answered"
            phases[current] = []
        elif line != "== bye":
            phases[current].append(line)
    return phases.get(name, [])


def check(record):
    problems = []
    text = "\n".join(record)
    if "== peer ready: True" not in record:
        problems.append("the SIP peer didn't start")
    out = section(record, "outgoing")
    if not any(l.startswith(f"<- INVITE sip:bob@{PEER[0]}:{PEER[1]}") and "(sdp: m=audio)" in l for l in out):
        problems.append("outgoing: no INVITE with an SDP offer reached the peer")
    if "<- ACK" not in "\n".join(out):
        problems.append("outgoing: the 486 wasn't acknowledged")
    declined = section(record, "declined")
    finals = [int(m) for m in re.findall(r"<- SIP/2.0 (\d+)", "\n".join(declined))]
    if not any(code >= 400 for code in finals):
        problems.append(f"declined: no final response of 400 or more (got {finals or 'none'})")
    answered = "\n".join(section(record, "answered"))
    if "<- SIP/2.0 200" not in answered:
        problems.append("answered: no 200 OK for the INVITE")
    if "-> BYE" in answered and not re.search(r"-> BYE.*\n(?:.*\n)*?<- SIP/2.0 200", answered):
        problems.append("answered: the BYE wasn't answered 200")
    for needle, count in (("incoming_call", 2), ("beginning_call", 1), ("finished_call", 3)):
        seen = text.count(f"KarotzInterface.{needle}(")
        if seen < count:
            problems.append(f"{needle} signalled {seen} time(s), expected {count} or more")
    if "== daemon still running: True" not in text:
        problems.append("the daemon died")
    return problems


def summary(record):
    return "; ".join(l for l in record if l.endswith((": True", ": False")) or l.startswith("k2k_"))


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "peer":
    peer(sys.argv[2])
