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
  it, the daemon signals beginning_call. Then the call's audio, both ways: the test appends a
  600 Hz tone to /dev/dsp (the microphone) and the peer sends a 1000 Hz tone as PCMU RTP to the
  port of the daemon's SDP answer. The peer's RTP must carry the 600 Hz tone and what the daemon
  writes to /dev/dsp the 1000 Hz one (compared by their share of the energy, not by bytes:
  buffering and timing shift the samples). /dev/dsp is one regular file opened once for reading
  and writing, so the daemon's writes overwrite part of the tone it hasn't read yet; what it
  reads is still the tone. Last the peer hangs up (BYE), which the daemon answers 200 and
  signals finished_call.

What the peer received and sent is logged and kept in the record; check() lists what's missing.
ARM only (the daemon's SIP threads need qemu-user's NPTL support).
"""

import math
import os
import random
import struct
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
# The peer's RTP port: the daemon's own is 7078 (linphone.conf), on the same interface.
PEER_RTP = 7080
SDP = ("v=0\r\no=eve 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
       f"m=audio {PEER_RTP} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
AVAILABLE = 16
# The call's audio: the rabbit's microphone (/dev/dsp's input) plays RABBIT_TONE, the peer sends
# PEER_TONE, each for TONE_SECONDS, at 8000 samples a second (PCMU's rate, and the OSS device's).
RATE = 8000
RABBIT_TONE = 600
PEER_TONE = 1000
TONE_SECONDS = 3
# The share of a half-second window's energy a tone must reach somewhere to count as heard.
MIN_SHARE = 0.5


EXPECTED_WARNINGS = k2k.EXPECTED_WARNINGS


# --- audio --------------------------------------------------------------------------------------

def tone(freq, seconds, amplitude=8000):
    """A sine tone as 16-bit samples."""
    return [int(amplitude * math.sin(2 * math.pi * freq * i / RATE)) for i in range(int(seconds * RATE))]


def ulaw_encode(sample):
    """G.711 mu-law: one 16-bit sample to a byte."""
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), 32635) + 0x84
    exponent = 7
    while exponent > 0 and not magnitude & (0x4000 >> (7 - exponent)):
        exponent -= 1
    return ~(sign | exponent << 4 | (magnitude >> (exponent + 3)) & 0x0F) & 0xFF


def ulaw_decode(byte):
    """G.711 mu-law: one byte to a 16-bit sample."""
    byte = ~byte & 0xFF
    magnitude = (((byte & 0x0F) << 3) + 0x84) << ((byte >> 4) & 7)
    return 0x84 - magnitude if byte & 0x80 else magnitude - 0x84


def strongest(samples, freqs, window=RATE // 2):
    """For each frequency, its largest share of a window's energy (0 to 1), over 0.5 s windows."""
    best = {f: 0.0 for f in freqs}
    for start in range(0, len(samples) - window + 1, window // 2):
        part = samples[start:start + window]
        energy = sum(x * x for x in part)
        if energy == 0:
            continue
        for f in freqs:
            w = 2 * math.pi * f / RATE
            re_ = sum(x * math.cos(w * i) for i, x in enumerate(part))
            im = sum(x * math.sin(w * i) for i, x in enumerate(part))
            best[f] = max(best[f], 2 * (re_ * re_ + im * im) / len(part) / energy)
    return best


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
    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind((PEER[0], PEER_RTP))

    def receive_rtp():
        """The PCMU payloads the daemon sends, appended to rtp-in."""
        with open(os.path.join(ctrl, "rtp-in"), "ab", buffering=0) as out:
            while True:
                data, _ = rtp.recvfrom(65535)
                if len(data) < 12 or data[0] >> 6 != 2 or data[1] & 0x7F != 0:
                    continue
                start = 12 + 4 * (data[0] & 0x0F)
                if data[0] & 0x10 and len(data) >= start + 4:
                    start += 4 + 4 * struct.unpack("!H", data[start + 2:start + 4])[0]
                out.write(data[start:])

    def send_tone():
        """PEER_TONE as PCMU, 20 ms a packet, to the address of the daemon's SDP answer."""
        payload = bytes(ulaw_encode(s) for s in tone(PEER_TONE, TONE_SECONDS))
        ssrc, started = random.getrandbits(32), time.time()
        for n, i in enumerate(range(0, len(payload), 160)):
            header = struct.pack("!BBHII", 0x80, 0, n & 0xFFFF, (i & 0xFFFFFFFF), ssrc)
            rtp.sendto(header + payload[i:i + 160], dialog["rtp"])
            time.sleep(max(0.0, started + (n + 1) * 0.02 - time.time()))
        log.write(f"== tone sent to port {dialog['rtp'][1]}\n")

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
                if cseq.endswith("INVITE") and 200 <= code < 300:
                    port = re.search(r"^m=audio (\d+)", body, re.M)
                    address = re.search(r"^c=IN IP4 (\S+)", body, re.M)
                    if port:
                        dialog["rtp"] = (address.group(1) if address else PEER[0], int(port.group(1)))
                if cseq.endswith("INVITE") and code >= 200:
                    to_tag = re.search(r";tag=([^;>\s]+)", fields["to"][0])
                    dialog["to_tag"] = to_tag.group(1) if to_tag else ""
                    # A 2xx is acknowledged in a new transaction, a failure in the INVITE's own.
                    branch = dialog["branch"] if code >= 300 else "z9hG4bK" + str(random.getrandbits(40))
                    request("ACK", f"sip:rabbit@{RABBIT[0]}:{RABBIT[1]}", cseq.split()[0], branch,
                            dialog["to_tag"])

    threading.Thread(target=receive, daemon=True).start()
    threading.Thread(target=receive_rtp, daemon=True).start()
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
        elif command == "tone":
            if "rtp" in dialog:
                threading.Thread(target=send_tone, daemon=True).start()
            else:
                log.write("== no RTP address in the daemon's answer\n")
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
        time.sleep(1)
        d.write("dsp", struct.pack(f"<{TONE_SECONDS * RATE}h", *tone(RABBIT_TONE, TONE_SECONDS)))
        command("tone")
        time.sleep(TONE_SECONDS + 1.5)
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
        record.extend(audio_lines(os.path.join(ctrl, "rtp-in"), d.fifos["dsp"]))
        err = open(os.path.join(ctrl, "err")).read().strip()
        if err:
            record.append("== the peer's errors")
            record.extend(err.splitlines()[-10:])
        shutil.rmtree(ctrl, ignore_errors=True)
    return record


def audio_lines(rtp_in, dsp):
    """How strongly each tone shows in what the peer received and in what the daemon wrote to /dev/dsp."""
    lines = ["== audio"]
    received = open(rtp_in, "rb").read() if os.path.exists(rtp_in) else b""
    shares = strongest([ulaw_decode(b) for b in received], (RABBIT_TONE, PEER_TONE))
    lines.append(f"RTP received: {len(received)} samples; rabbit tone {shares[RABBIT_TONE]:.2f}, "
                 f"peer tone {shares[PEER_TONE]:.2f}")
    # The device file also holds the rabbit's tone and the silence the daemon writes before the
    # call's audio; all-zero windows are skipped before decoding, since there can be a lot of them.
    raw = open(dsp, "rb").read() if os.path.exists(dsp) else b""
    window = RATE  # bytes: 0.5 s of 16-bit samples
    zero = bytes(window)
    samples = []
    for i in range(0, len(raw) - window + 1, window):
        if raw[i:i + window] != zero:
            samples.extend(struct.unpack(f"<{window // 2}h", raw[i:i + window]))
    shares = strongest(samples, (RABBIT_TONE, PEER_TONE))
    lines.append(f"/dev/dsp: {len(raw) // 2} samples, {len(samples)} outside silence; "
                 f"rabbit tone {shares[RABBIT_TONE]:.2f}, peer tone {shares[PEER_TONE]:.2f}")
    return lines


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
    for line, label, tone_name in (("RTP received: ", "the RTP the peer received", "rabbit tone"),
                                   ("/dev/dsp: ", "what the daemon wrote to /dev/dsp", "peer tone")):
        found = [l for l in record if l.startswith(line)]
        share = re.search(tone_name + r" ([0-9.]+)", found[0]) if found else None
        if not share or float(share.group(1)) < MIN_SHARE:
            problems.append(f"answered: the {tone_name} isn't in {label} "
                            f"({found[0] if found else 'not measured'}; expected {MIN_SHARE} or more)")
    if "== daemon still running: True" not in text:
        problems.append("the daemon died")
    return problems


def summary(record):
    return "; ".join(l for l in record if l.endswith((": True", ": False"))
                     or l.startswith(("k2k_", "RTP received: ", "/dev/dsp: ")))


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "peer":
    peer(sys.argv[2])
