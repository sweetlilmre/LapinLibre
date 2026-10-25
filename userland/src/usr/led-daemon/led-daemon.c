/* SPDX-License-Identifier: MIT */
/*
 * led-daemon: drive the nose LED through the SPI LED driver (/dev/spidev0.1)
 * on behalf of D-Bus clients, as com.mindscape.karotz.Led
 * (/com/mindscape/karotz/Led).
 *
 * light(id, color) sets a colour at once; fade(id, color, period) fades from
 * the current colour to a new one over `period` ms; pulse(id, color1, color2,
 * period, length) swings between two colours every `period` ms for `length`
 * ms (forever if negative). Colours are "RRGGBB". Animations run on a 30 ms
 * timer; when one ends or is replaced, the daemon emits event(id, 4), or
 * event(id, 5) if it was cut short. light emits event(id, 4) on success and
 * event(id, 5) for a bad colour or a failed SPI write.
 *
 * The SPI code is the same as the standalone led tool (src/rootfs/led).
 */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <linux/types.h> /* 2.6.28's spidev.h needs it first */
#include <linux/spi/spidev.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include "karotz-common.h"
#include "karotz-messages.h"
#include "karotz-marshal.h"

/* Animation timer interval (ms), and the shortest accepted period. */
#define TICK_MS 30
#define MIN_PERIOD_MS 25

/* Second argument of the "event" signal. */
enum { EVENT_DONE = 4, EVENT_ABORTED = 5 };

enum {
    SIGNAL_EVENT,
    SIGNAL_REGISTERED,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    int color[3];      /* current r, g, b */
    int ends[2][3];    /* animation colours: [0] start, [1] end */
    int period;        /* ms per swing */
    int length;        /* total animation time in ms; negative runs forever */
    int cancelled;     /* the running animation was replaced by another one */
    guint timer;       /* animation timeout source, 0 if none */
    char id[64];       /* id of the running animation (at most 63 characters, NUL-terminated) */
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
} LedObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} LedObjectClass;

GType led_object_get_type(void);
#define LED_TYPE_OBJECT (led_object_get_type())

G_DEFINE_TYPE(LedObject, led_object, G_TYPE_OBJECT)

gboolean led_object_light(LedObject *self, const char *id, const char *color, gboolean *result, GError **error);
gboolean led_object_fade(LedObject *self, const char *id, const char *color, int period, gboolean *result,
                         GError **error);
gboolean led_object_pulse(LedObject *self, const char *id, const char *color1, const char *color2, int period,
                          int length, gboolean *result, GError **error);

#include "led-daemon-glue.h"

/* ---- SPI LED driver (shared with the led tool) ------------------------- */

static const char *device = "/dev/spidev0.1";
static uint8_t bits = 8;
static uint32_t speed = 500000;
static uint16_t delay;
static uint8_t mode;
static int gFd;

static void pabort(const char *s)
{
    perror(s);
    abort();
}

/* Whether the last SPI write failed: the warning is logged once per run of
 * failures, not per write (an animation writes every tick). */
static int spi_failing;

/* One two-byte SPI write: { register, value }. Returns 0, or -1 if it failed. */
static int transfer_write(int fd, uint8_t *tx)
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
    /* The ioctl returns the bytes sent, or -1 on failure. Aborting on a
     * failure would take the daemon down, so it is logged and reported
     * instead. */
    if (ret < 0) {
        if (!spi_failing)
            g_log(NULL, G_LOG_LEVEL_WARNING, "can't access spidev ioctl interface: %s", strerror(errno));
        spi_failing = 1;
        return -1;
    }
    if (spi_failing)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "spidev ioctl interface working again");
    spi_failing = 0;
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

static int set_color_private(uint8_t r, uint8_t g, uint8_t b)
{
    uint8_t msg[2];
    int reg;
    int failed = 0;

    /* Every write is made even after one fails; the result says whether
     * any failed. */
    msg[1] = cast_color(r);
    for (reg = LED_REG_RED; reg < LED_REG_RED + LED_REGS_PER_COLOUR; reg++) {
        msg[0] = reg;
        failed |= transfer_write(gFd, msg);
    }
    msg[1] = cast_color(b);
    for (reg = LED_REG_BLUE; reg < LED_REG_BLUE + LED_REGS_PER_COLOUR; reg++) {
        msg[0] = reg;
        failed |= transfer_write(gFd, msg);
    }
    msg[1] = cast_color(g);
    for (reg = LED_REG_GREEN; reg < LED_REG_GREEN + LED_REGS_PER_COLOUR; reg++) {
        msg[0] = reg;
        failed |= transfer_write(gFd, msg);
    }
    return failed;
}

static int led_init(void)
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

/*
 * "RRGGBB" -> rgb[3]. Returns 0 on success; an empty string is black.
 * On a malformed colour sscanf may already have overwritten some of rgb[].
 */
static int StringToColor(const char *s, int rgb[3])
{
    if (*s == '\0') {
        rgb[0] = rgb[1] = rgb[2] = 0;
        return 0;
    }
    /* Quirk: a leading 'n' is rejected explicitly, though sscanf() would
     * reject it too ('n' is not a hex digit), so the result is the same. */
    if (*s == 'n')
        return 1;
    return sscanf(s, "%2x%2x%2x", (unsigned int *)&rgb[0], (unsigned int *)&rgb[1], (unsigned int *)&rgb[2]) != 3;
}

/* ---- the LED object ---------------------------------------------------- */

/* Time into the running animation (ms). */
static int elapsed;

static int set_color(LedObject *self, uint8_t r, uint8_t g, uint8_t b)
{
    self->color[0] = r;
    self->color[1] = g;
    self->color[2] = b;
    return set_color_private(r, g, b);
}

static int set_color_from_string(LedObject *self, const char *s)
{
    int rgb[3] = { 0, 0, 0 };

    if (StringToColor(s, rgb))
        return -1;
    return set_color(self, rgb[0], rgb[1], rgb[2]);
}

/* Animation step: blend linearly between the two ends, swapping every period. */
static gboolean animate(gpointer data)
{
    LedObject *self = data;
    gboolean finished = FALSE;
    int t, phase, k;
    uint8_t out[3];

    elapsed += TICK_MS;
    t = elapsed;
    if (self->length >= 0 && self->length < elapsed) {
        t = self->length;
        finished = TRUE;
    }
    phase = (t / self->period) & 1;
    t %= self->period;
    for (k = 0; k < 3; k++)
        out[k] = (t * self->ends[1 - phase][k] + self->ends[phase][k] * (self->period - t)) / self->period;
    set_color(self, out[0], out[1], out[2]);
    if (finished)
        self->cancelled = 0;
    return !finished;
}

/* Destroy notify of the animation timer: runs when it ends or is removed. */
static void animation_finished(gpointer data)
{
    LedObject *self = data;
    LedObjectClass *klass = (LedObjectClass *)G_OBJECT_GET_CLASS(self);

    g_log(NULL, G_LOG_LEVEL_DEBUG, "finish");
    self->timer = 0;
    g_signal_emit(self, klass->signals[SIGNAL_EVENT], 0, self->id, self->cancelled ? EVENT_ABORTED : EVENT_DONE);
}

/* Stop the running animation; its finish handler reports it as aborted. */
static void cancel_animation(LedObject *self)
{
    if (self->timer != 0) {
        self->cancelled = 1;
        g_source_remove(self->timer);
        self->timer = 0;
    }
}

/* Periods up to 25 ms become 25 ms. */
static int clamp_period(int period)
{
    return period < MIN_PERIOD_MS + 1 ? MIN_PERIOD_MS : period;
}

static gboolean start_timer(LedObject *self)
{
    self->timer = g_timeout_add_full(G_PRIORITY_DEFAULT, TICK_MS, animate, self, animation_finished);
    return self->timer != 0;
}

gboolean led_object_light(LedObject *self, const char *id, const char *color, gboolean *result, GError **error)
{
    LedObjectClass *klass = (LedObjectClass *)G_OBJECT_GET_CLASS(self);
    int failed;

    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "light %s", color);
    /* As for fade and pulse, the animation this replaces is reported as
     * aborted (5). */
    cancel_animation(self);
    failed = set_color_from_string(self, color);
    if (failed) {
        /* A bad colour, or a failed SPI write, is result FALSE in a normal
         * reply, with an aborted event (5). */
        g_signal_emit(self, klass->signals[SIGNAL_EVENT], 0, id, EVENT_ABORTED);
        *result = FALSE;
    } else {
        g_signal_emit(self, klass->signals[SIGNAL_EVENT], 0, id, EVENT_DONE);
        *result = TRUE;
    }
    return TRUE;
}

gboolean led_object_fade(LedObject *self, const char *id, const char *color, int period, gboolean *result,
                         GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "fade -> %s: period: %d", color, period);
    /* The colour is checked before the clock and the colours are touched,
     * so a refused fade leaves a running animation alone. A bad one is
     * result FALSE in a normal reply, with no event. */
    int end[3];
    if (StringToColor(color, end)) {
        *result = FALSE;
        return TRUE;
    }
    elapsed = 0;
    memcpy(self->ends[0], self->color, sizeof self->ends[0]);
    memcpy(self->ends[1], end, sizeof self->ends[1]);
    cancel_animation(self);
    g_strlcpy(self->id, id, sizeof self->id);
    self->period = clamp_period(period);
    self->length = self->period;
    *result = start_timer(self);
    return *result;
}

gboolean led_object_pulse(LedObject *self, const char *id, const char *color1, const char *color2, int period,
                          int length, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "pulse %s -> %s period: %d length: %d", color1, color2, period, length);
    elapsed = 0;
    /* A colour that does not parse is replaced by the current colour. */
    if (StringToColor(color1, self->ends[0]))
        memcpy(self->ends[0], self->color, sizeof self->ends[0]);
    if (StringToColor(color2, self->ends[1]))
        memcpy(self->ends[1], self->color, sizeof self->ends[1]);
    cancel_animation(self);
    g_strlcpy(self->id, id, sizeof self->id);
    self->period = clamp_period(period);
    self->length = length;
    *result = start_timer(self);
    return *result;
}

static void led_object_class_init(LedObjectClass *klass)
{
    GType type;

    g_return_if_fail(klass != NULL);
    type = G_TYPE_FROM_CLASS(klass);
    klass->signals[SIGNAL_EVENT] = g_signal_new("event", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, 2, G_TYPE_STRING, G_TYPE_INT);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    dbus_g_object_type_install_info(LED_TYPE_OBJECT, &dbus_glib_led_object_object_info);
}

/* Opens the LED, then joins the system bus: all of it from the instance init. */
static void led_object_init(LedObject *self)
{
    GError *error = NULL;
    guint request_result;

    g_return_if_fail(self != NULL);
    led_init();
    self->id[0] = '\0';
    self->color[0] = 0;
    self->color[1] = 0;
    self->color[2] = 0;
    self->bus = NULL;
    self->bus_proxy = NULL;

    self->bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (self->bus == NULL)
        die("Failed to open connection to bus", error);
    else
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Successfully connected to %s bus\n", "SYSTEM");
    dbus_g_connection_register_g_object(self->bus, "/com/mindscape/karotz/Led", G_OBJECT(self));

    self->bus_proxy = dbus_g_proxy_new_for_name(self->bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(self->bus_proxy, "com.mindscape.karotz.Led", 0, &request_result, &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }
}

int main(void)
{
    int log_priority = LOG_NOTICE;
    GMainLoop *loop;
    LedObject *led;

    init_karotz_log("Led", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");
    led = g_object_new(LED_TYPE_OBJECT, NULL);
    g_signal_emit(led, ((LedObjectClass *)G_OBJECT_GET_CLASS(led))->signals[SIGNAL_REGISTERED], 0);
    g_main_loop_run(loop);
    return 0;
}
