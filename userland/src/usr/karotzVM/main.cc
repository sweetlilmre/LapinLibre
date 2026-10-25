// SPDX-License-Identifier: MIT
/*
 * karotzVM's main program: reads application launches from standard input,
 * one per line, and runs each in a forked child (at most three at a time).
 * Logs go to syslog (LOG_LOCAL1) and, up to the --debug level, to stderr.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <syslog.h>
#include <unistd.h>

#include <libxml/parser.h>

#include "karotzvm.h"

/* Children running; a fourth launch is refused. */
#define MAX_CHILDREN 3

/* V8 heap limits: 2 MB young generation, 10 MB old generation. */
#define V8_MAX_YOUNG_SPACE (2 * 1024 * 1024)
#define V8_MAX_OLD_SPACE (10 * 1024 * 1024)
#define COMMAND_LINE_SIZE 4224 /* the buffer; fgets is given LINE_READ_MAX */
#define LINE_READ_MAX 0xfff

static volatile sig_atomic_t children;  /* changed by child_ended() */

/* SIGCHLD: reaps every ended child and counts each one down. Signals don't
 * queue, so children ending together may give a single SIGCHLD: the loop
 * reaps them all. Nothing is logged here (logging isn't safe in a signal
 * handler); the count is logged at the next launch ("nbChild"). waitpid()
 * can change errno, which the code it interrupted may be about to read, so
 * errno is put back. (Not a GLib child watch, as the controller and
 * multimedia-daemon have: this process has no main loop, it waits in fgets()
 * for the next launch, so a watch would never be dispatched.) */
static void child_ended(int)
{
    int status;
    int saved_errno = errno;

    while (waitpid(-1, &status, WNOHANG) > 0)
        children--;
    errno = saved_errno;
}

/* The rest of arg after prefix, or NULL. */
static const char *option_value(const char *arg, const char *prefix)
{
    size_t length = strlen(prefix);

    return strncmp(arg, prefix, length) == 0 ? arg + length : NULL;
}

/* argv[0] is examined too. */
static void parse_options(int argc, char **argv, Options *options)
{
    for (int i = 0; i < argc; i++) {
        const char *value = option_value(argv[i], "--debug=");
        if (value != NULL) {
            options->debug = atoi(value);
        } else if ((value = option_value(argv[i], "--app_folder=")) != NULL) {
            options->app_folder = value;
        }
    }
}

/*
 * Every message goes to syslog; to stderr as "LEVEL: message" when
 * its level is at most 1 << debug (the default 7 lets everything through).
 * Error and critical messages go to syslog as LOG_ERR, as the daemons'
 * (libkarotz-messages) do.
 */
static void log_handler(const gchar *, GLogLevelFlags level, const gchar *message, gpointer data)
{
    const Options *options = static_cast<const Options *>(data);
    int priority = LOG_DEBUG;
    const char *name = "";

    switch (level & G_LOG_LEVEL_MASK) {  /* without the fatal/recursion flags */
    case G_LOG_LEVEL_ERROR:
        priority = LOG_ERR;
        name = "ERROR";
        break;
    case G_LOG_LEVEL_CRITICAL:
        priority = LOG_ERR;
        name = "CRITICAL";
        break;
    case G_LOG_LEVEL_WARNING:
        priority = LOG_WARNING;
        name = "WARNING";
        break;
    case G_LOG_LEVEL_MESSAGE:
        priority = LOG_NOTICE;
        name = "MESSAGE";
        break;
    case G_LOG_LEVEL_INFO:
        priority = LOG_INFO;
        name = "INFO";
        break;
    case G_LOG_LEVEL_DEBUG:
        priority = LOG_DEBUG;
        name = "DEBUG";
        break;
    }
    if ((int)level <= 1 << (options->debug & 0xff))
        fprintf(stderr, "%s: %s\n", name, message);
    syslog(priority, "%s", message);
}

/*
 * Each line read is "<app> [<launch type> [<instance>]]"; a line
 * shorter than two characters (or end of file before any line) ends the
 * program, and so does the end of input. A launch refused for want of a
 * place is not queued; places are given back as children end (SIGCHLD).
 */
int main(int argc, char **argv)
{
    static char line[COMMAND_LINE_SIZE];
    Options options;

    options.debug = 7;
    options.app_folder = "";
    signal(SIGCHLD, child_ended);
    parse_options(argc, argv, &options);
    xmlCheckVersion(LIBXML_VERSION);
    openlog("KarotzVM", LOG_PID | LOG_CONS | LOG_NDELAY, LOG_LOCAL1);
    g_log_set_default_handler(log_handler, &options);
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "KarotzVM started...");
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "app folder: %s", options.app_folder);
    v8::ResourceConstraints constraints;
    constraints.set_max_young_space_size(V8_MAX_YOUNG_SPACE);
    constraints.set_max_old_space_size(V8_MAX_OLD_SPACE);
    v8::SetResourceConstraints(&constraints);

    for (;;) {
        KarotzData karotz;
        if (fgets(line, LINE_READ_MAX, stdin) == NULL) {
            /* End of input (the controller's pipe closed) ends the
             * program; the running applications carry on. */
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "KarotzVM: end of input -> break");
            break;
        }
        size_t length = strlen(line);
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "KarotzVM buffer:%s: len:%d", line, (int)length);
        if ((int)length < 2) {
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "KarotzVM buffer: empty, stopping");
            break;
        }
        if (line[length - 1] == '\n')
            line[length - 1] = '\0';
        karotz.options = &options;
        karotz.set_cmdline(line);
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "nbChild %d", children);
        if (children < MAX_CHILDREN) {
            children++;
            /* Without this a child inherits what the parent has
             * buffered for stdout (any pending output) and prints it
             * again when it exits normally. */
            fflush(stdout);
            pid_t pid = fork();
            if (pid == 0) {
                app_run(&karotz);
                break;
            }
            if (pid < 0) {
                /* No child: its place is given back at once (no SIGCHLD will). */
                children--;
                g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "can't start the application: fork: %s", strerror(errno));
            }
        } else {
            g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "too many kvm ignore request");
        }
    }
    closelog();
    return 0;
}
