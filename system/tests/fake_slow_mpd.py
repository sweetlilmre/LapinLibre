# SPDX-License-Identifier: MIT
"""system/tests/libmpd-eintr.sh's server: an MPD that greets, then answers "status" after 1.5 s.

    fake_slow_mpd.py PORT_FILE

Listens on a free loopback port (written to PORT_FILE), serves one connection, and exits.
"""

import socket
import sys
import time


def main(port_file):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    with open(port_file, "w", encoding="ascii") as f:
        f.write(str(srv.getsockname()[1]))
    srv.settimeout(60)
    conn, _ = srv.accept()
    conn.sendall(b"OK MPD 0.15.0\n")
    buf = b""
    conn.settimeout(30)
    while True:
        data = conn.recv(1024)
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line == b"status":
                time.sleep(1.5)
                conn.sendall(b"volume: 42\nrepeat: 0\nrandom: 0\nplaylist: 1\nplaylistlength: 0\nstate: stop\nOK\n")
            elif line == b"close":
                return
            else:
                conn.sendall(b"OK\n")


if __name__ == "__main__":
    main(sys.argv[1])
