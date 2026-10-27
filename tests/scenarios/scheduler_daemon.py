# SPDX-License-Identifier: MIT
"""scheduler-daemon: named date / weekly / periodic entries fire schedule_event(id, rescheduled).

The daemon's timeouts have one-second granularity (g_timeout_add_seconds).
Each step that expects a fire waits for that signal itself (a slow run under
load only takes longer), then acts at once, before the entry could fire
again; only the order of signals is recorded, never times. The dates are
taken on one UTC day: a run that would cross midnight waits until it's past.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import harness

DAEMON = "scheduler-daemon"
DEVICES = []
SERVICE = "com.mindscape.karotz.Scheduler"
PATH = "/com/mindscape/karotz/Scheduler"
IFACE = "com.mindscape.karotz.KarotzInterface"

# A delete of an unknown id, or an add that cannot be scheduled, replies
# normally with result FALSE (and the daemon logs why).

# The harness runs the daemon with TZ=UTC0 (the sysroot's /etc/TZ is
# empty); compute the dates below in the same zone.
os.environ["TZ"] = "UTC0"

# The periodic entries' period (s). A periodic entry's next time counts from its previous due time,
# so a step of the wall clock (WSL corrects its clock by seconds at a time) can bring it due again
# at once; with this period, a step must be about as long to fire one twice before the delete.
PERIOD = 10


def call(bus, method, *args, timed=False):
    """Call a method; return its reply without the header that carries times and serials.

    For calls with clock-derived arguments (timed=True) only the id is recorded.
    """
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    lines = out.splitlines()
    if lines and lines[0].startswith("method return"):
        lines = lines[1:]
    shown = args[0] + " ..." if timed else " ".join(args)
    return f"{method}({shown}) -> {' | '.join(l.strip() for l in lines)}"


def wait_for(bus, ident, count=1, timeout=30):
    """Wait until schedule_event for ident has been seen count times (or the timeout)."""
    needle = f'schedule_event(string "{ident}"'
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(needle in line for line in bus.signals()) >= count:
            return
        time.sleep(0.1)


def clear_of_midnight():
    """Wait, if needed, so the run's dates (a minute back to a minute ahead) stay on one UTC day."""
    now = datetime.now(timezone.utc)
    since = now.hour * 3600 + now.minute * 60 + now.second
    if since < 70 or since > 86400 - 120:
        time.sleep((86400 + 70 - since) % 86400)


def utc_in(seconds):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def date_args(t, year=None, month=None, day=None, hour=None):
    """add_date_entry fields for time t, with any of year/month/day/hour overridden (wildcards)."""
    return [f"int32:{t.year if year is None else year}", f"int32:{t.month if month is None else month}",
            f"int32:{t.day if day is None else day}", f"int32:{t.hour if hour is None else hour}",
            f"int32:{t.minute}", f"int32:{t.second}"]


def drive(d, bus):
    clear_of_midnight()
    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    record.append("== introspection")
    record.extend(harness.introspection(bus, SERVICE, PATH))
    record.append("== calls")

    # Periodic, every PERIOD s: deleted after its first fire, before the next.
    record.append(call(bus, "add_periodic_entry", "string:p1", f"int32:{PERIOD}", "int32:0"))
    wait_for(bus, "p1")
    record.append(call(bus, "delete_entry", "string:p1"))
    time.sleep(PERIOD)  # the stale timeout fires on an empty list: nothing emitted
    record.append(call(bus, "delete_entry", "string:p1"))  # unknown now

    # delta 1: rand() % 1 is always 0, so this is deterministic (larger deltas
    # would depend on the C library's rand()).
    record.append(call(bus, "add_periodic_entry", "string:p2", f"int32:{PERIOD}", "int32:1"))
    wait_for(bus, "p2")
    record.append(call(bus, "delete_entry", "string:p2"))

    # Duplicate ids: deletion takes the most recent first.
    record.append(call(bus, "add_periodic_entry", "string:dup", "int32:1000", "int32:0"))
    record.append(call(bus, "add_periodic_entry", "string:dup", "int32:2000", "int32:0"))
    record.append(call(bus, "print_task"))
    record.append(call(bus, "delete_entry", "string:dup"))
    record.append(call(bus, "delete_entry", "string:dup"))
    record.append(call(bus, "delete_entry", "string:dup"))
    record.append(call(bus, "delete_entry", "string:never-added"))

    # Dates: in the past (refused), far future (accepted, silent), one-shot soon.
    past = utc_in(-3600)
    recent = utc_in(-60)
    record.append(call(bus, "add_date_entry", "string:past", *date_args(past), timed=True))
    record.append(call(bus, "add_date_entry", "string:future", *date_args(utc_in(400 * 86400)), timed=True))
    record.append(call(bus, "add_date_entry", "string:once", *date_args(utc_in(4)), timed=True))
    wait_for(bus, "once")  # fires once with rescheduled = 0 and is dropped
    time.sleep(0.5)
    record.append(call(bus, "delete_entry", "string:once"))

    # Wildcards in the past: each steps one hour / day / month / year forward.
    record.append(call(bus, "add_date_entry", "string:past-h", *date_args(recent, hour=-1), timed=True))
    record.append(call(bus, "add_date_entry", "string:past-d", *date_args(utc_in(-60), day=0), timed=True))
    record.append(call(bus, "add_date_entry", "string:past-m", *date_args(utc_in(-60), month=13), timed=True))
    record.append(call(bus, "add_date_entry", "string:past-y", *date_args(utc_in(-60), year=0), timed=True))

    # Wildcards due soon, all at the same instant, so they fire from one
    # timeout in list order (newest first) and are each rescheduled.
    soon = utc_in(5)
    record.append(call(bus, "add_date_entry", "string:any-hour", *date_args(soon, year=0, month=0, day=0, hour=-1),
                       timed=True))
    record.append(call(bus, "add_date_entry", "string:any-day", *date_args(soon, day=32), timed=True))
    record.append(call(bus, "add_date_entry", "string:any-month", *date_args(soon, month=0), timed=True))
    record.append(call(bus, "add_date_entry", "string:any-year", *date_args(soon, year=1899), timed=True))
    wait_for(bus, "any-hour")  # the last of the four

    # Weekly: today a few seconds ahead fires; an invalid weekday is accepted.
    soon = utc_in(4)
    wday = (soon.weekday() + 1) % 7  # struct tm counts from Sunday
    record.append(call(bus, "add_weekly_entry", "string:weekly", f"int32:{wday}", f"int32:{soon.hour}",
                       f"int32:{soon.minute}", f"int32:{soon.second}", timed=True))
    earlier = utc_in(-60)
    record.append(call(bus, "add_weekly_entry", "string:next-week", f"int32:{(earlier.weekday() + 1) % 7}",
                       f"int32:{earlier.hour}", f"int32:{earlier.minute}", f"int32:{earlier.second}", timed=True))
    record.append(call(bus, "add_weekly_entry", "string:bad-day", "int32:9", "int32:12", "int32:0", "int32:0"))
    wait_for(bus, "weekly")

    # A periodic entry still running when everything is deleted stops firing.
    record.append(call(bus, "add_periodic_entry", "string:p3", f"int32:{PERIOD}", "int32:0"))
    wait_for(bus, "p3")
    record.append(call(bus, "print_task"))
    record.append(call(bus, "delete_all_entry"))
    time.sleep(PERIOD)
    record.append(call(bus, "delete_all_entry"))
    record.append(call(bus, "delete_entry", "string:weekly"))
    return record
