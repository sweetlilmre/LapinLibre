/* SPDX-License-Identifier: MIT */
/*
 * scheduler-daemon: a tiny alarm clock on D-Bus, com.mindscape.karotz.Scheduler
 * (/com/mindscape/karotz/Scheduler).
 *
 * Clients add named entries of three kinds:
 *   - date entries (add_date_entry): a calendar date and time. Out-of-range
 *     year / month / day / hour fields act as wildcards, which make the entry
 *     repeat yearly / monthly / daily / hourly;
 *   - weekly entries (add_weekly_entry): a weekday and a time of day;
 *   - periodic entries (add_periodic_entry): every `period` seconds, plus a
 *     random 0..delta-1 seconds when delta > 0.
 * When an entry falls due the daemon emits schedule_event(id, more), where
 * `more` is TRUE if the entry has been rescheduled and FALSE if it was a
 * one-shot entry that has now been dropped. A single GLib timeout, armed for
 * the earliest entry, drives everything.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <time.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include "karotz-common.h"
#include "karotz-messages.h"
#include "karotz-marshal.h"

enum {
    SIGNAL_SCHEDULE_EVENT,
    SIGNAL_REGISTERED,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    guint timer_id; /* the pending timeout, 0 if none */
    GSList *entries;
} SchedulerObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} SchedulerObjectClass;

GType scheduler_object_get_type(void);
#define SCHEDULER_TYPE_OBJECT (scheduler_object_get_type())

G_DEFINE_TYPE(SchedulerObject, scheduler_object, G_TYPE_OBJECT)

gboolean scheduler_object_add_date_entry(SchedulerObject *self, const char *id, gint year, gint month, gint day,
                                         gint hour, gint minute, gint second, gboolean *result, GError **error);
gboolean scheduler_object_add_weekly_entry(SchedulerObject *self, const char *id, gint week, gint hour,
                                           gint minute, gint second, gboolean *result, GError **error);
gboolean scheduler_object_add_periodic_entry(SchedulerObject *self, const char *id, gint period, gint delta,
                                             gboolean *result, GError **error);
gboolean scheduler_object_delete_entry(SchedulerObject *self, const char *id, gboolean *result, GError **error);
gboolean scheduler_object_delete_all_entry(SchedulerObject *self, gboolean *result, GError **error);
gboolean scheduler_object_print_task(SchedulerObject *self, GError **error);

#include "scheduler-daemon-glue.h"

enum { ENTRY_DATE, ENTRY_WEEKLY, ENTRY_PERIODIC };

/* Wildcard bits of a date entry: which fields were out of range. */
enum {
    ANY_YEAR = 1 << 0,
    ANY_MONTH = 1 << 1,
    ANY_DAY = 1 << 2,
    ANY_HOUR = 1 << 3,
};

typedef struct {
    int kind;
    time_t next;    /* when the entry falls due next */
    struct tm when; /* date and weekly entries: the local time it is set for */
    int wildcards;  /* date entries */
    int period;     /* periodic entries */
    int delta;      /* periodic entries */
    GString *id;
} Entry;

/*
 * Entries are allocated without being cleared; each kind only ever reads the
 * fields it sets.
 */
static Entry *entry_new(int kind, const char *id, const struct tm *now)
{
    Entry *entry = malloc(sizeof *entry);

    entry->kind = kind;
    entry->when = *now;
    entry->id = g_string_new(id);
    return entry;
}

static Entry *date_entry_new(int year, int month, int day, int hour, int minute, int second, const char *id,
                             const struct tm *now)
{
    Entry *entry = entry_new(ENTRY_DATE, id, now);

    /* A field out of range keeps the current value and becomes a wildcard. */
    entry->wildcards = 0;
    if (year < 1900)
        entry->wildcards |= ANY_YEAR;
    else
        entry->when.tm_year = year - 1900;
    if (month < 1 || month > 12)
        entry->wildcards |= ANY_MONTH;
    else
        entry->when.tm_mon = month - 1;
    if (day < 1 || day > 31)
        entry->wildcards |= ANY_DAY;
    else
        entry->when.tm_mday = day;
    if (hour < 0 || hour > 23)
        entry->wildcards |= ANY_HOUR;
    else
        entry->when.tm_hour = hour;
    /* Minutes and seconds are taken as given; mktime() normalises them. */
    entry->when.tm_min = minute;
    entry->when.tm_sec = second;
    return entry;
}

static Entry *weekly_entry_new(int week, int hour, int minute, int second, const char *id, const struct tm *now)
{
    Entry *entry = entry_new(ENTRY_WEEKLY, id, now);

    entry->when.tm_wday = week; /* not range-checked */
    entry->when.tm_hour = hour;
    entry->when.tm_min = minute;
    entry->when.tm_sec = second;
    return entry;
}

static Entry *periodic_entry_new(int period, int delta, const char *id, const struct tm *now)
{
    Entry *entry = entry_new(ENTRY_PERIODIC, id, now);

    entry->period = period;
    entry->delta = delta;
    return entry;
}

static void entry_free(Entry *entry)
{
    g_string_free(entry->id, TRUE);
    free(entry);
}

/*
 * mktime(), but keeping the wall-clock hour and minute asked for when a DST
 * change would shift them.
 */
static time_t mktime_keep_clock(struct tm *tm)
{
    int hour = tm->tm_hour;
    int minute = tm->tm_min;
    time_t t = mktime(tm);

    if (tm->tm_hour != hour || tm->tm_min != minute) {
        tm->tm_hour = hour;
        tm->tm_min = minute;
        t = mktime(tm);
    }
    return t;
}

static time_t random_extra(int delta)
{
    return delta > 0 ? rand() % delta : 0;
}

/* Move a due entry to its next occurrence; -1 if it does not repeat. */
static int entry_advance(Entry *entry)
{
    switch (entry->kind) {
    case ENTRY_WEEKLY:
        entry->when.tm_mday += 7;
        entry->next = mktime_keep_clock(&entry->when);
        return 0;

    case ENTRY_PERIODIC:
        /* From the previous due time, not from now. */
        entry->next += entry->period;
        entry->next += random_extra(entry->delta);
        return 0;

    case ENTRY_DATE:
        /* The finest wildcard sets the repeat interval. */
        if (entry->wildcards == 0)
            return -1;
        if (entry->wildcards & ANY_HOUR) {
            /* `when` moves with the timestamp (nothing reads it again for an
             * hourly entry, so this only keeps the two consistent). */
            entry->next += 3600;
            localtime_r(&entry->next, &entry->when);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "hourly entry: next run one hour on");
            return 0;
        }
        if (entry->wildcards & ANY_DAY) {
            entry->when.tm_mday++;
            g_log(NULL, G_LOG_LEVEL_DEBUG, "daily entry: moved to the next day");
        } else if (entry->wildcards & ANY_MONTH) {
            entry->when.tm_mon++;
            g_log(NULL, G_LOG_LEVEL_DEBUG, "monthly entry: moved to the next month");
        } else {
            entry->when.tm_year++;
            g_log(NULL, G_LOG_LEVEL_DEBUG, "yearly entry: moved to the next year");
        }
        entry->next = mktime_keep_clock(&entry->when);
        return 0;

    default:
        return -1;
    }
}

/* Work out when a new entry first falls due; -1 if it never will. */
static int entry_start(Entry *entry, const struct tm *now_tm, time_t now)
{
    int days;

    switch (entry->kind) {
    case ENTRY_WEEKLY:
        /*
         * Days until the requested weekday; today counts unless the time of
         * day has already passed (to the second: a time equal to now is due
         * immediately).
         */
        days = entry->when.tm_wday - now_tm->tm_wday;
        if (days < 0)
            days += 7;
        if (days == 0) {
            if (entry->when.tm_hour < now_tm->tm_hour)
                days = 7;
            else if (entry->when.tm_hour == now_tm->tm_hour) {
                if (entry->when.tm_min < now_tm->tm_min)
                    days = 7;
                else if (entry->when.tm_min == now_tm->tm_min && entry->when.tm_sec < now_tm->tm_sec)
                    days = 7;
            }
        }
        entry->when.tm_mday += days;
        entry->next = mktime_keep_clock(&entry->when);
        return 0;

    case ENTRY_PERIODIC:
        entry->next = now + entry->period;
        entry->next += random_extra(entry->delta);
        return 0;

    case ENTRY_DATE:
        /* Plain mktime() here, unlike everywhere else. */
        entry->next = mktime(&entry->when);
        if (entry->next < now) {
            /* In the past: a repeating date gets one step forward. */
            if (entry_advance(entry) != 0)
                return -1;
            if (entry->next < now)
                return -1;
        }
        return 0;

    default:
        return -1;
    }
}

/* The earliest due time, or 0 when there are no entries. */
static time_t earliest(GSList *entries)
{
    time_t first;

    if (entries == NULL)
        return 0;
    first = ((Entry *)entries->data)->next;
    for (entries = entries->next; entries != NULL; entries = entries->next)
        if (((Entry *)entries->data)->next < first)
            first = ((Entry *)entries->data)->next;
    return first;
}

static gboolean timer_expired(gpointer data);

/* Re-arm the timeout for the earliest entry. */
static void reschedule(SchedulerObject *self, time_t now)
{
    time_t delay;

    if (self->timer_id != 0) {
        g_source_remove(self->timer_id);
        self->timer_id = 0;
    }
    delay = earliest(self->entries);
    /* Quirk: an entry due at exactly the epoch counts as no entry. */
    if (delay != 0) {
        delay -= now;
        if (delay < 0)
            delay = 0;
        self->timer_id = g_timeout_add_seconds((guint)delay, timer_expired, self);
    }
}

/* Fire every due entry, reschedule or drop it, then re-arm. */
static gboolean timer_expired(gpointer data)
{
    SchedulerObject *self = data;
    SchedulerObjectClass *klass = G_TYPE_INSTANCE_GET_CLASS(self, SCHEDULER_TYPE_OBJECT, SchedulerObjectClass);
    time_t now;
    GSList *node, *next;

    time(&now);
    for (node = self->entries; node != NULL; node = next) {
        Entry *entry = node->data;
        gboolean drop = FALSE;

        if (entry->next <= now) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "DO %s\n", entry->id->str);
            if (entry_advance(entry) == 0) {
                g_signal_emit(self, klass->signals[SIGNAL_SCHEDULE_EVENT], 0, entry->id->str, TRUE);
            } else {
                g_signal_emit(self, klass->signals[SIGNAL_SCHEDULE_EVENT], 0, entry->id->str, FALSE);
                entry_free(entry);
                drop = TRUE;
            }
        }
        next = node->next;
        if (drop)
            self->entries = g_slist_delete_link(self->entries, node);
    }
    /* This source is removed by returning FALSE; reschedule() arms the next. */
    self->timer_id = 0;
    reschedule(self, now);
    return FALSE;
}

/*
 * Common tail of the add_* methods. An entry that cannot start is reported
 * as result FALSE in a normal reply, logged, and freed.
 */
static gboolean add_entry(SchedulerObject *self, Entry *entry, const struct tm *now_tm, time_t now,
                          gboolean *result)
{
    if (entry_start(entry, now_tm, now) != 0) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "add entry %s: its time cannot be scheduled (a date in the past?)",
              entry->id->str);
        entry_free(entry);
        *result = FALSE;
        return TRUE;
    }
    self->entries = g_slist_prepend(self->entries, entry);
    reschedule(self, now);
    *result = TRUE;
    return TRUE;
}

gboolean scheduler_object_add_date_entry(SchedulerObject *self, const char *id, gint year, gint month, gint day,
                                         gint hour, gint minute, gint second, gboolean *result, GError **error)
{
    time_t now;
    struct tm *now_tm;

    (void)error;
    time(&now);
    now_tm = localtime(&now);
    return add_entry(self, date_entry_new(year, month, day, hour, minute, second, id, now_tm), now_tm, now,
                     result);
}

gboolean scheduler_object_add_weekly_entry(SchedulerObject *self, const char *id, gint week, gint hour,
                                           gint minute, gint second, gboolean *result, GError **error)
{
    time_t now;
    struct tm *now_tm;

    (void)error;
    time(&now);
    now_tm = localtime(&now);
    return add_entry(self, weekly_entry_new(week, hour, minute, second, id, now_tm), now_tm, now, result);
}

gboolean scheduler_object_add_periodic_entry(SchedulerObject *self, const char *id, gint period, gint delta,
                                             gboolean *result, GError **error)
{
    time_t now;
    struct tm *now_tm;

    (void)error;
    time(&now);
    now_tm = localtime(&now);
    return add_entry(self, periodic_entry_new(period, delta, id, now_tm), now_tm, now, result);
}

static gint entry_has_id(gconstpointer entry, gconstpointer id)
{
    return strcmp(((const Entry *)entry)->id->str, id);
}

/*
 * Removes the most recently added entry with this id. The timeout is not
 * re-armed, so it still fires once for a removed entry: harmless (it finds
 * nothing due and re-arms for what is left).
 */
gboolean scheduler_object_delete_entry(SchedulerObject *self, const char *id, gboolean *result, GError **error)
{
    GSList *node;

    (void)error;
    node = g_slist_find_custom(self->entries, id, entry_has_id);
    if (node == NULL) {
        /* A normal reply with result FALSE, logged. */
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "delete entry %s: no such entry", id);
        *result = FALSE;
        return TRUE;
    }
    entry_free(node->data);
    self->entries = g_slist_delete_link(self->entries, node);
    *result = TRUE;
    return TRUE;
}

/* As with delete_entry, the timeout is left armed: harmless. */
gboolean scheduler_object_delete_all_entry(SchedulerObject *self, gboolean *result, GError **error)
{
    GSList *node;

    (void)error;
    for (node = self->entries; node != NULL; node = node->next)
        entry_free(node->data);
    g_slist_free(self->entries);
    self->entries = NULL;
    *result = TRUE;
    return TRUE;
}

static void print_entry(gpointer data, gpointer user_data)
{
    Entry *entry = data;
    time_t now;

    (void)user_data;
    time(&now);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Task: %s due in %d s\n", entry->id->str, (int)(entry->next - now));
}

/* Debug dump to the log: the current time, then seconds until each entry. */
gboolean scheduler_object_print_task(SchedulerObject *self, GError **error)
{
    time_t now;

    (void)error;
    time(&now);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "now %d\n", (int)now);
    g_slist_foreach(self->entries, print_entry, NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "end of the task list");
    return TRUE;
}

static void scheduler_object_class_init(SchedulerObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    /* schedule_event(id, rescheduled). */
    klass->signals[SIGNAL_SCHEDULE_EVENT] = g_signal_new("schedule_event", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                         karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, 2, G_TYPE_STRING, G_TYPE_INT);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    g_return_if_fail(klass != NULL);
}

static void scheduler_object_init(SchedulerObject *self)
{
    self->timer_id = 0;
    self->entries = NULL;
    dbus_g_object_type_install_info(SCHEDULER_TYPE_OBJECT, &dbus_glib_scheduler_object_object_info);
    g_return_if_fail(self != NULL);
}

int main(void)
{
    int log_priority = LOG_NOTICE;
    GError *error = NULL;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    GMainLoop *loop;
    SchedulerObject *scheduler;
    SchedulerObjectClass *klass;
    guint request_result;

    init_karotz_log("Scheduler", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");

    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL)
        die("Failed to open connection to bus", error);
    else
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Successfully connected to %s bus\n", "SYSTEM");

    scheduler = g_object_new(SCHEDULER_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Scheduler", G_OBJECT(scheduler));

    bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, "com.mindscape.karotz.Scheduler", 0, &request_result, &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }

    klass = G_TYPE_INSTANCE_GET_CLASS(scheduler, SCHEDULER_TYPE_OBJECT, SchedulerObjectClass);
    g_signal_emit(scheduler, klass->signals[SIGNAL_REGISTERED], 0);
    g_main_loop_run(loop);
    return 0;
}
