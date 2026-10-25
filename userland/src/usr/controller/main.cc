// SPDX-License-Identifier: MIT
/*
 * controller: the Karotz's central brain, com.mindscape.karotz.Controller
 * (/com/mindscape/karotz/Controller).
 *
 * It loads /usr/etc/conf/karotz.conf and the language file, tracks the
 * Karotz's status (published as karotz_status_changed), connects to the
 * platform and handles its protobuf messages, listens to the other
 * daemons' D-Bus signals and drives them, and runs the applications in
 * /usr/karotz/bin/karotzVM. D-Bus methods: get_status() -> (karotz, network,
 * platform), take_control() -> (success, interactive_id) and
 * release_control(interactive_id).
 */
#include <libintl.h>
#include <locale.h>
#include <signal.h>
#include <stdlib.h>
#include <syslog.h>

#include <dbus/dbus-glib-bindings.h>
#include <glib-unix.h>
#include <google/protobuf/stubs/common.h>

#include "controller.h"

/* SIGSEGV: stop the VM and exit, at once (a crash can't wait for the main loop). */
static void handle_kill(int signum)
{
    (void)signum;
    g_log(NULL, G_LOG_LEVEL_CRITICAL, "handKill");
    vm_kill();
    exit(0);
}

/* SIGINT and SIGTERM, in the main loop (g_unix_signal_add): stop the VM and exit. */
static gboolean handle_kill_signal(gpointer data)
{
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "handKill");
    vm_kill();
    exit(0);
    return G_SOURCE_REMOVE;
}

/* The VM's end is a GLib child watch of its own (vm.cc), so there is no SIGCHLD handler. */
static void install_signal_handlers(void)
{
    g_unix_signal_add(SIGINT, handle_kill_signal, NULL);
    g_unix_signal_add(SIGTERM, handle_kill_signal, NULL);
    signal(SIGSEGV, handle_kill);
}

int main(void)
{
    static int log_priority;
    GError *error = NULL;
    DBusGConnection *bus;
    GMainLoop *loop;
    guint request_result;

    GOOGLE_PROTOBUF_VERIFY_VERSION;
    log_priority = getenv("KAROTZ_DEBUG") == NULL ? LOG_NOTICE : LOG_DEBUG;
    init_karotz_log("Controller", &log_priority);
    loop = g_main_loop_new(NULL, FALSE);
    install_signal_handlers();
    g_type_init();
    setlocale(LC_ALL, "");
    bindtextdomain("karotz", "/usr/share/locale");
    textdomain("karotz");
    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);

    global_controller = (ControllerObject *)g_object_new(CONTROLLER_TYPE_OBJECT, NULL);
    init_status_network_listeners(global_controller);
    init_status_platform_listeners(global_controller);
    init_status_karotz_listeners(global_controller);
    init_dbus_listeners();

    if (bus == NULL)
        g_error("D-Bus - Failed to connect: %s", error->message);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Controller", G_OBJECT(global_controller));
    global_controller->bus_proxy =
        dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(global_controller->bus_proxy, "com.mindscape.karotz.Controller", 0,
                                           &request_result, &error)) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "D-Bus - Failed to get name: %s", error->message);
        exit(-1);
    }
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "Error - D-Bus - Got result code %u from requesting name", request_result);
        exit(-1);
    }

    g_signal_emit(global_controller,
                  ((ControllerObjectClass *)G_OBJECT_GET_CLASS(global_controller))->signals[SIGNAL_REGISTERED], 0);
    /* The network is assumed to be up (nothing watches for network changes). */
    controller_set_status(global_controller, E_STATUS_NETWORK_ONLINE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Controller started");
    g_main_loop_run(loop);
    g_main_loop_unref(loop);
    closelog();
    return 0;
}
