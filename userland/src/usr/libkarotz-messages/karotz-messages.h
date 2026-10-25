/* SPDX-License-Identifier: MIT */
/* libkarotz-messages: route GLib logging to syslog (facility LOCAL1). */
#ifndef KAROTZ_MESSAGES_H
#define KAROTZ_MESSAGES_H

#include <glib.h>

/*
 * Open syslog as `ident` and install the default GLib log handler. Messages
 * whose syslog priority is at most *max_priority go to syslog; `max_priority`
 * must stay valid for the life of the process.
 */
void init_karotz_log(const char *ident, int *max_priority);

void g_log_handler(const gchar *log_domain, GLogLevelFlags log_level, const gchar *message, gpointer max_priority);

#endif
