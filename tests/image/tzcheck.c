/* SPDX-License-Identifier: MIT */
/*
 * tzcheck: local times under a C library's TZ handling (for test_tzlist.py, built for the rabbit
 * and run under qemu on the image's sysroot).
 *
 *   tzcheck < LINES
 *
 * Each input line is "NAME<TAB>TZ<TAB>SECONDS SECONDS ..." (seconds since the epoch); each output
 * line is "NAME SECONDS YYYY-MM-DD HH:MM:SS DST", the local time with that TZ, as the controller's
 * /etc/TZ gives it to every program.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

int main(void)
{
    static char line[65536];

    while (fgets(line, sizeof line, stdin) != NULL) {
        char *name = strtok(line, "\t\n");
        char *tz = strtok(NULL, "\t\n");
        char *times = strtok(NULL, "\t\n");
        if (name == NULL || tz == NULL || times == NULL)
            continue;
        setenv("TZ", tz, 1);
        tzset();
        for (char *t = strtok(times, " "); t != NULL; t = strtok(NULL, " ")) {
            time_t when = (time_t)strtoll(t, NULL, 10);
            struct tm tm;
            if (localtime_r(&when, &tm) == NULL) {
                printf("%s %s error\n", name, t);
                continue;
            }
            printf("%s %s %04d-%02d-%02d %02d:%02d:%02d %d\n", name, t, tm.tm_year + 1900, tm.tm_mon + 1,
                   tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, tm.tm_isdst > 0);
        }
    }
    return 0;
}
