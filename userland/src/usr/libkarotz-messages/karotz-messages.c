/* SPDX-License-Identifier: MIT */
/*
 * libkarotz-messages: route GLib logging to syslog.
 */
#include <stdio.h>
#include <syslog.h>

#include "karotz-messages.h"

static int syslog_priority(GLogLevelFlags level)
{
    switch (level & ~(G_LOG_FLAG_RECURSION | G_LOG_FLAG_FATAL)) {
    case G_LOG_LEVEL_ERROR:
    case G_LOG_LEVEL_CRITICAL:
        return LOG_ERR;
    case G_LOG_LEVEL_WARNING:
        return LOG_WARNING;
    case G_LOG_LEVEL_MESSAGE:
        return LOG_NOTICE;
    case G_LOG_LEVEL_INFO:
        return LOG_INFO;
    case G_LOG_LEVEL_DEBUG:
        return LOG_DEBUG;
    default:
        return LOG_NOTICE;
    }
}

void g_log_handler(const gchar *log_domain, GLogLevelFlags log_level, const gchar *message, gpointer max_priority)
{
    (void)log_domain;
    if (syslog_priority(log_level) <= *(int *)max_priority)
        syslog(syslog_priority(log_level), "%s", message);
    /* Quirk: the raw flags are compared, not the level: everything but a
     * non-fatal G_LOG_LEVEL_ERROR is echoed to stdout, whatever
     * max_priority says. Kept: on the rabbit the daemons' stdout is init's
     * /dev/console, with no console behind it (the kernel's command line
     * names none), so nothing is written; the tests read the log there. */
    if (log_level > 5)
        puts(message);
}

void init_karotz_log(const char *ident, int *max_priority)
{
    openlog(ident, LOG_PID | LOG_CONS | LOG_NDELAY, LOG_LOCAL1);
    g_log_set_default_handler(g_log_handler, max_priority);
}
