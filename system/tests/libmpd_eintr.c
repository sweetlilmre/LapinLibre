/* SPDX-License-Identifier: MIT */
/*
 * system/tests/libmpd-eintr.sh's client: a status request to an MPD that takes
 * 1.5 s to answer, while a timer signal (installed without SA_RESTART, as a SIGCHLD handler
 * is) arrives every 0.2 s. libmpd must retry select() on EINTR and get the status; before the
 * fix it took the signal for a timeout. Prints the outcome and exits 0 on success.
 *
 *   libmpd_eintr PORT
 */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <libmpd/libmpdclient.h>

static volatile int signals;

static void on_alarm(int sig)
{
    (void)sig;
    signals++;
}

int main(int argc, char **argv)
{
    struct sigaction sa;
    struct itimerval every = { { 0, 200000 }, { 0, 200000 } };
    mpd_Connection *c;
    mpd_Status *status;

    if (argc != 2)
        return 2;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_alarm;  /* no SA_RESTART: select() returns EINTR */
    sigaction(SIGALRM, &sa, NULL);
    c = mpd_newConnection("127.0.0.1", atoi(argv[1]), 5.0);
    if (c->error) {
        printf("connect: error %d: %s\n", c->error, c->errorStr);
        return 1;
    }
    setitimer(ITIMER_REAL, &every, NULL);
    mpd_sendStatusCommand(c);
    status = mpd_getStatus(c);
    setitimer(ITIMER_REAL, &(struct itimerval){ { 0, 0 }, { 0, 0 } }, NULL);
    printf("signals during the request: %d\n", signals);
    if (status == NULL || c->error) {
        printf("status: error %d: %s\n", c->error, c->errorStr);
        return 1;
    }
    printf("status: volume %d\n", status->volume);
    mpd_freeStatus(status);
    mpd_closeConnection(c);
    return 0;
}
