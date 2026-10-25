/* SPDX-License-Identifier: MIT */
/*
 * button-daemon: turn the head button's press/release events into D-Bus
 * signals on com.mindscape.karotz.Button (/com/mindscape/karotz/Button).
 *
 * /dev/rabbit_button delivers '1' on press and anything else on release.
 * Presses less than 300 ms apart are counted into click / dclick / tclick /
 * mclick(n); holding for 450 ms gives lclick_start, then lclick_stop on
 * release.
 *
 * The button's contacts bounce: the driver queues every edge, so one press
 * can arrive as 1 0 1 0 ... a few ms apart. A level counts only once it has
 * held for DEBOUNCE_MS with no further byte, and only if it differs from
 * the last level that counted.
 */
#include <fcntl.h>
#include <stdlib.h>
#include <syslog.h>
#include <unistd.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include "karotz-common.h"
#include "karotz-messages.h"

#define LONG_PRESS_MS 450
#define MULTI_CLICK_MS 300
/* Bounces recorded on the rabbit are 7-28 ms apart; deliberate presses and
 * the gaps between clicks are 93 ms or more. */
#define DEBOUNCE_MS 50

enum {
    SIGNAL_REGISTERED,
    SIGNAL_CLICK,
    SIGNAL_DCLICK,
    SIGNAL_TCLICK,
    SIGNAL_MCLICK,
    SIGNAL_LCLICK_START,
    SIGNAL_LCLICK_STOP,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    int fd;
} ButtonObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} ButtonObjectClass;

GType button_object_get_type(void);
#define BUTTON_TYPE_OBJECT (button_object_get_type())

G_DEFINE_TYPE(ButtonObject, button_object, G_TYPE_OBJECT)

#include "button-daemon-glue.h"

/* What the button is doing. */
enum { STATE_PRESSED, STATE_LONG_PRESS, STATE_RELEASED };

/* Events handed to emit_event(). */
enum { EVENT_CLICKS = 2, EVENT_LONG_START = 6, EVENT_LONG_STOP = 7 };

static GSource *timer;
static guint timer_id;
static int click_count;
static ButtonObject *button;
static int state;
static GSource *settle_timer;
static gboolean level_read;     /* the latest level read: TRUE is pressed */
static gboolean level_accepted; /* the last level that held for DEBOUNCE_MS */

static void emit_event(int event)
{
    ButtonObjectClass *klass = (ButtonObjectClass *)G_OBJECT_GET_CLASS(button);

    if (event == EVENT_LONG_START) {
        g_signal_emit(button, klass->signals[SIGNAL_LCLICK_START], 0);
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "long click start");
    } else if (event == EVENT_LONG_STOP) {
        g_signal_emit(button, klass->signals[SIGNAL_LCLICK_STOP], 0);
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "long click stop");
    } else if (event == EVENT_CLICKS && click_count > 0) {
        if (click_count == 1) {
            g_signal_emit(button, klass->signals[SIGNAL_CLICK], 0);
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "click");
        } else if (click_count == 2) {
            g_signal_emit(button, klass->signals[SIGNAL_DCLICK], 0);
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "double click");
        } else if (click_count == 3) {
            g_signal_emit(button, klass->signals[SIGNAL_TCLICK], 0);
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "triple click");
        } else {
            g_signal_emit(button, klass->signals[SIGNAL_MCLICK], 0, click_count);
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "multiple click: %d", click_count);
        }
    }
}

static gboolean timeout(gpointer data)
{
    (void)data;
    if (state == STATE_PRESSED) {
        /* Held long enough: report earlier clicks, then the long press. */
        emit_event(EVENT_CLICKS);
        click_count = 0;
        emit_event(EVENT_LONG_START);
        state = STATE_LONG_PRESS;
    } else if (state == STATE_RELEASED) {
        /* No further press within the multi-click window. */
        emit_event(EVENT_CLICKS);
        click_count = 0;
    } else {
        g_log(NULL, G_LOG_LEVEL_WARNING, "WARNING: timeout(). Should not be here (state %d)\n", state);
    }
    return FALSE;
}

static void start_timer(guint ms)
{
    timer = g_timeout_source_new(ms);
    g_source_set_callback(timer, timeout, NULL, NULL);
    timer_id = g_source_attach(timer, NULL);
}

static void stop_timer(void)
{
    if (timer != NULL) {
        g_source_destroy(timer);
        timer = NULL;
    }
}

static void pressed(void)
{
    stop_timer();
    state = STATE_PRESSED;
    start_timer(LONG_PRESS_MS);
}

static void released(void)
{
    stop_timer();
    if (state == STATE_PRESSED) {
        click_count++;
        start_timer(MULTI_CLICK_MS);
    } else if (state == STATE_LONG_PRESS) {
        emit_event(EVENT_LONG_STOP);
    } else {
        g_log(NULL, G_LOG_LEVEL_WARNING, "WARNING: released(). Should not be here\n");
    }
    state = STATE_RELEASED;
}

/* The level has held for DEBOUNCE_MS: a change counts as a press or release. */
static gboolean settled(gpointer data)
{
    (void)data;
    settle_timer = NULL;
    if (level_read != level_accepted) {
        level_accepted = level_read;
        if (level_accepted)
            pressed();
        else
            released();
    }
    return FALSE;
}

static gboolean button_readable(GIOChannel *channel, GIOCondition condition, gpointer data)
{
    ButtonObject *self = data;
    char c[2];

    (void)channel;
    (void)condition;
    /* A failed or empty read is no event (c[0] would be uninitialised). */
    if (read(self->fd, c, 1) != 1)
        return TRUE;
    level_read = c[0] == '1';
    if (settle_timer != NULL)
        g_source_destroy(settle_timer);
    settle_timer = g_timeout_source_new(DEBOUNCE_MS);
    g_source_set_callback(settle_timer, settled, NULL, NULL);
    g_source_attach(settle_timer, NULL);
    g_source_unref(settle_timer);
    return TRUE;
}

static void button_object_class_init(ButtonObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_CLICK] = g_signal_new("click", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_DCLICK] = g_signal_new("dclick", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                 g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_TCLICK] = g_signal_new("tclick", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                 g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_MCLICK] = g_signal_new("mclick", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                 g_cclosure_marshal_VOID__INT, G_TYPE_NONE, 1, G_TYPE_INT);
    klass->signals[SIGNAL_LCLICK_START] = g_signal_new("lclick_start", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                       g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_LCLICK_STOP] = g_signal_new("lclick_stop", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                      g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    g_return_if_fail(klass != NULL);
}

static void button_object_init(ButtonObject *self)
{
    GIOChannel *channel;
    char c[2];

    dbus_g_object_type_install_info(BUTTON_TYPE_OBJECT, &dbus_glib_button_object_object_info);
    self->fd = open("/dev/rabbit_button", O_RDWR);
    if (self->fd < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "/dev/rabbit_button hasn't been opened");
        abort();
    }
    /* Discard the driver's initial state. */
    if (read(self->fd, c, 1) < 0) {
        /* Only to discard the driver's initial state: nothing to check. */
    }
    channel = g_io_channel_unix_new(self->fd);
    if (channel == NULL)
        g_log(NULL, G_LOG_LEVEL_WARNING, "/dev/rabbit_button IO Channel hasn't been opened");
    g_io_add_watch(channel, G_IO_IN, button_readable, self);
    g_return_if_fail(self != NULL);
}

int main(void)
{
    int log_priority = LOG_NOTICE;
    GError *error = NULL;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    GMainLoop *loop;
    guint request_result;

    init_karotz_log("Button", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");

    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL)
        die("Failed to open connection to bus", error);
    else
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Successfully connected to %s bus\n", "SYSTEM");

    button = g_object_new(BUTTON_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Button", G_OBJECT(button));

    bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, "com.mindscape.karotz.Button", 0, &request_result, &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }

    state = STATE_RELEASED;
    click_count = 0;
    timer = NULL;
    g_main_loop_run(loop);
    return 0;
}
