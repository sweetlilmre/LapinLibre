# SPDX-License-Identifier: MIT
"""multimedia-daemon: karotz-player and MPD playback, the MPD database lists, recording.

Test environment (set up by drive() in each run, before the daemon gets far):
 - An "mpd" stub is added to the stub PATH, so the daemon's fork +
   execlp("mpd") at each failed MPD connection starts something that exits
   at once (a failed exec now ends the child too; aggressive test
   multimedia_failures covers that).
 - The karotz-player stub is replaced by one that sleeps (exec /bin/sleep) like a
   playing karotz-player: 300 s, or 1 s for a URL containing "quick". With the
   stock stub, which exits at once, the SIGCHLD
   handler can run before play sets the "player playing" state, and a later
   stop then calls kill(-1, SIGTERM) (player_pid is -1), which would hit
   every process of the harness user.
 - /tmp/karotz-player.ctrl, karotz-player's command FIFO, is a regular file, so the
   daemon's fopen(..., "w") doesn't block for want of a reader; what the
   daemon writes there is recorded.
 - /dev/audio is a regular file holding one block of silence and one of
   sound; its OSS ioctls (type 'P') are faked. After those 2048 bytes read()
   returns 0 and the recording pump sends silence; only device I/O sizes
   are compared, not what goes to flac. flac isn't on PATH, so the shell
   popen() starts exits 127 and the pipe breaks (SIGPIPE is ignored): each
   recording ends as failed (recordFinished with ERROR).

Phase 1, no MPD: libmpd can't connect (every second the daemon runs
"killall mpd" and forks the stub), so lists are empty and MPD plays fail.

Phase 2, a fake MPD: the scenario enters the daemon's user and network
namespaces with nsenter (they belong to the harness user) and runs a small
MPD protocol server on 127.0.0.1:6600 there (this file, run with "serve"),
with a fixed database and a player state that follows the commands. Every
command it receives is recorded, except the status/stats polls whose number
depends on timing. Touching ctrl/external or ctrl/dbupdate makes its status
report an external client or a database update, as a real MPD would.

Each step waits for the daemon's 1 s status poll so state changes reported
by MPD come in the same place in every run.
"""

import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time

DAEMON = "multimedia-daemon"
DEVICES = ["audio"]
REGULAR_FILE_DEVICES = ["audio"]
FAKE_IOCTL_TYPES = [0x50]  # OSS SNDCTL_DSP_*
SERVICE = "com.mindscape.karotz.Multimedia"
PATH = "/com/mindscape/karotz/Multimedia"
IFACE = "com.mindscape.karotz.KarotzInterface"

PLAYER_CTRL = "/tmp/karotz-player.ctrl"
SETTLE = 1.6  # longer than the daemon's 1 s MPD status poll
POLL_COMMANDS = {"status", "stats", "outputs"}  # libmpd reads outputs with each status


# --- the fake MPD (run inside the daemon's network namespace) ---------------

SONGS = [
    {"file": "Music/Rock/a.mp3", "Time": "120", "Artist": "Alice", "Title": "Song A", "Genre": "Rock"},
    {"file": "Music/Rock/b.mp3", "Time": "130", "Artist": "Bob", "Title": "Song B", "Genre": "Rock"},
    {"file": "Music/Jazz/c.mp3", "Time": "140", "Artist": "Alice", "Title": "Song C", "Genre": "Jazz"},
    {"file": "top.mp3", "Time": "150", "Artist": "Carol", "Title": "", "Genre": ""},
]
DIRECTORIES = ["Music", "Music/Rock", "Music/Jazz", "Empty"]
PLAYLISTS = {"favourites": ["Music/Rock/a.mp3", "Music/Rock/b.mp3"], "party": ["Music/Jazz/c.mp3"]}
TAG_NAMES = {"artist": "Artist", "title": "Title", "genre": "Genre", "album": "Album", "file": "file",
             "filename": "file"}
COMMANDS = ["add", "clear", "commands", "crossfade", "currentsong", "disableoutput", "enableoutput", "find",
            "list", "listall", "listallinfo", "listplaylists", "load", "lsinfo", "next", "notcommands",
            "outputs", "password", "pause", "play", "playid", "playlistinfo", "plchanges", "plchangesposid",
            "previous", "random", "repeat", "search", "setvol", "stats", "status", "stop", "tagtypes",
            "urlhandlers", "update"]


# Syslog warnings the run is known to log (harness.py): the refused and failing commands the
# scenario sends and the stub encoder.
EXPECTED_WARNINGS = [
    r"^playlist not found!$",
    r"^Multimedia: API - (lock|mode)::\w* failed or unknown$",
    r"^Multimedia: API - already busy with mpd - abort$",
    r"^Multimedia: API - '[^']*': (unknown command|no argument after '::'), refused$",
    r"^mpd_control_play_(dir|artist)\. (directory|artist) not found\. abort$",
    r"^Record: write to the encoder failed: Broken pipe$",
    r"^Record Stop: the recording failed \(encoder status \d+\)$",
]


def song_block(song, pos=None):
    lines = [f"file: {song['file']}"]
    lines += [f"{k}: {v}" for k, v in song.items() if k != "file" and v != ""]
    if pos is not None:
        lines += [f"Pos: {pos}", f"Id: {pos + 1}"]
    return lines


def parent_dir(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


class FakeMpd:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.log = open(os.path.join(ctrl, "log"), "a", buffering=1)
        self.playlist, self.version, self.state, self.current = [], 1, "stop", 0
        self.volume, self.random, self.repeat, self.output = 80, 0, 0, 1

    def flag(self, name):
        return os.path.exists(os.path.join(self.ctrl, name))

    def run(self, line):
        """The response lines for one command (without the final OK)."""
        try:
            args = shlex.split(line)
        except ValueError:
            args = line.split()
        if not args:
            return []
        cmd, args = args[0].lower(), args[1:]
        arg = args[0] if args else ""
        if cmd == "status":
            out = [f"volume: {self.volume}", f"repeat: {self.repeat}", f"random: {self.random}",
                   f"playlist: {self.version}", f"playlistlength: {len(self.playlist)}", "xfade: 0",
                   f"state: {self.state}"]
            if self.state != "stop" and self.playlist:
                song = self.playlist[self.current]
                out += [f"song: {self.current}", f"songid: {self.current + 1}", f"time: 0:{song['Time']}",
                        "bitrate: 128", "audio: 44100:16:2"]
            out.append(f"count_externalClient: {1 if self.flag('external') else 0}")
            return out
        if cmd == "stats":
            return ["artists: 3", "albums: 0", f"songs: {len(SONGS)}", "uptime: 100", "playtime: 0",
                    "db_playtime: 540", f"db_update: {2000 if self.flag('dbupdate') else 1000}"]
        if cmd == "commands":
            return [f"command: {c}" for c in COMMANDS]
        if cmd == "tagtypes":
            return ["tagtype: Artist", "tagtype: Album", "tagtype: Title", "tagtype: Genre"]
        if cmd == "urlhandlers":
            return ["handler: http://"]
        if cmd == "outputs":
            return ["outputid: 0", "outputname: My OSS Device", f"outputenabled: {self.output}"]
        if cmd in ("enableoutput", "disableoutput"):
            self.output = 1 if cmd == "enableoutput" else 0
            return []
        if cmd in ("currentsong",):
            if self.state != "stop" and self.playlist:
                return song_block(self.playlist[self.current], self.current)
            return []
        if cmd == "playlistid":
            i = int(arg or 0) - 1
            return song_block(self.playlist[i], i) if 0 <= i < len(self.playlist) else []
        if cmd in ("playlistinfo", "plchanges"):
            return [l for i, s in enumerate(self.playlist) for l in song_block(s, i)]
        if cmd == "plchangesposid":
            return [l for i in range(len(self.playlist)) for l in (f"cpos: {i}", f"Id: {i + 1}")]
        if cmd == "list":
            tag = TAG_NAMES.get(arg.lower(), arg.capitalize())
            values = sorted({s.get(tag, "") for s in SONGS} - {""})
            return [f"{tag}: {v}" for v in values]
        if cmd in ("find", "search"):
            found = SONGS
            for i in range(0, len(args) - 1, 2):
                tag, value = TAG_NAMES.get(args[i].lower(), args[i]), args[i + 1]
                if cmd == "find":
                    found = [s for s in found if s.get(tag, "") == value]
                else:
                    found = [s for s in found if value.lower() in s.get(tag, "").lower()]
            return [l for s in found for l in song_block(s)]
        if cmd == "lsinfo":
            out = [f"directory: {d}" for d in DIRECTORIES if parent_dir(d) == arg]
            out += [l for s in SONGS if parent_dir(s["file"]) == arg for l in song_block(s)]
            return out
        if cmd in ("listallinfo", "listall"):
            arg = "" if arg == "/" else arg
            inside = lambda p: arg == "" or p == arg or p.startswith(arg + "/")
            out = [f"directory: {d}" for d in DIRECTORIES if inside(d) and d != arg]
            for s in SONGS:
                if inside(s["file"]):
                    out += song_block(s) if cmd == "listallinfo" else [f"file: {s['file']}"]
            return out
        if cmd == "listplaylists":
            return [l for p in PLAYLISTS for l in (f"playlist: {p}", "Last-Modified: 2010-11-01T00:00:00Z")]
        if cmd == "clear":
            self.playlist, self.state, self.current = [], "stop", 0
            self.version += 1
        elif cmd == "add":
            self.playlist += [s for s in SONGS if s["file"] == arg]
            self.version += 1
        elif cmd == "load":
            self.playlist += [s for f in PLAYLISTS.get(arg, []) for s in SONGS if s["file"] == f]
            self.version += 1
        elif cmd in ("play", "playid"):
            if self.playlist:
                self.state = "play"
        elif cmd == "pause":
            if arg == "1" or (arg == "" and self.state == "play"):
                self.state = "pause" if self.state != "stop" else "stop"
            elif self.state == "pause":
                self.state = "play"
        elif cmd == "stop":
            self.state = "stop"
        elif cmd in ("next", "previous"):
            if self.playlist and self.state != "stop":
                step = 1 if cmd == "next" else -1
                self.current = (self.current + step) % len(self.playlist)
        elif cmd == "setvol":
            self.volume = int(arg or 0)
        elif cmd == "random":
            self.random = int(arg or 0)
        elif cmd == "repeat":
            self.repeat = int(arg or 0)
        return []

    def client(self, conn):
        f = conn.makefile("rwb", buffering=0)
        f.write(b"OK MPD 0.15.0\n")
        in_list, list_ok, pending = False, False, []
        for raw in f:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            self.log.write(line + "\n")
            word = line.split(" ", 1)[0]
            if word in ("command_list_begin", "command_list_ok_begin"):
                in_list, list_ok, pending = True, word == "command_list_ok_begin", []
                continue
            if word == "command_list_end":
                out = []
                for l in pending:
                    out += self.run(l) + (["list_OK"] if list_ok else [])
                in_list = False
            elif in_list:
                pending.append(line)
                continue
            else:
                out = self.run(line)
            f.write(("".join(l + "\n" for l in out) + "OK\n").encode())
        conn.close()


def serve(ctrl, watch_pid):
    """Run the fake MPD until the daemon (watch_pid) has gone."""
    mpd = FakeMpd(ctrl)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 6600))
    srv.listen(5)
    srv.settimeout(0.5)
    open(os.path.join(ctrl, "ready"), "w").close()
    while True:
        try:
            os.kill(watch_pid, 0)
        except OSError:
            break
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        conn.settimeout(None)
        threading.Thread(target=mpd.client, args=(conn,), daemon=True).start()


class FakeServer:
    def __init__(self, pid):
        self.ctrl = tempfile.mkdtemp(prefix="karotz-fake-mpd-")
        self.offset = 0
        open(os.path.join(self.ctrl, "log"), "w").close()
        self.proc = subprocess.Popen(["nsenter", "-t", str(pid), "-U", "-n", "--preserve-credentials", "--", sys.executable, "-I",
                                      os.path.abspath(__file__), "serve", self.ctrl, str(pid)],
                                     stdout=subprocess.DEVNULL, stderr=open(os.path.join(self.ctrl, "err"), "w"),
                                     start_new_session=True)
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(os.path.join(self.ctrl, "ready")):
            time.sleep(0.1)
        self.ready = os.path.exists(os.path.join(self.ctrl, "ready"))

    def received(self):
        """Commands received since the last call, polls left out."""
        with open(os.path.join(self.ctrl, "log")) as f:
            f.seek(self.offset)
            text = f.read()
            self.offset = f.tell()
        return [l for l in text.splitlines() if l.split(" ", 1)[0] not in POLL_COMMANDS]

    def connected(self):
        """The daemon has logged in (libmpd sends the password after connecting)."""
        with open(os.path.join(self.ctrl, "log")) as f:
            return any(l.startswith("password") for l in f)

    def touch(self, name):
        open(os.path.join(self.ctrl, name), "w").close()


# --- the scenario -----------------------------------------------------------

def reply(out):
    """The reply's values, or the error name."""
    lines = out.splitlines()
    if not lines:
        return "(no reply)"
    if lines[0].startswith("Error "):
        return lines[0].split(":")[0]
    return " | ".join(l.strip() for l in lines[1:]) or "(no values)"


def read_ctrl():
    try:
        with open(PLAYER_CTRL) as f:
            text = f.read()
        open(PLAYER_CTRL, "w").close()
        return text
    except OSError as e:
        return f"<{e.strerror}>"


class Steps:
    def __init__(self, bus, record):
        self.bus, self.record, self.mpd = bus, record, None

    def call(self, method, *args, settle=SETTLE):
        out = self.bus.send("--reply-timeout=20000", f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
        self.record.append(f"{method}({', '.join(args)}) -> {reply(out)}")
        time.sleep(settle)
        self.notes()

    def notes(self):
        if self.mpd:
            self.record.extend("  mpd: " + l for l in self.mpd.received())
        ctrl = read_ctrl()
        if ctrl:
            self.record.append(f"  player_ctrl: {ctrl!r}")

    def play(self, ident, url, behaviour=0, settle=SETTLE):
        self.call("play", f"string:{ident}", f"string:{url}", f"int32:{behaviour}", settle=settle)

    def simple(self, method, ident="id", behaviour=0):
        self.call(method, f"string:{ident}", f"int32:{behaviour}")

    def lists(self):
        for method in ["get_artist", "get_genre", "get_song", "get_playlist", "get_folder"]:
            self.call(method, settle=0.3)


def prepare(d):
    """The test environment the docstring describes (stubs, /tmp/karotz-player.ctrl, /dev/audio)."""
    stubs = os.path.join(os.path.dirname(d.log.name), "stub-bin")
    with open(os.path.join(stubs, "mpd"), "w") as f:
        f.write("#!/bin/true\n")
    os.chmod(os.path.join(stubs, "mpd"), 0o755)
    with open(os.path.join(stubs, "karotz-player"), "w") as f:
        f.write('#!/bin/sh\ncase "$*" in *quick*) exec /bin/sleep 1;; *) exec /bin/sleep 300;; esac\n')
    os.chmod(os.path.join(stubs, "karotz-player"), 0o755)
    if os.path.lexists(PLAYER_CTRL):
        os.unlink(PLAYER_CTRL)
    open(PLAYER_CTRL, "w").close()
    # One block of silence (unsigned 16-bit 0x8000), then one of sound.
    d.write("audio", b"\x00\x80" * 512 + bytes(range(256)) * 4)


def normalise_record(record):
    """Take the flac starts out of the ordered device I/O and list the flac
    command lines once, in a section of their own.

    record_go() popen()s flac (uClibc: vfork + execve of sh -c) before it
    adds the pump that reads /dev/audio. On the rabbit vfork suspends the
    parent until the execve, but qemu-user turns vfork into fork: the child
    and the parent then run at once and both write qemu's strace, so the
    child's execve line can land among the parent's reads. That made the
    end of the record differ in about one run in five. The
    command flac is started with is still compared.
    """
    from harness import collapse_repeats

    if "== device I/O" not in record:
        return record
    start = record.index("== device I/O") + 1
    end = next((i for i in range(start, len(record)) if record[i].startswith("== ")), len(record))
    io = [line for line in record[start:end] if not line.startswith("system flac ")]
    flac = sorted({line for line in record[start:end] if line.startswith("system flac ")})
    return record[:start] + collapse_repeats(io) + ["== flac started as"] + flac + record[end:]


def drive(d, bus):
    from harness import introspection

    prepare(d)
    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    record.append("== introspection")
    record.extend(introspection(bus, SERVICE, PATH))
    s = Steps(bus, record)

    record.append("== phase 1: no MPD")
    s.lists()
    s.play("p1", "playlist::favourites")
    s.play("p2", "mode::random")
    s.play("p3", "lock::yes")
    s.simple("next")
    s.simple("previous")
    s.simple("pause")
    s.simple("resume")
    s.simple("stop")

    record.append("== phase 2: fake MPD")
    s.mpd = FakeServer(d.proc.pid)
    record.append(f"fake MPD ready: {s.mpd.ready}")
    deadline = time.time() + 30
    while time.time() < deadline and not s.mpd.connected():
        time.sleep(0.2)
    connected = s.mpd.connected()
    record.append(f"daemon connected to the fake MPD: {connected}")
    if not connected:
        # Every second a forked mpd stub exits; with karotz-player running, the
        # SIGCHLD handler would then wait for karotz-player forever.
        return record
    time.sleep(2 * SETTLE)  # the first status report
    s.notes()

    record.append("-- lists")
    s.lists()

    record.append("-- MPD playback")
    s.play("m1", "playlist::favourites")
    s.simple("pause", "m1")
    s.simple("resume", "m1")
    s.simple("next", "m1")
    s.simple("previous", "m1")
    s.play("m2", "/usr/karotz/sounds/notify.wav")  # busy with MPD
    s.simple("stop", "m1")
    s.simple("stop", "m1")
    s.play("m3", "playlist::nope")
    s.play("m4", "p::party")  # commands match by their exact names: refused
    s.simple("stop")
    s.play("m5", "dir::Music")
    s.play("m6", "dir::Nope")
    s.play("m7", "artist::Alice")
    s.play("m8", "artist::Nobody")
    s.play("m9", "genre::Rock")
    s.play("m10", "title::Song B")
    s.play("m11", "allsong::")  # allsong takes no argument
    s.play("m12", "allsong::x")  # (one is ignored)
    s.play("m13", "bogus::x")
    s.simple("stop")
    for mode in ["random", "repeat", "normal", "norepeat", "shuffle"]:
        s.play(f"mode-{mode}", f"mode::{mode}")
    s.play("lock1", "lock::no")
    s.play("lock2", "lock::yes")
    s.play("lock3", "lock::maybe")  # yes or no only: refused

    record.append("-- MPD reports")
    s.mpd.touch("external")
    time.sleep(SETTLE)
    s.notes()
    s.mpd.touch("dbupdate")
    time.sleep(SETTLE)
    s.notes()

    record.append("-- record")
    s.call("record", "string:rec1", "string:/tmp/rec1.flac", "int32:0")
    s.call("record", "string:rec2", "string:/tmp/rec2.flac", "int32:0")  # stops rec1 first
    s.simple("stop", "rec2")
    s.call("record", "string:", "string:/tmp/rec3.flac", "int32:0")  # an empty id is stopped like any other
    s.simple("stop")
    s.call("record", "string:rec4", "string:/tmp/rec4.flac", "int32:0")
    s.simple("stop", "rec4")

    record.append("-- karotz-player")
    # This one ends by itself after 1 s: the SIGCHLD handler then reaps the
    # mpd stubs' zombies, so stop's wait() below really waits for karotz-player.
    # (Otherwise wait() returns a zombie at once and SIGCHLD can arrive while
    # the daemon is inside g_log(); GLib then aborts on the recursive log
    # message, but only sometimes.)
    s.play("quick", "/tmp/quick.mp3", settle=2.5)
    # A setting during a play is not a play: the play keeps its id, so its
    # playFinished carries q2 (not q2-mode).
    s.play("q2", "/tmp/quick.mp3", settle=0.3)
    s.play("q2-mode", "mode::random", settle=2.5)
    s.play("mp1", "file:///usr/karotz/sounds/notify.wav")
    s.play("mp2", "  /tmp/radio.m3u \n")  # running: loadlist, trimmed
    s.play("mp3", "http://127.0.0.1:9/stream.mp3")
    s.play("mp4", "playlist::favourites")  # busy with karotz-player
    s.play("mp5", "lock::no")  # a setting: applied while karotz-player plays
    s.simple("pause", "mp1")
    s.simple("pause", "mp1")
    s.simple("next", "mp1")
    s.simple("resume", "mp1")
    s.simple("previous", "mp1")
    s.simple("stop", "mp1")
    s.simple("stop", "mp1")
    s.play("mp6", "::x.pls")  # "::" at 0 is a path for karotz-player
    s.simple("resume", "mp6")
    s.simple("stop", "mp6")
    s.play("mp7", "playlist::party")

    record.append("-- empty argument")
    # Nothing after "::" is refused (false) where the command needs an
    # argument (all but allsong), and the daemon goes on.
    s.play("e1", "mode::")
    s.play("e2", "lock::")
    s.play("e3", "playlist::")
    s.simple("next", "e4")
    return record


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "serve":
    serve(sys.argv[2], int(sys.argv[3]))
