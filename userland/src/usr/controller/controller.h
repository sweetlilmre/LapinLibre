/* SPDX-License-Identifier: MIT */
/*
 * controller: the Karotz's central brain, com.mindscape.karotz.Controller
 * (/com/mindscape/karotz/Controller).
 *
 * Declarations shared by the controller's modules, one file per area.
 */
#ifndef CONTROLLER_H
#define CONTROLLER_H

#include <string>
#include <sys/types.h>
#include <time.h>

#include <dbus/dbus-glib.h>
#include <gio/gio.h>
#include <glib-object.h>

#include "platform-message.pb.h"

extern "C" {
#include "karotz-common.h"
#include "karotz-messages.h"
}
#include "karotz-marshal.h" /* the daemons' signal marshallers (glib-genmarshal) */

namespace pm = lapinlibre::platform;

/*
 * The four status values the controller tracks. Each lives in its own range,
 * so one set of listeners can be told about every change and pick out the
 * values it cares about. Values missing here are never used.
 */
enum {
    E_STATUS_NETWORK_ONLINE = 2,
    E_STATUS_NETWORK_OFFLINE = 3,

    E_STATUS_PLATFORM_ONLINE = 6,
    E_STATUS_PLATFORM_OFFLINE = 7,

    E_STATUS_SLEEP_FALSE = 10,
    E_STATUS_SLEEP_TRUE = 11,

    E_STATUS_KAROTZ_CONNECTING = 14,
    E_STATUS_KAROTZ_CONNECTED = 15,
    E_STATUS_KAROTZ_AVAILABLE = 16,
    E_STATUS_KAROTZ_UPDATING_CONF = 20,
    E_STATUS_KAROTZ_INTERACTIVE = 21,
    E_STATUS_KAROTZ_INTERACTIVE_STOP = 22,
    E_STATUS_KAROTZ_DISCONNECTED = 23,
    E_STATUS_KAROTZ_SLEEP = 24,
    E_STATUS_KAROTZ_WAKEUP = 25,
    E_STATUS_KAROTZ_LAST = 26,
};

/* The platform's default port, also the port of the controller's own server. */
#define DEFAULT_PORT_SERVER 9123
#define PORT_MAX 0xfffe /* the highest port_server accepted */

/* LED timings in milliseconds. */
#define PULSE_PERIOD 1600      /* the slow status pulse */
#define SHORT_PULSE_PERIOD 500 /* tag and video feedback */
#define SHORT_PULSE_LENGTH 500
#define FADE_PERIOD 100

/* Interactive mode: the longest (and default) idle ttl in seconds, and how often it is checked. */
#define INTERACTIVE_TTL_MAX 900
#define INTERACTIVE_CHECK_MS 10000

typedef struct {
    GObject parent;

    GKeyFile *karotz_conf; /* /usr/etc/conf/karotz.conf, group "karotz" */
    GKeyFile *account_conf;   /* the platform account, group "account" */
    GKeyFile *lang_conf;   /* /usr/karotz/res/lang/<lang>, group "lang" */
    DBusGConnection *bus;
    GList *status_listeners; /* StatusListener, told about every status change */

    struct {
        int karotz;
        int network;
        int platform;
        int sleep;
    } status;

    char interactive_id[256];       /* the application in control, "" if none */
    time_t interactive_last_event;  /* last message for the interactive application */
    char *connection_id;            /* identifies the current platform connection */
    gboolean platform_authenticated;
    gboolean config_pending;  /* a configuration arrived while busy; load it when available */
    gboolean pending_message; /* the platform holds unread messages (blue LED) */
    gboolean play_sounds;     /* /etc/conf/playsounds exists */
    gboolean sleeping;
    guint rfid_asr_timer;     /* while set, RFID and ASR don't start applications */
    int platform_socket;      /* -1 when not connected */
    guint platform_watch;     /* the idle source polling platform_socket, 0 if none */
    int interactive_socket;
    time_t platform_last_message;
    time_t last_connect_attempt;
    int connect_attempts;
    GSocketService *server;

    DBusGProxy *bus_proxy;
    DBusGProxy *button;
    DBusGProxy *ears;
    DBusGProxy *led;
    DBusGProxy *voice;
    DBusGProxy *multimedia;
    DBusGProxy *rfid;
    DBusGProxy *webcam;
    DBusGProxy *choreography;
    DBusGProxy *scheduler;
    DBusGProxy *k2k;
    DBusGProxy *serial;
    DBusGProxy *dock;

    pm::Config *config;        /* the applications, from the platform */
    pm::ContactList *contacts; /* from the platform, handed back on request */
} ControllerObject;

enum { SIGNAL_REGISTERED, SIGNAL_KAROTZ_STATUS_CHANGED, N_SIGNALS };

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} ControllerObjectClass;

typedef void (*StatusListener)(ControllerObject *controller, int status);

extern "C" GType controller_object_get_type(void);
#define CONTROLLER_TYPE_OBJECT (controller_object_get_type())

/* The one controller; the D-Bus signal handlers reach it through this. */
extern ControllerObject *global_controller;

/* controller_object.cc */
void controller_add_status_listener(ControllerObject *controller, StatusListener listener);
void controller_set_status(ControllerObject *controller, int status);
char *controller_get_config(ControllerObject *controller, const char *key, const char *fallback);
void controller_set_config(ControllerObject *controller, const char *key, const char *value);
char *controller_get_account(ControllerObject *controller, const char *key, const char *fallback);
char *controller_language(ControllerObject *controller);
gboolean controller_get_lang_value(ControllerObject *controller, const char *key, char **value);
gboolean controller_load_account(ControllerObject *controller);
gboolean controller_load_lang(ControllerObject *controller, const char *lang);
gboolean controller_is_interactive_id(ControllerObject *controller, const char *id);
gboolean controller_interactive_mode_start(ControllerObject *controller, const char *id, int socket);
gboolean controller_interactive_mode_stop(ControllerObject *controller, const char *id);
void controller_clear_interactive_id(ControllerObject *controller);
int controller_socket(ControllerObject *controller);
void controller_reset(ControllerObject *controller);
void controller_run_app(ControllerObject *controller, const char *api_key, const char *trigger,
                        const char *instance, const pm::ConfigApp *app);
const pm::ConfigApp *controller_search_rfid(ControllerObject *controller, const char *tag, std::string &instance);
const pm::ConfigApp *controller_find_app(ControllerObject *controller, const char *api_key);
gboolean reenable_rfid_asr(gpointer data);

/* vm.cc: the JavaScript VM running the applications. */
void vm_launch(void);
pid_t vm_pid(void);
void vm_unwatch(void);
int vm_send(const char *command);
/* A command line's fields: a word may not be empty or hold
 * whitespace or control characters; vm_word() replaces those with '_'. */
#define VM_LINE_MAX 0xffe /* the VM's fgets reads 0xfff - 1 characters */
gboolean vm_safe_word(const char *s);
std::string vm_word(const char *s);
void vm_kill(void);

/* update.cc */
gboolean controller_check_update(ControllerObject *controller);

/* status_listeners.cc */
void init_status_karotz_listeners(ControllerObject *controller);
void init_status_network_listeners(ControllerObject *controller);
void init_status_platform_listeners(ControllerObject *controller);

/* dbus_listeners.cc */
void init_dbus_listeners(void);

/* platform.cc: talking to the platform. */
int platform_create_socket(void);
gboolean platform_connect(ControllerObject *controller);
void platform_close(ControllerObject *controller);
gboolean connect_server(int sock, const char *host, uint16_t port);
GSocketService *platform_bind_server(int port);
gboolean platform_watch_socket(gpointer data);
void platform_send(int sock, const pm::PlatformMessage *msg);
gboolean platform_receive_frame(int sock, std::string *bytes);
pm::PlatformMessage *new_platform_message(void);
pm::PlatformMessage *new_response(const pm::PlatformMessage *request, pm::ReturnCode code, const char *description);
void generate_lower_uuid(char *out);

/* platform_listeners.cc */
void platform_dispatch(ControllerObject *controller, int sock, pm::PlatformMessage *msg);

/* apps.cc */
void controller_load_configuration(ControllerObject *controller);

/* scheduler.cc */
const std::string *scheduler_find(ControllerObject *controller, const char *uuid);
void scheduler_send_event(const std::string *entry, const char *uuid);
void scheduler_remove(ControllerObject *controller, const char *uuid);
char *scheduler_add_app(ControllerObject *controller, const pm::ScheduleConf::ScheduleApp &app);

/* karotz_info.cc */
void karotz_info_fill(pm::PlatformMessage *msg);
void get_mac_address(char **mac);
void get_ip_address(char **ip);
void get_flash_usage(int *used, int *size);

/* webcam.cc: snapshots taken by the controller itself. */
int webcam_take_snapshot(void **jpeg, unsigned long *size);

/* md5.cc */
void md5_hex(const char *text, char hex[33]);

#endif
