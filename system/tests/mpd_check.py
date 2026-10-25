# SPDX-License-Identifier: MIT
"""Check the system build's mpd 0.15.12 under qemu.

    mpd_check.py WORKDIR QEMU_ROOT:MPD

The mpd gets a music directory of one test signal in six formats (made by the host's ffmpeg),
a FIFO output, and two passwords (an admin one and an "external" read+add+control one). Prints:
- the tags it read for each file;
- for each file, the length it played and the signal-to-noise ratio of its PCM against the host
  ffmpeg's decode of the same file (inf when identical);
- a seek in the FLAC file through the FFmpeg plug-in;
- the count_externalClient sequence as clients log in, change password and leave.
Then the checks against fixed limits. Exit status 0 when all pass.
"""

import fcntl
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

RATE, SECONDS = 44100, 4.0
ADMIN, EXTERNAL = "testadmin", "testexternal"
# The file, the plug-in mpd uses for it, and whether it is lossless.
FILES = [("t.mp3", "mad", False), ("t.ogg", "vorbis", False), ("t.flac", "ffmpeg", True),
         ("t.wav", "ffmpeg", True), ("t.m4a", "ffmpeg", False), ("t.wma", "ffmpeg", False)]
ENCODE = {"t.mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], "t.ogg": ["-c:a", "libvorbis", "-q:a", "6"],
          "t.flac": ["-c:a", "flac"], "t.wav": ["-c:a", "pcm_s16le"],
          "t.m4a": ["-c:a", "aac", "-b:a", "192k"], "t.wma": ["-c:a", "wmav2", "-b:a", "192k"]}
TAGS = {"title": "Test Tone", "artist": "LapinLibre", "album": "Test", "genre": "Testing"}


def make_music(music):
    """The test signal: 0.5 s of silence, then 440 Hz left and 660 Hz right, at half scale."""
    music.mkdir(parents=True, exist_ok=True)
    src = music.parent / "source.wav"
    n = int(RATE * SECONDS)
    frames = bytearray()
    for i in range(n):
        on = i >= RATE // 2
        left = int(16384 * math.sin(2 * math.pi * 440 * i / RATE)) if on else 0
        right = int(16384 * math.sin(2 * math.pi * 660 * i / RATE)) if on else 0
        frames += struct.pack("<hh", left, right)
    with open(src, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 2, RATE, RATE * 4, 4, 16)
                + b"data" + struct.pack("<I", len(frames)) + frames)
    meta = [a for k, v in TAGS.items() for a in ("-metadata", f"{k}={v}")]
    for name, _, _ in FILES:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *ENCODE[name], *meta,
                        str(music / name)], check=True)


def reference(path):
    """The host ffmpeg's decode, as interleaved s16le stereo samples."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "2",
                          "-ar", str(RATE), "-"], check=True, capture_output=True).stdout
    return struct.unpack(f"<{len(raw) // 2}h", raw)


def strip_underruns(pcm):
    """pcm without the silence mpd 0.15's player sends while its decoder is behind (whole runs
    of 512 or more all-zero frames inside the tone, which itself is never zero for long), and
    how many such runs there were."""
    n = len(pcm) // 2
    loud = [i for i in range(n) if abs(pcm[2 * i]) > 2000]
    if not loud:
        return pcm, 0
    start, end = loud[0], loud[-1]
    out, gaps, i = list(pcm[:2 * start]), 0, start
    while i <= end:
        j = i
        while j <= end and pcm[2 * j] == 0 and pcm[2 * j + 1] == 0:
            j += 1
        if j - i >= 512:
            gaps += 1
        else:
            out.extend(pcm[2 * i:2 * max(j, i + 1)])
        i = max(j, i + 1)
    out.extend(pcm[2 * (end + 1):])
    return out, gaps


def snr(got, ref):
    """SNR in dB of got against ref, after aligning them on the tone's onset (+-64 frames)."""
    def onset(x):
        for i in range(0, len(x) - 1, 2):
            if abs(x[i]) > 2000:
                return i // 2
        return 0
    base = onset(got) - onset(ref)
    best = -math.inf
    for lag in range(base - 64, base + 65):
        start = max(0, lag)
        n = min(len(got) // 2 - start, len(ref) // 2 - (start - lag)) - RATE // 10
        if n < RATE:
            continue
        sig = noise = 0
        # every 4th frame from the onset on, for speed
        for f in range(start + RATE // 10, start + n, 4):
            for c in (0, 1):
                g, r = got[2 * f + c], ref[2 * (f - lag) + c]
                sig += r * r
                noise += (g - r) * (g - r)
        best = max(best, math.inf if noise == 0 else 10 * math.log10(sig / noise))
    return best


class Client:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=30)
        self.f = self.s.makefile("rb")
        self.f.readline()  # OK MPD 0.15.0

    def cmd(self, line):
        self.s.sendall(line.encode() + b"\n")
        out = []
        while True:
            raw = self.f.readline()
            if not raw:
                raise ConnectionError(f"mpd closed the connection during {line!r}")
            r = raw.decode("utf-8", "replace").rstrip("\n")
            if r == "OK" or r.startswith("ACK"):
                return out, r
            out.append(r)

    def status(self):
        return dict(r.split(": ", 1) for r in self.cmd("status")[0])

    def close(self):
        # the socket's descriptor stays open while its makefile() reader does
        self.f.close()
        self.s.close()


def conf(d, music, bind, port):
    return (f'music_directory "{music}"\nplaylist_directory "{d}/playlists"\ndb_file "{d}/db"\n'
            f'log_file "{d}/log"\npid_file "{d}/pid"\nbind_to_address "{bind}"\nport "{port}"\n'
            f'password "{ADMIN}@read,add,control,admin"\npassword "{EXTERNAL}@read,add,control"\n'
            f'audio_output {{\n type "fifo"\n name "f"\n path "{d}/fifo"\n format "{RATE}:16:2"\n}}\n')


class Local:
    """An mpd under qemu on this machine."""

    def __init__(self, name, root, mpd, work, music):
        self.d = work / name
        (self.d / "playlists").mkdir(parents=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.host, self.port = "127.0.0.1", s.getsockname()[1]
        (self.d / "mpd.conf").write_text(conf(self.d, music, "127.0.0.1", self.port))
        self.proc = subprocess.Popen(["qemu-arm", "-cpu", "arm926", "-L", root, mpd, "--no-daemon",
                                      str(self.d / "mpd.conf")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.fd = -1

    def fifo_reader(self, sink):
        fifo = self.d / "fifo"
        while not fifo.exists():
            time.sleep(0.1)
        # Opened without blocking (there may be no writer yet), then read blocking, from a pipe
        # made larger: mpd's FIFO output empties the pipe itself when a write finds it full, so a
        # reader that falls behind for a moment (qemu catching up in a burst) loses 64 KiB.
        self.fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        fcntl.fcntl(self.fd, fcntl.F_SETPIPE_SZ, 1 << 20)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, fcntl.fcntl(self.fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)

        def loop():
            while self.fd >= 0:
                try:
                    b = os.read(self.fd, 1 << 20)
                except OSError:
                    return
                if b:
                    sink(b)
                else:
                    time.sleep(0.01)  # no writer between files
        threading.Thread(target=loop, daemon=True).start()

    def log_tail(self):
        log = self.d / "log"
        return log.read_text(errors="replace").splitlines()[-15:] if log.exists() else []

    def exit_status(self):
        return self.proc.poll()

    def stop(self):
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            os.close(fd)
        self.proc.terminate()
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def run(name, spec, work, music):
    root, mpd = spec.split(":", 1)
    m = Local(name, root, mpd, work, music)
    result = {}
    try:
        for _ in range(300):
            try:
                c = Client(m.host, m.port)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("mpd didn't start")
        c.cmd(f'password "{ADMIN}"')
        c.cmd("update")
        while "updating_db" in c.status():
            time.sleep(0.5)
        # Tags (lsinfo takes only a directory in mpd 0.15).
        rows, _ = c.cmd("listallinfo")
        songs, info = {}, None
        for r in rows:
            k, _, v = r.partition(": ")
            if k == "file":
                info = songs.setdefault(v, {})
            elif info is not None:
                info[k] = v
        for fname, _, _ in FILES:
            info = songs.get(fname, {})
            result[f"tags {fname}"] = "; ".join(f"{k}={info.get(k.capitalize(), '-')}"
                                              for k in ("title", "artist", "album", "genre", "time"))
        # Playback through the FIFO; each capture runs from quiet to quiet, so that nothing of
        # the previous file (still in the pipe) gets into the next one.
        captured, lock = bytearray(), threading.Lock()
        received = [0]

        def sink(b):
            with lock:
                captured.extend(b)
                received[0] += len(b)

        def wait_quiet(seconds=1.0):
            last, since = -1, time.monotonic()
            while time.monotonic() - since < seconds:
                time.sleep(0.1)
                with lock:
                    now = received[0]
                if now != last:
                    last, since = now, time.monotonic()
        m.fifo_reader(sink)
        for fname, _, _ in FILES:
            c.cmd("clear")
            c.cmd(f'add "{fname}"')
            wait_quiet()
            with lock:
                captured.clear()
            c.cmd("play")
            time.sleep(1)
            while c.status().get("state") == "play":
                time.sleep(0.2)
            wait_quiet()
            with lock:
                raw = bytes(captured)
            if os.environ.get("MPD_COMPARE_DUMP"):  # to look at a capture: its raw bytes
                Path(os.environ["MPD_COMPARE_DUMP"], f"{name}-{fname}.raw").write_bytes(raw)
            pcm = struct.unpack(f"<{len(raw) // 2}h", raw[:len(raw) // 4 * 4])
            pcm, gaps = strip_underruns(pcm)
            result[f"play {fname}"] = (len(pcm) / 2 / RATE, snr(pcm, reference(music / fname)), gaps)
        # A seek through the FFmpeg plug-in.
        c.cmd("clear")
        c.cmd('add "t.flac"')
        c.cmd("play")
        time.sleep(0.5)
        _, ack = c.cmd("seek 0 2")
        time.sleep(1.5)
        st = c.status()
        result["seek t.flac"] = f"{ack} elapsed={st.get('time', '-').split(':')[0]} state={st.get('state')}"
        c.cmd("stop")
        # count_externalClient, read by the admin client after each step.
        seq = []
        count = lambda: c.status().get("count_externalClient", "absent")
        e1, e2 = Client(m.host, m.port), Client(m.host, m.port)
        e1.cmd(f'password "{EXTERNAL}"'); seq.append(count())
        e2.cmd(f'password "{EXTERNAL}"'); seq.append(count())
        e1.cmd(f'password "{ADMIN}"'); seq.append(count())
        e2.close(); time.sleep(0.5); seq.append(count())
        e1.cmd(f'password "{EXTERNAL}"'); seq.append(count())
        e1.cmd('password "wrong"'); seq.append(count())
        e1.close(); time.sleep(0.5); seq.append(count())
        result["count"] = " ".join(seq)
        c.close()
    except (OSError, RuntimeError) as e:
        print(f"  {name}: {e}; mpd exit status {m.exit_status()}; the end of its log:")
        print("\n".join(f"      {r}" for r in m.log_tail()))
        m.stop()
        raise SystemExit(1)
    m.stop()
    return result


def main(work, spec):
    work = Path(work)
    music = work / "music"
    make_music(music)
    new = run("system", spec, work, music)
    ok = True
    for key, value in new.items():
        print(f"  {key:<14} {fmt(value)}")
    for fname, plugin, lossless in FILES:
        secs, s, gaps = new[f"play {fname}"]
        # under qemu nothing is slower than real time: no underruns
        ok &= report(gaps == 0 and abs(secs - SECONDS) < 0.25 and (s == math.inf if lossless else s >= 25),
                     f"{fname} ({plugin})")
    seek = new["seek t.flac"]
    elapsed = seek.split(" ")[1].split("=")[1]
    ok &= report(seek.startswith("OK ") and elapsed.isdigit() and 2 <= int(elapsed) <= 4, "seek in t.flac")
    ok &= report(new["count"] == "1 2 1 0 1 1 0", "count_externalClient")
    return 0 if ok else 1


def fmt(v):
    if isinstance(v, tuple):
        gaps = f", {v[2]} underruns" if v[2] else ""
        return f"{v[0]:.2f} s, SNR {'inf' if v[1] == math.inf else f'{v[1]:.1f}'} dB{gaps}"
    return str(v)


def report(good, what):
    print(f"  {'ok  ' if good else 'FAIL'} {what}")
    return good


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
