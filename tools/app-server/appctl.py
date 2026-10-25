#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""appctl.py: control a running app_server.py (on 127.0.0.1:9124 by default).

appctl.py COMMAND [ARGS] runs one command; appctl.py shell gives a
prompt for many, with the rabbit's messages shown as they arrive.
"appctl.py help" lists the commands, "appctl.py help COMMAND" explains one
(the same "help" works in the shell).
"""

import difflib
import json
import re
import shlex
import shutil
import socket
import sys
import textwrap
import threading

DEFAULT_PORT = 9124
# What ends input at the shell's prompt on this platform.
EOF_KEY = "Ctrl-Z then Enter" if sys.platform == "win32" else "Ctrl-D"

# The commands: (group, name, arguments, summary, details, needs interactive
# mode). Each summary must read on its own, in the list and in "help COMMAND".
I, F = True, False
COMMANDS = [
    ("Getting started", "status", "",
     "show the rabbit's connection and interactive mode",
     "Also lists the applications the server offers the rabbit (for run).", F),
    ("Getting started", "take", "[SECONDS]",
     "take control: the rabbit then obeys your commands",
     "While you hold interactive mode the rabbit does what the commands that need it (marked * in the full list) ask: light, move, speak, "
     "play, record. It stops what it was doing when you take it. If no command comes for SECONDS (900, the "
     "most, by default), the rabbit takes itself back.\nExample: take 300", F),
    ("Getting started", "release", "",
     "give control back to the rabbit",
     "The rabbit resets: the ears go back to their start, the LED to its normal colour, and any sound stops.", I),
    ("Getting started", "shell", "",
     "type commands at a prompt, watching the rabbit",
     "One command per line, as you would type it after appctl.py. What the rabbit sends (button presses, "
     "RFID tags, the answers to commands) is shown as it arrives. \"help\" and \"help COMMAND\" work there "
     f"too; \"quit\", \"exit\" or {EOF_KEY} ends it.", F),
    ("Getting started", "help", "[COMMAND]",
     "list the commands, or explain one", "Example: help ears", F),
    ("The rabbit", "info", "",
     "show the rabbit's addresses, versions and more", "", F),
    ("The rabbit", "events", "[N]",
     "show what the rabbit sent lately (the last N)",
     "Button presses, RFID tags, ear movements, and the answers to commands.", F),
    ("The rabbit", "run", "APP [INSTANCE]",
     "start an application on the rabbit",
     "APP is an application's name, as status lists them. INSTANCE is one of its configurations in the "
     "server's settings file (the first by default). The application starts about 3 s later; what it reports "
     "comes back with app-log. The settings file needs the rabbit's time zone.\nExample: run devicetest", F),
    ("The rabbit", "reload", "",
     "send the settings file to the rabbit again", "", F),
    ("The rabbit", "system", "COMMAND",
     "turn telnet or logging on/off, or reboot",
     "COMMAND is TELNET_ON or TELNET_OFF (the rabbit's telnet access), LOG_ON or LOG_OFF (its logging), "
     "LOG_REMOTE_ON or LOG_REMOTE_OFF, or SYSTEM_REBOOT. The rabbit doesn't answer these. After a reboot, "
     "the rabbit reconnects only once its services have been started again.\nExample: system TELNET_ON", F),
    ("Messages and contacts", "messages", "",
     "list the messages waiting for the rabbit",
     "Text or sound messages from the rabbit's contacts, kept by the server (in its settings file). "
     "Applications on the rabbit can read, mark and delete them.", F),
    ("Messages and contacts", "pending", "[on|off]",
     "turn the \"you have messages\" light on or off",
     "The LED pulses green and blue while it is on. Without on or off, it is on while a message is unread.", F),
    ("Messages and contacts", "contacts", "",
     "send the rabbit its contact list again", "", F),
    ("LED and ears", "led", "COLOUR",
     "light the LED in a colour",
     "COLOUR is six hexadecimal digits, red green blue: FF0000 red, 00FF00 green, 0000FF blue, FFFF00 "
     "yellow, FFFFFF white, 000000 off.\nExample: led 00FF00", I),
    ("LED and ears", "fade", "COLOUR [MS]",
     "fade the LED to a colour over MS milliseconds",
     "MS is 1000 (one second) by default. COLOUR as for led.\nExample: fade FF0000 2000", I),
    ("LED and ears", "pulse", "COLOUR [MS] [LENGTH]",
     "pulse the LED, MS milliseconds per pulse",
     "MS is 500 by default. LENGTH is how long it pulses, in milliseconds; without it, or negative, it pulses "
     "until the LED is given something else to do. COLOUR as for led.\nExample: pulse FFFF00 500 5000   "
     "(yellow, for 5 seconds)", I),
    ("LED and ears", "ears", "LEFT RIGHT",
     "move the ears to two positions",
     "Each position counts steps from the ears' start position; 17 steps make one full turn, so 0 to 16 "
     "covers every position.\nExample: ears 5 12", I),
    ("LED and ears", "ears-reset", "",
     "move the ears back to their start position", "", I),
    ("Speech", "say", "TEXT...",
     "say TEXT out loud, in the rabbit's language",
     "The speech is made by the server (started with --tts-port) and played by the rabbit. The language is "
     "the one in the server's settings file.\nExample: say hello from the PC", I),
    ("Speech", "say-in", "LANGUAGE TEXT...",
     "say TEXT out loud in another language",
     "LANGUAGE is a code such as en-US, fr-FR or de-DE.\nExample: say-in fr-FR bonjour", I),
    ("Speech", "say-stop", "",
     "stop speaking", "", I),
    ("Speech", "listen", "LANGUAGE WORD...",
     "listen for one of the words (speech recognition)",
     "The rabbit records what it hears and sends it to the recognition server, which answers with the word "
     "it heard.\n"
     "This app server only pretends to recognise speech: it doesn't analyse the recording, it always "
     "answers with the first word (or what asr-answer chooses). That is enough to test the rabbit's "
     "recording and answers, not to understand you.\n"
     "The rabbit sends recordings to the server named in its voice.conf. Out of the box that is the old "
     "Karotz servers, which no longer exist; the README shows how to point it at this server.\n"
     "Example: listen en-US yes no", I),
    ("Sound and music", "play", "WHAT",
     "play a sound from the web, rabbit or USB stick",
     "WHAT is a web address (http://...), a sound file on the rabbit "
     "(file:///usr/karotz/sounds/notify.mp3), or music from the USB stick: dir::FOLDER, "
     "artist::NAME, genre::NAME, title::NAME, playlist::NAME, or allsong:: for everything. The music "
     "player's settings work the same way: mode::normal, mode::random, mode::repeat, mode::norepeat, "
     "lock::yes, lock::no. Names with spaces need no quotes.\n"
     "Examples: play title::Little Wing   play mode::random", I),
    ("Sound and music", "stop", "",
     "stop playing (or recording)", "", I),
    ("Sound and music", "pause", "",
     "pause what is playing", "", I),
    ("Sound and music", "resume", "",
     "carry on playing after a pause", "", I),
    ("Sound and music", "next", "",
     "skip to the next song (USB stick)", "", I),
    ("Sound and music", "previous", "",
     "go back to the previous song (USB stick)", "", I),
    ("Sound and music", "artists", "", "list the artists on the USB stick", "", I),
    ("Sound and music", "folders", "", "list the folders on the USB stick", "", I),
    ("Sound and music", "genres", "", "list the music genres on the USB stick", "", I),
    ("Sound and music", "songs", "", "list the songs on the USB stick", "", I),
    ("Sound and music", "playlists", "", "list the music player's playlists", "", I),
    ("Camera and microphone", "photo", "WHERE",
     "take a picture and upload it",
     "WHERE is the address the rabbit uploads it to. This server's /upload saves it in the server's uploads "
     "folder; \"file\" keeps it on the rabbit instead (/tmp/picture.jpg).\n"
     "Example: photo http://192.168.1.10:8080/upload/photo   (use this PC's address)", I),
    ("Camera and microphone", "record", "",
     "record from the microphone until you run stop",
     "The recording is kept on the rabbit, in /tmp/record.flac.", I),
    ("Other", "choreography", "FILE",
     "play a choreography file (a sequence of moves)", "", I),
    ("Other", "ping", "",
     "check that the rabbit answers", "", I),
    ("Testing and development", "app-log", "[SINCE]",
     "show what applications reported to the server",
     "Applications report by calling the server's /log address (devicetest does). The answer has the reports "
     "and a count; give that count as SINCE next time to see only the newer ones.\nExample: app-log 12", F),
    ("Testing and development", "asr-answer", "[first|nomatch|WORDS...]",
     "choose what speech recognition answers from now on",
     "first: the first of the words listen offered (the default); nomatch: that nothing matched; anything "
     "else: those words.\nExample: asr-answer nomatch", F),
    ("Testing and development", "take-as", "ID [SECONDS]",
     "take interactive mode under an id of your choice",
     "For testing the rabbit: an empty id (take-as \"\") must be refused (BAD_INTERACTIVEID).", F),
    ("Testing and development", "login", "",
     "let the rabbit log in (server started with --hold)", "", F),
    ("Testing and development", "send", "MESSAGE...",
     "send the rabbit any message (protobuf text)",
     "For developers: MESSAGE is a PlatformMessage body (see userland/proto/platform-message.proto); your interactive "
     "id is added when you hold interactive mode.\n"
     "Example: send serial { open { port: \"/dev/ttyUSB0\" baud: 9600 } }", F),
    ("Testing and development", "send-raw", "[--no-id] [--cut N] MESSAGE...",
     "send a broken message, to test the rabbit",
     "As send, but --no-id leaves out the message's (required) id, --cut N drops its last N bytes, and "
     "required fields may be missing. The rabbit should answer ERROR and do nothing.\n"
     "Example: send-raw --cut 1 led { light { color: \"FF0000\" } }", F),
]
BY_NAME = {c[1]: c for c in COMMANDS}


SHORT_USAGE = """usage: appctl.py [--port N | --socket PATH] COMMAND [ARGS]
       appctl.py shell            many commands at a prompt
       appctl.py help [COMMAND]   the commands, or more on one"""


class ServerUnreachable(Exception):
    pass


class Server:
    def __init__(self, port=DEFAULT_PORT, path=None):
        self.port, self.path = port, path

    def where(self):
        return self.path or f"127.0.0.1:{self.port}"

    def __call__(self, cmd, *args):
        """One command; returns (ok, result or error). Raises ServerUnreachable."""
        try:
            if self.path:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(30)
                sock.connect(self.path)
            else:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        except OSError as e:
            raise ServerUnreachable(
                f"no app server at {self.where()} ({e.strerror or e}): start app_server.py first "
                "(see README.md), or give its --port or --socket") from None
        with sock:
            stream = sock.makefile("rw")
            stream.write(json.dumps({"cmd": cmd, "args": list(args)}) + "\n")
            stream.flush()
            line = stream.readline()
        if not line:
            raise ServerUnreachable(f"the app server at {self.where()} closed the connection")
        answer = json.loads(line)
        return (True, answer["result"]) if answer["ok"] else (False, answer["error"])


def usage(name):
    c = BY_NAME[name]
    return f"{c[1]} {c[2]}".strip()


def needs_interactive(c):
    return c[5]


def wrapped(text, indent, width):
    """text wrapped to width, every line after the first indented by `indent` spaces."""
    return textwrap.fill(text, width=width, subsequent_indent=" " * indent, break_on_hyphens=False)


def help_text(topic=None, width=None, in_shell=False):
    """The list of commands, or the help for one, wrapped to the terminal's width
    (in_shell: as the shell's own help, for its prompt)."""
    width = width or max(60, min(110, shutil.get_terminal_size((100, 24)).columns - 1))
    if topic:
        if topic not in BY_NAME:
            close = difflib.get_close_matches(topic, BY_NAME, n=3)
            return f"no command {topic!r}" + (f"; did you mean {', '.join(close)}?" if close else "; see \"help\"")
        c = BY_NAME[topic]
        lines = [usage(c[1]), "", "  " + wrapped(c[3][0].upper() + c[3][1:] + ".", 2, width)]
        for paragraph in c[4].split("\n") if c[4] else []:
            lines += ["", "  " + wrapped(paragraph, 2, width)]
        if needs_interactive(c):
            lines += ["", "  Needs interactive mode: run \"take\" first."]
        return "\n".join(lines)
    column = 28
    if in_shell:
        intro = ("Type one command per line; what the rabbit sends is shown as it arrives. "
                 f"\"quit\" or {EOF_KEY} ends the shell.")
        prefix = ""
    else:
        intro = ("appctl.py drives a Karotz rabbit through the app server (app_server.py), which must be "
                 "running; the rabbit connects to it. Run one command as \"appctl.py COMMAND\", or type "
                 "many with \"appctl.py shell\".")
        prefix = "appctl.py "
    lines = [wrapped(intro, 0, width), "",
             wrapped("The rabbit only obeys the commands marked * while you hold interactive mode: run "
                     "\"take\" first, and \"release\" when you are done. For example:", 0, width)]
    lines += [f"    {prefix}{c}" for c in ("take", "led FF0000", "say hello", "release")]
    lines += [wrapped("\"help COMMAND\" explains one command, e.g. \"help ears\".", 0, width), ""]
    group = None
    for c in COMMANDS:
        if in_shell and c[1] == "shell":
            continue
        if c[0] != group:
            group = c[0]
            lines.append(group)
        mark = "*" if needs_interactive(c) else " "
        left = f" {mark} {usage(c[1])}"
        if width >= 70 and len(left) + 2 <= column and column + len(c[3]) <= width:
            lines.append(f"{left:<{column}}{c[3]}")
        else:
            # Too long to sit beside the command: on its own lines below, flowing to the full width.
            lines.append(left)
            lines.append("      " + wrapped(c[3], 6, width))
    if not in_shell:
        lines += ["", wrapped("Options, before the command: --port N or --socket PATH, for a server started "
                              "with --control-port or --control-socket.", 0, width)]
    return "\n".join(lines)


IDS = re.compile(r'\b(id|correlationId|interactiveId): "[^"]*" ?')


def compact(text):
    """A message line without its ids (the shell's view): 'response { code: OK }'."""
    return IDS.sub("", str(text)).strip()


def show(result):
    if isinstance(result, list):
        return "\n".join(str(r) for r in result)
    if isinstance(result, dict):
        return json.dumps(result, indent=2)
    return str(result)


def shell(server):
    """Read commands, one per line (the same as appctl.py's), and show the
    rabbit's messages (button, RFID, ears, events, ...) as they arrive."""
    try:
        import readline  # noqa: F401  (line editing and history, where available)
    except ImportError:
        pass
    ok, status = server("status")
    if not ok:
        print("error:", status)
        return 1
    if not status["connected"]:
        state = "The rabbit isn't connected to the server yet; it connects by itself once it is running."
    elif not status["logged_in"]:
        state = "The rabbit is connected but not logged in yet (the server was started with --hold: run \"login\")."
    elif status["interactive_id"]:
        state = "The rabbit is connected, and in interactive mode: it obeys your commands."
    else:
        state = ("The rabbit is connected. To make it light up, move, speak or play, run \"take\" first "
                 "(interactive mode); \"release\" gives it back.")
    print(wrapped(state, 0, max(60, min(110, shutil.get_terminal_size((100, 24)).columns - 1))))
    print(f'"help" lists the commands, "help COMMAND" explains one; "quit" or {EOF_KEY} ends the shell.')
    ok, since = server("events-since")
    count = [since["count"] if ok else 0]
    stop = threading.Event()
    printing = threading.Lock()

    def watch():
        while not stop.wait(1):
            try:
                ok, new = server("events-since", str(count[0]))
            except (OSError, ServerUnreachable):
                continue
            if ok and new["events"]:
                count[0] = new["count"]
                with printing:
                    for event in new["events"]:
                        print(f"\r<- {compact(event)}")
                    print("rabbit> ", end="", flush=True)

    threading.Thread(target=watch, daemon=True).start()
    try:
        while True:
            try:
                line = input("rabbit> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("quit", "exit"):
                break
            try:
                words = shlex.split(line)
            except ValueError as e:
                print("error:", e)
                continue
            if words[0] in ("help", "?"):
                print(help_text(words[1] if len(words) > 1 else None, in_shell=True))
                continue
            try:
                ok, result = server(*words)
            except ServerUnreachable as e:
                print("error:", e)
                continue
            with printing:
                print(compact(show(result)) if ok else f"error: {result}")
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
    return 0


def main(argv):
    port, path = DEFAULT_PORT, None
    if len(argv) > 1 and argv[0] == "--port":
        port, argv = int(argv[1]), argv[2:]
    elif len(argv) > 1 and argv[0] == "--socket":
        path, argv = argv[1], argv[2:]
    if not argv:
        print(SHORT_USAGE)
        return 2
    if argv[0] in ("-h", "--help", "help"):
        print(help_text(argv[1] if len(argv) > 1 else None))
        return 0
    server = Server(port, path)
    try:
        if argv[0] == "shell":
            return shell(server)
        ok, result = server(*argv)
    except ServerUnreachable as e:
        print("error:", e, file=sys.stderr)
        return 1
    if not ok:
        print("error:", result, file=sys.stderr)
        return 1
    print(show(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
