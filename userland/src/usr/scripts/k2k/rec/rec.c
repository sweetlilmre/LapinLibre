/* SPDX-License-Identifier: MIT */
/*
 * rec: record raw audio from the rabbit's OSS device.
 *
 *   rec                 to standard output, until killed (k2k: "rec | nc ...")
 *   rec FILE            882,000 bytes (5 s) into FILE, then exit
 *
 * Always 16-bit little-endian, stereo, 44,100 Hz from /dev/audio. Messages go
 * to standard error (standard output carries the audio); a failed step is
 * reported and the next one still runs.
 */
#include <fcntl.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <sys/soundcard.h>
#include <unistd.h>

#define BUFSIZE 0xd7550  /* 882,000 bytes: 5 s of 16-bit stereo at 44.1 kHz */

static const char *recdevice = "/dev/audio";
static int fdrec;
static unsigned char audiobuf[BUFSIZE];

static int recpabort(const char *msg)
{
    fprintf(stderr, "Record error: %s\n", msg);
    return -1;
}

/* Quirk: a failed open leaves fdrec at -1, which every later step takes for an
 * open device (they test for 0 only). */
static int rec_init(void)
{
    fprintf(stderr, "record device=%s\n", recdevice);
    fdrec = open(recdevice, O_RDONLY);
    if (fdrec == -1)
        return recpabort("unable to open device");
    return 0;
}

static int rec_stop(void)
{
    if (fdrec == 0)
        return -1;
    close(fdrec);
    fdrec = 0;
    return 0;
}

static int rec_config(int bits, int stereo, int speed)
{
    int v;

    if (fdrec == 0)
        return -1;
    if (ioctl(fdrec, SNDCTL_DSP_RESET, 0) < 0)
        return recpabort("Unable to reset OSS driver.");
    v = 0;
    if (bits == 8)
        v = AFMT_U8;
    if (bits == 16)
        v = AFMT_S16_LE;
    fprintf(stderr, "expected format=%d\n", v);
    if (ioctl(fdrec, SNDCTL_DSP_SETFMT, &v) < 0)
        return recpabort("Unable to set format");
    fprintf(stderr, "accepted format=%d\n", v);
    v = stereo;
    fprintf(stderr, "expected stereo=%d\n", stereo);
    if (ioctl(fdrec, SNDCTL_DSP_STEREO, &v) < 0)
        return recpabort("Unable to set stereo");
    fprintf(stderr, "accepted stereo=%d\n", v);
    v = speed;
    if (ioctl(fdrec, SNDCTL_DSP_SPEED, &v) < 0)
        return recpabort("Unable to set speed");
    fprintf(stderr, "speed=%d\n", v);
    return 0;
}

/* Quirk: one byte per read() call, errors ignored. */
static int rec_tobuf(unsigned char *buf, int len)
{
    int i;

    if (fdrec == 0)
        return -1;
    for (i = 0; i < len; i++)
        if (read(fdrec, buf + i, 1) < 0) {
            /* ignored: the stream goes on */
        }
    return 0;
}

/* Quirk: never returns; ends when killed, or when its reader goes (SIGPIPE),
 * which is how k2k_stop.sh stops it. A read of 0 is reported and the loop goes
 * on; a read error's -1 is passed to write as it is. */
static void rec_tostdout(void)
{
    char buf[0x800];
    ssize_t n;

    if (fdrec == 0)
        return;
    for (;;) {
        n = read(fdrec, buf, sizeof buf);
        if (n == 0)
            fputs("rec_tostdout, read == 0\n", stderr);
        if (write(1, buf, n) < 0) {
            /* ignored: the stream goes on */
        }
    }
}

static int recordfile(const char *name)
{
    rec_init();
    rec_config(16, 1, 44100);
    if (name == NULL)
        rec_tostdout();
    else
        rec_tobuf(audiobuf, BUFSIZE);
    rec_stop();
    if (name != NULL) {
        FILE *f = fopen(name, "wb");

        if (f == NULL) {
            fprintf(stderr, "unable to open %s\n", name);
        } else {
            fwrite(audiobuf, 1, BUFSIZE, f);
            fclose(f);
            fputs("sound stored\n", stderr);
        }
    }
    return 0;
}

int main(int argc, char **argv)
{
    recordfile(argc == 2 ? argv[1] : NULL);
    return 0;
}
