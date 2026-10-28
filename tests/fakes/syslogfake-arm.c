/* SPDX-License-Identifier: MIT */
/*
 * syslogfake-arm: LD_PRELOAD shim for ARM builds under qemu-arm.
 *
 * syslog() and vsyslog() append one line per message to $KAROTZ_SYSLOG_FILE,
 * "<level> <message>" (the level is the priority's low three bits: 3 error,
 * 4 warning, 5 notice, 6 info, 7 debug), with one write each, so several
 * processes can share the file. Only %s, %d, %u, %x, %c and %% are
 * formatted; anything else is written as it is. A message is cut at about
 * 1000 bytes. Without the variable nothing is written. The sandbox has no
 * syslog daemon, so nothing is lost. devtrace.c does the same for host builds.
 *
 * Freestanding, like ioctlfake-arm.c: build with
 *   arm-buildroot-linux-uclibcgnueabi-gcc (the system toolchain's) -march=armv4t -marm -O2 -nostdlib -shared -fPIC -Wl,--hash-style=sysv
 */
#include <stdarg.h>

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

struct line {
    char text[1024];
    unsigned n;
};

static void put(struct line *l, char c)
{
    if (l->n < sizeof l->text - 1)
        l->text[l->n++] = c;
}

static void put_number(struct line *l, unsigned long v, unsigned base, int negative)
{
    char digits[24];
    int i = 0;

    if (negative)
        put(l, '-');
    do {
        digits[i++] = "0123456789abcdef"[v % base];
        v /= base;
    } while (v);
    while (i)
        put(l, digits[--i]);
}

void vsyslog(int priority, const char *format, va_list ap)
{
    const char *path = env_value("KAROTZ_SYSLOG_FILE=");
    struct line l;
    const char *f;
    long fd;

    if (path == 0)
        return;
    l.n = 0;
    put(&l, '0' + (priority & 7));
    put(&l, ' ');
    for (f = format; f && *f; f++) {
        if (*f != '%' || !f[1]) {
            put(&l, *f);
            continue;
        }
        switch (*++f) {
        case 's': {
            const char *s = va_arg(ap, const char *);
            for (s = s ? s : "(null)"; *s; s++)
                put(&l, *s);
            break;
        }
        case 'd': {
            int v = va_arg(ap, int);
            put_number(&l, v < 0 ? -(unsigned long)v : (unsigned long)v, 10, v < 0);
            break;
        }
        case 'u':
            put_number(&l, va_arg(ap, unsigned), 10, 0);
            break;
        case 'x':
            put_number(&l, va_arg(ap, unsigned), 16, 0);
            break;
        case 'c':
            put(&l, (char)va_arg(ap, int));
            break;
        case '%':
            put(&l, '%');
            break;
        default:
            put(&l, '%');
            put(&l, *f);
        }
    }
    while (l.n > 2 && l.text[l.n - 1] == '\n')
        l.n--;
    l.text[l.n++] = '\n';
    fd = syscall3(5 /* __NR_open */, (long)path, 01 | 0100 | 02000 /* O_WRONLY | O_CREAT | O_APPEND */, 0644);
    if (fd < 0)
        return;
    syscall3(4 /* __NR_write */, fd, (long)l.text, l.n);
    syscall3(6 /* __NR_close */, fd, 0, 0);
}

void syslog(int priority, const char *format, ...)
{
    va_list ap;

    va_start(ap, format);
    vsyslog(priority, format, ap);
    va_end(ap);
}
