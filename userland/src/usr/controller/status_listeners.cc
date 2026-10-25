// SPDX-License-Identifier: MIT
/*
 * What the controller does when a status changes. Every listener is called
 * for every change and acts on its own status value.
 */
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "controller.h"

/* ---- Karotz ----------------------------------------------------------------- */

static void karotz_connecting(ControllerObject *controller, int status)
{
    (void)controller;
    if (status == E_STATUS_KAROTZ_CONNECTING)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz becomes CONNECTING");
}

static void karotz_updating_conf(ControllerObject *controller, int status)
{
    if (status == E_STATUS_KAROTZ_UPDATING_CONF) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz becomes UPDATING_CONF");
        if (controller->play_sounds == TRUE)
            multimediaPlay(global_controller->multimedia, NULL, "/usr/karotz/sounds/acknowledge.mp3", 1);
        controller_load_configuration(controller);
        controller->platform_last_message = time(NULL);
        controller_set_status(controller, E_STATUS_KAROTZ_AVAILABLE);
    }
}

static void karotz_wakeup(ControllerObject *controller, int status)
{
    if (status == E_STATUS_KAROTZ_WAKEUP) {
        if (global_controller->sleeping == TRUE) {
            if (controller->play_sounds == TRUE)
                multimediaPlay(global_controller->multimedia, NULL, "/usr/karotz/sounds/wakeup.mp3", 1);
            sleep(3);
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz becomes WAKEUP");
        }
        controller_set_status(controller, E_STATUS_SLEEP_FALSE);
        controller->sleeping = FALSE;
        controller_set_status(controller, E_STATUS_KAROTZ_AVAILABLE);
    }
}

/* Going to sleep is also when updates are looked for. */
static void karotz_sleep(ControllerObject *controller, int status)
{
    if (status == E_STATUS_KAROTZ_SLEEP) {
        if (!controller->sleeping) {
            controller_check_update(controller);
            controller_set_status(controller, E_STATUS_SLEEP_TRUE);
            controller->sleeping = TRUE;
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz becomes SLEEP");
            earsMove(controller->ears, NULL, 4, 30, 4, 30);
            multimediaStop(controller->multimedia, NULL, 0);
            ttsStop(controller->voice, NULL, 0);
            asrStop(controller->voice, NULL, 0);
        } else {
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - was already aSLEEP");
        }
        ledLight(controller->led, NULL, LED_OFF);
    }
}

static void karotz_interactive_stop(ControllerObject *controller, int status)
{
    if (status == E_STATUS_KAROTZ_INTERACTIVE_STOP) {
        controller_clear_interactive_id(controller);
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz stop interactive mode");
        controller_set_status(controller, E_STATUS_KAROTZ_AVAILABLE);
    }
}

static void karotz_interactive(ControllerObject *controller, int status)
{
    if (status == E_STATUS_KAROTZ_INTERACTIVE) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz becomes INTERACTIVE");
        ledPulse(controller->led, NULL, LED_LIGHT_GREEN, LED_OFF, PULSE_PERIOD, LED_FOREVER);
    }
}

/* Green pulse, blue when messages are waiting. */
static void pulse_available(ControllerObject *controller)
{
    if (!controller->pending_message)
        ledPulse(controller->led, NULL, LED_GREEN, LED_OFF, PULSE_PERIOD, LED_FOREVER);
    else
        ledPulse(controller->led, NULL, LED_GREEN, LED_BLUE, PULSE_PERIOD, LED_FOREVER);
}

/* Available: first load a configuration that arrived while busy. */
static void karotz_available(ControllerObject *controller, int status)
{
    if (status != E_STATUS_KAROTZ_AVAILABLE)
        return;
    if (controller->config_pending) {
        controller_set_status(controller, E_STATUS_KAROTZ_UPDATING_CONF);
    } else {
        controller_reset(controller);
        if (!controller->sleeping) {
            pulse_available(controller);
            if (!earsMove(controller->ears, NULL, 14, 30, 14, 30))
                g_log(NULL, G_LOG_LEVEL_WARNING, "earsMove returned FALSE");
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Status - Karotz becomes AVAILABLE");
    }
}

static void karotz_disconnected(ControllerObject *controller, int status)
{
    if (status == E_STATUS_KAROTZ_DISCONNECTED) {
        controller_reset(controller);
        if (!controller->sleeping) {
            ledPulse(controller->led, NULL, LED_RED, LED_OFF, PULSE_PERIOD, LED_FOREVER);
            if (!earsMove(controller->ears, NULL, 4, 30, 4, 30))
                g_log(NULL, G_LOG_LEVEL_WARNING, "earsMove returned FALSE");
        }
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz becomes DISCONNECTED");
    }
}

/* Leaving interactive mode for any other Karotz status forgets the application. */
static void karotz_clear_interactive_id(ControllerObject *controller, int status)
{
    if (status != E_STATUS_KAROTZ_INTERACTIVE && status > 13 && status <= E_STATUS_KAROTZ_LAST)
        controller_clear_interactive_id(controller);
}

void init_status_karotz_listeners(ControllerObject *controller)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_status_karotz_listeners");
    controller_add_status_listener(controller, karotz_disconnected);
    controller_add_status_listener(controller, karotz_available);
    controller_add_status_listener(controller, karotz_updating_conf);
    controller_add_status_listener(controller, karotz_connecting);
    controller_add_status_listener(controller, karotz_sleep);
    controller_add_status_listener(controller, karotz_wakeup);
    controller_add_status_listener(controller, karotz_interactive_stop);
    controller_add_status_listener(controller, karotz_interactive);
    controller_add_status_listener(controller, karotz_clear_interactive_id);
}

/* ---- network ---------------------------------------------------------------- */

/* Keeps trying (every second, throttled by platform_connect) until connected. */
static gboolean retry_connection(gpointer data)
{
    if (platform_connect(*(ControllerObject **)data)) {
        /* Allocated by network_online(). */
        delete[] (ControllerObject **)data;
        return FALSE;
    }
    return TRUE;
}

static void network_online(ControllerObject *controller, int status)
{
    if (status == E_STATUS_NETWORK_ONLINE) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Network - Online %d", E_STATUS_NETWORK_ONLINE);
        g_socket_service_start(controller->server);
        if (!platform_connect(controller)) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Network - controller_platform_connect FALSE");
            ControllerObject **data = new ControllerObject *[2]; /* freed by retry_connection() */
            data[0] = controller;
            g_timeout_add_seconds(1, retry_connection, data);
        } else {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Network - controller_platform_connect TRUE");
        }
    }
}

static void network_offline(ControllerObject *controller, int status)
{
    if (status == E_STATUS_NETWORK_OFFLINE) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Network - Offline");
        controller_set_status(controller, E_STATUS_PLATFORM_OFFLINE);
        g_socket_service_stop(controller->server);
    }
}

void init_status_network_listeners(ControllerObject *controller)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_status_network_listeners");
    controller_add_status_listener(controller, network_offline);
    controller_add_status_listener(controller, network_online);
}

/* ---- platform --------------------------------------------------------------- */

/* A ping timer, bound to one platform connection. */
typedef struct {
    ControllerObject *controller;
    char *connection_id;
    int period;
    gboolean failed_once;
} PingTimer;

static void free_ping_timer(PingTimer *ping)
{
    free(ping->connection_id);
    delete ping;
}

/*
 * Ping the platform, first after 20 s, then every 60 s, for as long as the
 * connection lasts. Between 75 s and an hour without any message from the
 * platform counts as a failed ping; the second one drops the connection.
 */
static gboolean ping_platform(gpointer data)
{
    PingTimer *ping = (PingTimer *)data;
    ControllerObject *controller = ping->controller;

    if (controller->status.platform != E_STATUS_PLATFORM_ONLINE ||
        strcmp(ping->connection_id, controller->connection_id) != 0) {
        free_ping_timer(ping);
        return FALSE;
    }
    int silence = time(NULL) - controller->platform_last_message;
    if (silence > 75 && silence < 3600) {
        if (ping->failed_once) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Platform - ping timeout");
            ping->failed_once = FALSE;
            platform_close(controller);
            controller_set_status(controller, E_STATUS_NETWORK_OFFLINE);
            controller_set_status(controller, E_STATUS_NETWORK_ONLINE);
            free_ping_timer(ping);
            return FALSE;
        }
        g_log(NULL, G_LOG_LEVEL_WARNING, "Platform - ping timeout first_fail");
        ping->failed_once = TRUE;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Platform - send ping");
    pm::PlatformMessage *msg = new_platform_message();
    msg->mutable_ping();
    platform_send(controller->platform_socket, msg);
    delete msg;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Platform - send pingend");
    if (ping->period == 20) {
        ping->period = 60;
        g_timeout_add_seconds(ping->period, ping_platform, ping);
        return FALSE;
    }
    return TRUE;
}

static void platform_online(ControllerObject *controller, int status)
{
    if (status == E_STATUS_PLATFORM_ONLINE) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Platform - Online");
        PingTimer *ping = new PingTimer;
        ping->controller = controller;
        ping->connection_id = strdup(controller->connection_id);
        ping->period = 20;
        ping->failed_once = FALSE;
        g_timeout_add_seconds(ping->period, ping_platform, ping);
        controller_set_status(controller, E_STATUS_KAROTZ_CONNECTED);
    }
}

static void platform_offline(ControllerObject *controller, int status)
{
    if (status == E_STATUS_PLATFORM_OFFLINE) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Platform - Offline");
        platform_close(controller);
        controller->platform_authenticated = FALSE;
        controller_clear_interactive_id(controller);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Platform - destroying interactiveId");
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Platform - disconnected");
        multimediaPlay(controller->multimedia, NULL, "file:///usr/karotz/sounds/notify.mp3", 1);
        controller_set_status(controller, E_STATUS_KAROTZ_DISCONNECTED);
    }
}

void init_status_platform_listeners(ControllerObject *controller)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_status_platform_listeners");
    controller_add_status_listener(controller, platform_online);
    controller_add_status_listener(controller, platform_offline);
}
