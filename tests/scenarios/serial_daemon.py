# SPDX-License-Identifier: MIT
"""serial-daemon: serial ports over D-Bus; received lines become readline signals.

/dev/ttyUSB0 is a FIFO here, so libserial's set-up (tcflush, tcgetattr) fails
on it with ENOTTY and open() returns false: that covers the failure paths.
(/dev/ttyUSB0 is not used: opening it runs system("pidof miniusb_read.sh"),
which the harness answers differently under qemu and natively.
FAKE_IOCTL_TYPES is not used either: a faked TCGETS on a FIFO leaves the
termios struct unwritten, so the daemon would decide on whatever it held.)

For the success paths the daemon opens a real pseudo terminal through a
symlink /tmp/karotz-serial-pty-<harness pid> (the pid is masked in the
record). Under qemu it points at /dev/pts/<n>. A native run's /dev is a
private tmpfs, but its namespace keeps the host /dev bound at Daemon.hostdev
(see harness.py), so the symlink points into that. I/O on the pty isn't
traced (not under /dev), but what the daemon writes is read back from the
master side and recorded, and lines fed into the master produce readline
signals. close replies true when something was closed, false for an unknown
port.
"""

import fcntl
import os
import select
import time

DAEMON = "serial-daemon"
DEVICES = ["ttyUSB0"]
SERVICE = "com.mindscape.karotz.Serial"
PATH = "/com/mindscape/karotz/Serial"
IFACE = "com.mindscape.karotz.KarotzInterface"



def call(bus, method, *args):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    # Drop the "method return time=... sender=..." header; keep values and errors.
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return f"{method}{args}: {' | '.join(lines)}"


def drain(master, wait=0.5):
    """Bytes the daemon wrote to the pseudo terminal."""
    data = b""
    deadline = time.time() + wait
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.05)
        if r:
            try:
                data += os.read(master, 4096)
            except OSError:
                break
    return data


def drive(d, bus):
    record = []
    record.append(f"== registered: {bus.wait_for_name(SERVICE)}")
    record.append("== introspection")
    from harness import introspection
    record.extend(introspection(bus, SERVICE, PATH))

    record.append("== no ports open")
    record.append(call(bus, "ping"))
    record.append(call(bus, "write", "string:/dev/ttyUSB0", "string:hello", "string:ascii"))
    record.append(call(bus, "readlinemode", "string:/dev/ttyUSB0", "string:hex"))
    record.append(call(bus, "close", "string:/dev/ttyUSB0"))
    record.append(call(bus, "close", "string:"))

    record.append("== opening ports that can't be set up")
    for baud in (9600, 4800, 115200, 230400, 0, -1):
        record.append(call(bus, "open", "string:/dev/ttyUSB0", f"int32:{baud}"))
    d.write("ttyUSB0", b"ignored\n")
    record.append(call(bus, "open", "string:/dev/nonexistent", "int32:9600"))
    record.append(call(bus, "open", "string:", "int32:9600"))
    record.append(call(bus, "write", "string:/dev/ttyUSB0", "string:41", "string:hex"))
    time.sleep(0.3)

    record.append("== a pseudo terminal")
    master, slave = os.openpty()
    fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)
    pts = os.ttyname(slave)
    if not d.qemu:  # a native run is in a namespace with its own /dev
        pts = pts.replace("/dev/", f"{d.hostdev}/", 1)
    port = f"/tmp/karotz-serial-pty-{os.getpid()}"
    if os.path.lexists(port):
        os.unlink(port)
    os.symlink(pts, port)
    try:
        for baud in (115200, 230400, 460800):
            record.append(call(bus, "open", f"string:{port}", f"int32:{baud}"))
        record.append(call(bus, "write", f"string:{port}", "string:x", "string:ascii"))
        record.append(call(bus, "open", f"string:{port}", "int32:9600"))
        drain(master, 0.2)

        record.append(call(bus, "write", f"string:{port}", "string:hello world\r\n", "string:ascii"))
        record.append(f"pty got {drain(master)!r}")
        record.append(call(bus, "write", f"string:{port}", "string:48656c6C6f0A", "string:hex"))
        record.append(f"pty got {drain(master)!r}")
        record.append(call(bus, "write", f"string:{port}", "string:4G1z7", "string:h"))
        record.append(f"pty got {drain(master)!r}")
        record.append(call(bus, "write", f"string:{port}", "string:3132", "string:"))
        record.append(f"pty got {drain(master)!r}")
        record.append(call(bus, "write", f"string:{port}", "string:", "string:ascii"))
        record.append(f"pty got {drain(master)!r}")
        record.append(call(bus, "write", f"string:{port}", "string:abc", "string:a"))
        record.append(f"pty got {drain(master)!r}")

        os.write(master, b"hello\n")
        time.sleep(0.5)
        os.write(master, b"two\nlines\n\n")
        time.sleep(0.5)
        os.write(master, b"split ")
        time.sleep(0.3)
        os.write(master, b"line\r\n")
        time.sleep(0.5)

        record.append(call(bus, "readlinemode", f"string:{port}", "string:hex"))
        os.write(master, b"\x00\x01\x7f\x80\xffAz\n")
        time.sleep(0.5)
        record.append(call(bus, "readlinemode", f"string:{port}", "string:"))
        os.write(master, b"9\n")
        time.sleep(0.5)
        record.append(call(bus, "readlinemode", f"string:{port}", "string:ascii"))
        os.write(master, b"back to text\n")
        time.sleep(0.5)

        # A partial line, then reopening: the new entry starts empty, in text mode.
        record.append(call(bus, "readlinemode", f"string:{port}", "string:hex"))
        os.write(master, b"lost")
        time.sleep(0.5)
        record.append(call(bus, "open", f"string:{port}", "int32:57600"))
        os.write(master, b"after reopen\n")
        time.sleep(0.5)
        record.append(call(bus, "open", f"string:{port}", "int32:4800"))
        drain(master, 0.2)
        record.append(call(bus, "write", f"string:{port}", "string:still here", "string:ascii"))
        record.append(f"pty got {drain(master)!r}")

        record.append(call(bus, "close", f"string:{port}"))
        record.append(call(bus, "write", f"string:{port}", "string:gone", "string:ascii"))
        record.append(call(bus, "readlinemode", f"string:{port}", "string:ascii"))
        record.append(call(bus, "close", f"string:{port}"))

        record.append(call(bus, "open", f"string:{port}", "int32:19200"))
        os.write(master, b"once more\n")
        time.sleep(0.5)
        record.append(call(bus, "close", "string:"))
        record.append(call(bus, "write", f"string:{port}", "string:gone", "string:ascii"))
        os.write(master, b"unheard\n")
        time.sleep(0.5)
        record.append(call(bus, "ping"))
    finally:
        os.unlink(port)
        os.close(slave)
        os.close(master)
    return record
