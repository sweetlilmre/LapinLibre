// SPDX-License-Identifier: MIT
/*
 * Handling of the messages from the platform and from interactive
 * applications. Every message goes through every handler, each of which
 * picks out its part; requests from applications must carry the current
 * interactive id, and most are answered with a Response (OK or ERROR).
 */
#include <signal.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "controller.h"

static void send_response(int sock, const pm::PlatformMessage *request, pm::ReturnCode code)
{
    pm::PlatformMessage *response = new_response(request, code, NULL);

    platform_send(sock, response);
    delete response;
}

static void send_result(int sock, const pm::PlatformMessage *request, gboolean ok)
{
    send_response(sock, request, ok ? pm::OK : pm::ERROR);
}

/* A request from the application currently in control. */
static gboolean from_interactive_app(ControllerObject *controller, const pm::PlatformMessage *msg)
{
    return msg->has_interactiveid() == true &&
           controller_is_interactive_id(controller, msg->interactiveid().c_str());
}

/* ---- authentication ------------------------------------------------------------ */

/*
 * The platform's challenge: answer with the login and md5(password + token)
 * from account.conf, which authenticates the Karotz (CONNECTING).
 */
static void platform_listener_authentication(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    char hash[33];
    char challenge[255];

    if (msg->has_authenticationtoken() != true || controller->platform_socket != sock)
        return;
    const char *token = msg->authenticationtoken().token().c_str();
    if (!controller_load_account(controller))
        g_log(NULL, G_LOG_LEVEL_WARNING, "Error: the platform account couldn't be loaded");
    char *login = controller_get_account(controller, "id", "");
    char *password = controller_get_account(controller, "pwd", "");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_authentication login: %s", login);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_authentication password: %s (%d characters)", (password && *password) ? "set" : "not set", password ? (int)strlen(password) : 0);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_authentication token: %s (%d characters)", (token && *token) ? "set" : "not set", token ? (int)strlen(token) : 0);
    if (*login == '\0' || *password == '\0') {
        /* Free them before returning. */
        free(login);
        free(password);
        return;
    }
    sprintf(challenge, "%s%s", password, token);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_authentication buff: %d characters", (int)strlen(challenge));
    md5_hex(challenge, hash);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_authentication md5_string: %s", hash);
    pm::PlatformMessage *answer = new_platform_message();
    answer->set_correlationid(msg->id());
    pm::AuthenticationResponse *response = answer->mutable_authenticationresponse();
    response->set_login(login);
    response->set_authentication(hash);
    platform_send(sock, answer);
    delete answer;
    controller->platform_authenticated = TRUE;
    free(login);
    free(password);
    controller_set_status(controller, E_STATUS_KAROTZ_CONNECTING);
}

/* ---- information --------------------------------------------------------------- */

static void platform_listener_karotzinfo(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    (void)controller;
    if (msg->has_karotzinfo_query() != true)
        return;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Received karotzinfo_query");
    pm::PlatformMessage *answer = new_platform_message();
    answer->set_correlationid(msg->id());
    karotz_info_fill(answer);
    platform_send(sock, answer);
    delete answer;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Sent KarotzInfoResponse");
}

/* Any message keeps the platform connection (and the application) alive. */
static void platform_listener_activity(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (controller->platform_socket == sock)
        controller->platform_last_message = time(NULL);
    if (msg->has_interactiveid() && controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        controller->interactive_last_event = time(NULL);
}

/* ---- ears ------------------------------------------------------------------------- */

/* Move one or both ears (step-wise when relative), or reset them. */
static void platform_listener_ears(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    gboolean ok = FALSE;

    if (msg->has_ears() != true || msg->has_interactiveid() != true)
        return;
    if (!controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    const char *id = msg->id().c_str();
    const pm::Ears &ears = msg->ears();
    gboolean relative = ears.has_relative() == true && ears.relative() == true;
    if (ears.has_reset() == true && ears.reset() == true) {
        if (ears.has_right() == true && ears.has_left() == false)
            ok = earsReset(controller->ears, id, "R");
        else if (ears.has_left() == true && ears.has_right() == false)
            ok = earsReset(controller->ears, id, "L");
        else
            ok = earsReset(controller->ears, id, "B");
    } else if (ears.has_left() == true && ears.has_right() == true) {
        if (relative)
            ok = earsMoveStep(controller->ears, id, ears.left(), 30, ears.right(), 30);
        else
            ok = earsMove(controller->ears, id, ears.left(), 30, ears.right(), 30);
    } else if (ears.has_left()) {
        if (relative)
            ok = earsMoveLeftStep(controller->ears, id, ears.left(), 30);
        else
            ok = earsMoveLeft(controller->ears, id, ears.left(), 30);
    } else if (ears.has_right()) {
        if (relative)
            ok = earsMoveRightStep(controller->ears, id, ears.right(), 30);
        else
            ok = earsMoveRight(controller->ears, id, ears.right(), 30);
    }
    send_result(sock, msg, ok);
}

/* ---- interactive mode ------------------------------------------------------------- */

typedef struct {
    char *interactive_id;
    ControllerObject *controller;
    int sock;
    int ttl;
} InteractiveTimer;

static void free_interactive_timer(InteractiveTimer *timer)
{
    free(timer->interactive_id); /* a strdup'ed copy */
    delete timer;
}

/* Every 10 s: stop interactive mode once the application has been silent
 * for longer than its ttl, telling it so. */
static gboolean check_last_event(gpointer data)
{
    InteractiveTimer *timer = (InteractiveTimer *)data;
    ControllerObject *controller = timer->controller;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "check_lastevent: %d", (int)(time(NULL) - controller->interactive_last_event));
    if (controller->status.karotz != E_STATUS_KAROTZ_INTERACTIVE) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "data->controller->status.karotz != E_STATUS_KAROTZ_INTERACTIVE");
        free_interactive_timer(timer);
        return FALSE;
    }
    if (strcmp(controller->interactive_id, timer->interactive_id) != 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "%s != %s", controller->interactive_id, timer->interactive_id);
        free_interactive_timer(timer);
        return FALSE;
    }
    if (timer->ttl >= time(NULL) - controller->interactive_last_event)
        return TRUE;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "time since last event: %d, stopping interactive mode", (int)(time(NULL) - controller->interactive_last_event));
    controller_interactive_mode_stop(controller, timer->interactive_id);
    pm::PlatformMessage *msg = new_platform_message();
    msg->set_interactiveid(timer->interactive_id);
    msg->mutable_interactivemode()->set_action(pm::InteractiveMode::STOP);
    platform_send(timer->sock, msg);
    delete msg;
    free_interactive_timer(timer);
    return FALSE;
}

/*
 * An application asks for control. With a configId it must be one of the
 * configured instances; the ttl (at most 900 s) is how long it may stay
 * silent.
 */
static void platform_listener_interactive_mode_start(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    gboolean started;

    if (msg->has_interactivemode() != true)
        return;
    const pm::InteractiveMode &mode = msg->interactivemode();
    if (mode.action() != pm::InteractiveMode::START)
        return;
    const char *id = mode.interactiveid().c_str();
    if (id[0] == '\0') {
        g_log(NULL, G_LOG_LEVEL_WARNING, "interactive mode start refused: empty interactiveId");
        send_response(sock, msg, pm::BAD_INTERACTIVEID);
        return;
    }
    if (mode.has_configid()) {
        gboolean found = FALSE;
        g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_interactive_mode_start: the request names a configuration");
        /* config is NULL until the platform's first configuration: no
         * configuration means no match, so ERROR. */
        int apps = controller->config != NULL ? controller->config->configapp_size() : 0;
        for (int i = 0; i < apps && !found; i++) {
            const pm::ConfigApp &app = controller->config->configapp(i);
            int instances = app.configinstance_size();
            for (int j = 0; j < instances; j++) {
                if (strcmp(mode.configid().c_str(), app.configinstance(j).uuid().c_str()) == 0) {
                    g_log(NULL, G_LOG_LEVEL_DEBUG,
                          "platform_listener_interactive_mode_start: the configuration is an installed instance");
                    found = TRUE;
                    break;
                }
            }
        }
        if (!found) {
            send_response(sock, msg, pm::ERROR);
            return;
        }
    }
    if (controller_is_interactive_id(controller, id)) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_interactive_mode_start: already in interactive mode with this id");
        started = TRUE;
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_interactive_mode_start: a new interactive id");
        started = controller_interactive_mode_start(controller, id, sock);
    }
    pm::PlatformMessage *response;
    if (!started) {
        response = new_response(msg, pm::ERROR, NULL);
    } else {
        response = new_response(msg, pm::OK, NULL);
        response->set_interactiveid(id);
        InteractiveTimer *timer = new InteractiveTimer();
        if (mode.has_ttl() == 0) {
            timer->ttl = INTERACTIVE_TTL_MAX;
        } else {
            timer->ttl = mode.ttl();
            if (timer->ttl > INTERACTIVE_TTL_MAX)
                timer->ttl = INTERACTIVE_TTL_MAX;
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "interactivemode ttl= %d", timer->ttl);
        timer->interactive_id = strdup(id);
        timer->sock = sock;
        timer->controller = controller;
        g_timeout_add(INTERACTIVE_CHECK_MS, check_last_event, timer);
    }
    platform_send(sock, response);
    delete response;
}

static void platform_listener_interactive_mode_stop(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_interactivemode() != true || msg->has_interactiveid() != true)
        return;
    if (msg->interactivemode().action() != pm::InteractiveMode::STOP)
        return;
    send_result(sock, msg, controller_interactive_mode_stop(controller, msg->interactiveid().c_str()));
}

/* A request with an interactive id that isn't (or no longer) in control. */
static void platform_listener_bad_interactive_id(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_interactiveid() == true && msg->has_interactivemode() == false &&
        !controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        send_response(sock, msg, pm::ERROR);
}

static void platform_listener_ping(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_ping() == true && from_interactive_app(controller, msg))
        send_response(sock, msg, pm::OK);
}

/* ---- LED ------------------------------------------------------------------------- */

static gboolean led_request(ControllerObject *controller, const pm::PlatformMessage *msg)
{
    return msg->has_led() == true && from_interactive_app(controller, msg);
}

static void platform_listener_led_light(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (!led_request(controller, msg) || msg->led().has_light() != true)
        return;
    gboolean ok = ledLight(controller->led, msg->id().c_str(), msg->led().light().color().c_str());
    pm::PlatformMessage *response = new_response(msg, ok ? pm::OK : pm::ERROR, NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_led_light sock: %d ", sock);
    platform_send(sock, response);
    delete response;
}

static void platform_listener_led_fade(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (!led_request(controller, msg) || msg->led().has_fade() != true)
        return;
    const char *id = msg->id().c_str();
    const pm::Led::Fade &fade = msg->led().fade();
    g_log(NULL, G_LOG_LEVEL_DEBUG, "uuid: %s color: %s period: %d ", id, fade.color().c_str(), fade.period());
    send_result(sock, msg, ledFade(controller->led, id, fade.color().c_str(), fade.period()));
}

/* The first color is "n", which led-daemon rejects as a colour and so replaces
 * with the current one: the pulse goes between the LED's colour and the new one. */
static void platform_listener_led_pulse(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (!led_request(controller, msg) || msg->led().has_pulse() != true)
        return;
    const pm::Led::Pulse &pulse = msg->led().pulse();
    send_result(sock, msg,
                ledPulse(controller->led, msg->id().c_str(), "n", pulse.color().c_str(), pulse.period(), pulse.pulse()));
}

/* ---- voice ----------------------------------------------------------------------- */

/* Speech recognition from a grammar file or a dictionary (" | "-separated);
 * the default language is the account's. */
static void platform_listener_asr(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    gboolean ok;

    if (msg->has_asr() != true || msg->has_interactiveid() != true)
        return;
    if (!controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "ASR message");
    const char *id = msg->id().c_str();
    std::string grammar("");
    const pm::Asr &asr = msg->asr();
    if (asr.has_file()) {
        ok = asrStartFile(controller->voice, id, asr.file().c_str(), 1);
    } else {
        char *default_lang = controller_language(controller);
        const char *lang = default_lang;
        if (asr.has_lang()) {
            lang = asr.lang().c_str();
            if (*lang == '\0')
                lang = default_lang;
        }
        for (int i = 0; i < asr.dictionary_size(); i++) {
            if (i > 0)
                grammar.append(" | ");
            grammar.append(asr.dictionary(i));
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "gramm: %s", grammar.c_str());
        ok = asrStartString(controller->voice, id, grammar.c_str(), lang, 1);
        free(default_lang);
    }
    pm::PlatformMessage *response;
    if (!ok) {
        response = new_response(msg, pm::ERROR, NULL);
    } else {
        response = new_response(msg, pm::OK, NULL);
        ledFade(controller->led, NULL, LED_ORANGE, FADE_PERIOD);
    }
    platform_send(sock, response);
    delete response;
}

/* The ERROR for no known action can't happen: the action is required, and a request without it
 * doesn't parse, so platform.cc answers it with an error and it never gets here. */
static void platform_listener_tts(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    gboolean ok = FALSE;

    if (msg->has_tts() != true || msg->has_interactiveid() != true)
        return;
    if (!controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    const char *id = msg->id().c_str();
    const pm::Tts &tts = msg->tts();
    if (tts.action() == pm::Tts::START) {
        if (tts.has_lang() == true) {
            ok = ttsSpeak(controller->voice, id, tts.text().c_str(), tts.lang().c_str(), 1);
        } else {
            char *lang = controller_language(controller);
            ok = ttsSpeak(controller->voice, id, tts.text().c_str(), lang, 1);
            free(lang);
        }
    } else if (tts.action() == pm::Tts::STOP) {
        ok = ttsStop(controller->voice, id, 1);
    }
    send_result(sock, msg, ok);
}

/* ---- multimedia ------------------------------------------------------------------- */

/* Each requested action gets its own response. */
static void platform_listener_multimedia(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_multimedia() != true || msg->has_interactiveid() != true ||
        !controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    const pm::Multimedia &multimedia = msg->multimedia();
    if (multimedia.has_play()) {
        const char *id = msg->id().c_str();
        send_result(sock, msg, multimediaPlay(controller->multimedia, id, multimedia.play().url().c_str(), 1));
    }
    if (multimedia.has_pause())
        send_result(sock, msg, multimediaPause(controller->multimedia, msg->id().c_str(), 1));
    if (multimedia.has_stop())
        send_result(sock, msg, multimediaStop(controller->multimedia, msg->id().c_str(), 1));
    if (multimedia.has_resume())
        send_result(sock, msg, multimediaResume(controller->multimedia, msg->id().c_str(), 1));
    if (multimedia.has_record()) {
        const char *id = msg->id().c_str();
        gboolean ok = multimediaRecord(controller->multimedia, id, "/tmp/record.wav", 1);
        pm::PlatformMessage *response;
        if (!ok) {
            response = new_response(msg, pm::ERROR, NULL);
        } else {
            ledFade(controller->led, NULL, LED_ORANGE, FADE_PERIOD);
            response = new_response(msg, pm::OK, NULL);
        }
        /* The id is logged as a string. */
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "RECORD %s", id);
        platform_send(sock, response);
        delete response;
    }
    if (multimedia.has_next())
        send_result(sock, msg, multimediaNext(controller->multimedia, 1));
    if (multimedia.has_previous())
        send_result(sock, msg, multimediaPrevious(controller->multimedia, 1));

    /* The library lists, returned in the response's multimedia part. */
    struct ListRequest {
        bool requested;
        const char *name;
        gboolean (*get)(DBusGProxy *, char **);
        void (pm::Multimedia::*add)(const char *);
    };
    const ListRequest lists[] = {
        { multimedia.has_getartist(), "artist", multimediaGetArtist, &pm::Multimedia::add_artistlist },
        { multimedia.has_getfolder(), "folder", multimediaGetFolder, &pm::Multimedia::add_folderlist },
        { multimedia.has_getplaylist(), "playlist", multimediaGetPlaylist, &pm::Multimedia::add_playlistlist },
        { multimedia.has_getgenre(), "genre", multimediaGetGenre, &pm::Multimedia::add_genrelist },
        { multimedia.has_getsong(), "song", multimediaGetSong, &pm::Multimedia::add_songlist },
    };
    for (unsigned i = 0; i < G_N_ELEMENTS(lists); i++) {
        const ListRequest &list = lists[i];
        if (!list.requested)
            continue;
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Received platform_listener_multimedia_get_%s", list.name);
        char *items = NULL;
        pm::PlatformMessage *response;
        if (!list.get(controller->multimedia, &items)) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Received platform_listener_multimedia_get_%s ERROR", list.name);
            response = new_response(msg, pm::ERROR, NULL);
        } else {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Received platform_listener_multimedia_get_%s OK", list.name);
            response = new_response(msg, pm::OK, NULL);
            (response->mutable_multimedia()->*list.add)(items);
            g_free(items);
        }
        platform_send(sock, response);
        delete response;
    }
}

/* ---- k2k ---------------------------------------------------------------------------- */

/* A k2k request with none of call, answer, hangup or account is answered
 * ERROR (tested). */
static void platform_listener_k2k(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    gboolean ok = FALSE;

    if (msg->has_k2k() != true || msg->has_interactiveid() != true ||
        !controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    const pm::K2k &k2k = msg->k2k();
    if (k2k.has_call()) {
        ok = k2kCall(controller->k2k, k2k.call().address().c_str());
    } else if (k2k.has_answer()) {
        ok = k2kAnswer(controller->k2k, k2k.answer().accept());
    } else if (k2k.has_hangup()) {
        ok = k2kHangup(controller->k2k);
    } else if (k2k.has_account()) {
        const pm::K2k::Account &account = k2k.account();
        ok = k2kUseAccount(controller->k2k, account.sipid().c_str(), account.sipdomain().c_str(),
                           account.sippassword().c_str(), account.stunserver().c_str());
    }
    send_result(sock, msg, ok);
}

/* ---- webcam -------------------------------------------------------------------------- */

/* The video stream: a copy of the request and its socket. */
typedef struct {
    pm::PlatformMessage *request;
    int sock;
} VideoStream;

static VideoStream *video;

static void video_stop(void)
{
    delete video->request;
    delete video;
    video = NULL;
}

/* A snapshot taken by the controller, sent back as a WebcamDirectResponse.
 * webcam_take_snapshot() returns 1 or -1. jpeg and size start as NULL and
 * 0, so a failed capture sends and frees nothing, and answers ERROR. */
static void webcam_direct(int sock, const pm::PlatformMessage *request)
{
    void *jpeg = NULL;
    unsigned long size = 0;
    pm::PlatformMessage *response;

    int result = webcam_take_snapshot(&jpeg, &size);
    if (result != 1) {
        response = new_response(request, pm::ERROR, NULL);
    } else {
        response = new_response(request, pm::OK, NULL);
        response->mutable_webcamdirectresponse()->set_data(jpeg, size);
    }
    free(jpeg);
    platform_send(sock, response);
    delete response;
}

/* Idle callback streaming snapshots while the application is in control. */
static gboolean video_loop(gpointer data)
{
    (void)data;
    if (global_controller->status.karotz != E_STATUS_KAROTZ_INTERACTIVE && video != NULL)
        video_stop();
    if (video == NULL)
        return FALSE;
    webcam_direct(video->sock, video->request);
    return TRUE;
}

/* "video" starts streaming, "videoStop" stops it, "direct" takes one
 * snapshot here; anything else is the URL the webcam daemon posts to. */
static void platform_listener_webcam(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_webcam() != true || msg->has_interactiveid() != true ||
        !controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    const char *photo = msg->webcam().photo().c_str();
    if (strcmp(photo, "video") == 0) {
        VideoStream *stream = new VideoStream();
        stream->request = new pm::PlatformMessage(*msg);
        stream->sock = sock;
        if (video == NULL) {
            g_idle_add(video_loop, NULL);
            ledPulse(global_controller->led, NULL, LED_SEA_GREEN, LED_OFF, SHORT_PULSE_PERIOD, LED_FOREVER);
        } else {
            video_stop();
        }
        video = stream;
    } else if (strcmp(photo, "videoStop") == 0) {
        if (video != NULL)
            video_stop();
    } else if (strcmp(photo, "direct") == 0) {
        webcam_direct(sock, msg);
    } else {
        GString *id = g_string_new("");
        g_string_printf(id, "%s:%s", msg->interactiveid().c_str(), msg->id().c_str());
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "platform_listener_webcam: %s", id->str);
        send_result(sock, msg, webcamTakeSnapshot(controller->webcam, id->str, photo, 1));
        g_string_free(id, TRUE);
    }
}

/* ---- choreography ------------------------------------------------------------------- */

/*
 * The choreography manager prepares the script; the VM then runs it through
 * the "appli_choreography" application. A script path without '/', or a
 * command rejected as unsafe or too long, is answered ERROR.
 */
static void platform_listener_choreography(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    char command[255];
    char *script = NULL;
    gboolean ok;

    if (msg->has_choreography() != true || msg->has_interactiveid() != true)
        return;
    if (!controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    ok = TRUE;
    memset(command, 0, sizeof command);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Choreography");
    if (msg->choreography().has_start_file()) {
        const char *file = msg->choreography().start_file().c_str();
        g_log(NULL, G_LOG_LEVEL_DEBUG, "CHOREOGRAPHY has_file: %s", file);
        ok = choreographyLaunchScript(controller->choreography, file, &script, 1);
        if (ok) {
            char *name = g_strrstr(script, "/");
            if (name != NULL)
                name++;
            g_log(NULL, G_LOG_LEVEL_DEBUG, "CHOREOGRAPHY jsPath: %s", name != NULL ? name : "(no '/')");
            /* The platform's interactive id and the script name are
             * checked before they go into command[255]: too long would
             * overflow it, and a newline would start another application. */
            if (name == NULL || !vm_safe_word(msg->interactiveid().c_str()) || !vm_safe_word(name) ||
                snprintf(command, sizeof command, "appli_choreography %s %s\n", msg->interactiveid().c_str(), name) >=
                    (int)sizeof command) {
                g_log(NULL, G_LOG_LEVEL_WARNING, "CHOREOGRAPHY: no script name, or an unsafe or too long command: not sent");
                ok = FALSE;
            } else {
                g_log(NULL, G_LOG_LEVEL_DEBUG, "CHOREOGRAPHY arg_run_app!:%s", command);
                vm_send(command);
            }
            free(script);
        }
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Choreography done");
    send_result(sock, msg, ok);
}

/* ---- schedules ------------------------------------------------------------------------- */

/* The POSIX TZ string for a zone, from /etc/timezone.list ("<zone>;<TZ>"
 * lines), Central European by default. */
static void find_timezone(const char *zone, char **tz)
{
    static const char central_europe[] = "CET-1CEST-2,M3.5.0/02:00:00,M10.5.0/03:00:00";
    char line[255];
    gboolean found = FALSE;

    *tz = (char *)malloc(81);
    FILE *list = fopen("/etc/timezone.list", "r");
    if (list == NULL) {
        memcpy(*tz, central_europe, sizeof central_europe);
        return;
    }
    /* The zone is a substring match, so it may be found in several lines
     * ("Europe", one letter): the first match is used, copied bounded to
     * *tz (81), and a last line without '\n' is handled. */
    while (fgets(line, sizeof line, list) != NULL) {
        if (strstr(line, zone) == NULL)
            continue;
        char *semicolon = strchr(line, ';');
        if (semicolon == NULL)
            continue;
        size_t n = strcspn(semicolon + 1, "\r\n");
        if (n > 80)
            n = 80;
        memcpy(*tz, semicolon + 1, n);
        (*tz)[n] = '\0';
        found = TRUE;
        break;
    }
    if (!found)
        memcpy(*tz, central_europe, sizeof central_europe);
    fclose(list);
}

/*
 * The platform's schedules: set the clock and time zone, replace all
 * schedules with the applications' ones and the wake-up/sleep times, and
 * wake up or go to sleep now according to whichever of today's times is
 * nearest.
 */
static void platform_listener_schedule(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    int added = 0;

    if (msg->has_scheduleconfig() != true || controller->platform_socket != sock)
        return;
    const pm::ScheduleConf &conf = msg->scheduleconfig();
    if (conf.has_time() && *conf.gmt().c_str() != '\0') {
        struct timeval now;
        now.tv_sec = conf.time() / 1000;
        now.tv_usec = 0;
        settimeofday(&now, NULL);
        if (conf.has_gmt()) {
            char *tz;
            find_timezone(conf.gmt().c_str(), &tz);
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "Setting timezone to %s", tz);
            /* /etc/TZ is written directly, not through the shell; the log
             * line still shows the equivalent command. It ends with a
             * newline: uClibc ignores a TZ file without one, and every
             * program would fall back to UTC. */
            FILE *tz_file = fopen("/etc/TZ", "w");
            if (tz_file != NULL) {
                fputs(tz, tz_file);
                fputc('\n', tz_file);
                fclose(tz_file);
            }
            g_log(NULL, G_LOG_LEVEL_DEBUG, "echo \"%s\" > /etc/TZ", tz);
            free(tz);
        }
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "list_schedulers");
    schedulerDeleteAllEntry(controller->scheduler);
    for (int i = 0; i < conf.scheduleapp_size(); i++) {
        if (scheduler_add_app(controller, conf.scheduleapp(i)) != NULL)
            added++;
    }
    time_t now = time(NULL);
    struct tm *local = localtime(&now);
    int weekday = local->tm_wday, hour = local->tm_hour, minute = local->tm_min;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "wake_scheduler %d", conf.schedulewakeup_size());
    for (int i = 0; i < conf.schedulewakeup_size(); i++) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "wake_scheduler");
        const pm::ScheduleConf::ScheduleWakeup &wakeup = conf.schedulewakeup(i);
        int day = wakeup.dayweek();
        if (day == 7)
            day = 0;
        schedulerAddWeeklyEntry(controller->scheduler, "E_STATUS_KAROTZ_WAKEUP", day, wakeup.wakeuphour(),
                                wakeup.wakeupminute(), 0);
        schedulerAddWeeklyEntry(controller->scheduler, "E_STATUS_KAROTZ_SLEEP", day, wakeup.sleephour(),
                                wakeup.sleepminute(), 0);
        if (weekday == day && controller->status.karotz != E_STATUS_KAROTZ_INTERACTIVE) {
            int since_wakeup = (hour - wakeup.wakeuphour()) * 60 + (minute - wakeup.wakeupminute());
            int since_sleep = (hour - wakeup.sleephour()) * 60 + (minute - wakeup.sleepminute());
            g_log(NULL, G_LOG_LEVEL_DEBUG, "sched weekday %d, minutes since wake-up %d, since sleep %d", weekday, since_wakeup, since_sleep);
            if (abs(since_wakeup) < abs(since_sleep))
                controller_set_status(controller, since_wakeup < 0 ? E_STATUS_KAROTZ_SLEEP : E_STATUS_KAROTZ_WAKEUP);
            else
                controller_set_status(controller, since_sleep < 0 ? E_STATUS_KAROTZ_WAKEUP : E_STATUS_KAROTZ_SLEEP);
        }
    }
    send_result(sock, msg, added != 0);
}

/* ---- configuration ------------------------------------------------------------------- */

/*
 * A new configuration from the platform, loaded right away when the Karotz
 * is connecting or available, otherwise once it is. The configuration and a
 * karotzinfo_query are both answered with the Karotz's details.
 */
static void platform_listener_config(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_config() == false && msg->has_karotzinfo_query() == false)
        return;
    if (msg->has_config()) {
        if (controller->platform_socket != sock)
            return;
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Received new configuration");
        controller->connect_attempts = 0;
        delete controller->config;
        controller->config = new pm::Config(msg->config());
        if (controller->config->has_playsounds() == true && controller->config->playsounds() == true) {
            if (system("echo 1 > /etc/conf/playsounds") != 0) {
                /* The setting applies anyway (play_sounds below); only the flag
                 * file for the next start is missing. */
            }
            controller->play_sounds = TRUE;
        } else {
            if (system("rm /etc/conf/playsounds") != 0) {
                /* Fails when the file isn't there: nothing to do. */
            }
            controller->play_sounds = FALSE;
        }
        if (controller->status.karotz == E_STATUS_KAROTZ_CONNECTING ||
            controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE) {
            controller_set_status(controller, E_STATUS_KAROTZ_UPDATING_CONF);
        } else {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "New configuration will be loaded later");
            controller->config_pending = TRUE;
        }
    }
    pm::PlatformMessage *answer = new_platform_message();
    karotz_info_fill(answer);
    platform_send(sock, answer);
    delete answer;
}

/* ---- system -------------------------------------------------------------------------- */

static void run_command(const char *message, const char *command)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s", message);
    if (command != NULL && system(command) != 0)
        g_log(NULL, G_LOG_LEVEL_WARNING, "%s: the command failed", message);
}

static void platform_listener_system(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_system() != true || controller->platform_socket != sock)
        return;
    switch (msg->system().command()) {
    /* Telnet is the install's setting (TELNET= in lapinlibre.txt, read by inetd.sh at boot), never
     * the platform's: a root shell with no password. The system log always runs. */
    case pm::TELNET_ON:
    case pm::TELNET_OFF:
        g_log(NULL, G_LOG_LEVEL_WARNING, "Telnet %s from the platform ignored: TELNET= in lapinlibre.txt sets it",
              msg->system().command() == pm::TELNET_ON ? "on" : "off");
        break;
    case pm::LOG_ON:
    case pm::LOG_OFF:
        g_log(NULL, G_LOG_LEVEL_WARNING, "Log %s from the platform ignored: the system log always runs",
              msg->system().command() == pm::LOG_ON ? "on" : "off");
        break;
    case pm::LOG_REMOTE_ON:
        run_command("Log remote ON", NULL);
        break;
    case pm::LOG_REMOTE_OFF:
        run_command("Log remote OFF", NULL);
        break;
    case pm::SYSTEM_REBOOT:
        /* By full path, as for ifconfig (karotz_info.cc): PATH may lack /sbin. */
        run_command("System rebooting", "/sbin/reboot");
        break;
    }
}

/* ---- messages -------------------------------------------------------------------------- */

/*
 * Karotz messages go between the platform and the application: queries to
 * the platform, its responses to the application. A pending-message flag
 * changes the LED.
 */
static void platform_listener_karotz_message(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    (void)sock;
    if (msg->has_karotzmessagequery()) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "platform_listener_karotzMessage: KarotzMessageQuery");
        platform_send(controller->platform_socket, msg);
    } else if (msg->has_karotzmessageresponse()) {
        platform_send(controller->interactive_socket, msg);
    } else if (msg->has_pendingmessage() == true && msg->pendingmessage() != (bool)controller->pending_message) {
        controller->pending_message = msg->pendingmessage();
        if (!controller->pending_message)
            ledPulse(controller->led, NULL, LED_GREEN, LED_OFF, PULSE_PERIOD, LED_FOREVER);
        else
            ledPulse(controller->led, NULL, LED_GREEN, LED_BLUE, PULSE_PERIOD, LED_FOREVER);
    }
}

/* ---- serial ---------------------------------------------------------------------------- */

static void platform_listener_serial(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_serial() == true && msg->has_interactiveid() == true) {
        if (!controller_is_interactive_id(controller, msg->interactiveid().c_str()))
            return;
        gboolean ok = FALSE; /* stays FALSE when nothing is asked */
        const pm::Serial &serial = msg->serial();
        if (serial.has_open())
            ok = serialOpen(controller->serial, serial.open().port().c_str(), serial.open().baud());
        if (serial.has_write())
            ok = serialWrite(controller->serial, serial.write().port().c_str(), serial.write().data().c_str(),
                             serial.write().base().c_str());
        if (serial.has_close())
            ok = serialClose(controller->serial, serial.close().port().c_str());
        send_result(sock, msg, ok);
    }
    if (msg->has_serialcallback() == true && msg->has_interactiveid() == true) {
        if (controller_is_interactive_id(controller, msg->interactiveid().c_str()) &&
            msg->serialcallback().has_readline()) {
            const pm::SerialCallback::Readline &readline = msg->serialcallback().readline();
            serialReadLineMode(controller->serial, readline.port().c_str(), readline.base().c_str());
        }
    }
}

/* ---- contacts and social --------------------------------------------------------------- */

/* The platform sends the contact list; applications ask for it. */
static void platform_listener_contacts(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (msg->has_getcontactlist()) {
        pm::PlatformMessage *response;
        if (controller->contacts == NULL) {
            response = new_response(msg, pm::ERROR, NULL);
        } else {
            response = new_response(msg, pm::OK, NULL);
            response->mutable_contactlist()->CopyFrom(*controller->contacts);
        }
        platform_send(sock, response);
        delete response;
    } else if (msg->has_contactlist() == true && controller->platform_socket == sock) {
        delete controller->contacts;
        controller->contacts = new pm::ContactList(msg->contactlist());
    }
}

static void platform_listener_social(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    if (!msg->has_socialquery())
        return;
    pm::PlatformMessage *response;
    if (controller->config == NULL) {
        response = new_response(msg, pm::ERROR, NULL);
    } else {
        response = new_response(msg, pm::OK, NULL);
        response->mutable_social()->CopyFrom(controller->config->social());
    }
    platform_send(sock, response);
    delete response;
}

/* ---- dock ------------------------------------------------------------------------------ */

/* The dock's LED and motors. The first pulse color is "n": the current colour
 * (as for the rabbit's own LED pulse). */
static void platform_listener_dock(ControllerObject *controller, int sock, const pm::PlatformMessage *msg)
{
    gboolean ok = FALSE;

    if (msg->has_dock() != true || msg->has_interactiveid() != true)
        return;
    if (!controller_is_interactive_id(controller, msg->interactiveid().c_str()))
        return;
    const char *id = msg->id().c_str();
    const pm::Dock &dock = msg->dock();
    if (dock.has_led()) {
        const pm::Led &led = dock.led();
        if (led.has_light())
            ok = dockLedLight(controller->dock, id, led.light().color().c_str());
        else if (led.has_fade())
            ok = dockLedFade(controller->dock, id, led.fade().color().c_str(), led.fade().period());
        else if (led.has_pulse())
            ok = dockLedPulse(controller->dock, id, "n", led.pulse().color().c_str(), led.pulse().period(),
                              led.pulse().pulse());
    } else if (dock.has_move()) {
        ok = dockMove(controller->dock, id, dock.move().cmd(), dock.move().time());
    }
    send_result(sock, msg, ok);
}

/* ---- dispatch -------------------------------------------------------------------------- */

void platform_dispatch(ControllerObject *controller, int sock, pm::PlatformMessage *msg)
{
    platform_listener_authentication(controller, sock, msg);
    platform_listener_karotzinfo(controller, sock, msg);
    platform_listener_ears(controller, sock, msg);
    platform_listener_activity(controller, sock, msg);
    platform_listener_interactive_mode_start(controller, sock, msg);
    platform_listener_interactive_mode_stop(controller, sock, msg);
    platform_listener_led_light(controller, sock, msg);
    platform_listener_led_fade(controller, sock, msg);
    platform_listener_led_pulse(controller, sock, msg);
    platform_listener_asr(controller, sock, msg);
    platform_listener_multimedia(controller, sock, msg);
    platform_listener_tts(controller, sock, msg);
    platform_listener_k2k(controller, sock, msg);
    platform_listener_webcam(controller, sock, msg);
    platform_listener_choreography(controller, sock, msg);
    platform_listener_schedule(controller, sock, msg);
    platform_listener_config(controller, sock, msg);
    platform_listener_system(controller, sock, msg);
    platform_listener_ping(controller, sock, msg);
    platform_listener_bad_interactive_id(controller, sock, msg);
    platform_listener_karotz_message(controller, sock, msg);
    platform_listener_serial(controller, sock, msg);
    platform_listener_contacts(controller, sock, msg);
    platform_listener_social(controller, sock, msg);
    platform_listener_dock(controller, sock, msg);
}
