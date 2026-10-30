/* SPDX-License-Identifier: MIT */
/*
 * immortaldog: daemonize, write a pid file and keep a program running,
 * relaunching it whenever it exits (at most once every 5 seconds). The
 * arguments after the program are passed to it.
 *
 *   immortaldog <path_to_pidfile> <path_to_executable> [<argument>...]
 *
 * SIGTERM stops the child and removes the pid file.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

static pid_t child_pid;
static const char *pid_file_name;

static void usage(const char *prog)
{
    fprintf(stderr, "Usage: %s <path_to_pidfile> <path_to_executable> [<argument>...]\n", prog);
}

static void forkAndExit(void)
{
    pid_t pid = fork();

    if (pid < 0) {
        syslog(LOG_WARNING, "Error while forking immortaldog daemon");
        exit(-1);
    }
    if (pid > 0)
        exit(0);
    umask(0);
    if (setsid() < 0) {
        syslog(LOG_WARNING, "Error while setsid");
        exit(-1);
    }
    if (chdir("/") < 0) {
        syslog(LOG_WARNING, "Error while chdir");
        exit(-1);
    }
    close(STDIN_FILENO);
}

static void sigterm_handler(int sig)
{
    (void)sig;
    if (child_pid == 0)
        exit(0);
    syslog(LOG_INFO, "received sigterm\n");
    syslog(LOG_DEBUG, "sending signal term to pid %d\n", child_pid);
    kill(child_pid, SIGTERM);
    if (pid_file_name != NULL)
        remove(pid_file_name);
    exit(0);
}

static void install_kill_handler(void)
{
    signal(SIGTERM, sigterm_handler);
}

/* Returns 0 on success, 1 if the file can't be opened, 2 if it can't be written. */
static int write_my_pid(const char *path)
{
    struct stat st;
    pid_t pid = getpid();
    FILE *f;

    if (stat(path, &st) == 0) {
        syslog(LOG_WARNING, "daemon seems running, %s already exists (remove file if needed)\n", path);
        exit(0);
    }
    f = fopen(path, "w");
    if (f == NULL) {
        syslog(LOG_WARNING, "Could not open %s for writing: %s\n", path, strerror(errno));
        return 1;
    }
    if (fprintf(f, "%d", pid) < 0) {
        syslog(LOG_WARNING, "Could not print pid in %s: %s\n", path, strerror(errno));
        return 2;
    }
    fclose(f);
    return 0;
}

/* Seconds on the monotonic clock: a change of the wall clock (ntpd setting
 * it) doesn't change the time between two starts. */
static time_t now_monotonic(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec;
}

static void wait_before_run(time_t started)
{
    int elapsed = now_monotonic() - started;

    syslog(LOG_DEBUG, "elapsed: %d\n", elapsed);
    if (elapsed < 5)
        sleep(5 - elapsed);
}

static void launch_immortal(const char *path, char **argv)
{
    int status = 0;
    time_t started;
    sigset_t term, saved;

    sigemptyset(&term);
    sigaddset(&term, SIGTERM);
    for (;;) {
        started = now_monotonic();
        /* SIGTERM waits until child_pid is set: the handler needs it to stop
         * the child and remove the pid file, and a child whose exec fails
         * sends SIGTERM at once. */
        sigprocmask(SIG_BLOCK, &term, &saved);
        child_pid = fork();
        if (child_pid == 0) {
            sigprocmask(SIG_SETMASK, &saved, NULL); /* the program gets the usual mask */
            execvp(path, argv);
            break; /* in the child: exec failed */
        }
        sigprocmask(SIG_SETMASK, &saved, NULL);
        if (child_pid < 0) {
            syslog(LOG_WARNING, "cannot fork: %s\n", strerror(errno));
            exit(-1);
        }
        wait(&status);
        if (WIFEXITED(status))
            syslog(LOG_INFO, "end process: %d\n", child_pid);
        wait_before_run(started);
        syslog(LOG_INFO, "will relaunch: %s\n", path);
    }

    /* In the child, whose exec failed: it SIGTERMs the parent, which then
     * stops and removes its pid file. A fork(), not a vfork(): the child has
     * memory of its own, so it may log and signal, and its child_pid (0)
     * isn't the parent's. */
    syslog(LOG_WARNING, "cannot run %s: %s\n", path, strerror(errno));
    kill(getppid(), SIGTERM);
    _exit(-1);
}

int main(int argc, char **argv)
{
    openlog("ImmortalDog", LOG_PID | LOG_CONS | LOG_NDELAY, LOG_LOCAL1);
    child_pid = 0;

    if (argc < 3) {
        usage(argv[0]);
        syslog(LOG_WARNING, "bad commandline, need at least 3 args");
        return 1;
    }

    forkAndExit();
    install_kill_handler();
    if (write_my_pid(argv[1]) != 0)
        return 1;
    pid_file_name = argv[1];
    launch_immortal(argv[2], argv + 2);
    closelog();
    return 0;
}
