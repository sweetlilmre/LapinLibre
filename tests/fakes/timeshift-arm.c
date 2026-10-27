/* SPDX-License-Identifier: MIT */
/*
 * timeshift-arm: LD_PRELOAD shim for the aggressive tests (tests/aggressive),
 * for karotzVM under qemu.
 *
 * time() returns the real time plus a number of seconds read, on every call,
 * from the file named by $KAROTZ_TIMESHIFT_FILE (no file, or no variable: no
 * shift; a leading "-" moves it back). The test's fake controller writes the
 * file to age karotzVM's reply handlers past their ten hours without waiting
 * for them; karotzvm_https moves the clock back to 1970, as the rabbit's is at boot.
 *
 * The rabbit's libc is uClibc, so this is freestanding: build with
 *   arm-buildroot-linux-uclibcgnueabi-gcc (the system toolchain's) -march=armv4t -marm -O2 -nostdlib -shared -fPIC -Wl,--hash-style=sysv
 */

extern char **environ; /* resolved from the program's libc */

static long syscall3(long number, long a, long b, long c)
{
    register long r0 __asm__("r0") = a;
    register long r1 __asm__("r1") = b;
    register long r2 __asm__("r2") = c;
    register long r7 __asm__("r7") = number;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory");
    return r0;
}

static const char *shift_file(void)
{
    static const char name[] = "KAROTZ_TIMESHIFT_FILE=";
    char **e;

    for (e = environ; e != 0 && *e != 0; e++) {
        const char *s = *e;
        unsigned i = 0;

        while (name[i] && s[i] == name[i])
            i++;
        if (name[i] == '\0')
            return s + i;
    }
    return 0;
}

static long shift(void)
{
    const char *path = shift_file();
    char text[24];
    long fd, n, i = 0, v = 0, sign = 1;

    if (path == 0)
        return 0;
    fd = syscall3(5 /* __NR_open */, (long)path, 0 /* O_RDONLY */, 0);
    if (fd < 0)
        return 0;
    n = syscall3(3 /* __NR_read */, fd, (long)text, sizeof text);
    syscall3(6 /* __NR_close */, fd, 0, 0);
    if (n > 0 && text[0] == '-') {
        sign = -1;
        i = 1;
    }
    for (; i < n && text[i] >= '0' && text[i] <= '9'; i++)
        v = v * 10 + (text[i] - '0');
    return sign * v;
}

long time(long *t)
{
    long tv[2];
    long now;

    syscall3(78 /* __NR_gettimeofday */, (long)tv, 0, 0);
    now = tv[0] + shift();
    if (t != 0)
        *t = now;
    return now;
}
