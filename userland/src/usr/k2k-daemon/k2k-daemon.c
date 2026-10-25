/* SPDX-License-Identifier: MIT */
/*
 * k2k-daemon: "Karotz to Karotz" voice calls. Wraps a liblinphone core (SIP
 * signalling through eXosip2, audio through mediastreamer2/oRTP) and exports
 * it on the system bus as com.mindscape.karotz.K2k
 * (/com/mindscape/karotz/K2k, interface com.mindscape.karotz.KarotzInterface):
 *
 *   methods  k2k_call(s), k2k_answer(b), k2k_hangup(), k2k_set_busy(b),
 *            k2k_set_answering_machine(b), k2k_use_account(ssss),
 *            k2k_echo_calibration(), each returning a boolean "result";
 *   signals  registered, incoming_call(s), beginning_call(s),
 *            beginning_outgoing_call(s), finished_call(s), the string being
 *            the peer's SIP address.
 *
 * The SIP account (sipDomain and stunServer from [k2k] in
 * /etc/conf/karotz.conf, id and pwd from [account] in /etc/conf/account.conf) is
 * registered at start-up; k2k_use_account replaces it. The rabbit shows as
 * busy (SIP presence) unless the controller reports
 * E_STATUS_KAROTZ_AVAILABLE, at start-up and on its karotz_status_changed
 * signal. "--debug" enables linphone's log (/tmp/linphone.log) and a few
 * listings in the daemon's own log.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include <linphonecore.h>
#include <linphonecore_utils.h>
#include <linphonefriend.h>

#include "karotz-common.h"
#include "karotz-messages.h"

/* The controller's status for "the rabbit is available" (platform-message.proto
 * E_STATUS_KAROTZ_AVAILABLE), and the value assumed when it can't be read. */
#define KAROTZ_STATUS_AVAILABLE 16
#define KAROTZ_STATUS_UNAVAILABLE 17

/* linphone_core_iterate() period. */
#define ITERATE_MS 100

enum {
    SIGNAL_REGISTERED,
    SIGNAL_INCOMING_CALL,
    SIGNAL_BEGINNING_CALL,
    SIGNAL_BEGINNING_OUTGOING_CALL,
    SIGNAL_FINISHED_CALL,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    LinphoneCore *lc;
    char *stun_server;  /* [k2k] stunServer */
    char *sip_domain;   /* [k2k] sipDomain */
    char *sip_id;       /* [account] id */
    char *sip_password; /* [account] pwd */
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    DBusGProxy *controller;
} K2kObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} K2kObjectClass;

GType k2k_object_get_type(void);
#define K2K_TYPE_OBJECT (k2k_object_get_type())
#define IS_K2KOBJ(obj) (G_TYPE_CHECK_INSTANCE_TYPE((obj), K2K_TYPE_OBJECT))

/* Like g_assert(): aborts when it fails, naming this file, the line and the
 * expression. */
#define K2K_ASSERT(expr)                                                                           \
    do {                                                                                           \
        if (!(expr))                                                                               \
            g_assertion_message_expr(NULL, __FILE__, __LINE__, G_STRFUNC, #expr);                   \
    } while (0)

G_DEFINE_TYPE(K2kObject, k2k_object, G_TYPE_OBJECT)

gboolean k2k_object_k2k_call(K2kObject *self, const char *sip_addr, gboolean *result, GError **error);
gboolean k2k_object_k2k_answer(K2kObject *self, gboolean ans, gboolean *result, GError **error);
gboolean k2k_object_k2k_hangup(K2kObject *self, gboolean *result, GError **error);
gboolean k2k_object_k2k_set_busy(K2kObject *self, gboolean busy, gboolean *result, GError **error);
gboolean k2k_object_k2k_set_answering_machine(K2kObject *self, gboolean on, gboolean *result, GError **error);
gboolean k2k_object_k2k_use_account(K2kObject *self, const char *sip_id, const char *sip_domain,
                                    const char *sip_password, const char *stun_server, gboolean *result,
                                    GError **error);
gboolean k2k_object_k2k_echo_calibration(K2kObject *self, gboolean *result, GError **error);

#include "k2k-daemon-glue.h"

/* Set by "--debug". */
static gboolean debug;

/* Must outlive the core: linphone keeps a pointer to it. */
static LinphoneCoreVTable vtable;

static void k2k_linphone_set_busy(K2kObject *self, gboolean busy);
static void k2k_use_account(K2kObject *self, const char *sip_id, const char *sip_domain, const char *sip_password,
                            const char *stun_server);

/* --- configuration ------------------------------------------------------ */

/* Read the account settings. Missing values stay NULL.
 *
 * A load failure's error is logged and cleared, so the next load starts with
 * no error set. */
static void k2k_load_config(K2kObject *self)
{
    GError *error = NULL;
    GKeyFile *karotz_conf;
    GKeyFile *account_conf;

    karotz_conf = g_key_file_new();
    if (!g_key_file_load_from_file(karotz_conf, "/etc/conf/karotz.conf", G_KEY_FILE_NONE, &error)) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Could not read /etc/conf/karotz.conf file  (%s)", error->message);
        g_clear_error(&error); /* cleared so the next load can set it */
    } else {
        self->stun_server = g_key_file_get_value(karotz_conf, "k2k", "stunServer", &error);
        if (error != NULL) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Could not read stun server address in conf file (%s)", error->message);
            g_clear_error(&error);
        }
        self->sip_domain = g_key_file_get_value(karotz_conf, "k2k", "sipDomain", &error);
        if (error != NULL) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Could not read sip server address in conf file (%s)", error->message);
            g_clear_error(&error);
        }
    }
    g_key_file_free(karotz_conf);

    account_conf = g_key_file_new();
    if (!g_key_file_load_from_file(account_conf, "/etc/conf/account.conf", G_KEY_FILE_NONE, &error)) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Could not read /etc/conf/account.conf file (%s)", error->message);
        g_clear_error(&error);
    } else {
        self->sip_id = g_key_file_get_value(account_conf, "account", "id", &error);
        if (error != NULL) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Could not read object id in conf file (%s)", error->message);
            g_clear_error(&error);
        }
        self->sip_password = g_key_file_get_value(account_conf, "account", "pwd", &error);
        if (error != NULL) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Could not read object password in conf file (%s)", error->message);
            g_clear_error(&error);
        }
    }
    g_key_file_free(account_conf);

    /* uClibc prints NULL strings as "(null)". */
    g_log(NULL, G_LOG_LEVEL_DEBUG, "stunServer: %s", self->stun_server);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sipDomain: %s", self->sip_domain);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sipId: %s", self->sip_id);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sipPassword: %s (%d characters)", self->sip_password ? "set" : "not set", self->sip_password ? (int)strlen(self->sip_password) : 0);
}

/* --- controller --------------------------------------------------------- */

static void karotz_status_changed(DBusGProxy *proxy, int status, gpointer data)
{
    (void)proxy;
    g_log(NULL, G_LOG_LEVEL_DEBUG,
          "Controller sent message to DBUS: new karotz status: %d (= E_STATUS_KAROTZ_AVAILABLE ? %d)", status,
          status == KAROTZ_STATUS_AVAILABLE);
    k2k_linphone_set_busy(data, status != KAROTZ_STATUS_AVAILABLE);
}

/* --- D-Bus methods -------------------------------------------------------- */

/* Every method returns TRUE (the call itself worked), and *result says
 * whether calling, answering and hanging up did; the other methods always
 * set it TRUE. */

/* k2k_call(sipAddr): call a SIP address, unless a call is under way. */
gboolean k2k_object_k2k_call(K2kObject *self, const char *sip_addr, gboolean *result, GError **error)
{
    gboolean ret;

    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "dbus, Asked to call %s", sip_addr);
    if (linphone_core_in_call(self->lc)) {
        char *peer = linphone_call_get_remote_address_as_string(linphone_core_get_current_call(self->lc));

        g_log(NULL, G_LOG_LEVEL_WARNING, "Already calling someone (%s) !", peer);
        ortp_free(peer);
        ret = FALSE;
    } else if (linphone_core_invite(self->lc, sip_addr) == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Could not start the call");
        ret = FALSE;
    } else {
        K2kObjectClass *klass = (K2kObjectClass *)G_OBJECT_GET_CLASS(self);

        g_signal_emit(self, klass->signals[SIGNAL_BEGINNING_OUTGOING_CALL], 0, sip_addr);
        ret = TRUE;
    }
    *result = ret;
    return TRUE;
}

/* k2k_answer(ans): accept the incoming call, or decline it. */
gboolean k2k_object_k2k_answer(K2kObject *self, gboolean ans, gboolean *result, GError **error)
{
    int ret;

    (void)error;
    if (ans) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "dbus, ANSWER answer");
        ret = linphone_core_accept_call(self->lc, NULL);
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "dbus, ANSWER decline call");
        ret = linphone_core_terminate_call(self->lc, NULL);
    }
    *result = ret == 0; /* linphone: 0, or -1 with no call to answer */
    return TRUE;
}

/* k2k_hangup(): end the current call. */
gboolean k2k_object_k2k_hangup(K2kObject *self, gboolean *result, GError **error)
{
    int ret;

    *error = NULL;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "dbus, Asked to hangup call");
    ret = linphone_core_terminate_call(self->lc, NULL);
    if (ret == -1)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Asked to hangup, but no active call.\n");
    *result = ret == 0;
    return TRUE;
}

/* k2k_set_busy(busy): show the rabbit as busy or online. */
gboolean k2k_object_k2k_set_busy(K2kObject *self, gboolean busy, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "dbus, setBusy. %d", busy);
    k2k_linphone_set_busy(self, busy);
    *result = TRUE;
    return TRUE;
}

/* k2k_set_answering_machine(on): never implemented. */
gboolean k2k_object_k2k_set_answering_machine(K2kObject *self, gboolean on, gboolean *result, GError **error)
{
    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_WARNING, "dbus, set answering machine TODO (value: %d)", on);
    *result = TRUE;
    return TRUE;
}

/* k2k_use_account(sipId, sipDomain, sipPassword, stunServer): replace the
 * SIP account. */
gboolean k2k_object_k2k_use_account(K2kObject *self, const char *sip_id, const char *sip_domain,
                                    const char *sip_password, const char *stun_server, gboolean *result,
                                    GError **error)
{
    (void)error;
    k2k_use_account(self, sip_id, sip_domain, sip_password, stun_server);
    *result = TRUE;
    return TRUE;
}

static void echo_calibration_done(LinphoneCore *lc, LinphoneEcCalibratorStatus status, int delay_ms, void *data)
{
    (void)lc;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Echo calibration done. delay_ms: %d", delay_ms);
    if (status == LinphoneEcCalibratorDone)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "done");
    else if (status == LinphoneEcCalibratorInProgress)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "in progress");
    else if (status == LinphoneEcCalibratorFailed)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "failed");
    else
        g_log(NULL, G_LOG_LEVEL_WARNING, "unknown state");
}

/* k2k_echo_calibration(): measure the speaker-to-microphone echo delay. */
gboolean k2k_object_k2k_echo_calibration(K2kObject *self, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Starting echo calibration");
    linphone_core_start_echo_calibration(self->lc, echo_calibration_done, self);
    *result = TRUE;
    return TRUE;
}

/* --- linphone ------------------------------------------------------------- */

static gboolean iterate(gpointer data)
{
    linphone_core_iterate(data);
    return TRUE;
}

static void k2k_linphone_vtable_call_state_changed(LinphoneCore *lc, LinphoneCall *call, LinphoneCallState state,
                                                   const char *message)
{
    K2kObject *self = linphone_core_get_user_data(lc);
    K2kObjectClass *klass;
    char *peer;

    K2K_ASSERT(IS_K2KOBJ(self));
    klass = (K2kObjectClass *)G_OBJECT_GET_CLASS(self);
    switch (state) {
    case LinphoneCallIncomingReceived:
        peer = linphone_call_get_remote_address_as_string(call);
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "incoming call, from %s", peer);
        g_signal_emit(self, klass->signals[SIGNAL_INCOMING_CALL], 0, peer);
        ortp_free(peer);
        break;
    case LinphoneCallConnected:
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "call connected");
        peer = linphone_call_get_remote_address_as_string(call);
        g_signal_emit(self, klass->signals[SIGNAL_BEGINNING_CALL], 0, peer);
        ortp_free(peer);
        break;
    case LinphoneCallError:
        g_log(NULL, G_LOG_LEVEL_WARNING, "call error (%s)", message);
        peer = linphone_call_get_remote_address_as_string(call);
        g_signal_emit(self, klass->signals[SIGNAL_FINISHED_CALL], 0, peer);
        ortp_free(peer);
        break;
    case LinphoneCallEnd:
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "call end");
        peer = linphone_call_get_remote_address_as_string(call);
        g_signal_emit(self, klass->signals[SIGNAL_FINISHED_CALL], 0, peer);
        ortp_free(peer);
        break;
    default:
        if (debug)
            g_log(NULL, G_LOG_LEVEL_DEBUG, "call state changed (%s).", linphone_call_state_to_string(state));
        break;
    }
}

static void k2k_linphone_vtable_registration_state_changed(LinphoneCore *lc, LinphoneProxyConfig *cfg,
                                                           LinphoneRegistrationState state, const char *message)
{
    K2kObject *self = linphone_core_get_user_data(lc);

    (void)cfg;
    K2K_ASSERT(IS_K2KOBJ(self));
    if (state == LinphoneRegistrationOk)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Registration ok (%s)", message);
    else if (state == LinphoneRegistrationFailed)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Registration failed (%s)", message);
    else if (state == LinphoneRegistrationProgress)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Registration progress (%s)", message);
    else
        g_log(NULL, G_LOG_LEVEL_WARNING, "Unhandled registration message (%s)", message);
}

/* Create the linphone core from /etc/conf/linphone.conf (as the factory
 * configuration: there is no user configuration, so nothing is written
 * back), start iterating it, and set the presence from the controller's
 * status. */
static void k2k_linphone_init(K2kObject *self)
{
    LinphoneCore *lc;
    int karotz_status, network_status, platform_status;

    K2K_ASSERT(IS_K2KOBJ(self));
    memset(&vtable, 0, sizeof(vtable));
    vtable.call_state_changed = k2k_linphone_vtable_call_state_changed;
    vtable.registration_state_changed = k2k_linphone_vtable_registration_state_changed;
    lc = linphone_core_new(&vtable, NULL, "/etc/conf/linphone.conf", self);
    self->lc = lc;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s: lc: %p\n", "k2k_linphone_init", (void *)lc);

    if (debug) {
        const char **devices;
        const char **device;

        linphone_core_enable_logs(fopen("/tmp/linphone.log", "w"));
        devices = linphone_core_get_sound_devices(lc);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "listing sound devices");
        for (device = devices; device != NULL && *device != NULL; device++)
            g_log(NULL, G_LOG_LEVEL_DEBUG, " - %s", *device);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "audio_jittcomp: %d", linphone_core_get_audio_jittcomp(lc));
    }
    linphone_core_enable_video(lc, FALSE, FALSE);
    if (debug) {
        const MSList *codec;

        g_log(NULL, G_LOG_LEVEL_DEBUG, "Listing codecs (desc, enabled ?)");
        for (codec = linphone_core_get_audio_codecs(lc); codec != NULL; codec = codec->next) {
            PayloadType *pt = codec->data;

            g_log(NULL, G_LOG_LEVEL_DEBUG, " - %s (%d)", linphone_core_get_payload_type_description(lc, pt),
                  linphone_core_payload_type_enabled(lc, pt));
        }
    }
    linphone_core_set_ring(lc, NULL);
    g_timeout_add(ITERATE_MS, iterate, lc);

    if (!controllerGetStatus(self->controller, &karotz_status, &network_status, &platform_status)) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Could not get controller current status. Assuming unavailable");
        karotz_status = KAROTZ_STATUS_UNAVAILABLE;
    }
    k2k_linphone_set_busy(self, karotz_status != KAROTZ_STATUS_AVAILABLE);
}

/* Register the configured account, if it is complete (the STUN server is
 * optional). */
static void k2k_register(K2kObject *self)
{
    K2K_ASSERT(IS_K2KOBJ(self));
    K2K_ASSERT(NULL != self->lc);
    if (self->sip_domain != NULL && self->sip_id != NULL && self->sip_password != NULL)
        k2k_use_account(self, self->sip_id, self->sip_domain, self->sip_password, self->stun_server);
}

/* Busy shows as "busy" presence, otherwise "online". */
static void k2k_linphone_set_busy(K2kObject *self, gboolean busy)
{
    K2K_ASSERT(IS_K2KOBJ(self));
    K2K_ASSERT(NULL != self->lc);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "setting linphone busy (%d)", busy);
    linphone_core_set_presence_info(self->lc, 0, NULL, busy ? LinphoneStatusBusy : LinphoneStatusOnline);
}

static void k2k_linphone_destroy(K2kObject *self)
{
    linphone_core_destroy(self->lc);
    self->lc = NULL;
}

/* Replace the account: drop every proxy and credential, set up STUN (an
 * empty server disables it), then register sip:<id>@<domain> on
 * sip:<domain>. Only a NULL password means "no credentials": an empty one
 * is registered as is. */
static void k2k_use_account(K2kObject *self, const char *sip_id, const char *sip_domain, const char *sip_password,
                            const char *stun_server)
{
    linphone_core_clear_all_auth_info(self->lc);
    linphone_core_clear_proxy_config(self->lc);
    if (stun_server == NULL || stun_server[0] == '\0') {
        g_log(NULL, G_LOG_LEVEL_WARNING, "No stun server configured");
        linphone_core_set_firewall_policy(self->lc, LinphonePolicyNoFirewall);
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Using stun server %s", stun_server);
        linphone_core_set_stun_server(self->lc, stun_server);
        linphone_core_set_firewall_policy(self->lc, LinphonePolicyUseStun);
    }
    if (sip_domain == NULL || sip_domain[0] == '\0' || sip_id == NULL || sip_id[0] == '\0') {
        g_log(NULL, G_LOG_LEVEL_WARNING, "No proxy information");
    } else {
        LinphoneProxyConfig *proxy;
        char *address;

        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Registering on %s with id %s", sip_domain, sip_id);
        proxy = linphone_proxy_config_new();
        linphone_proxy_config_enable_register(proxy, TRUE);
        address = g_strdup_printf("sip:%s", sip_domain);
        linphone_proxy_config_set_server_addr(proxy, address);
        g_free(address);
        address = g_strdup_printf("sip:%s@%s", sip_id, sip_domain);
        linphone_proxy_config_set_identity(proxy, address);
        g_free(address);
        linphone_core_add_proxy_config(self->lc, proxy);
        if (sip_password == NULL)
            g_log(NULL, G_LOG_LEVEL_WARNING, "No auth information for %s@%s", sip_id, sip_domain);
        else
            linphone_core_add_auth_info(self->lc,
                                        linphone_auth_info_new(sip_id, sip_id, sip_password, NULL, sip_domain));
    }
}

/* --- object ----------------------------------------------------------------- */

static void k2k_object_class_init(K2kObjectClass *klass)
{
    GType type;

    g_return_if_fail(klass != NULL);
    type = G_TYPE_FROM_CLASS(klass);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_INCOMING_CALL] = g_signal_new("incoming_call", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                        g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1,
                                                        G_TYPE_STRING);
    klass->signals[SIGNAL_BEGINNING_OUTGOING_CALL] =
        g_signal_new("beginning_outgoing_call", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                     g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1, G_TYPE_STRING);
    klass->signals[SIGNAL_BEGINNING_CALL] = g_signal_new("beginning_call", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                         g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1,
                                                         G_TYPE_STRING);
    klass->signals[SIGNAL_FINISHED_CALL] = g_signal_new("finished_call", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                        g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1,
                                                        G_TYPE_STRING);
    dbus_g_object_type_install_info(K2K_TYPE_OBJECT, &dbus_glib_k2k_object_object_info);
}

/* Read the configuration, join the bus as com.mindscape.karotz.K2k and
 * follow the controller's status. */
static void k2k_object_init(K2kObject *self)
{
    GError *error = NULL;
    guint request_result;

    g_return_if_fail(self != NULL);
    self->lc = NULL;
    self->stun_server = NULL;
    self->sip_domain = NULL;
    self->sip_id = NULL;
    self->sip_password = NULL;
    self->bus = NULL;
    self->bus_proxy = NULL;
    self->controller = NULL;
    k2k_load_config(self);

    self->bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (self->bus == NULL)
        die("Failed to open connection to bus", error);
    else
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Successfully connected to %s bus\n", "SYSTEM");
    dbus_g_connection_register_g_object(self->bus, "/com/mindscape/karotz/K2k", G_OBJECT(self));

    self->bus_proxy = dbus_g_proxy_new_for_name(self->bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(self->bus_proxy, "com.mindscape.karotz.K2k", 0, &request_result, &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Got result code %u from requesting name", request_result);
        exit(1);
    }

    dbus_load_proxy(&self->controller, KAROTZ_PROXY_CONTROLLER, self->bus);
    dbus_g_proxy_add_signal(self->controller, "karotz_status_changed", G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(self->controller, "karotz_status_changed", G_CALLBACK(karotz_status_changed), self,
                                NULL);
}

int main(int argc, char **argv)
{
    int log_priority = LOG_NOTICE;
    GMainLoop *loop;
    K2kObject *k2k;
    int i;

    for (i = 1; i < argc; i++) {
        if (g_strcmp0("--debug", argv[i]) == 0)
            debug = TRUE;
    }
    init_karotz_log("K2k", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Loading K2K MODULE\n");

    k2k = g_object_new(K2K_TYPE_OBJECT, NULL);
    k2k_linphone_init(k2k);
    k2k_register(k2k);
    g_signal_emit(k2k, ((K2kObjectClass *)G_OBJECT_GET_CLASS(k2k))->signals[SIGNAL_REGISTERED], 0);

    g_main_loop_run(loop);
    k2k_linphone_destroy(k2k);
    return 0;
}
