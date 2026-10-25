// SPDX-License-Identifier: MIT
/*
 * serial-daemon: serial ports over D-Bus, as com.mindscape.karotz.Serial
 * (/com/mindscape/karotz/Serial).
 *
 * open(port, baud) opens a serial port (8N1, no flow control) and keeps it in
 * a table by name; write(port, data, base) sends text as is (base "a...") or
 * hex-decoded (any other base); close(port) forgets a port, or all of them
 * for an empty name. Every port is polled from an idle callback and each
 * received line (up to '\n', not included) is signalled as readline(port,
 * line), as text or as upper-case hex digits depending on readlinemode(port,
 * base). /dev/ttyGS0 is the USB gadget serial port, which the rabbit's
 * miniusb_read.sh script may be holding; it is killed first.
 *
 * A C++ program built on SerialStream (see serial-stream.h).
 */
#include <map>
#include <string>

#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>
#include "karotz-marshal.h"

#include "serial-stream.h"

enum {
    SIGNAL_REGISTERED,
    SIGNAL_READLINE,
    N_SIGNALS
};

typedef struct {
    GObject parent;
} SerialObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} SerialObjectClass;

extern "C" {
GType serial_object_get_type(void);
gboolean serial_object_ping(SerialObject *self, gboolean *result, GError **error);
gboolean serial_object_open(SerialObject *self, const char *port, gint baud, gboolean *result, GError **error);
gboolean serial_object_close(SerialObject *self, const char *port, gboolean *result, GError **error);
gboolean serial_object_write(SerialObject *self, const char *port, const char *data, const char *base,
                             gboolean *result, GError **error);
gboolean serial_object_readlinemode(SerialObject *self, const char *port, const char *base, gboolean *result,
                                    GError **error);
}

#define SERIAL_TYPE_OBJECT (serial_object_get_type())

G_DEFINE_TYPE(SerialObject, serial_object, G_TYPE_OBJECT)

#include "serial-daemon-glue.h"

/* An open port: its stream, how received bytes are reported ('a' for text,
 * anything else for hex) and the line received so far. */
struct Port {
    explicit Port(SerialStream *stream) : mode('a'), stream(stream) {}
    ~Port() { delete stream; }

    char mode;
    SerialStream *stream;
    std::string line;

private:
    Port(const Port &);
    Port &operator=(const Port &);
};

typedef std::map<std::string, Port *> PortTable;

static PortTable ports;

/* Two upper-case hex digits for a byte (not NUL-terminated). */
static char *byte_to_hex(unsigned char byte, char *hex)
{
    unsigned char nibble = byte & 0xf;

    hex[1] = nibble < 10 ? '0' + nibble : 'A' + nibble - 10;
    nibble = byte >> 4;
    hex[0] = nibble < 10 ? '0' + nibble : 'A' + nibble - 10;
    return hex;
}

/* The value of a hex digit; anything else counts as 0. */
static char hex_digit_value(unsigned char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    return 0;
}

/* Idle callback: drain every port and signal each complete line. It runs
 * whenever the main loop is idle, sleeping 5 ms per round. */
static gboolean read_ports(gpointer data)
{
    SerialObject *self = (SerialObject *)data;
    char hex[2];
    char c = 0; /* a failing get() leaves the previous byte here; the loop stops before using it */

    for (PortTable::iterator it = ports.begin(); it != ports.end(); ++it) {
        Port *port = it->second;

        /* On a stream gone bad (after a failed write) get() fails while the
         * byte stays available, so this loop would spin for good: the state
         * is cleared first, and if get() still fails the port is left for
         * the next round. No test reaches this path. */
        while (port->stream->in_avail() > 0) {
            if (!port->stream->good())
                port->stream->clear();
            port->stream->get(c);
            if (!port->stream->good())
                break;
            if (c == '\n') {
                SerialObjectClass *klass = (SerialObjectClass *)G_OBJECT_GET_CLASS(self);
                g_signal_emit(self, klass->signals[SIGNAL_READLINE], 0, it->first.c_str(), port->line.c_str());
                port->line.clear();
            } else if (port->mode == 'a') {
                port->line += c;
            } else {
                port->line.append(byte_to_hex(c, hex), 2);
            }
        }
    }
    usleep(5000);
    return TRUE;
}

gboolean serial_object_readlinemode(SerialObject *self, const char *port, const char *base, gboolean *result,
                                    GError **error)
{
    PortTable::iterator it = ports.find(port);

    (void)self;
    (void)error;
    if (it != ports.end()) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "readlinemode %s", base);
        it->second->mode = base[0];
        *result = TRUE;
    } else {
        *result = FALSE;
    }
    return TRUE;
}

gboolean serial_object_write(SerialObject *self, const char *port, const char *data, const char *base,
                             gboolean *result, GError **error)
{
    PortTable::iterator it = ports.find(port);

    (void)self;
    (void)error;
    if (it != ports.end()) {
        if (base[0] == 'a') {
            *it->second->stream << data;
        } else {
            /* Pairs of hex digits; a trailing odd digit is dropped. */
            std::string bytes;
            for (int i = 0; data[i] != '\0' && data[i + 1] != '\0'; i += 2)
                bytes += (char)(hex_digit_value(data[i]) * 16 + hex_digit_value(data[i + 1]));
            it->second->stream->write(bytes.c_str(), bytes.length());
        }
        *result = TRUE; /* even when the write failed */
    } else {
        *result = FALSE;
    }
    return TRUE;
}

/* close(port): forget one port, or every port for an empty name. */
gboolean serial_object_close(SerialObject *self, const char *port, gboolean *result, GError **error)
{
    /* Every call replies normally, and *result says whether a port was
     * closed (all of them: TRUE; an unknown one: FALSE). */
    (void)self;
    (void)error;
    if (port == NULL || port[0] == '\0') {
        for (PortTable::iterator it = ports.begin(); it != ports.end(); ++it)
            delete it->second;
        ports.clear();
        *result = TRUE;
        return TRUE;
    }
    PortTable::iterator it = ports.find(port);
    if (it == ports.end()) {
        *result = FALSE;
        return TRUE;
    }
    delete it->second;
    ports.erase(it);
    *result = TRUE;
    return TRUE;
}

/* The termios speed for a baud rate; unknown rates mean 9600. */
static speed_t baud_to_speed(gint baud)
{
    switch (baud) {
    case 50: return B50;
    case 75: return B75;
    case 110: return B110;
    case 134: return B134;
    case 150: return B150;
    case 200: return B200;
    case 300: return B300;
    case 600: return B600;
    case 1200: return B1200;
    case 1800: return B1800;
    case 2400: return B2400;
    case 4800: return B4800;
    case 9600: return B9600;
    case 19200: return B19200;
    case 38400: return B38400;
    case 57600: return B57600;
    case 115200: return B115200;
    case 230400: return B230400;
    case 460800: return B460800;
    default: return B9600;
    }
}

/* open(port, baud): 8 data bits, no parity, 1 stop bit, no flow control.
 * 230400 and 460800 fail: SerialStream accepts rates up to 115200 only. */
gboolean serial_object_open(SerialObject *self, const char *port, gint baud, gboolean *result, GError **error)
{
    std::string name(port);
    speed_t speed = baud_to_speed(baud);
    SerialStream *stream;

    (void)self;
    (void)error;
    if (strcmp("/dev/ttyGS0", port) == 0 && system("pidof miniusb_read.sh") == 0) {
        if (system("killall miniusb_read.sh") != 0) {
            /* killall fails when none is running: nothing to do */
        }
        sleep(1);
    }
    stream = new SerialStream(std::string(port), speed, serial::CHAR_SIZE_8, serial::PARITY_NONE, 1,
                              serial::FLOW_CONTROL_NONE);
    if (stream->good()) {
        /* A reopened port's old stream is closed before its entry is
         * replaced, so its descriptor does not stay open. */
        PortTable::iterator old = ports.find(name);
        if (old != ports.end())
            delete old->second;
        ports[name] = new Port(stream);
        *result = TRUE;
    } else {
        delete stream;
        *result = FALSE;
    }
    return TRUE;
}

gboolean serial_object_ping(SerialObject *self, gboolean *result, GError **error)
{
    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "pong");
    *result = TRUE;
    return TRUE;
}

static void serial_object_class_init(SerialObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    klass->signals[SIGNAL_READLINE] =
        g_signal_new("readline", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL, karotz_smarshal_VOID__STRING_STRING, G_TYPE_NONE,
                     2, G_TYPE_STRING, G_TYPE_STRING);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_LAST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__INT, G_TYPE_NONE, 0);
    g_return_if_fail(klass != NULL);
}

static void serial_object_init(SerialObject *self)
{
    dbus_g_object_type_install_info(SERIAL_TYPE_OBJECT, &dbus_glib_serial_object_object_info);
    g_return_if_fail(self != NULL);
}

int main(void)
{
    GError *error = NULL;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    GMainLoop *loop;
    SerialObject *serial;
    guint request_result;

    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Loading SERIAL MODULE\n");

    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to open connection to bus");
        exit(1);
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Successfully connected to %s bus\n", "SYSTEM");

    serial = (SerialObject *)g_object_new(SERIAL_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Serial", G_OBJECT(serial));

    bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, "com.mindscape.karotz.Serial", 0, &request_result, &error)) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to get name");
        exit(1);
    }
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }

    g_signal_emit(serial, ((SerialObjectClass *)G_OBJECT_GET_CLASS(serial))->signals[SIGNAL_REGISTERED], 0);
    g_idle_add(read_ports, serial);
    g_main_loop_run(loop);
    return 0;
}
