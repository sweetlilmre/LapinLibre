/* SPDX-License-Identifier: MIT */
/*
 * playback: play raw audio on the rabbit's OSS device.
 *
 *   playback            standard input, until killed (k2k: "nc -l | playback")
 *   playback FILE       the first 882,000 bytes of FILE (5 s), then exit
 *
 * Always 16-bit little-endian, stereo, 44,100 Hz on /dev/audio. Messages go to
 * standard output; a failed step is reported and the next one still runs.
 */
#include <fcntl.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <sys/soundcard.h>
#include <unistd.h>

#define BUFSIZE 0xd7550  /* 882,000 bytes: 5 s of 16-bit stereo at 44.1 kHz */

static const char *playbackdevice = "/dev/audio";
static int fdplayback;
static unsigned char audiobuf[BUFSIZE];

int playbackpabort(const char *msg)
{
    printf("Playback error: %s\n", msg);
    return -1;
}

/* Quirk: a failed open leaves fdplayback at -1, which every later step takes
 * for an open device (they test for 0 only). */
static int playback_init(void)
{
    printf("audio device=%s\n", playbackdevice);
    fdplayback = open(playbackdevice, O_WRONLY);
    if (fdplayback == -1)
        return playbackpabort("unable to open device");
    return 0;
}

static int playback_stop(void)
{
    if (fdplayback == 0)
        return -1;
    close(fdplayback);
    fdplayback = 0;
    return 0;
}

static int playback_config(int bits, int stereo, int speed)
{
    int v;

    if (fdplayback == 0)
        return -1;
    if (ioctl(fdplayback, SNDCTL_DSP_RESET, 0) < 0)
        return playbackpabort("Unable to reset OSS driver.");
    v = 0;
    if (bits == 8)
        v = AFMT_U8;
    if (bits == 16)
        v = AFMT_S16_LE;
    printf("expected format=%d\n", v);
    if (ioctl(fdplayback, SNDCTL_DSP_SETFMT, &v) < 0)
        return playbackpabort("Unable to set format");
    printf("accepted format=%d\n", v);
    v = stereo;
    printf("expected stereo=%d\n", stereo);
    if (ioctl(fdplayback, SNDCTL_DSP_STEREO, &v) < 0)
        return playbackpabort("Unable to set stereo");
    printf("accepted stereo=%d\n", v);
    v = speed;
    if (ioctl(fdplayback, SNDCTL_DSP_SPEED, &v) < 0)
        return playbackpabort("Unable to set speed");
    printf("speed=%d\n", v);
    return 0;
}

/* Quirk: never returns. At the end of input it prints its message and goes on
 * reading (a busy loop) until k2k_stop.sh kills it; a read error's -1 is passed
 * to write as it is. */
static void playback_fromstdin(void)
{
    char buf[0x800];
    ssize_t n;

    if (fdplayback == 0)
        return;
    for (;;) {
        n = read(0, buf, sizeof buf);
        if (n == 0)
            fputs("playback_fromstdin, read == 0\n", stderr);
        if (write(fdplayback, buf, n) < 0) {
            /* ignored: the stream goes on */
        }
    }
}

/* Quirk: one byte per write() call, and always the whole buffer, past the end
 * of a shorter file (the rest is silence: the buffer starts zeroed). */
static int playback_frombuf(const unsigned char *buf, int len)
{
    int i;

    if (fdplayback == 0)
        return -1;
    for (i = 0; i < len; i++)
        if (write(fdplayback, buf + i, 1) < 0) {
            /* ignored: the stream goes on */
        }
    return 0;
}

static int playfile(const char *name)
{
    if (name != NULL) {
        FILE *f = fopen(name, "rb");

        if (f == NULL) {
            printf("unable to open %s\n", name);
        } else {
            printf("ret: %d\n", (int)fread(audiobuf, 1, BUFSIZE, f));
            fclose(f);
            puts("sound read");
        }
    }
    playback_init();
    playback_config(16, 1, 44100);
    if (name == NULL)
        playback_fromstdin();
    else
        playback_frombuf(audiobuf, BUFSIZE);
    playback_stop();
    return 0;
}

int main(int argc, char **argv)
{
    playfile(argc == 2 ? argv[1] : NULL);
    return 0;
}
