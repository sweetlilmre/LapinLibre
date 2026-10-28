/* SPDX-License-Identifier: MIT */
/*
 * devtrace: LD_PRELOAD shim that logs a program's device I/O to $DEVTRACE.
 *
 * Only file descriptors opened on /dev paths (other than null, zero, random,
 * urandom and tty) are traced. Lines match what tests/harness.py derives
 * from qemu-arm -strace for ARM builds:
 *
 *   open /dev/rabbit_ears
 *   ioctl /dev/rabbit_ears 0x40084501
 *   write /dev/spidev0.1 2
 *   read /dev/rabbit_button 1
 *   system reboot
 *
 * system() is logged and not executed, so a program under test can never
 * really reboot or run anything else. ioctls whose type byte is listed in
 * $KAROTZ_FAKE_IOCTL_TYPES report success (see ioctlfake-arm.c). With
 * $KAROTZ_SYSROOT, absolute paths that exist in the sysroot are used from it,
 * as qemu-arm -L does for ARM builds.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <termios.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_FD 1024

static char *paths[MAX_FD];

static int traced(const char *path)
{
    static const char *skip[] = { "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty", NULL };
    int i;

    if (path == NULL || strncmp(path, "/dev/", 5) != 0)
        return 0;
    for (i = 0; skip[i]; i++)
        if (strcmp(path, skip[i]) == 0)
            return 0;
    return 1;
}

static void logf_(const char *fmt, ...)
{
    const char *file = getenv("DEVTRACE");
    FILE *f;
    va_list ap;

    if (file == NULL || (f = fopen(file, "a")) == NULL)
        return;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static int track(int fd, const char *path)
{
    if (fd >= 0 && fd < MAX_FD && traced(path)) {
        free(paths[fd]);
        paths[fd] = strdup(path);
        logf_("open %s", path);
    }
    return fd;
}

/*
 * qemu-arm -L SYSROOT resolves an absolute path inside SYSROOT when it exists
 * there, so ARM builds see the sysroot's files. Give a host build the same
 * view: with $KAROTZ_SYSROOT set, an absolute path (outside /dev, /proc
 * and /sys) that exists under it is used from there.
 */
static const char *remap(const char *path, char *buf, size_t size)
{
    const char *root = getenv("KAROTZ_SYSROOT");
    struct stat st;

    if (root == NULL || path == NULL || path[0] != '/' || strncmp(path, "/dev/", 5) == 0 ||
        strncmp(path, "/proc/", 6) == 0 || strncmp(path, "/sys/", 5) == 0)
        return path;
    if ((size_t)snprintf(buf, size, "%s%s", root, path) >= size)
        return path;
    return syscall(SYS_newfstatat, AT_FDCWD, buf, &st, AT_SYMLINK_NOFOLLOW) == 0 ? buf : path;
}

/* Look up the libc function this wrapper hides, once. */
#define REAL(name) static __typeof__(&name) real_##name; if (!real_##name) real_##name = (__typeof__(&name))dlsym(RTLD_NEXT, #name)

int open(const char *path, int flags, ...)
{
    mode_t mode = 0;
    REAL(open);
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
    }
    char buf[4096];
    return track(real_open(remap(path, buf, sizeof buf), flags, mode), path);
}

int open64(const char *path, int flags, ...)
{
    mode_t mode = 0;
    REAL(open64);
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
    }
    char buf[4096];
    return track(real_open64(remap(path, buf, sizeof buf), flags, mode), path);
}

int openat(int dirfd, const char *path, int flags, ...)
{
    mode_t mode = 0;
    REAL(openat);
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
    }
    char buf[4096];
    return track(real_openat(dirfd, remap(path, buf, sizeof buf), flags, mode), path);
}

FILE *fopen(const char *path, const char *mode)
{
    FILE *f;
    REAL(fopen);
    char buf[4096];
    f = real_fopen(remap(path, buf, sizeof buf), mode);
    if (f != NULL)
        track(fileno(f), path);
    return f;
}

FILE *fopen64(const char *path, const char *mode)
{
    FILE *f;
    REAL(fopen64);
    char buf[4096];
    f = real_fopen64(remap(path, buf, sizeof buf), mode);
    if (f != NULL)
        track(fileno(f), path);
    return f;
}

/* Path lookups that should see the sysroot too. */
int stat(const char *path, struct stat *st)
{
    char buf[4096];
    REAL(stat);
    return real_stat(remap(path, buf, sizeof buf), st);
}

int lstat(const char *path, struct stat *st)
{
    char buf[4096];
    REAL(lstat);
    return real_lstat(remap(path, buf, sizeof buf), st);
}

int access(const char *path, int mode)
{
    char buf[4096];
    REAL(access);
    return real_access(remap(path, buf, sizeof buf), mode);
}

DIR *opendir(const char *path)
{
    char buf[4096];
    REAL(opendir);
    return real_opendir(remap(path, buf, sizeof buf));
}

/* stdio reads go to libc's internal read(); log them at the fread level. */
size_t fread(void *buf, size_t size, size_t n, FILE *f)
{
    int fd;
    REAL(fread);
    fd = fileno(f);
    if (fd >= 0 && fd < MAX_FD && paths[fd])
        logf_("read %s %zu", paths[fd], size * n);
    return real_fread(buf, size, n, f);
}

/* Whether the n-character command name is in the comma-separated list. */
static int stub_listed(const char *list, const char *name, size_t n)
{
    while (list && *list) {
        size_t len = strcspn(list, ",");
        if (len == n && strncmp(list, name, n) == 0)
            return 1;
        list += len;
        if (*list == ',')
            list++;
    }
    return 0;
}

/* Never run commands under test (power runs "reboot"): log them instead. */
int system(const char *command)
{
    const char *stubs = getenv("KAROTZ_STUB_DIR");
    char path[512];
    size_t n;

    logf_("system %s", command ? command : "(null)");
    if (command == NULL)
        return 1;
    /* Under the test, PATH holds only no-op stubs (exit 0): answer as the
     * shell would, 0 for a stubbed command and 127 (not found) otherwise. */
    n = strcspn(command, " 	");
    if (stubs && n > 0 && snprintf(path, sizeof path, "%s/%.*s", stubs, (int)n, command) < (int)sizeof path
        && syscall(SYS_faccessat, AT_FDCWD, path, X_OK) == 0) {
        /* A scenario that puts working stubs in place (controller_platform's
         * killall stops its fake karotzVM) names them in $KAROTZ_RUN_STUBS
         * (comma-separated) to have them run, as they are in the qemu runs,
         * where system() reaches the stub directory. Like uClibc's system()
         * (and unlike glibc's, which blocks SIGCHLD), SIGCHLD is at its
         * default meanwhile, so a child that ends then raises no signal. */
        if (stub_listed(getenv("KAROTZ_RUN_STUBS"), command, n)) {
            struct sigaction dfl, old;
            int status = -1;
            pid_t pid;

            memset(&dfl, 0, sizeof dfl);
            dfl.sa_handler = SIG_DFL;
            sigaction(SIGCHLD, &dfl, &old);
            pid = fork();
            if (pid == 0) {
                execl("/bin/sh", "sh", "-c", command, (char *)NULL);
                _exit(127);
            }
            while (pid > 0 && waitpid(pid, &status, 0) < 0 && errno == EINTR)
                ;
            sigaction(SIGCHLD, &old, NULL);
            return status;
        }
        return 0;
    }
    return 127 << 8;
}

/* syslog() appends "<level> <message>" to $KAROTZ_SYSLOG_FILE, as
 * syslogfake-arm.c does for ARM builds; the sandbox has no syslog daemon. */
void vsyslog(int priority, const char *format, va_list ap)
{
    const char *file = getenv("KAROTZ_SYSLOG_FILE");
    char text[1024];
    size_t n;
    FILE *f;

    if (file == NULL)
        return;
    vsnprintf(text, sizeof text, format, ap);
    n = strlen(text);
    while (n > 0 && text[n - 1] == '\n')
        text[--n] = '\0';
    if ((f = fopen(file, "a")) == NULL)
        return;
    fprintf(f, "%d %s\n", priority & 7, text);
    fclose(f);
}

void syslog(int priority, const char *format, ...)
{
    va_list ap;

    va_start(ap, format);
    vsyslog(priority, format, ap);
    va_end(ap);
}

/* What syslog() becomes with _FORTIFY_SOURCE. */
void __syslog_chk(int priority, int flag, const char *format, ...)
{
    va_list ap;

    (void)flag;
    va_start(ap, format);
    vsyslog(priority, format, ap);
    va_end(ap);
}

int close(int fd)
{
    REAL(close);
    if (fd >= 0 && fd < MAX_FD && paths[fd]) {
        free(paths[fd]);
        paths[fd] = NULL;
    }
    return real_close(fd);
}

ssize_t read(int fd, void *buf, size_t n)
{
    REAL(read);
    if (fd >= 0 && fd < MAX_FD && paths[fd])
        logf_("read %s %zu", paths[fd], n);
    return real_read(fd, buf, n);
}

ssize_t write(int fd, const void *buf, size_t n)
{
    REAL(write);
    if (fd >= 0 && fd < MAX_FD && paths[fd])
        logf_("write %s %zu", paths[fd], n);
    return real_write(fd, buf, n);
}

/* Is this ioctl's type byte listed in $KAROTZ_FAKE_IOCTL_TYPES (hex, comma-separated)? */
static int faked(unsigned long request)
{
    const char *s = getenv("KAROTZ_FAKE_IOCTL_TYPES");
    unsigned long type = (request >> 8) & 0xff;
    char *end;

    while (s && *s) {
        if (strtoul(s, &end, 16) == type)
            return 1;
        s = *end ? end + 1 : end;
    }
    return 0;
}

int ioctl(int fd, unsigned long request, ...)
{
    va_list ap;
    void *arg;
    int r;
    REAL(ioctl);
    va_start(ap, request);
    arg = va_arg(ap, void *);
    va_end(ap);
    if (fd >= 0 && fd < MAX_FD && paths[fd])
        logf_("ioctl %s 0x%lx", paths[fd], request);
    r = real_ioctl(fd, request, arg);
    /* Like ioctlfake-arm.c for ARM builds: listed ioctl types succeed, and
     * ones that return data (_IOC_READ) return zeros, so the program never
     * reads uninitialised memory. */
    if (!faked(request))
        return r;
    if (((request >> 30) & 2) && arg)
        memset(arg, 0, (request >> 16) & 0x3fff);
    return 0;
}

/* glibc's termios calls use its internal ioctl; log them as the ioctls they
 * are, as qemu's strace shows them for ARM builds. */
int tcgetattr(int fd, struct termios *t)
{
    REAL(tcgetattr);
    if (fd >= 0 && fd < MAX_FD && paths[fd])
        logf_("ioctl %s 0x%x", paths[fd], TCGETS);
    return real_tcgetattr(fd, t);
}

int tcsetattr(int fd, int action, const struct termios *t)
{
    REAL(tcsetattr);
    if (fd >= 0 && fd < MAX_FD && paths[fd])
        logf_("ioctl %s 0x%x", paths[fd], action == TCSANOW ? TCSETS : action == TCSADRAIN ? TCSETSW : TCSETSF);
    return real_tcsetattr(fd, action, t);
}

int tcflush(int fd, int queue)
{
    REAL(tcflush);
    if (fd >= 0 && fd < MAX_FD && paths[fd])
        logf_("ioctl %s 0x%x", paths[fd], TCFLSH);
    return real_tcflush(fd, queue);
}
