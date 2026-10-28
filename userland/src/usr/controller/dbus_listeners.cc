// SPDX-License-Identifier: MIT
/*
 * The other daemons' D-Bus signals. Once the platform has authenticated the
 * Karotz, most of them are forwarded to it (or to the interactive
 * application) as platform callbacks; some also act locally: the button resets
 * the Karotz or starts speech recognition, RFID tags and recognised words
 * start applications, schedules wake the Karotz up or put it to sleep.
 */
#include <signal.h>
#include <sstream>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "controller.h"

/* A callback message, tagged with the interactive application if any. */
static pm::PlatformMessage *new_callback(void)
{
    pm::PlatformMessage *msg = new_platform_message();

    if (global_controller->status.karotz == E_STATUS_KAROTZ_INTERACTIVE)
        msg->set_interactiveid(global_controller->interactive_id);
    return msg;
}

static void send_callback(pm::PlatformMessage *msg)
{
    platform_send(controller_socket(global_controller), msg);
    delete msg;
}

/*
 * An Event carrying a daemon's (uuid, code) event signal, correlated to
 * the request it ends. A request without an id (the controller's own: the
 * reset's LED and ears, its sounds) has nobody waiting for its end, so its
 * event isn't forwarded: no client could match it to anything (karotzVM
 * logged "NO CORRELATION FOUND"; other clients drop uncorrelated Events).
 */
static void send_event(const char *uuid, int code)
{
    if (uuid == NULL || uuid[0] == '\0') {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "event %d for a request without an id: not forwarded", code);
        return;
    }
    pm::PlatformMessage *msg = new_callback();

    msg->set_correlationid(uuid);
    msg->mutable_event()->set_code((pm::ReturnCode)code);
    send_callback(msg);
}

static void pulse_available(void)
{
    if (!global_controller->pending_message)
        ledPulse(global_controller->led, NULL, LED_GREEN, LED_OFF, PULSE_PERIOD, LED_FOREVER);
    else
        ledPulse(global_controller->led, NULL, LED_GREEN, LED_BLUE, PULSE_PERIOD, LED_FOREVER);
}

/* ---- button ------------------------------------------------------------- */

static void send_button(pm::ButtonCallback::Type type)
{
    pm::PlatformMessage *msg = new_callback();

    msg->mutable_buttoncallback()->set_type(type);
    send_callback(msg);
}

static void button_click(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received ButtonClick signal");
    if (global_controller->platform_authenticated)
        send_button(pm::ButtonCallback::SIMPLE);
}

static void button_dclick(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received ButtonDClick signal");
    if (global_controller->platform_authenticated)
        send_button(pm::ButtonCallback::DOUBLE);
}

/* Triple click: reset the Karotz, waking it up if asleep. */
static void button_tclick(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received ButtonTClick signal");
    controller_reset(global_controller);
    if (global_controller->status.karotz == E_STATUS_KAROTZ_SLEEP) {
        if (global_controller->play_sounds == TRUE)
            multimediaPlay(global_controller->multimedia, NULL, "/usr/karotz/sounds/wakeup.mp3", 1);
        sleep(3);
        global_controller->sleeping = FALSE;
    }
    if (global_controller->platform_authenticated) {
        controller_set_status(global_controller, E_STATUS_KAROTZ_AVAILABLE);
        earsMove(global_controller->ears, NULL, 14, 30, 14, 30);
        if (global_controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE)
            ledPulse(global_controller->led, NULL, LED_GREEN, LED_OFF, PULSE_PERIOD, LED_FOREVER);
    }
}

/* Four clicks: log the network details. The success flag says whether the
 * address lookups gave anything; the strings are freed on every path. */
static void button_mclick(DBusGProxy *proxy, int count, gpointer data)
{
    char *ip, *mac;
    int used = 0, size = 0;
    gboolean ok = FALSE;

    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received ButtonMClick signal nb: %d", count);
    if (count == 4) {
        get_ip_address(&ip);
        get_mac_address(&mac);
        get_flash_usage(&used, &size);
        ok = ip != NULL && *ip != '\0' && mac != NULL && *mac != '\0';
        if (!ok) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Cannot read the IP or MAC address");
        } else {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Ip: %s", ip);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Mac address: %s", mac);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Flash used: %d, total: %d", used, size);
        }
        free(ip);
        free(mac);
    }
    if (global_controller->platform_authenticated)
        send_button(pm::ButtonCallback::MULTIPLE);
}

/* Long press: listen for an application's name (orange light). */
static void button_lclick_start(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received ButtonLClickStart signal");
    if (!global_controller->platform_authenticated)
        return;
    if (global_controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE && global_controller->rfid_asr_timer == 0) {
        multimediaStop(global_controller->multimedia, NULL, 0);
        if (asrStartFile(global_controller->voice, NULL, "/tmp/apps.abnf", 0))
            ledFade(global_controller->led, NULL, LED_ORANGE, FADE_PERIOD);
    }
    send_button(pm::ButtonCallback::LONG_START);
}

static void button_lclick_stop(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received ButtonLClickStop signal");
    if (!global_controller->platform_authenticated)
        return;
    pm::PlatformMessage *msg = new_callback();
    msg->mutable_buttoncallback()->set_type(pm::ButtonCallback::LONG_STOP);
    platform_send(controller_socket(global_controller), msg);
    if (global_controller->status.karotz != E_STATUS_KAROTZ_AVAILABLE &&
        global_controller->status.karotz != E_STATUS_KAROTZ_INTERACTIVE)
        ledLight(global_controller->led, NULL, LED_OFF);
    delete msg;
}

static void init_dbus_button_listeners(void)
{
    DBusGProxy *button = global_controller->button;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_button_listeners");
    dbus_g_proxy_add_signal(button, "click", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(button, "click", G_CALLBACK(button_click), NULL, NULL);
    dbus_g_proxy_add_signal(button, "dclick", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(button, "dclick", G_CALLBACK(button_dclick), NULL, NULL);
    dbus_g_proxy_add_signal(button, "tclick", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(button, "tclick", G_CALLBACK(button_tclick), NULL, NULL);
    dbus_g_proxy_add_signal(button, "mclick", G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(button, "mclick", G_CALLBACK(button_mclick), NULL, NULL);
    dbus_g_proxy_add_signal(button, "lclick_start", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(button, "lclick_start", G_CALLBACK(button_lclick_start), NULL, NULL);
    dbus_g_proxy_add_signal(button, "lclick_stop", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(button, "lclick_stop", G_CALLBACK(button_lclick_stop), NULL, NULL);
}

/* ---- dock ------------------------------------------------------------------- */

static void dock_status(DBusGProxy *proxy, int status, int change, gpointer data)
{
    (void)proxy;
    (void)data;
    if (global_controller->platform_authenticated) {
        pm::PlatformMessage *msg = new_callback();
        pm::Dock *dock = msg->mutable_dock();
        dock->set_status(status);
        dock->set_change(change);
        send_callback(msg);
    }
}

static void dock_event(DBusGProxy *proxy, const char *uuid, int code, gpointer data)
{
    (void)proxy;
    (void)data;
    if (global_controller->platform_authenticated)
        send_event(uuid, code);
}

/* Both signals are added and connected on the dock proxy. (No program on
 * the rabbit provides the dock service.) */
static void init_dbus_dock_listeners(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_dock_listeners");
    dbus_g_proxy_add_signal(global_controller->dock, "event", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(global_controller->dock, "event", G_CALLBACK(dock_event), NULL, NULL);
    dbus_g_proxy_add_signal(global_controller->dock, "status", G_TYPE_INT, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(global_controller->dock, "status", G_CALLBACK(dock_status), NULL, NULL);
}

/* ---- ears ------------------------------------------------------------------- */

static void send_ears(pm::EarsCallback::Type type)
{
    pm::PlatformMessage *msg = new_callback();

    msg->mutable_earscallback()->set_type(type);
    send_callback(msg);
}

static void ears_event(DBusGProxy *proxy, const char *uuid, int code, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received EarsEvent signal %s", uuid);
    if (global_controller->platform_authenticated)
        send_event(uuid, code);
}

/* A finished reset: TERMINATED, with the reported positions. */
static void ears_reset_finished(DBusGProxy *proxy, const char *uuid, int right, int left, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received ears reset_finished signal %s", uuid);
    if (global_controller->platform_authenticated) {
        pm::PlatformMessage *msg = new_callback();
        msg->mutable_event()->set_code(pm::TERMINATED);
        msg->set_correlationid(uuid);
        pm::Ears *ears = msg->mutable_ears();
        ears->set_right(right);
        ears->set_left(left);
        send_callback(msg);
    }
}

static void ears_left_blocked(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received EarsLeftBlocked signal");
    if (global_controller->platform_authenticated)
        send_ears(pm::EarsCallback::BLOCKED_LEFT);
}

static void ears_right_blocked(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received EarsRightBlocked signal");
    if (global_controller->platform_authenticated)
        send_ears(pm::EarsCallback::BLOCKED_RIGHT);
}

/* A manual ear move; one step or less doesn't count. */
static void send_ears_stop(pm::EarsCallback::Type type, int step, int length)
{
    pm::PlatformMessage *msg = new_callback();
    pm::EarsCallback *callback = msg->mutable_earscallback();

    callback->set_type(type);
    callback->set_step(step);
    callback->set_length(length);
    send_callback(msg);
}

static void ears_right_stop(DBusGProxy *proxy, int step, int length, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received EarsRightMoveStop signal step: %d, length: %d", step, length);
    if (global_controller->platform_authenticated && step > 1)
        send_ears_stop(pm::EarsCallback::STOP_RIGHT, step, length);
}

static void ears_left_stop(DBusGProxy *proxy, int step, int length, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received EarsLeftMoveStop signal step: %d length: %d", step, length);
    if (global_controller->platform_authenticated && step > 1)
        send_ears_stop(pm::EarsCallback::STOP_LEFT, step, length);
}

static void ears_right_start(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received EarsRightMoveStart signal");
    if (global_controller->platform_authenticated)
        send_ears(pm::EarsCallback::START_RIGHT);
}

static void ears_left_start(DBusGProxy *proxy, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received EarsLeftMoveStart signal");
    if (global_controller->platform_authenticated)
        send_ears(pm::EarsCallback::START_LEFT);
}

static void init_dbus_ear_listeners(void)
{
    DBusGProxy *ears = global_controller->ears;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_ear_listeners");
    dbus_g_proxy_add_signal(ears, "event", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "event", G_CALLBACK(ears_event), NULL, NULL);
    dbus_g_proxy_add_signal(ears, "move_left_start", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "move_left_start", G_CALLBACK(ears_left_start), NULL, NULL);
    dbus_g_proxy_add_signal(ears, "move_left_stop", G_TYPE_INT, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "move_left_stop", G_CALLBACK(ears_left_stop), NULL, NULL);
    dbus_g_proxy_add_signal(ears, "move_right_start", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "move_right_start", G_CALLBACK(ears_right_start), NULL, NULL);
    dbus_g_proxy_add_signal(ears, "move_right_stop", G_TYPE_INT, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "move_right_stop", G_CALLBACK(ears_right_stop), NULL, NULL);
    dbus_g_proxy_add_signal(ears, "move_left_blocked", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "move_left_blocked", G_CALLBACK(ears_left_blocked), NULL, NULL);
    dbus_g_proxy_add_signal(ears, "move_right_blocked", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "move_right_blocked", G_CALLBACK(ears_right_blocked), NULL, NULL);
    dbus_g_proxy_add_signal(ears, "reset_finished", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(ears, "reset_finished", G_CALLBACK(ears_reset_finished), NULL, NULL);
}

/* ---- k2k (Karotz to Karotz calls) ----------------------------------------- */

/*
 * The part of the first application key containing "k2k", starting there.
 * Quirk: the key is used from "k2k" onward, not whole; the result points
 * into the configuration, so it is valid until that is replaced. With no
 * configuration yet there is no k2k application. What the platform's k2k
 * application key looked like, and so why only its "k2k..." part is
 * passed to the VM, is not known (an application folder named by the whole
 * key would not be found from it unless the key starts with "k2k").
 */
static const char *find_k2k_app(ControllerObject *controller)
{
    if (controller->config == NULL)
        return NULL;
    unsigned n = controller->config->configapp_size();
    for (unsigned i = 0; i < n; i++) {
        const char *found = strstr(controller->config->configapp(i).apikey().c_str(), "k2k");
        if (found != NULL)
            return found;
    }
    return NULL;
}

/* An incoming call is handed to the k2k application in the VM. */
static void k2k_incoming_call(DBusGProxy *proxy, const char *from, gpointer data)
{
    char command[300];

    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "K2k tells us there is an incoming call from %s", from);
    memset(command, 0, sizeof command);
    const char *app = find_k2k_app(global_controller);
    if (app != NULL) {
        /* The key comes from the platform's configuration, so it is
         * checked before it goes into command[]. */
        if (!vm_safe_word(app))
            g_log(NULL, G_LOG_LEVEL_WARNING, "k2k application key unsafe: call not passed on");
        else if (snprintf(command, sizeof command, "%s %%recv%%\n", app) >= (int)sizeof command)
            g_log(NULL, G_LOG_LEVEL_WARNING, "k2k application key too long: call not passed on");
        else
            vm_send(command);
    }
}

static void send_k2k_event(const char *correspondant, pm::K2kCallback::Type type)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sendK2kEvent");
    if (global_controller->platform_authenticated) {
        pm::PlatformMessage *msg = new_callback();
        pm::K2kCallback *callback = msg->mutable_k2kcallback();
        callback->set_type(type);
        callback->set_correspondant(correspondant);
        send_callback(msg);
    }
}

static void k2k_finished_call(DBusGProxy *proxy, const char *with, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "K2k tells us a call with %s has finished", with);
    send_k2k_event(with, pm::K2kCallback::TERMINATED);
}

static void k2k_beginning_call(DBusGProxy *proxy, const char *with, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "K2k tells us a call with %s is beginning", with);
    send_k2k_event(with, pm::K2kCallback::BEGINNING);
}

static void init_dbus_k2k_listeners(void)
{
    DBusGProxy *k2k = global_controller->k2k;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_k2k_listeners");
    dbus_g_proxy_add_signal(k2k, "incoming_call", G_TYPE_STRING, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(k2k, "incoming_call", G_CALLBACK(k2k_incoming_call), NULL, NULL);
    dbus_g_proxy_add_signal(k2k, "beginning_call", G_TYPE_STRING, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(k2k, "beginning_call", G_CALLBACK(k2k_beginning_call), NULL, NULL);
    dbus_g_proxy_add_signal(k2k, "finished_call", G_TYPE_STRING, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(k2k, "finished_call", G_CALLBACK(k2k_finished_call), NULL, NULL);
}

/* ---- LED ---------------------------------------------------------------------- */

static void led_event(DBusGProxy *proxy, const char *uuid, int code, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received LedEvent signal %s", uuid);
    if (global_controller->platform_authenticated)
        send_event(uuid, code);
}

static void init_dbus_led_listeners(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_led_listeners");
    dbus_g_proxy_add_signal(global_controller->led, "event", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(global_controller->led, "event", G_CALLBACK(led_event), NULL, NULL);
}

/* ---- multimedia ----------------------------------------------------------------- */

/*
 * Player callbacks; action 6 (not a protocol value) instead tells every
 * running application, through the VM, under six fixed application keys.
 * Quirk: the second key is one digit short (its last group has 11 hex
 * digits), so it only reaches an application installed under that exact
 * short key.
 */
static void multimedia_callback(DBusGProxy *proxy, int action, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Received MultimediaEventCallback signal. action: %d", action);
    if (!global_controller->platform_authenticated)
        return;
    if (!pm::MultimediaCallback_TypeAction_IsValid(action)) {
        if (action == 6) {
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "Received MultimediaEventCallback action == 6");
            vm_send("93be962e-39cd-11e0-ad97-001fc63cfdff \n");
            vm_send("7ea76521-d2f3-4eb7-a9bd-87862404c31 \n");
            vm_send("8fb8075e-3dba-11e0-a802-001fc63cfdff \n");
            vm_send("9cda727b-0ab9-4673-983c-6d1898e13dfa \n");
            vm_send("dbe050ee-7fcb-4510-9a4d-23d17ed93237 \n");
            vm_send("8133aaaa-4fd6-4fee-87a3-0d468a8f8e25 \n");
        }
    } else {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Received MultimediaEventCallback TypeAction_IsValid");
        pm::PlatformMessage *msg = new_callback();
        msg->mutable_multimediacallback()->set_action((pm::MultimediaCallback::TypeAction)action);
        send_callback(msg);
    }
}

static void multimedia_play_finished(DBusGProxy *proxy, const char *uuid, int code, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received MultimediaEvent signal. uuid: %s", uuid);
    if (global_controller->platform_authenticated)
        send_event(uuid, code);
}

/* The LED fades to "", which led-daemon takes as black: off after a recording. */
static void multimedia_record_finished(DBusGProxy *proxy, const char *uuid, int code, gpointer data)
{
    (void)proxy;
    (void)data;
    if (global_controller->platform_authenticated) {
        ledFade(global_controller->led, NULL, "", FADE_PERIOD);
        send_event(uuid, code);
    }
}

static void init_dbus_multimedia_listeners(void)
{
    DBusGProxy *multimedia = global_controller->multimedia;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_multimedia_listeners");
    dbus_g_proxy_add_signal(multimedia, "multi_playfinished", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(multimedia, "multi_playfinished", G_CALLBACK(multimedia_play_finished), NULL, NULL);
    dbus_g_proxy_add_signal(multimedia, "multi_recordfinished", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(multimedia, "multi_recordfinished", G_CALLBACK(multimedia_record_finished), NULL,
                                NULL);
    dbus_g_proxy_add_signal(multimedia, "multi_callback", G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(multimedia, "multi_callback", G_CALLBACK(multimedia_callback), NULL, NULL);
}

/* ---- RFID ------------------------------------------------------------------------- */

static void rfid_tag_out(DBusGProxy *proxy, const char *id, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Received RfidTagOut signal");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received RfidTagOut signal id: %s", id);
}

/*
 * A tag on the nose. The tag listener gives every tag's feedback and asks
 * has_tag_app and start_tag_app to start an application's; here the tag is
 * only passed on: to an interactive application, or to the platform, unless
 * a hosted application's instance has the tag (start_tag_app starts it).
 */
static void rfid_tag_in(DBusGProxy *proxy, const char *id, const char *content, int app_id, int type, int pict,
                        int color, gpointer data)
{
    gboolean handled = FALSE;

    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Received RfidTagIn signal");
    if (!global_controller->platform_authenticated) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "controllerRfidTagIn !global_controllerObject->platform_authenticated");
        return;
    }
    if (global_controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE) {
        std::string instance("");
        const pm::ConfigApp *app = controller_search_rfid(global_controller, id, instance);
        handled = app != NULL && strcmp(app->deployment().c_str(), "hosted") == 0;
    }

    pm::PlatformMessage *msg = new_callback();
    pm::RfidCallback *callback = msg->mutable_rfidcallback();
    callback->set_id(id);
    callback->set_content(content);
    callback->set_direction(pm::RfidCallback::IN);
    callback->set_app(app_id);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received controllerRfidTagIn signal. tag_type: %d", type);
    if (!pm::RfidCallback_Type_IsValid(type)) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Received controllerRfidTagIn signal. tag_type: NOT VALID");
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Received controllerRfidTagIn signal. tag_type: VALID");
        callback->set_type((pm::RfidCallback::Type)type);
    }
    callback->set_pict(pict);
    if (pm::RfidCallback_Col_IsValid(color))
        callback->set_color((pm::RfidCallback::Col)color);
    if (!handled)
        platform_send(controller_socket(global_controller), msg);
    delete msg;
}

static void init_dbus_rfid_listeners(void)
{
    DBusGProxy *rfid = global_controller->rfid;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_rfid_listeners");
    dbus_g_proxy_add_signal(rfid, "tag_in", G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INT, G_TYPE_INT, G_TYPE_INT,
                            G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(rfid, "tag_in", G_CALLBACK(rfid_tag_in), NULL, NULL);
    dbus_g_proxy_add_signal(rfid, "tag_out", G_TYPE_STRING, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(rfid, "tag_out", G_CALLBACK(rfid_tag_out), NULL, NULL);
}

/* ---- scheduler ------------------------------------------------------------------ */

/*
 * A schedule fired. The controller's own schedules start their application;
 * one-shot ones (periodic == 0) are then forgotten. The wake-up schedule
 * makes the controller exit (to be restarted); the sleep one puts the Karotz
 * to sleep; anything else goes to the platform.
 */
static void scheduler_event(DBusGProxy *proxy, const char *uuid, int periodic, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received SchedulerEvent signal %s", uuid);
    const std::string *entry = scheduler_find(global_controller, uuid);
    if (entry != NULL) {
        scheduler_send_event(entry, uuid);
        if (periodic == 0)
            scheduler_remove(global_controller, uuid);
        return;
    }
    if (strcmp(uuid, "E_STATUS_KAROTZ_WAKEUP") == 0) {
        if (global_controller->play_sounds == TRUE)
            multimediaPlay(global_controller->multimedia, NULL, "/usr/karotz/sounds/wakeup.mp3", 1);
        sleep(3);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "EXIT");
        exit(1);
    }
    if (strcmp(uuid, "E_STATUS_KAROTZ_SLEEP") == 0) {
        controller_set_status(global_controller, E_STATUS_KAROTZ_SLEEP);
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "send schedEvent to platform");
        pm::PlatformMessage *msg = new_platform_message();
        msg->mutable_scheduleevent()->set_uuid(uuid);
        platform_send(controller_socket(global_controller), msg);
        delete msg;
    }
}

static void init_dbus_scheduler_listeners(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_scheduler_listeners");
    dbus_g_proxy_add_signal(global_controller->scheduler, "schedule_event", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(global_controller->scheduler, "schedule_event", G_CALLBACK(scheduler_event), NULL,
                                NULL);
}

/* ---- serial ------------------------------------------------------------------ */

static void serial_readline(DBusGProxy *proxy, const char *port, const char *line, gpointer data)
{
    (void)proxy;
    (void)data;
    if (global_controller->platform_authenticated) {
        pm::PlatformMessage *msg = new_callback();
        pm::SerialCallback::Readline *readline = msg->mutable_serialcallback()->mutable_readline();
        readline->set_port(port);
        readline->set_data(line);
        send_callback(msg);
    }
}

static void init_dbus_serial_listeners(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_serial_listeners");
    dbus_g_proxy_add_signal(global_controller->serial, "readline", G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(global_controller->serial, "readline", G_CALLBACK(serial_readline), NULL, NULL);
}

/* ---- voice ------------------------------------------------------------------ */

static void voice_tts_event(DBusGProxy *proxy, const char *uuid, int code, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received VoiceTtsEvent signal");
    if (global_controller->platform_authenticated)
        send_event(uuid, code);
}

/* The text between the '>' following `tag` and the next '<', or NULL. */
static GString *extract_tag(const char *text, const char *tag)
{
    const char *start = g_strstr_len(text, -1, tag);

    if (start == NULL)
        return NULL;
    start = g_strstr_len(start, -1, ">");
    if (start == NULL)
        return NULL;
    const char *end = g_strstr_len(start, -1, "<");
    if (end == NULL)
        return NULL;
    gssize length = end - (start + 1);
    GString *value = g_string_sized_new(length + 1);
    g_string_append_len(value, start + 1, length);
    g_string_append_c(value, '\0');
    return value;
}

/* Play the language file's sound `key` (or en-US's, see
 * controller_get_lang_value()); nothing, and a warning, if neither has it. */
static void play_lang_sound(const char *key)
{
    char *sound = NULL;

    if (controller_get_lang_value(global_controller, key, &sound))
        multimediaPlay(global_controller->multimedia, NULL, sound, 1);
    else
        g_log(NULL, G_LOG_LEVEL_WARNING, "no sound for %s in the language files", key);
    free(sound);
}

/*
 * A recognition result (the long button press). When available, failures
 * play a sound; an <app> in the semantic part, scored above asr_threshold,
 * starts that application with the <par> instance. The platform always gets
 * the recognition.
 */
static void voice_asr_event(DBusGProxy *proxy, const char *uuid, const char *text, const char *semantic, int score,
                            gpointer data)
{
    char threshold_default[4] = { 0 };
    char *lang = NULL;
    gboolean done = FALSE;

    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received VoiceAsrEvent signal");
    if (global_controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE) {
        pulse_available();
        sprintf(threshold_default, "%d", 30);
        char *threshold_text = controller_get_config(global_controller, "asr_threshold", threshold_default);
        int threshold = atoi(threshold_text);
        lang = controller_get_config(global_controller, "lang", "en-US");
        if (g_strstr_len(text, -1, "<nomatch>") != NULL || g_strstr_len(text, -1, "<noinput>") != NULL) {
            play_lang_sound("speech_no_match");
            done = TRUE;
        }
        if (g_strstr_len(text, -1, "<error_server_timeout>") != NULL) {
            /* French gets the French sound, everyone else the British one.
             * The configured value is left as it is, and the path is built
             * with a bounded snprintf(). */
            char path[150];
            const char *sound_lang = "fr-FR";
            if (strcmp(lang, "fr-FR") != 0) {
                g_log(NULL, G_LOG_LEVEL_DEBUG, "Received VoiceAsrEvent signal !=fr-FR");
                sound_lang = "en-GB";
            }
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Received VoiceAsrEvent signal lang: %s", sound_lang);
            snprintf(path, sizeof path, "/usr/karotz/sounds/speech_server_timeout_%s.mp3", sound_lang);
            multimediaPlay(global_controller->multimedia, NULL, path, 1);
            done = TRUE;
        }
        if (!done && global_controller->status.karotz != E_STATUS_KAROTZ_INTERACTIVE) {
            GString *app = extract_tag(semantic, "<app>");
            if (app == NULL) {
                play_lang_sound("speech_not_understood");
            } else {
                multimediaPlay(global_controller->multimedia, NULL, "/usr/karotz/sounds/acknowledge.mp3", 1);
                g_log(NULL, G_LOG_LEVEL_DEBUG, "ASR app: %s", app->str);
                g_log(NULL, G_LOG_LEVEL_DEBUG, "ASR score: %d/%d", score, threshold);
                if (score > threshold) {
                    GString *instance = extract_tag(semantic, "<par>");
                    if (instance != NULL) {
                        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting application: %s instance %s", app->str,
                              instance->str);
                        g_log(NULL, G_LOG_LEVEL_DEBUG, "ASR instanceName: %s", instance->str);
                        std::string trigger("ASR;");
                        trigger += text;
                        trigger += ";";
                        trigger += semantic;
                        for (unsigned i = 0; i < trigger.length(); i++) {
                            if (trigger[i] == ' ')
                                trigger[i] = '_';
                        }
                        controller_run_app(global_controller, app->str, trigger.c_str(), instance->str, NULL);
                    }
                    g_string_free(app, TRUE);
                    if (instance != NULL)
                        g_string_free(instance, TRUE);
                } else {
                    play_lang_sound("speech_not_understood");
                }
            }
        }
        free(threshold_text); /* freed on every path */
    } else {
        ledFade(global_controller->led, NULL, LED_OFF, FADE_PERIOD);
    }

    if (!global_controller->platform_authenticated) {
        g_free(lang);
        return;
    }
    pm::PlatformMessage *msg = new_platform_message();
    if (global_controller->status.karotz == E_STATUS_KAROTZ_INTERACTIVE) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Received VoiceAsrEvent signal: interactive");
        msg->set_interactiveid(global_controller->interactive_id);
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Received VoiceAsrEvent signal: not interactive");
    }
    msg->set_correlationid(uuid);
    GString *semantic_xml = g_string_new("");
    g_string_append_printf(semantic_xml, "<semantic>");
    g_string_append_printf(semantic_xml, "%s", semantic);
    g_string_append_printf(semantic_xml, "</semantic>");
    pm::AsrCallback::Recognition *recognition = msg->mutable_asrcallback()->add_recognition();
    recognition->set_text(text);
    recognition->set_confident(score);
    recognition->set_semantic(semantic_xml->str);
    platform_send(controller_socket(global_controller), msg);
    delete msg;
    if (lang != NULL)
        g_free(lang);
    g_string_free(semantic_xml, TRUE);
}

static void init_dbus_voice_listeners(void)
{
    DBusGProxy *voice = global_controller->voice;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_voice_listeners");
    dbus_g_proxy_add_signal(voice, "tts_event", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(voice, "tts_event", G_CALLBACK(voice_tts_event), NULL, NULL);
    dbus_g_proxy_add_signal(voice, "asr_event", G_TYPE_STRING, G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INT,
                            G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(voice, "asr_event", G_CALLBACK(voice_asr_event), NULL, NULL);
}

/* ---- webcam ------------------------------------------------------------------ */

static void webcam_event(DBusGProxy *proxy, const char *uuid, int code, gpointer data)
{
    (void)proxy;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received controllerWebcamEvent signal. uuid: %s", uuid);
    if (global_controller->platform_authenticated)
        send_event(uuid, code);
}

static void init_dbus_webcam_listeners(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "init_dbus_webcam_listeners");
    dbus_g_proxy_add_signal(global_controller->webcam, "event", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(global_controller->webcam, "event", G_CALLBACK(webcam_event), NULL, NULL);
}

/* Marshallers for the daemons' signals. VOID__STRING_STRING_INT is never
 * registered; STRING_STRING_STRING_INT is registered twice. */
static void register_karotz_marshallers(void)
{
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__INT_INT, G_TYPE_NONE, G_TYPE_INT, G_TYPE_INT,
                                      G_TYPE_INVALID);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, G_TYPE_STRING, G_TYPE_INT,
                                      G_TYPE_INVALID);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_INT_INT, G_TYPE_NONE, G_TYPE_STRING, G_TYPE_INT,
                                      G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_STRING_INT_INT_INT_INT, G_TYPE_NONE,
                                      G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INT, G_TYPE_INT, G_TYPE_INT, G_TYPE_INT,
                                      G_TYPE_INVALID);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_STRING, G_TYPE_NONE, G_TYPE_STRING, G_TYPE_STRING,
                                      G_TYPE_INVALID);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_STRING_STRING_INT, G_TYPE_NONE, G_TYPE_STRING,
                                      G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_STRING_STRING_INT, G_TYPE_NONE, G_TYPE_STRING,
                                      G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
}

void init_dbus_listeners(void)
{
    register_karotz_marshallers();
    init_dbus_button_listeners();
    init_dbus_ear_listeners();
    init_dbus_led_listeners();
    init_dbus_rfid_listeners();
    init_dbus_voice_listeners();
    init_dbus_multimedia_listeners();
    init_dbus_webcam_listeners();
    init_dbus_scheduler_listeners();
    init_dbus_k2k_listeners();
    init_dbus_serial_listeners();
    init_dbus_dock_listeners();
}
