# SPDX-License-Identifier: MIT
"""rfid-daemon: SRI512 tags on the nose reader become tag_in / tag_out signals.

The daemon polls an MFRC522-style coupler over /dev/i2c-0 every 100 ms. On
the FIFO standing in for the bus, everything it writes comes back on its own
reads, and a poll that never blocks makes the amount of I/O depend on how
long the test runs. So this scenario opens the FIFO itself before the daemon
does and preloads it:

  * the head holds the coupler's answers, in the exact order the daemon will
    read them (register values, IRQ flags, FIFO levels, tag data); the daemon
    only ever reads from the head, its own writes pile up behind;
  * the rest is filler, sized so that the pipe fills up and the daemon blocks
    for good in a write() a fixed number of bytes after the script ends.

The whole run is therefore a pure function of the script: every run does
the same I/O and stops at the same point, however fast it runs. The one getTags
made while a tag is on the reader also reads from the FIFO; it is sent as
soon as the FIFO shows the poll before it is done (each poll adds a known
number of bytes), so it is handled before the next poll, 100 ms later, and the
script holds its answers at that point.
"""

import fcntl
import os
import struct
import termios
import time

import harness

DAEMON = "rfid-daemon"
DEVICES = ["i2c-0"]
FAKE_IOCTL_TYPES = [0x07]  # I2C_SLAVE succeeds, as on the rabbit
SERVICE = "com.mindscape.karotz.Rfid"
PATH = "/com/mindscape/karotz/Rfid"
IFACE = "com.mindscape.karotz.KarotzInterface"

F_GETPIPE_SZ = 1032
IRQ_TIMER, IRQ_ERR, IRQ_RX = 0x01, 0x02, 0x20


# Syslog warnings the run is known to log (harness.py): tags read with the fake reader's errors.
EXPECTED_WARNINGS = [
    r"^RFID: getTagInfo - SRI512_(SELECT|GET_UID) failed - abort$",
]


class Script:
    """The coupler's answers, and how many bytes the daemon writes meanwhile."""

    def __init__(self):
        self.reads = bytearray()
        self.written = 0

    def read_reg(self, value):
        self.written += 1  # register number
        self.reads.append(value)

    def write_reg(self, n=1):
        self.written += 2 * n

    def transceive(self, tx_len, level=None, irqs=(IRQ_RX,), rx=b"", error=0, rx_max=8):
        """One frame, as transceive() in the daemon reads it.

        level: FIFO level after loading the frame (default tx_len: accepted);
        irqs: ComIrq values read until one ends the wait; rx: the answer
        (empty: FIFO level 0); error: ErrorReg after IRQ_ERR.
        """
        self.write_reg(1 + tx_len)
        self.read_reg(tx_len if level is None else level)
        if level is not None and level != tx_len:
            return
        self.write_reg(3)
        for irq in irqs:
            self.read_reg(irq)
        self.write_reg(1)
        if irqs[-1] & IRQ_ERR:
            self.read_reg(error)
        elif irqs[-1] & IRQ_RX:
            self.read_reg(len(rx))
            if len(rx) <= rx_max:
                for b in rx:
                    self.read_reg(b)

    # Frames: INITIATE, PCALL16, SELECT, READ_BLOCK send 2 bytes; SLOT_MARKER, GET_UID 1.
    def no_answer(self, tx_len=2):
        self.transceive(tx_len, level=0)

    def answer(self, tx_len, data):
        self.transceive(tx_len, rx=bytes(data))

    def read_tag(self, chip, uid, blocks, system=b"\xff\xff\xff\xff"):
        """SELECT, GET_UID, blocks 0-15 and, unless block 7 is all zero, block 255."""
        self.answer(2, [chip])
        self.answer(1, uid)
        for b in blocks:
            self.answer(2, b)
        if any(blocks[7]):
            self.answer(2, system)

    def poll_idle(self, present=()):
        """INITIATE gets nothing; the known tags (chip ids, newest first) answer SELECT."""
        self.no_answer()
        for chip in present:
            self.answer(2, [chip])


def tag_blocks(seed, block7):
    blocks = [bytes((seed + 4 * i + j) & 0xFF for j in range(4)) for i in range(16)]
    blocks[7] = bytes(block7)
    return blocks


TAG_A = dict(chip=0x42, uid=bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x02, 0xD0]),
             blocks=tag_blocks(0x10, [0x04, 0x10, 0x01, 0x00]))  # app 1, type 1, pict 0, colour 4
TAG_B = dict(chip=0x17, uid=bytes([0x99, 0x88, 0x77, 0x66, 0x55, 0x44, 0x02, 0xD0]),
             blocks=tag_blocks(0x80, [0x00, 0x00, 0x00, 0x00]))  # block 7 empty: "too fast"
TAG_C = dict(chip=0xC2, uid=bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x02, 0xD0]),
             blocks=tag_blocks(0x40, [0xFF, 0xA7, 0x34, 0x12]))  # app 0x1234, type 10, pict 63, colour 31

IDLE_START = 25  # idle polls before the first tag: time for the first getTags
TAIL_POLLS = 10  # idle polls after the script before the pipe is full


def build_script():
    """Returns the script and the FIFO growth at which getTags is to be sent."""
    a, b, c = TAG_A["chip"], TAG_B["chip"], TAG_C["chip"]
    s = Script()
    s.read_reg(0x00)  # TModeReg, read twice while setting the timer up
    s.read_reg(0x00)
    s.write_reg(19)  # the other 19 set-up register writes

    for _ in range(IDLE_START):
        s.poll_idle()

    # Tag A arrives: INITIATE answers, the tag is read (tag_in), then checked.
    s.answer(2, [a])
    s.read_tag(**TAG_A)
    s.answer(2, [a])
    for _ in range(3):
        s.poll_idle([a])
    gettags_level = s.written - len(s.reads)
    s.answer(2, [a])  # getTags SELECTs the tag first
    for _ in range(3):
        s.poll_idle([a])

    # INITIATE finds tag A again: known UID, read abandoned.
    s.answer(2, [a])
    s.answer(2, [a])
    s.answer(1, TAG_A["uid"])
    s.answer(2, [a])
    # SELECT answered by the wrong chip id; GET_UID unanswered: both abandoned.
    s.answer(2, [a])
    s.answer(2, [0x99])
    s.answer(2, [a])
    s.answer(2, [a])
    s.answer(2, [a])
    s.no_answer(1)
    s.answer(2, [a])
    # INITIATE: protocol error after two busy reads, timeout, over-long answer.
    s.transceive(2, irqs=(0x00, 0x10, IRQ_ERR), error=0x01)
    s.answer(2, [a])
    s.transceive(2, irqs=(IRQ_TIMER,))
    s.answer(2, [a])
    s.transceive(2, rx=b"\x01\x02", rx_max=1)
    s.answer(2, [a])
    # Tag A leaves: its SELECT gets an empty answer -> tag_out.
    s.no_answer()
    s.transceive(2, rx=b"")
    for _ in range(3):
        s.poll_idle()

    # Tag B pulled away mid-read (block 7 reads as zeros): no signal; read again next poll.
    for _ in range(2):
        s.answer(2, [b])
        s.read_tag(**TAG_B)
    s.poll_idle()

    # Two tags at once: a CRC error on INITIATE starts the anti-collision loop.
    s.transceive(2, irqs=(IRQ_ERR,), error=0x04)
    s.no_answer()  # PCALL16
    for slot in range(1, 16):
        if slot == 3:
            s.answer(1, [c])
            s.read_tag(**TAG_C)
        elif slot == 9:
            s.transceive(1, irqs=(IRQ_ERR,), error=0x04)  # still colliding: another round
        else:
            s.no_answer(1)
    s.answer(2, [a])  # second round: PCALL16 finds tag A, read in full
    s.read_tag(**TAG_A)
    for slot in range(1, 16):
        s.no_answer(1)
    s.answer(2, [a])  # check, newest first
    s.answer(2, [c])
    for _ in range(2):
        s.poll_idle([a, c])
    # Tag C stops answering (timeout), then tag A (no answer): two tag_out.
    s.no_answer()
    s.answer(2, [a])
    s.transceive(2, irqs=(IRQ_TIMER,))
    s.no_answer()
    s.no_answer()
    return s, gettags_level


def call(bus, method, *args):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return f"{method}{args} -> {' | '.join(lines)}"


def pipe_level(fd):
    return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]


def drive(d, bus):
    record = []
    script, gettags_level = build_script()

    # Preload the FIFO before the daemon opens it (it does so after joining the bus).
    fd = os.open(d.fifos["i2c-0"], os.O_RDWR | os.O_NONBLOCK)
    capacity = fcntl.fcntl(fd, F_GETPIPE_SZ)
    room = script.written + 7 * TAIL_POLLS
    data = bytes(script.reads) + b"\x00" * (capacity - room - len(script.reads))  # 0: FIFO empty
    sent = 0
    while sent < len(data):
        sent += os.write(fd, data[sent:])

    registered = bus.wait_for_name(SERVICE)
    record.append(f"== registered: {registered}")
    if not registered:
        os.close(fd)
        return record
    record.append("== interface")
    record.extend(harness.introspection(bus, SERVICE, PATH))
    record.append("== calls")
    record.append(call(bus, "getTags"))  # no tag yet: no reader I/O

    # getTags with tag A on the reader, between two polls.
    target = len(data) + gettags_level
    deadline = time.time() + 120
    while pipe_level(fd) < target and time.time() < deadline:
        time.sleep(0.001)
    level = pipe_level(fd)
    if level == target:
        record.append(call(bus, "getTags"))
    else:
        record.append(f"getTags: missed the poll ({level} != {target})")

    # Let the script run out; the daemon then blocks with the pipe full.
    deadline = time.time() + 120
    stable_since, last = time.time(), -1
    while time.time() < deadline:
        level = pipe_level(fd)
        if level != last:
            last, stable_since = level, time.time()
        elif time.time() - stable_since > 3:
            break
        time.sleep(0.05)
    record.append(f"== reader FIFO full: {last >= capacity - 4096}")
    return record
