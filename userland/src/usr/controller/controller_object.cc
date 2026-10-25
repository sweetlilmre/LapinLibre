// SPDX-License-Identifier: MIT
/*
 * The ControllerObject: its D-Bus methods (get_status, take_control,
 * release_control), the configuration and language files, the status
 * values and their listeners, interactive mode and application launching.
 */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>

#include "controller.h"

#define KAROTZ_CONF "/usr/etc/conf/karotz.conf"
#define LANG_DIR "/usr/karotz/res/lang/"
#define LANG_DEFAULT LANG_DIR "en-US"

ControllerObject *global_controller;

/* The D-Bus methods; the server glue names them controller_object_<method>. */
extern "C" {
gboolean controller_get_status(ControllerObject *controller, int *karotz, int *network, int *platform, GError **error);
gboolean controller_take_control(ControllerObject *controller, gboolean *success, gchar **interactive_id,
                                 GError **error);
gboolean controller_release_control(ControllerObject *controller, const gchar *interactive_id, GError **error);
}
#define controller_object_get_status controller_get_status
#define controller_object_take_control controller_take_control
#define controller_object_release_control controller_release_control

G_DEFINE_TYPE(ControllerObject, controller_object, G_TYPE_OBJECT)

#include "controller-glue.h"

#define IS_CONTROLLEROBJ(obj) G_TYPE_CHECK_INSTANCE_TYPE((obj), CONTROLLER_TYPE_OBJECT)

/* ---- status ------------------------------------------------------------ */

typedef struct {
    ControllerObject *controller;
    int status;
} StatusChange;

static void call_listener(gpointer listener, gpointer data)
{
    StatusChange *change = (StatusChange *)data;

    ((StatusListener)listener)(change->controller, change->status);
}

void controller_add_status_listener(ControllerObject *controller, StatusListener listener)
{
    controller->status_listeners = g_list_append(controller->status_listeners, (gpointer)listener);
}

/* Every listener hears about every change, whichever status it concerns. */
static void notify_listeners(ControllerObject *controller, int status)
{
    StatusChange change = { controller, status };

    g_list_foreach(controller->status_listeners, call_listener, &change);
}

static void controller_platform_status(ControllerObject *controller, int status)
{
    if (controller->status.platform != status) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "controller_platform_status");
        controller->status.platform = status;
        notify_listeners(controller, status);
    }
}

static void controller_sleep_status(ControllerObject *controller, int status)
{
    if (controller->status.sleep != status) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "controller_sleep_status");
        controller->status.sleep = status;
        notify_listeners(controller, status);
    }
}

static void controller_network_status(ControllerObject *controller, int status)
{
    if (controller->status.network != status) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "controller_network_status");
        controller->status.network = status;
        notify_listeners(controller, status);
    }
}

/* Only the Karotz status is published, as karotz_status_changed(status). */
static void controller_karotz_status(ControllerObject *controller, int status)
{
    if (controller->status.karotz != status) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "controller_karotz_status");
        controller->status.karotz = status;
        g_signal_emit(controller, ((ControllerObjectClass *)G_OBJECT_GET_CLASS(controller))->signals[SIGNAL_KAROTZ_STATUS_CHANGED],
                      0, status);
        notify_listeners(controller, status);
    }
}

void controller_set_status(ControllerObject *controller, int status)
{
    if (status >= 2 && status <= 3)
        controller_network_status(controller, status);
    else if (status >= 6 && status <= 7)
        controller_platform_status(controller, status);
    else if (status >= 10 && status <= 11)
        controller_sleep_status(controller, status);
    else if (status > 13 && status <= E_STATUS_KAROTZ_LAST)
        controller_karotz_status(controller, status);
}

/* ---- interactive mode ---------------------------------------------------- */

gboolean controller_is_interactive_id(ControllerObject *controller, const char *id)
{
    /* "" is the "no application" state, never a session's id. */
    return id[0] != '\0' && strcmp(controller->interactive_id, id) == 0;
}

void controller_clear_interactive_id(ControllerObject *controller)
{
    controller->interactive_id[0] = '\0';
}

/* Hand the Karotz to an application; only possible while it is available. */
gboolean controller_interactive_mode_start(ControllerObject *controller, const char *id, int socket)
{
    gboolean available = controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE;

    if (available) {
        strcpy(controller->interactive_id, id);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Platform - Setting interactiveId: %s - Current sock = %d", id, socket);
        multimediaStop(controller->multimedia, NULL, 0);
        ttsStop(controller->voice, NULL, 0);
        asrStop(controller->voice, NULL, 0);
        controller->interactive_last_event = time(NULL);
        controller->interactive_socket = socket;
        controller_set_status(controller, E_STATUS_KAROTZ_INTERACTIVE);
    }
    return available;
}

gboolean controller_interactive_mode_stop(ControllerObject *controller, const char *id)
{
    gboolean current = controller_is_interactive_id(controller, id);

    if (current)
        controller_set_status(controller, E_STATUS_KAROTZ_INTERACTIVE_STOP);
    return current;
}

/* Where messages go: the interactive application's socket, or the platform's. */
int controller_socket(ControllerObject *controller)
{
    int sock;

    if (controller->status.karotz == E_STATUS_KAROTZ_INTERACTIVE) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "INTERACTIVE SOCKET = %d ", controller->interactive_socket);
        sock = controller->interactive_socket;
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "PLATFORM SOCKET = %d ", controller->platform_socket);
        sock = controller->platform_socket;
    }
    return sock;
}

/* ---- applications ----------------------------------------------------------- */

const pm::ConfigApp *controller_find_app(ControllerObject *controller, const char *api_key)
{
    if (controller->config != NULL) {
        int n = controller->config->configapp_size();
        for (int i = 0; i < n; i++) {
            if (strcmp(controller->config->configapp(i).apikey().c_str(), api_key) == 0)
                return &controller->config->configapp(i);
        }
    }
    return NULL;
}

/* RFID and ASR are ignored for a second after an application was started. */
gboolean reenable_rfid_asr(gpointer data)
{
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "reEnable Rfid and Asr");
    global_controller->rfid_asr_timer = 0;
    return FALSE;
}

/* Ask the VM to start a hosted application: "<apikey> <trigger> <instance>". */
void controller_run_app(ControllerObject *controller, const char *api_key, const char *trigger,
                        const char *instance, const pm::ConfigApp *app)
{
    global_controller->rfid_asr_timer = g_timeout_add_seconds(1, reenable_rfid_asr, NULL);
    if (app == NULL)
        app = controller_find_app(controller, api_key);
    if (app == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "apikey not found");
    } else if (strcmp(app->deployment().c_str(), "hosted") == 0) {
        /* The fields are checked before they are joined: a newline in one
         * would start another application and a space would shift the
         * arguments. The key and the instance (which may be empty) must
         * be words; the trigger, which carries an RFID tag's content or the
         * ASR text, gets its whitespace and control characters replaced. */
        if (!vm_safe_word(api_key) || (instance != NULL && *instance != '\0' && !vm_safe_word(instance))) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "unsafe application key or instance: not started");
            return;
        }
        std::string command(api_key);
        command += " ";
        command += vm_word(trigger);
        command += " ";
        command += instance != NULL ? instance : "";
        command += "\n";
        if (command.length() > VM_LINE_MAX) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "application command too long: not started");
            return;
        }
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Running %s: %s", app->name().c_str(), command.c_str());
        vm_send(command.c_str());
    }
}

/* The application with an instance configured for an RFID tag; the
 * instance's name is appended to `instance`. */
const pm::ConfigApp *controller_search_rfid(ControllerObject *controller, const char *tag, std::string &instance)
{
    if (controller->config != NULL) {
        int n = controller->config->configapp_size();
        g_log(NULL, G_LOG_LEVEL_DEBUG, "App size: %d", n);
        for (int i = 0; i < n; i++) {
            const pm::ConfigApp &app = controller->config->configapp(i);
            int instances = app.configinstance_size();
            g_log(NULL, G_LOG_LEVEL_DEBUG, "instance size: %d", instances);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "application name: %s", app.name().c_str());
            for (int j = 0; j < instances; j++) {
                const pm::ConfigInstance &config = app.configinstance(j);
                if (!config.has_rfid()) {
                    g_log(NULL, G_LOG_LEVEL_DEBUG, "controller_search_rfid: no rfid configured");
                    continue;
                }
                /* Logs the application's name. */
                g_log(NULL, G_LOG_LEVEL_DEBUG, "application name: %s", app.name().c_str());
                g_log(NULL, G_LOG_LEVEL_DEBUG, "comparing tag %s with %s", config.rfid().c_str(), tag);
                if (strcmp(config.rfid().c_str(), tag) == 0) {
                    g_log(NULL, G_LOG_LEVEL_DEBUG, "the tag matches entry %d", i);
                    instance += config.name().c_str();
                    return &app;
                }
            }
        }
    }
    return NULL;
}

/* Back to a clean state: kill the VM and everything it was doing, start a new one. */
void controller_reset(ControllerObject *controller)
{
    int status;
    pid_t pid;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Status - Karotz resetting.");
    pid_t running = vm_pid();
    if (system("killall karotzVM") != 0) {
        /* killall fails when none is running: nothing to do */
    }
    /* Only the VM: system()'s own children are system()'s to reap. Its child watch is removed
     * first, so this wait is the one that reaps it (a VM already reaped by the watch has
     * pid -1 here). */
    vm_unwatch();
    status = 0;
    pid = running > 0 ? waitpid(running, &status, 0) : -1;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "controller_reset kill: wait return: %d", pid);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "controller_reset kill: WEXITSTATUS: %d", WEXITSTATUS(status));
    earsReset(controller->ears, "", "B");
    multimediaStop(controller->multimedia, NULL, 0);
    ttsStop(controller->voice, NULL, 0);
    asrStop(global_controller->voice, NULL, 0);
    multimediaPlay(controller->multimedia, NULL, "lock::yes", 0);
    k2kHangup(controller->k2k);
    vm_launch();
}

/* ---- configuration ------------------------------------------------------- */

/* A value from karotz.conf, or a copy of `fallback` when missing or empty. */
char *controller_get_config(ControllerObject *controller, const char *key, const char *fallback)
{
    char *value = g_key_file_get_value(controller->karotz_conf, "karotz", key, NULL);

    if (value == NULL || *value == '\0') {
        if (value != NULL)
            free(value);
        value = strdup(fallback);
    }
    return value;
}

/* Change a value in karotz.conf and write the file back. */
void controller_set_config(ControllerObject *controller, const char *key, const char *value)
{
    gsize length;
    gchar *data;

    g_key_file_set_value(controller->karotz_conf, "karotz", key, value);
    data = g_key_file_to_data(controller->karotz_conf, &length, NULL);
    g_file_set_contents(KAROTZ_CONF, data, length, NULL);
    g_free(data);
}

/* A value of the platform account, or a copy of `fallback`. */
char *controller_get_account(ControllerObject *controller, const char *key, const char *fallback)
{
    char *value = g_key_file_get_value(controller->account_conf, "account", key, NULL);

    if (value == NULL || *value == '\0') {
        /* An empty value is freed before the fallback replaces it. */
        if (value != NULL)
            free(value);
        value = strdup(fallback);
    }
    return value;
}

/* The rabbit's language: karotz.conf's (the install's LANGUAGE=), else the platform account's, else
 * en-US. A copy, for the caller to free. */
char *controller_language(ControllerObject *controller)
{
    char *account = controller_get_account(controller, "lang", "en-US");
    char *lang = controller_get_config(controller, "lang", account);
    free(account);
    return lang;
}

/* The default language file (en-US), loaded on first use; NULL while it
 * can't be loaded. */
static GKeyFile *default_lang_conf(void)
{
    static GKeyFile *conf = NULL;

    if (conf == NULL) {
        GKeyFile *file = g_key_file_new();
        if (g_key_file_load_from_file(file, LANG_DEFAULT, G_KEY_FILE_NONE, NULL))
            conf = file;
        else
            g_key_file_free(file);
    }
    return conf;
}

/* A sentence or sound from the language file or, when that file lacks the
 * key, from the default one (en-US), so a partial language file still gets
 * every sound. On error (neither has it) *value is not set. */
gboolean controller_get_lang_value(ControllerObject *controller, const char *key, char **value)
{
    GKeyFile *sources[] = { controller->lang_conf, default_lang_conf() };

    for (size_t i = 0; i < sizeof sources / sizeof sources[0]; i++) {
        GError *error = NULL;
        if (sources[i] == NULL)
            continue;
        char *text = g_key_file_get_value(sources[i], "lang", key, &error);
        if (error != NULL) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Error in g_key_file_get_value: %s", error->message);
            g_error_free(error);
            continue;
        }
        *value = strdup(text);
        g_free(text);
        return TRUE;
    }
    return FALSE;
}

static gboolean controller_load_karotz_config(ControllerObject *controller)
{
    GError *error = NULL;

    if (controller->karotz_conf == NULL)
        return FALSE;
    if (!g_key_file_load_from_file(controller->karotz_conf, KAROTZ_CONF, G_KEY_FILE_NONE, &error)) {
        g_error_free(error);
        return FALSE;
    }
    return TRUE;
}

/* The platform account is read from a copy of account.conf. */
gboolean controller_load_account(ControllerObject *controller)
{
    GError *error = NULL;

    if (controller->account_conf == NULL)
        return FALSE;
    if (system("cp -f /usr/etc/conf/account.conf /tmp/account.conf") != 0) {
        /* A failed copy leaves the previous copy (or none, and the load below fails). */
    }
    if (!g_key_file_load_from_file(controller->account_conf, "/tmp/account.conf", G_KEY_FILE_NONE, &error)) {
        g_error_free(error);
        return FALSE;
    }
    return TRUE;
}

/* Load /usr/karotz/res/lang/<lang>, falling back to en-US; the program
 * exits if the file can't be parsed. */
gboolean controller_load_lang(ControllerObject *controller, const char *lang)
{
    GError *error = NULL;
    char path[150];
    FILE *file;

    if (controller->lang_conf == NULL)
        return FALSE;
    file = fopen(LANG_DEFAULT, "r");
    if (file == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "error, while opening lang default path: %s", LANG_DEFAULT);
        return FALSE;
    }
    fclose(file);
    /* The name comes from karotz.conf or the platform's configuration, so it
     * is checked before it goes into path[]: a name too long for path[] or
     * with a '/' (which could reach any file) is taken as unknown: en-US. */
    if (snprintf(path, sizeof path, LANG_DIR "%s", lang) >= (int)sizeof path || strchr(lang, '/') != NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "language name rejected: %s", lang);
        strcpy(path, LANG_DEFAULT);
    } else if ((file = fopen(path, "r")) == NULL) {
        strcpy(path, LANG_DEFAULT);
    } else {
        fclose(file);
    }
    if (!g_key_file_load_from_file(controller->lang_conf, path, G_KEY_FILE_NONE, &error)) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "the language configuration couldn't be loaded: %s", error->message);
        g_clear_error(&error);
        exit(1);
    }
    return TRUE;
}

/* karotz.conf, the language, and the controller's own server. */
static void controller_init_config(ControllerObject *controller)
{
    char default_port[10] = { 0 };
    char *lang;
    char *port_text;
    int port;

    if (!controller_load_karotz_config(controller)) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "Error: Karotz config couldn't load. Exit");
        exit(-1);
    }
    lang = controller_get_config(controller, "lang", "en-US");
    gboolean loaded = controller_load_lang(controller, lang);
    g_free(lang);
    if (!loaded) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "Error: Karotz couldn't load language. Exit");
        exit(1);
    }
    sprintf(default_port, "%d", DEFAULT_PORT_SERVER);
    port_text = controller_get_config(controller, "port_server", default_port);
    port = atoi(port_text);
    if (port < 1 || port > PORT_MAX)
        port = DEFAULT_PORT_SERVER;
    g_free(port_text);
    controller->server = platform_bind_server(port);
}

/* ---- D-Bus ------------------------------------------------------------- */

static void controller_load_proxies(ControllerObject *controller)
{
    dbus_load_proxy(&controller->button, KAROTZ_PROXY_BUTTON, controller->bus);
    dbus_load_proxy(&controller->led, KAROTZ_PROXY_LED, controller->bus);
    dbus_load_proxy(&controller->ears, KAROTZ_PROXY_EARS, controller->bus);
    dbus_load_proxy(&controller->voice, KAROTZ_PROXY_VOICE, controller->bus);
    dbus_load_proxy(&controller->multimedia, KAROTZ_PROXY_MULTIMEDIA, controller->bus);
    dbus_load_proxy(&controller->rfid, KAROTZ_PROXY_RFID, controller->bus);
    dbus_load_proxy(&controller->webcam, KAROTZ_PROXY_WEBCAM, controller->bus);
    dbus_load_proxy(&controller->choreography, KAROTZ_PROXY_CHOREOGRAPHY_MANAGER, controller->bus);
    dbus_load_proxy(&controller->scheduler, KAROTZ_PROXY_SCHEDULER, controller->bus);
    dbus_load_proxy(&controller->k2k, KAROTZ_PROXY_K2K, controller->bus);
    dbus_load_proxy(&controller->serial, KAROTZ_PROXY_SERIAL, controller->bus);
    dbus_load_proxy(&controller->dock, KAROTZ_PROXY_DOCK, controller->bus);
}

static void controller_connect_dbus(ControllerObject *controller)
{
    GError *error = NULL;

    controller->bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (controller->bus == NULL) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "D-Bus - Failed to open connection: %s", error->message);
        exit(-1);
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "D-Bus - Successfully connected");
    controller_load_proxies(controller);
}

gboolean controller_get_status(ControllerObject *controller, int *karotz, int *network, int *platform, GError **error)
{
    (void)error;
    g_assert(IS_CONTROLLEROBJ(controller));
    *karotz = controller->status.karotz;
    *network = controller->status.network;
    *platform = controller->status.platform;
    return TRUE;
}

/* A local program takes control like an interactive application would. */
gboolean controller_take_control(ControllerObject *controller, gboolean *success, gchar **interactive_id,
                                 GError **error)
{
    char id[37];

    (void)error;
    g_assert(IS_CONTROLLEROBJ(controller));
    *success = FALSE;
    if (controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE) {
        generate_uuid(id);
        if (controller_interactive_mode_start(controller, id, 0) == TRUE) {
            *interactive_id = g_strdup(id);
            *success = TRUE;
        }
    }
    /* On failure *interactive_id is NULL (no session: dbus-glib sends it
     * as ""). */
    if (!*success)
        *interactive_id = NULL;
    return TRUE;
}

gboolean controller_release_control(ControllerObject *controller, const gchar *interactive_id, GError **error)
{
    (void)error;
    g_assert(IS_CONTROLLEROBJ(controller));
    if (!controller_interactive_mode_stop(controller, interactive_id))
        g_log(NULL, G_LOG_LEVEL_WARNING, "controller_interactive_mode_stop failed");
    return TRUE;
}

/* ---- the GObject ------------------------------------------------------- */

static void controller_object_class_init(ControllerObjectClass *klass)
{
    if (klass == NULL) {
        g_return_if_fail_warning(NULL, G_STRFUNC, "klass != NULL");
        return;
    }
    GType type = G_TYPE_FROM_CLASS(klass);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_KAROTZ_STATUS_CHANGED] =
        g_signal_new("karotz_status_changed", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL, g_cclosure_marshal_VOID__INT,
                     G_TYPE_NONE, 1, G_TYPE_INT);
}

/* Creating the controller connects to D-Bus, makes the proxies, loads the
 * configuration and language and binds the server socket. */
static void controller_object_init(ControllerObject *controller)
{
    FILE *file;

    if (controller == NULL) {
        g_return_if_fail_warning(NULL, G_STRFUNC, "controller != NULL");
        return;
    }
    controller->status_listeners = NULL;
    controller->status.karotz = 0;
    controller->status.network = 0;
    controller->status.platform = 0;
    controller->interactive_id[0] = '\0';
    controller->interactive_last_event = 0;
    controller->connection_id = NULL;
    controller->platform_socket = -1;
    controller->platform_watch = 0;
    controller->platform_authenticated = FALSE;
    controller->config_pending = FALSE;
    controller->pending_message = FALSE;
    file = fopen("/etc/conf/playsounds", "r");
    if (file == NULL) {
        controller->play_sounds = FALSE;
    } else {
        controller->play_sounds = TRUE;
        fclose(file);
    }
    controller->connect_attempts = 0;
    controller->sleeping = FALSE;
    controller->rfid_asr_timer = 0;
    /* The proxies start out empty. */
    controller->bus_proxy = NULL;
    controller->button = NULL;
    controller->ears = NULL;
    controller->led = NULL;
    controller->voice = NULL;
    controller->multimedia = NULL;
    controller->rfid = NULL;
    controller->webcam = NULL;
    controller->choreography = NULL;
    controller->scheduler = NULL;
    controller->k2k = NULL;
    controller->serial = NULL;
    controller->dock = NULL;
    controller_connect_dbus(controller);
    controller->lang_conf = g_key_file_new();
    controller->account_conf = g_key_file_new();
    controller->karotz_conf = g_key_file_new();
    controller_init_config(controller);
    dbus_g_object_type_install_info(CONTROLLER_TYPE_OBJECT, &dbus_glib_controller_object_object_info);
}
