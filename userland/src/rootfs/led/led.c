/* SPDX-License-Identifier: MIT */
/*
 * led: set, pulse or blink the nose LED through the SPI LED driver.
 *
 *   led -l RRGGBB [-p RRGGBB | -b RRGGBB] [-d period_ms]
 */
#include <fcntl.h>
#include <getopt.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/select.h>
#include <sys/time.h>
#include <unistd.h>
#include <linux/types.h> /* 2.6.28's spidev.h needs it first */
#include <linux/spi/spidev.h>

static const char *device = "/dev/spidev0.1";
static uint8_t mode;
static uint8_t bits = 8;
static uint32_t speed = 500000;
static uint16_t delay;
static int gFd;
static struct timeval gStartTime;

#define DEFAULT_PERIOD_MS  1000   /* -d default */
#define PULSE_STEP_US      25000  /* a fade step every 25 ms */
/* A blink cycle lasts 2 x the period: 70% primary colour, 30% secondary. */
#define BLINK_PRIMARY_US_PER_MS    1400
#define BLINK_SECONDARY_US_PER_MS  600

static void pabort(const char *s)
{
    perror(s);
    abort();
}

/* One two-byte SPI write: { register, value }. */
static void transfer_write(int fd, uint8_t *tx)
{
    uint8_t rx[2] = { 0, 0 };
    struct spi_ioc_transfer tr;
    int ret;

    memset(&tr, 0, sizeof tr);
    tr.tx_buf = (unsigned long)tx;
    tr.rx_buf = (unsigned long)rx;
    tr.len = 2;
    tr.speed_hz = speed;
    tr.delay_usecs = delay;
    tr.bits_per_word = bits;

    ret = ioctl(fd, SPI_IOC_MESSAGE(1), &tr);
    /* The ioctl returns the bytes sent, or -1 on failure. */
    if (ret < 0)
        pabort("can't access spidev ioctl interface");
}

int Sleep(unsigned int ms)
{
    struct timeval tv;
    tv.tv_sec = ms / 1000;
    tv.tv_usec = (ms % 1000) * 1000;
    select(0, NULL, NULL, NULL, &tv);
    return 0;
}

/*
 * The LED driver chip, written as two-byte { register, value } SPI messages.
 * Registers 0-2 take red, 3-5 blue and 6-8 green (three channels each);
 * register 0x10 gets 0x05 at start-up (what its bits mean isn't known).
 * The driver treats the value 0xff specially: values below 3 are written as
 * 0xff, and 0xff itself as 0xfe.
 */
#define LED_REG_RED          0
#define LED_REG_BLUE         3
#define LED_REG_GREEN        6
#define LED_REGS_PER_COLOUR  3
#define LED_REG_INIT         0x10
#define LED_INIT_VALUE       0x05
#define LED_VALUE_SPECIAL    0xff
#define LED_VALUE_MIN        3
#define LED_VALUE_MAX        0xfe

static uint8_t cast_color(uint8_t c)
{
    if (c < LED_VALUE_MIN)
        return LED_VALUE_SPECIAL;
    if (c == LED_VALUE_SPECIAL)
        return LED_VALUE_MAX;
    return c;
}

int set_color_private(uint8_t r, uint8_t g, uint8_t b)
{
    uint8_t msg[2];
    int reg;

    msg[1] = cast_color(r);
    for (reg = LED_REG_RED; reg < LED_REG_RED + LED_REGS_PER_COLOUR; reg++) {
        msg[0] = reg;
        transfer_write(gFd, msg);
    }
    msg[1] = cast_color(b);
    for (reg = LED_REG_BLUE; reg < LED_REG_BLUE + LED_REGS_PER_COLOUR; reg++) {
        msg[0] = reg;
        transfer_write(gFd, msg);
    }
    msg[1] = cast_color(g);
    for (reg = LED_REG_GREEN; reg < LED_REG_GREEN + LED_REGS_PER_COLOUR; reg++) {
        msg[0] = reg;
        transfer_write(gFd, msg);
    }
    return 0;
}

int led_init(void)
{
    uint8_t init[2] = { LED_REG_INIT, LED_INIT_VALUE };

    gFd = open(device, O_RDWR);
    if (gFd < 0)
        pabort("can't open device");
    if (ioctl(gFd, SPI_IOC_WR_MODE, &mode) == -1)
        pabort("can't set spi mode");
    if (ioctl(gFd, SPI_IOC_RD_MODE, &mode) == -1)
        pabort("can't get spi mode");
    if (ioctl(gFd, SPI_IOC_WR_BITS_PER_WORD, &bits) == -1)
        pabort("can't set bits per word");
    if (ioctl(gFd, SPI_IOC_RD_BITS_PER_WORD, &bits) == -1)
        pabort("can't get bits per word");
    if (ioctl(gFd, SPI_IOC_WR_MAX_SPEED_HZ, &speed) == -1)
        pabort("can't set max speed hz");
    if (ioctl(gFd, SPI_IOC_RD_MAX_SPEED_HZ, &speed) == -1)
        pabort("can't get max speed hz");
    transfer_write(gFd, init);
    return 0;
}

/* "RRGGBB" -> rgb[3]. Returns 0 on success; an empty string is black. */
int StringToColor(const char *s, unsigned int rgb[3])
{
    if (*s == '\0') {
        rgb[0] = rgb[1] = rgb[2] = 0;
        return 0;
    }
    /* Quirk: a leading 'n' is rejected explicitly, though sscanf() would
     * reject it too ('n' is not a hex digit), so the result is the same. */
    if (*s == 'n')
        return 1;
    return sscanf(s, "%2x%2x%2x", &rgb[0], &rgb[1], &rgb[2]) != 3;
}

static void initTime(void)
{
    gettimeofday(&gStartTime, NULL);
}

static int elapsedTime(void)
{
    struct timeval now;
    gettimeofday(&now, NULL);
    return (now.tv_sec - gStartTime.tv_sec) * 1000 + (now.tv_usec - gStartTime.tv_usec) / 1000;
}

static void print_usage(const char *prog)
{
    printf("Usage: %s -l <primary color> [-p <secondary color>] [-b <secondary color>] [ -d <periodicity> ]\n", prog);
    puts("  -l --light   set led color\n"
         "  -p --pulse    set pulse 2nd color\n"
         "  -d --delay    pulse period (msec)\n"
         "  -b --blink    blink (without fading effect");
    exit(1);
}

static void parse_opts(int argc, char **argv)
{
    static const struct option lopts[] = {
        { "light", 1, 0, 'l' },
        { "pulse", 1, 0, 'p' },
        { "blink", 1, 0, 'b' },
        { "delay", 1, 0, 'd' },
        { NULL, 0, 0, 0 },
    };
    unsigned int color[2][3] = { { 0, 0, 0 }, { 0, 0, 0 } }; /* primary, secondary */
    int period = DEFAULT_PERIOD_MS;
    int pulse = 0, blink = 0;
    int c;

    while ((c = getopt_long(argc, argv, "l:p:d:b:", lopts, NULL)) != -1) {
        switch (c) {
        case 'd':
            period = atoi(optarg);
            break;
        case 'l':
            if (StringToColor(optarg, color[0])) {
                print_usage(argv[0]);
                return;
            }
            set_color_private(color[0][0], color[0][1], color[0][2]);
            break;
        case 'p':
            if (StringToColor(optarg, color[1])) {
                print_usage(argv[0]);
                return;
            }
            pulse = 1;
            break;
        case 'b':
            if (StringToColor(optarg, color[1])) {
                print_usage(argv[0]);
                return;
            }
            blink = 1;
            break;
        default:
            print_usage(argv[0]);
            break;
        }
    }

    if (pulse) {
        /* Fade linearly between the two colours, swapping ends every period. */
        initTime();
        for (;;) {
            int t, phase, k;
            unsigned int out[3];

            usleep(PULSE_STEP_US);
            t = elapsedTime();
            phase = (t / period) & 1;
            t %= period;
            for (k = 0; k < 3; k++)
                out[k] = (t * color[1 - phase][k] + color[phase][k] * (period - t)) / period;
            set_color_private(out[0], out[1], out[2]);
        }
    }

    if (blink) {
        /* Primary, then secondary colour (see BLINK_*_US_PER_MS). */
        for (;;) {
            usleep(period * BLINK_PRIMARY_US_PER_MS);
            set_color_private(color[0][0], color[0][1], color[0][2]);
            usleep(period * BLINK_SECONDARY_US_PER_MS);
            set_color_private(color[1][0], color[1][1], color[1][2]);
        }
    }
}

int main(int argc, char **argv)
{
    led_init();
    parse_opts(argc, argv);
    return 0;
}
