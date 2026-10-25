/* SPDX-License-Identifier: MIT */
/*
 * ioctlfake-arm: LD_PRELOAD shim for ARM builds under qemu-arm.
 *
 * Makes ioctls whose type byte ((request >> 8) & 0xff) is listed in
 * $KAROTZ_FAKE_IOCTL_TYPES (comma-separated hex, e.g. "6b,45") report
 * success, so a program whose device is a FIFO in the test (SPI, ears, I2C,
 * termios...) gets past its device setup. The real syscall is still made, so
 * qemu's strace logs it. Faked ioctls that return data (_IOC_READ) fill
 * their buffer with zeros. devtrace.c does the same for host builds.
 *
 * $KAROTZ_FAKE_IOCTL_DATA_FILE names a file of lines "<request> <bytes>",
 * in hex (e.g. "80044504 03000000"): a faked _IOC_READ ioctl whose request
 * is listed gets those bytes (the rest zeros) instead. The file is read at
 * each such call, so a test can change it while the program runs; without
 * the file nothing changes. (ARM builds only.)
 *
 * The rabbit's libc is uClibc, so this is freestanding: build with
 *   arm-buildroot-linux-uclibcgnueabi-gcc (the system toolchain's) -march=armv4t -marm -O2 -nostdlib -shared -fPIC -Wl,--hash-style=sysv
 */

extern char **environ;

static long syscall3(long number, long a, long b, long c)
{
    register long r0 __asm__("r0") = a;
    register long r1 __asm__("r1") = b;
    register long r2 __asm__("r2") = c;
    register long r7 __asm__("r7") = number;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory");
    return r0;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

static int faked(unsigned long request)
{
    static const char key[] = "KAROTZ_FAKE_IOCTL_TYPES=";
    unsigned type = (request >> 8) & 0xff;
    char **e;

    for (e = environ; e && *e; e++) {
        const char *s = *e;
        unsigned i;
        for (i = 0; key[i] && s[i] == key[i]; i++)
            ;
        if (key[i])
            continue;
        s += i;
        while (*s) {
            int v = 0, d;
            while ((d = hexval(*s)) >= 0) {
                v = v * 16 + d;
                s++;
            }
            if ((unsigned)v == type)
                return 1;
            if (*s)
                s++;
        }
    }
    return 0;
}

/* The value of environment variable `key` (given with its '='), or 0. */
static const char *env_value(const char *key)
{
    char **e;

    for (e = environ; e && *e; e++) {
        const char *s = *e;
        unsigned i;
        for (i = 0; key[i] && s[i] == key[i]; i++)
            ;
        if (!key[i])
            return s + i;
    }
    return 0;
}

/* The bytes $KAROTZ_FAKE_IOCTL_DATA_FILE lists for `request`, into p. */
static void fill_from_file(unsigned long request, unsigned char *p, unsigned long size)
{
    const char *path = env_value("KAROTZ_FAKE_IOCTL_DATA_FILE=");
    char buf[512];
    long n, i = 0;
    long fd;

    if (path == 0)
        return;
    fd = syscall3(5 /* __NR_open */, (long)path, 0 /* O_RDONLY */, 0);
    if (fd < 0)
        return;
    n = syscall3(3 /* __NR_read */, fd, (long)buf, sizeof buf);
    syscall3(6 /* __NR_close */, fd, 0, 0);
    while (i < n) {
        unsigned long listed = 0;
        int d;
        while (i < n && (d = hexval(buf[i])) >= 0) {
            listed = listed * 16 + d;
            i++;
        }
        while (i < n && buf[i] == ' ')
            i++;
        if (listed == request) {
            unsigned long k = 0;
            while (k < size && i + 1 < n && hexval(buf[i]) >= 0 && hexval(buf[i + 1]) >= 0) {
                p[k++] = (unsigned char)(hexval(buf[i]) * 16 + hexval(buf[i + 1]));
                i += 2;
            }
            return;
        }
        while (i < n && buf[i] != '\n')
            i++;
        i++;
    }
}

int ioctl(int fd, unsigned long request, void *arg)
{
    long r = syscall3(54 /* __NR_ioctl */, fd, (long)request, (long)arg);

    if (faked(request)) {
        /* Ioctls that return data (_IOC_READ) return zeros, or the bytes
         * the data file lists, unless the program passed no buffer (the
         * webcam's VGA switch passes NULL; its driver ignores it). */
        if (((request >> 30) & 2) && arg) {
            unsigned char *p = arg;
            unsigned long i, size = (request >> 16) & 0x3fff;
            for (i = 0; i < size; i++)
                p[i] = 0;
            fill_from_file(request, p, size);
        }
        return 0;
    }
    return (r < 0 && r > -4096) ? -1 : (int)r; /* errno is not set */
}
