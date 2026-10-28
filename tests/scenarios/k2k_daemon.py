# SPDX-License-Identifier: MIT
"""k2k-daemon: Karotz-to-Karotz SIP calls through liblinphone.

What runs under qemu: start-up (karotz.conf read from the sysroot, account.conf
missing so no account is registered, the controller absent so the rabbit is
set busy), every exported method with valid and invalid arguments, and an
unknown one. The daemon's network namespace has no interface up, so nothing
reaches another host and no registration or incoming call can happen; an
outgoing call is still created, stays in progress (so a second call is
refused) and ends on hangup with a finished_call signal. Every method
replies, with a result saying whether calling, answering or hanging up
worked. The aggressive test k2k_sip_peer puts calls on the wire, with a SIP
peer on the loopback interface.

/dev/dsp and /dev/mixer are FIFOs with the OSS ioctl types ('P', 'M') faked
so mediastreamer finds a sound card.
Afterwards a helper owns com.mindscape.karotz.Controller and sends
karotz_status_changed (available, unavailable, available); its effect (SIP
presence) is only visible in the daemon's log.
"""

import subprocess
import sys
import time

from harness import introspection

DAEMON = "k2k-daemon"
DEVICES = ["dsp", "mixer"]
FAKE_IOCTL_TYPES = [0x50, 0x4D]
SERVICE = "com.mindscape.karotz.K2k"
PATH = "/com/mindscape/karotz/K2k"
IFACE = "com.mindscape.karotz.KarotzInterface"

CONTROLLER = r'''
import sys
import dbus, dbus.lowlevel

bus = dbus.SystemBus()
bus.request_name("com.mindscape.karotz.Controller")
print("ready", flush=True)
for line in sys.stdin:
    signal = dbus.lowlevel.SignalMessage("/com/mindscape/karotz/Controller", "com.mindscape.karotz.KarotzInterface",
                                         "karotz_status_changed")
    signal.append(int(line), signature="i")
    bus.get_connection().send_message(signal)
    bus.flush()
    print("sent", flush=True)
'''


# Syslog warnings the run is known to log (harness.py): no account, no controller at first, no
# STUN server or proxy, calls refused as busy, and the answering machine setting, not implemented.
EXPECTED_WARNINGS = [
    r"^Could not read /etc/conf/account\.conf file \(No such file or directory\)$",
    r"^Could not get controller current status\. Assuming unavailable$",
    r"^dbus, set answering machine TODO \(value: \d+\)$",
    r"^Asked to hangup, but no active call\.$",
    r"^No stun server configured$",
    r"^No proxy information$",
    r"^Could not start the call$",
    r"^call error \(User is busy\.\)$",
]


def call(bus, method, *args):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return f"{method}{args}: {' | '.join(lines)}"


def drive(d, bus):
    record = []
    record.append(f"== registered: {bus.wait_for_name(SERVICE)}")
    record.append("== interface")
    record.extend(introspection(bus, SERVICE, PATH))
    record.append("== calls")
    calls = [
        # Unknown method (libkarotz-common's k2kCanRing) and wrong argument types.
        ("k2k_can_ring", "boolean:true"),
        ("k2k_call", "int32:5"),
        ("k2k_set_busy", "string:yes"),
        ("k2k_use_account", "string:alice", "string:127.0.0.1"),
        # Simple methods.
        ("k2k_set_answering_machine", "boolean:true"),
        ("k2k_set_answering_machine", "boolean:false"),
        ("k2k_set_busy", "boolean:true"),
        ("k2k_set_busy", "boolean:false"),
        # No call yet: hangup and answer/decline fail inside linphone (-1).
        ("k2k_hangup",),
        ("k2k_answer", "boolean:true"),
        ("k2k_answer", "boolean:false"),
        # Accounts: no STUN, STUN, empty id/domain, empty password (still used).
        ("k2k_use_account", "string:alice", "string:127.0.0.1", "string:pw", "string:"),
        ("k2k_use_account", "string:alice", "string:127.0.0.1", "string:pw", "string:stun.example.com"),
        ("k2k_use_account", "string:", "string:127.0.0.1", "string:pw", "string:"),
        ("k2k_use_account", "string:alice", "string:", "string:pw", "string:"),
        ("k2k_use_account", "string:alice", "string:127.0.0.1", "string:", "string:"),
        # Calls: invalid address, a call (stays outgoing: no SIP transport),
        # a second call while it is under way, hang up (succeeds: 0).
        ("k2k_call", "string:"),
        ("k2k_call", "string:sip:bob@127.0.0.1"),
        ("k2k_call", "string:sip:carol@127.0.0.1"),
        ("k2k_hangup",),
        ("k2k_hangup",),
        # Decline an outgoing call (terminate succeeds: 0), answer one (no
        # incoming call: -1).
        ("k2k_call", "string:sip:dave@127.0.0.1"),
        ("k2k_answer", "boolean:true"),
        ("k2k_answer", "boolean:false"),
        ("k2k_echo_calibration",),
    ]
    for c in calls:
        record.append(call(bus, *c))
        time.sleep(0.5)
    ctl = subprocess.Popen([sys.executable, "-c", CONTROLLER], env=bus.env(), stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True)
    record.append(f"== controller {ctl.stdout.readline().strip()}")
    time.sleep(0.5)
    for status in (16, 17, 16):
        ctl.stdin.write(f"{status}\n")
        ctl.stdin.flush()
        ctl.stdout.readline()
        time.sleep(0.3)
    ctl.stdin.close()
    ctl.wait()
    time.sleep(2)
    return record
