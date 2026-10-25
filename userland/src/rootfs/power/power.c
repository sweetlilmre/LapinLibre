/* SPDX-License-Identifier: MIT */
/*
 * power: reboot the rabbit when the power button driver reports '0'.
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void)
{
    char state = '1';  /* "not pressed" until a byte says otherwise */
    FILE *f = fopen("/dev/rabbit_power", "r");

    /* A failed open is reported, then tried again every second until the
     * device appears (inittab respawns power, so exiting would spin
     * instead). */
    if (f == NULL) {
        puts("can't open rabbit_power file");
        fflush(stdout);
        while ((f = fopen("/dev/rabbit_power", "r")) == NULL)
            sleep(1);
    }

    for (;;) {
        do {
            sleep(1);
            /* Only a byte actually read counts: a failed read keeps the
             * last state, "not pressed" before the first. */
            if (fread(&state, 1, 1, f) != 1)
                clearerr(f);
        } while (state != '0');
        if (system("reboot") != 0)
            fputs("power: reboot failed\n", stderr);
    }
}
