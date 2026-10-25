# SPDX-License-Identifier: MIT
"""The rabbit's /usr/etc/timezone.list, generated from IANA's time-zone database.

    tzlist.py TZDATA_TAR TZCODE_TAR OUT [--work DIR]

The controller (find_timezone() in userland/src/usr/controller/platform_listeners.cc) takes the TZ
string from the first line of /etc/timezone.list that contains the platform's zone name anywhere,
and writes it to /etc/TZ, which uClibc-ng reads. So each line is "<zone>;<POSIX TZ>":

- zic, built from the pinned tzcode, compiles the pinned tzdata. A zone's TZ is its TZif file's
  footer: the POSIX rule that holds after the zone's last listed transition.
- uClibc-ng accepts a rule's time of day only from 0 to 24 hours, not POSIX.1-2024's wider range
  (M3.4.4/26, M3.5.0/-1). Such a rule is replaced by the old-style rule that is wrong for the least
  time over the next 20 years.
- A zone whose footer doesn't hold yet (its listed transitions run past the reference date, as
  Morocco's Ramadan changes do) gets a fixed string for its most common offset over the next year.
- Every name tzdata has is listed, its links included (they give old names the current rules), and
  the lines are ordered so that each zone's own line is the first one containing its name.

The reference date is tzdata's release date (its NEWS), so the list depends only on the pins.
"""

import argparse
import calendar
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

SOURCES = ["africa", "antarctica", "asia", "australasia", "europe", "northamerica", "southamerica",
           "etcetera", "backward"]
MAX_TZ = 80    # find_timezone() copies at most 80 characters
MAX_LINE = 254  # and reads lines into a 255-byte buffer
YEARS = 20     # how far ahead a replaced rule is judged


class TzListError(Exception):
    pass


# ---- POSIX TZ strings ---------------------------------------------------------------------------

def _name(s, i):
    if s[i] == "<":
        j = s.index(">", i)
        return s[i:j + 1], j + 1
    j = i
    while j < len(s) and s[j].isalpha():
        j += 1
    if j - i < 3:
        raise ValueError(f"bad name at {i} in {s!r}")
    return s[i:j], j


def _time(s, i):
    """[+-]hh[:mm[:ss]] at i: (seconds, next index)."""
    sign = 1
    if s[i] in "+-":
        sign = -1 if s[i] == "-" else 1
        i += 1
    m = re.match(r"(\d+)(?::(\d+))?(?::(\d+))?", s[i:])
    if not m:
        raise ValueError(f"bad time at {i} in {s!r}")
    secs = int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    return sign * secs, i + m.end()


def _rule(s, i):
    m = re.match(r"M(\d+)\.(\d)\.(\d)", s[i:])
    if not m:
        raise ValueError(f"only Mm.w.d rules are expected, at {i} in {s!r}")
    i += m.end()
    t = 7200
    if i < len(s) and s[i] == "/":
        t, i = _time(s, i + 1)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), t), i


def parse(tz):
    """{std, utoff, dst, dst_utoff, start, end}: offsets east of UTC; rules (m, w, d, seconds)."""
    std, i = _name(tz, 0)
    off, i = _time(tz, i)
    out = {"std": std, "utoff": -off, "dst": None}
    if i == len(tz):
        return out
    out["dst"], i = _name(tz, i)
    out["dst_utoff"] = out["utoff"] + 3600
    if i < len(tz) and tz[i] != ",":
        off, i = _time(tz, i)
        out["dst_utoff"] = -off
    if i == len(tz) or tz[i] != ",":
        raise ValueError(f"a DST zone without rules: {tz!r}")
    out["start"], i = _rule(tz, i + 1)
    if tz[i] != ",":
        raise ValueError(f"no end rule: {tz!r}")
    out["end"], i = _rule(tz, i + 1)
    if i != len(tz):
        raise ValueError(f"trailing text: {tz!r}")
    return out


def rule_day(year, m, w, d):
    """The day of the month of the w-th weekday d (0 Sunday) of month m; w 5 is the last."""
    first = (calendar.weekday(year, m, 1) + 1) % 7  # Python's Monday-0 to POSIX's Sunday-0
    day = 1 + (d - first) % 7 + 7 * (w - 1)
    last = calendar.monthrange(year, m)[1]
    while day > last:
        day -= 7
    return day


def instant(year, rule, utoff):
    """When a rule's change happens in a year, as seconds since the epoch (local time at utoff)."""
    m, w, d, t = rule
    return calendar.timegm((year, m, rule_day(year, m, w, d), 0, 0, 0)) + t - utoff


def fmt_time(secs):
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    h, rest = divmod(secs, 3600)
    mi, se = divmod(rest, 60)
    return f"{sign}{h}" + (f":{mi:02d}" if mi or se else "") + (f":{se:02d}" if se else "")


def fmt_rule(rule):
    m, w, d, t = rule
    return f"M{m}.{w}.{d}" + ("" if t == 7200 else f"/{fmt_time(t)}")


def uclibc_ok(rule):
    """uClibc-ng's tzset() takes a rule time of at most two hour digits below 25, without a sign."""
    return 0 <= rule[3] < 25 * 3600


def nearest_rule(rule, utoff, first_year):
    """The rule uClibc-ng accepts that differs least from rule over YEARS years (total seconds)."""
    truth = [instant(y, rule, utoff) for y in range(first_year, first_year + YEARS)]
    m = rule[0]
    best = None
    times = sorted({rule[3] % 86400} | {h * 3600 for h in range(25)})
    for w in range(1, 6):
        for d in range(7):
            for t in times:
                cand = (m, w, d, t)
                err = sum(abs(instant(y, cand, utoff) - truth[k]) for k, y in enumerate(range(first_year, first_year + YEARS)))
                exact = sum(instant(y, cand, utoff) == truth[k] for k, y in enumerate(range(first_year, first_year + YEARS)))
                key = (err, -exact, cand)
                if best is None or key < best[0]:
                    best = (key, cand, err, exact)
    return best[1], best[2], best[3]


# ---- TZif files ---------------------------------------------------------------------------------

def read_tzif(data):
    """(transitions [(time, utoff, isdst, abbr)], types [(utoff, isdst, abbr)], footer) of a v2+ file."""
    if data[:4] != b"TZif" or data[4:5] < b"2":
        raise TzListError("not a TZif v2+ file")
    def counts(at):
        return struct.unpack(">6l", data[at + 20:at + 44])
    isut, isstd, leap, timecnt, typecnt, charcnt = counts(0)
    v2 = 44 + timecnt * 5 + typecnt * 6 + charcnt + leap * 8 + isstd + isut
    isut, isstd, leap, timecnt, typecnt, charcnt = counts(v2)
    p = v2 + 44
    times = struct.unpack(f">{timecnt}q", data[p:p + 8 * timecnt]); p += 8 * timecnt
    idx = data[p:p + timecnt]; p += timecnt
    types = []
    for k in range(typecnt):
        utoff, isdst, ai = struct.unpack(">lBB", data[p + 6 * k:p + 6 * k + 6])
        types.append((utoff, isdst, ai))
    p += 6 * typecnt
    chars = data[p:p + charcnt]; p += charcnt + leap * 12 + isstd + isut
    def abbr(ai):
        return chars[ai:chars.index(b"\0", ai)].decode()
    types = [(u, dst, abbr(ai)) for u, dst, ai in types]
    footer = data[p:].strip(b"\n").decode()
    return [(t, *types[i]) for t, i in zip(times, idx)], types, footer


def tz_utoff(p, t):
    """The offset (east of UTC) a parsed TZ string gives at time t."""
    if p["dst"] is None:
        return p["utoff"]
    year = datetime.fromtimestamp(t, timezone.utc).year
    events = sorted([(instant(y, p["start"], p["utoff"]), p["dst_utoff"]) for y in (year - 1, year, year + 1)] +
                    [(instant(y, p["end"], p["dst_utoff"]), p["utoff"]) for y in (year - 1, year, year + 1)])
    utoff = events[0][1]
    for when, after in events:
        if when > t:
            break
        utoff = after
    return utoff


def zone_utoff(transitions, types, t):
    """The offset tzdata's listed transitions give at time t (before the footer takes over)."""
    utoff = types[0][0]
    for when, u, *_ in transitions:
        if when > t:
            break
        utoff = u
    return utoff


def hours_wrong(tz, transitions, types, start, end):
    """The hours between start and end in which tz gives a different offset from tzdata's list."""
    p = parse(tz)
    return sum(tz_utoff(p, t) != zone_utoff(transitions, types, t) for t in range(start, end, 3600))


def fixed_string(transitions, types, start, end):
    """A TZ string for the offset in force longest between start and end."""
    spans = {}
    current = types[0]
    for t, *ty in transitions:
        if t > start:
            break
        current = tuple(ty)
    at = start
    for t, *ty in transitions:
        if t <= start:
            continue
        if t >= end:
            break
        spans[current] = spans.get(current, 0) + t - at
        at, current = t, tuple(ty)
    spans[current] = spans.get(current, 0) + end - at
    utoff, _, abbr = max(spans, key=lambda k: (spans[k], k))
    name = abbr if abbr.isalpha() and len(abbr) >= 3 else f"<{abbr}>"
    return f"{name}{fmt_time(-utoff)}"


# ---- the list -----------------------------------------------------------------------------------

def build_zic(tzdata, tzcode, work):
    """zic built from tzcode, with tzdata unpacked beside it: (source dir, zic)."""
    key = hashlib.sha256(Path(tzdata).read_bytes() + Path(tzcode).read_bytes()).hexdigest()[:16]
    src = Path(work) / f"src-{key}"
    zic = src / "zic"
    if not zic.exists():
        shutil.rmtree(src, ignore_errors=True)
        src.mkdir(parents=True)
        for tar in (tzcode, tzdata):
            with tarfile.open(tar) as t:
                t.extractall(src, filter="data")
        r = subprocess.run(["make", "CC=gcc", "zic"], cwd=src, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not zic.exists():
            raise TzListError(f"building zic failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    return src, zic


def release_date(src):
    m = re.search(r"^Release (\S+) - (\d{4})-(\d\d)-(\d\d)", (src / "NEWS").read_text(encoding="utf-8"), re.M)
    if not m:
        raise TzListError("no release line in tzdata's NEWS")
    return m.group(1), calendar.timegm((int(m.group(2)), int(m.group(3)), int(m.group(4)), 0, 0, 0))


def order(lines):
    """The (name, tz) lines ordered so that a line containing another zone's name, with a different
    TZ, comes after that zone's own line. (Lines with the same TZ may come in either order: GMT's
    line contains "GMT0" and GMT0's contains "GMT", and either one gives the right answer.)"""
    tzs = dict(lines)
    text = {n: f"{n};{tz}" for n, tz in lines}
    names = sorted(text)
    before = {n: set() for n in names}  # before[b]: the names whose lines must come before b's
    for a in names:
        for b in names:
            if a != b and a in text[b] and tzs[a] != tzs[b]:
                before[b].add(a)
    out, done = [], set()
    while len(out) < len(names):
        ready = [n for n in names if n not in done and before[n] <= done]
        if not ready:
            left = [n for n in names if n not in done]
            raise TzListError(f"no order for {left[:10]}: each contains another's name")
        out.append(ready[0])
        done.add(ready[0])
    return [(n, tzs[n]) for n in out]


def lookup(lines, zone):
    """find_timezone()'s answer for a zone from these lines (None for its default)."""
    for n, tz in lines:
        if zone in f"{n};{tz}":
            return tz[:MAX_TZ]
    return None


def generate(tzdata, tzcode, work):
    """(list text, report lines)."""
    src, zic = build_zic(tzdata, tzcode, work)
    version, ref = release_date(src)
    zi = Path(work) / f"zoneinfo-{version}"
    shutil.rmtree(zi, ignore_errors=True)
    r = subprocess.run([str(zic), "-d", str(zi)] + SOURCES, cwd=src, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise TzListError(f"zic failed:\n{r.stderr[-2000:]}")
    first_year = datetime.fromtimestamp(ref, timezone.utc).year
    report = [f"tzdata {version} (released {datetime.fromtimestamp(ref, timezone.utc):%Y-%m-%d}), zic {version}"]
    lines = []
    for path in sorted(p for p in zi.rglob("*") if p.is_file()):
        zone = path.relative_to(zi).as_posix()
        transitions, types, footer = read_tzif(path.read_bytes())
        if not footer:
            raise TzListError(f"{zone}: no TZ string")
        tz = footer
        p = parse(tz)
        if p["dst"] is not None:
            for key, utoff in (("start", p["utoff"]), ("end", p["dst_utoff"])):
                if not uclibc_ok(p[key]):
                    new, err, exact = nearest_rule(p[key], utoff, first_year)
                    tz = tz.replace(fmt_rule(p[key]), fmt_rule(new), 1)
                    report.append(f"{zone}: {fmt_rule(p[key])} written {fmt_rule(new)} (exact in {exact} of {YEARS} years,"
                                  f" {err // 3600} h off in all)")
            parse(tz)
        if transitions and transitions[-1][0] > ref:
            # tzdata lists changes past its release, so the footer doesn't hold yet: keep whichever
            # of it and the year's most common offset is wrong for fewer hours over the next year.
            year = (ref, ref + 365 * 86400)
            fixed = fixed_string(transitions, types, *year)
            wrong = {c: hours_wrong(c, transitions, types, *year) for c in dict.fromkeys((tz, fixed))}
            choice = min(wrong, key=lambda c: (wrong[c], c != tz))
            if wrong[choice]:
                report.append(f"{zone}: tzdata lists changes to {datetime.fromtimestamp(transitions[-1][0], timezone.utc):%Y},"
                              f" no TZ string follows them all: {choice} is wrong for {wrong[choice]} h of the next year"
                              + (f" ({tz}: {wrong[tz]} h)" if choice != tz else f" ({fixed}: {wrong[fixed]} h)"))
            tz = choice
        if len(tz) > MAX_TZ or len(zone) + 1 + len(tz) > MAX_LINE:
            raise TzListError(f"{zone}: {tz!r} too long for the controller")
        lines.append((zone, tz))
    lines = order(lines)
    wrong = [n for n, tz in lines if lookup(lines, n) != tz]
    if wrong:
        raise TzListError(f"the controller would find another zone's line for {wrong[:10]}")
    report.append(f"{len(lines)} zones")
    return "".join(f"{n};{tz}\n" for n, tz in lines), report


def main(argv):
    p = argparse.ArgumentParser(description="The rabbit's /usr/etc/timezone.list from IANA's time-zone database.")
    p.add_argument("tzdata")
    p.add_argument("tzcode")
    p.add_argument("out")
    p.add_argument("--work", default=None)
    args = p.parse_args(argv)
    work = Path(args.work) if args.work else Path(args.out).resolve().parent / "tzlist-work"
    try:
        text, report = generate(args.tzdata, args.tzcode, work)
    except (TzListError, ValueError, OSError) as e:
        print(f"tzlist: {e}", file=sys.stderr)
        return 1
    Path(args.out).write_text(text, encoding="ascii", newline="\n")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
