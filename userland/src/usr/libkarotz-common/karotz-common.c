/* SPDX-License-Identifier: MIT */
/*
 * libkarotz-common: helpers and the D-Bus client calls shared by the Karotz
 * programs.
 */
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <uuid/uuid.h>

#include "karotz-common.h"

#define CALL_FAILED "Call to dbus_g_proxy_call failed"

/*
 * The usual call: FALSE without a proxy; on failure report `message` and
 * return FALSE; otherwise return the daemon's boolean result. The variadic
 * part is the input arguments, G_TYPE_INVALID, then any outputs that come
 * before the boolean result.
 */
#define RESULT_CALL(proxy, message, method, ...)                                                         \
    do {                                                                                                 \
        GError *error = NULL;                                                                            \
        gboolean result = FALSE;                                                                         \
        if ((proxy) == NULL)                                                                             \
            return FALSE;                                                                                \
        if (!dbus_g_proxy_call((proxy), (method), &error, __VA_ARGS__, G_TYPE_BOOLEAN, &result,          \
                               G_TYPE_INVALID)) {                                                        \
            handle_error((message), error, 0);                                                           \
            return FALSE;                                                                                \
        }                                                                                                \
        return result != 0;                                                                              \
    } while (0)

/* A call without a result: FALSE without a proxy or on failure, else TRUE. */
#define PLAIN_CALL(proxy, method, ...)                                                                   \
    do {                                                                                                 \
        GError *error = NULL;                                                                            \
        if ((proxy) == NULL)                                                                             \
            return FALSE;                                                                                \
        if (!dbus_g_proxy_call((proxy), (method), &error, __VA_ARGS__, G_TYPE_INVALID)) {                \
            handle_error(CALL_FAILED, error, 0);                                                         \
            return FALSE;                                                                                \
        }                                                                                                \
        return TRUE;                                                                                     \
    } while (0)

/* --- helpers ----------------------------------------------------------------- */

void die(const char *message, GError *error)
{
    g_log(NULL, G_LOG_LEVEL_ERROR, "%s: %s\n", message, error->message);
    for (;;)
        ;
}

void handle_error(const char *message, GError *error, int fatal)
{
    g_print("%s: %s\n", message, error->message);
    g_error_free(error);
    if (fatal)
        exit(1);
}

void to_lower(char *s)
{
    int i;
    for (i = 0; s[i] != '\0'; i++)
        s[i] = tolower((unsigned char)s[i]);
}

int generate_uuid(char *out)
{
    uuid_t uuid;
    uuid_generate(uuid);
    uuid_unparse(uuid, out);
    to_lower(out);
    return 1;
}

gboolean dbus_load_proxy(DBusGProxy **proxy, KarotzProxy which, DBusGConnection *bus)
{
    /* Every service is reached through the interface com.mindscape.karotz.KarotzInterface. */
    static const struct {
        const char *service, *path, *label;
    } services[] = {
        [KAROTZ_PROXY_LED] = { "com.mindscape.karotz.Led", "/com/mindscape/karotz/Led", "LED" },
        [KAROTZ_PROXY_EARS] = { "com.mindscape.karotz.Ears", "/com/mindscape/karotz/Ears", "EARS" },
        [KAROTZ_PROXY_VOICE] = { "com.mindscape.karotz.Voice", "/com/mindscape/karotz/Voice", "TTS" },
        [KAROTZ_PROXY_RFID] = { "com.mindscape.karotz.Rfid", "/com/mindscape/karotz/Rfid", "RFID" },
        [KAROTZ_PROXY_BUTTON] = { "com.mindscape.karotz.Button", "/com/mindscape/karotz/Button", "BUTTON" },
        [KAROTZ_PROXY_MULTIMEDIA] = { "com.mindscape.karotz.Multimedia", "/com/mindscape/karotz/Multimedia", "MULTIMEDIA" },
        [KAROTZ_PROXY_CONTROLLER] = { "com.mindscape.karotz.Controller", "/com/mindscape/karotz/Controller", "CONTROLLER" },
        [KAROTZ_PROXY_CHOREOGRAPHY_MANAGER] = { "com.mindscape.karotz.Choreography_manager",
                                                "/com/mindscape/karotz/Choreography_manager", "CHOREOGRAPHY_MANAGER" },
        [KAROTZ_PROXY_WEBCAM] = { "com.mindscape.karotz.Webcam", "/com/mindscape/karotz/Webcam", "WEBCAM" },
        [KAROTZ_PROXY_SCHEDULER] = { "com.mindscape.karotz.Scheduler", "/com/mindscape/karotz/Scheduler", "SCHEDULER" },
        [KAROTZ_PROXY_K2K] = { "com.mindscape.karotz.K2k", "/com/mindscape/karotz/K2k", "K2K" },
        [KAROTZ_PROXY_SERIAL] = { "com.mindscape.karotz.Serial", "/com/mindscape/karotz/Serial", "SERIAL" },
        [KAROTZ_PROXY_DOCK] = { "com.mindscape.karotz.Dock", "/com/mindscape/karotz/Dock", "DOCK" },
    };
    /* The numbers without a service (2, 8) are unknown too. */
    gboolean known = (unsigned)which < G_N_ELEMENTS(services) && services[which].service != NULL;
    const char *label = known ? services[which].label : "unknown";

    if (!known || proxy == NULL) {
        /* Nothing to create, or nowhere to put it: report it by the
         * service's name. */
        g_warning("Failed to create proxy for %s Module", label);
        return FALSE;
    }
    *proxy = dbus_g_proxy_new_for_name(bus, services[which].service, services[which].path, "com.mindscape.karotz.KarotzInterface");
    return TRUE;
}

/* --- controller and system ------------------------------------------------------ */

gboolean choreographyLaunchScript(DBusGProxy *p, const char *script, char **out, int arg)
{
    RESULT_CALL(p, CALL_FAILED, "launchScript", G_TYPE_STRING, script, G_TYPE_INT, arg, G_TYPE_INVALID,
                G_TYPE_STRING, out);
}

gboolean controllerGetStatus(DBusGProxy *p, int *karotz, int *network, int *platform)
{
    PLAIN_CALL(p, "get_status", G_TYPE_INVALID, G_TYPE_INT, karotz, G_TYPE_INT, network, G_TYPE_INT, platform);
}

gboolean controllerTakeControl(DBusGProxy *p, gboolean *success, char **interactive_id)
{
    PLAIN_CALL(p, "take_control", G_TYPE_INVALID, G_TYPE_BOOLEAN, success, G_TYPE_STRING, interactive_id);
}

gboolean controllerReleaseControl(DBusGProxy *p, const char *interactive_id)
{
    PLAIN_CALL(p, "release_control", G_TYPE_STRING, interactive_id, G_TYPE_INVALID);
}

/* --- dock (no such service in this firmware) --------------------------------------- */

gboolean dockLedLight(DBusGProxy *p, const char *id, const char *color)
{
    RESULT_CALL(p, CALL_FAILED, "light", G_TYPE_STRING, id, G_TYPE_STRING, color, G_TYPE_INVALID);
}

gboolean dockLedFade(DBusGProxy *p, const char *id, const char *color, int period)
{
    RESULT_CALL(p, CALL_FAILED, "fade", G_TYPE_STRING, id, G_TYPE_STRING, color, G_TYPE_INT, period, G_TYPE_INVALID);
}

gboolean dockLedPulse(DBusGProxy *p, const char *id, const char *color1, const char *color2, int period, int length)
{
    RESULT_CALL(p, CALL_FAILED, "pulse", G_TYPE_STRING, id, G_TYPE_STRING, color1, G_TYPE_STRING, color2, G_TYPE_INT,
                period, G_TYPE_INT, length, G_TYPE_INVALID);
}

gboolean dockMove(DBusGProxy *p, const char *id, int a, int b)
{
    RESULT_CALL(p, CALL_FAILED, "move", G_TYPE_STRING, id, G_TYPE_INT, a, G_TYPE_INT, b, G_TYPE_INVALID);
}

/* --- ears ------------------------------------------------------------------------- */

gboolean earsReset(DBusGProxy *p, const char *id, const char *ears)
{
    GError *error = NULL;
    gboolean result = FALSE;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "earsReset");
    if (p == NULL)
        return FALSE;
    /* Unlike the other calls, a failure is not reported (or freed). */
    if (!dbus_g_proxy_call(p, "reset", &error, G_TYPE_STRING, id, G_TYPE_STRING, ears, G_TYPE_INVALID,
                           G_TYPE_BOOLEAN, &result, G_TYPE_INVALID))
        return FALSE;
    return result != 0;
}

gboolean earsMove(DBusGProxy *p, const char *id, int l_position, int l_speed, int r_position, int r_speed)
{
    RESULT_CALL(p, CALL_FAILED, "move", G_TYPE_STRING, id, G_TYPE_INT, l_position, G_TYPE_INT, l_speed, G_TYPE_INT,
                r_position, G_TYPE_INT, r_speed, G_TYPE_INVALID);
}

gboolean earsMoveLeft(DBusGProxy *p, const char *id, int l_position, int l_speed)
{
    RESULT_CALL(p, CALL_FAILED, "move_left", G_TYPE_STRING, id, G_TYPE_INT, l_position, G_TYPE_INT, l_speed,
                G_TYPE_INVALID);
}

gboolean earsMoveRight(DBusGProxy *p, const char *id, int r_position, int r_speed)
{
    RESULT_CALL(p, CALL_FAILED, "move_right", G_TYPE_STRING, id, G_TYPE_INT, r_position, G_TYPE_INT, r_speed,
                G_TYPE_INVALID);
}

gboolean earsMoveStep(DBusGProxy *p, const char *id, int l_position, int l_speed, int r_position, int r_speed)
{
    RESULT_CALL(p, CALL_FAILED, "move_step", G_TYPE_STRING, id, G_TYPE_INT, l_position, G_TYPE_INT, l_speed,
                G_TYPE_INT, r_position, G_TYPE_INT, r_speed, G_TYPE_INVALID);
}

gboolean earsMoveLeftStep(DBusGProxy *p, const char *id, int l_position, int l_speed)
{
    RESULT_CALL(p, CALL_FAILED, "move_left_step", G_TYPE_STRING, id, G_TYPE_INT, l_position, G_TYPE_INT, l_speed,
                G_TYPE_INVALID);
}

gboolean earsMoveRightStep(DBusGProxy *p, const char *id, int r_position, int r_speed)
{
    RESULT_CALL(p, CALL_FAILED, "move_right_step", G_TYPE_STRING, id, G_TYPE_INT, r_position, G_TYPE_INT, r_speed,
                G_TYPE_INVALID);
}

/*
 * --- k2k ---------------------------------------------------------------------------
 * As in every RESULT_CALL, the output list ends with G_TYPE_INVALID after
 * the boolean result, so dbus-glib reads that one output only. Without the
 * terminator it would take the next stack word as another output type, and
 * unless that word was 0, a call the daemon carried out and answered would
 * still fail ("Too few
 * arguments in reply"): the controller would then answer the platform's k2k
 * requests with ERROR.
 */

gboolean k2kCanRing(DBusGProxy *p, gboolean can_ring)
{
    RESULT_CALL(p, CALL_FAILED, "k2k_can_ring", G_TYPE_BOOLEAN, can_ring, G_TYPE_INVALID);
}

gboolean k2kCall(DBusGProxy *p, const char *uri)
{
    RESULT_CALL(p, CALL_FAILED, "k2k_call", G_TYPE_STRING, uri, G_TYPE_INVALID);
}

gboolean k2kAnswer(DBusGProxy *p, gboolean answer)
{
    RESULT_CALL(p, CALL_FAILED, "k2k_answer", G_TYPE_BOOLEAN, answer, G_TYPE_INVALID);
}

gboolean k2kHangup(DBusGProxy *p)
{
    RESULT_CALL(p, CALL_FAILED, "k2k_hangup", G_TYPE_INVALID);
}

gboolean k2kUseAccount(DBusGProxy *p, const char *a, const char *b, const char *c, const char *d)
{
    RESULT_CALL(p, CALL_FAILED, "k2k_use_account", G_TYPE_STRING, a, G_TYPE_STRING, b, G_TYPE_STRING, c,
                G_TYPE_STRING, d, G_TYPE_INVALID);
}

/* --- led ------------------------------------------------------------------------- */

gboolean ledLight(DBusGProxy *p, const char *id, const char *color)
{
    RESULT_CALL(p, CALL_FAILED, "light", G_TYPE_STRING, id, G_TYPE_STRING, color, G_TYPE_INVALID);
}

gboolean ledFade(DBusGProxy *p, const char *id, const char *color, int period)
{
    RESULT_CALL(p, CALL_FAILED, "fade", G_TYPE_STRING, id, G_TYPE_STRING, color, G_TYPE_INT, period, G_TYPE_INVALID);
}

gboolean ledPulse(DBusGProxy *p, const char *id, const char *color1, const char *color2, int period, int length)
{
    RESULT_CALL(p, CALL_FAILED, "pulse", G_TYPE_STRING, id, G_TYPE_STRING, color1, G_TYPE_STRING, color2, G_TYPE_INT,
                period, G_TYPE_INT, length, G_TYPE_INVALID);
}

/* --- multimedia ------------------------------------------------------------------- */

gboolean multimediaPlay(DBusGProxy *p, const char *id, const char *url, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "play", G_TYPE_STRING, id, G_TYPE_STRING, url, G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

gboolean multimediaPause(DBusGProxy *p, const char *id, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "pause", G_TYPE_STRING, id, G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

gboolean multimediaStop(DBusGProxy *p, const char *id, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "stop", G_TYPE_STRING, id, G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

gboolean multimediaResume(DBusGProxy *p, const char *id, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "resume", G_TYPE_STRING, id, G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

gboolean multimediaRecord(DBusGProxy *p, const char *id, const char *url, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "record", G_TYPE_STRING, id, G_TYPE_STRING, url, G_TYPE_INT, behaviour,
                G_TYPE_INVALID);
}

/* next and previous send an empty id. */
gboolean multimediaNext(DBusGProxy *p, int behaviour)
{
    char id = '\0';
    RESULT_CALL(p, "Call to dbus_g_proxy_call failed!", "next", G_TYPE_STRING, &id, G_TYPE_INT, behaviour,
                G_TYPE_INVALID);
}

gboolean multimediaPrevious(DBusGProxy *p, int behaviour)
{
    char id = '\0';
    RESULT_CALL(p, CALL_FAILED, "previous", G_TYPE_STRING, &id, G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

gboolean multimediaGetArtist(DBusGProxy *p, char **artistList)
{
    RESULT_CALL(p, CALL_FAILED, "get_artist", G_TYPE_INVALID, G_TYPE_STRING, artistList);
}

gboolean multimediaGetFolder(DBusGProxy *p, char **folderList)
{
    RESULT_CALL(p, CALL_FAILED, "get_folder", G_TYPE_INVALID, G_TYPE_STRING, folderList);
}

gboolean multimediaGetPlaylist(DBusGProxy *p, char **playlistList)
{
    RESULT_CALL(p, CALL_FAILED, "get_playlist", G_TYPE_INVALID, G_TYPE_STRING, playlistList);
}

gboolean multimediaGetGenre(DBusGProxy *p, char **genreList)
{
    RESULT_CALL(p, CALL_FAILED, "get_genre", G_TYPE_INVALID, G_TYPE_STRING, genreList);
}

gboolean multimediaGetSong(DBusGProxy *p, char **songList)
{
    RESULT_CALL(p, CALL_FAILED, "get_song", G_TYPE_INVALID, G_TYPE_STRING, songList);
}

/* --- rfid ------------------------------------------------------------------------ */

gboolean rfidGetTags(DBusGProxy *p, char **tags)
{
    RESULT_CALL(p, CALL_FAILED, "getTags", G_TYPE_INVALID, G_TYPE_STRING, tags);
}

/* --- scheduler ---------------------------------------------------------------------- */

gboolean schedulerAddDateEntry(DBusGProxy *p, const char *idScheduler, int year, int month, int day, int hour,
                               int minute, int second)
{
    RESULT_CALL(p, "Call to dbus_g_proxy_call add_date_entry failed", "add_date_entry", G_TYPE_STRING, idScheduler,
                G_TYPE_INT, year, G_TYPE_INT, month, G_TYPE_INT, day, G_TYPE_INT, hour, G_TYPE_INT, minute,
                G_TYPE_INT, second, G_TYPE_INVALID);
}

gboolean schedulerAddWeeklyEntry(DBusGProxy *p, const char *idScheduler, int week, int hour, int minute, int second)
{
    RESULT_CALL(p, "Call to dbus_g_proxy_call add_weekly_entry failed", "add_weekly_entry", G_TYPE_STRING,
                idScheduler, G_TYPE_INT, week, G_TYPE_INT, hour, G_TYPE_INT, minute, G_TYPE_INT, second,
                G_TYPE_INVALID);
}

gboolean schedulerAddPeriodicEntry(DBusGProxy *p, const char *idScheduler, int period, int delta)
{
    RESULT_CALL(p, "Call to dbus_g_proxy_call add_periodic_entry failed", "add_periodic_entry", G_TYPE_STRING,
                idScheduler, G_TYPE_INT, period, G_TYPE_INT, delta, G_TYPE_INVALID);
}

gboolean schedulerDeleteAllEntry(DBusGProxy *p)
{
    RESULT_CALL(p, "Call to dbus_g_proxy_call delete_all_entry failed", "delete_all_entry", G_TYPE_INVALID);
}

gboolean schedulerDeleteEntry(DBusGProxy *p, const char *idScheduler)
{
    RESULT_CALL(p, "Call to dbus_g_proxy_call delete_entry failed", "delete_entry", G_TYPE_STRING, idScheduler,
                G_TYPE_INVALID);
}

/* --- serial ------------------------------------------------------------------------- */

gboolean serialOpen(DBusGProxy *p, const char *port, int baud)
{
    RESULT_CALL(p, CALL_FAILED, "open", G_TYPE_STRING, port, G_TYPE_INT, baud, G_TYPE_INVALID);
}

gboolean serialWrite(DBusGProxy *p, const char *port, const char *data, const char *base)
{
    RESULT_CALL(p, CALL_FAILED, "write", G_TYPE_STRING, port, G_TYPE_STRING, data, G_TYPE_STRING, base,
                G_TYPE_INVALID);
}

gboolean serialClose(DBusGProxy *p, const char *port)
{
    RESULT_CALL(p, CALL_FAILED, "close", G_TYPE_STRING, port, G_TYPE_INVALID);
}

gboolean serialReadLineMode(DBusGProxy *p, const char *port, const char *base)
{
    RESULT_CALL(p, CALL_FAILED, "readlinemode", G_TYPE_STRING, port, G_TYPE_STRING, base, G_TYPE_INVALID);
}

/* --- voice ------------------------------------------------------------------------- */

gboolean ttsSpeak(DBusGProxy *p, const char *id, const char *text, const char *lang, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "tts_speak", G_TYPE_STRING, id, G_TYPE_STRING, text, G_TYPE_STRING, lang, G_TYPE_INT,
                behaviour, G_TYPE_INVALID);
}

gboolean ttsStop(DBusGProxy *p, const char *id, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "tts_stop", G_TYPE_STRING, id, G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

gboolean asrStop(DBusGProxy *p, const char *id, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "asr_stop", G_TYPE_STRING, id, G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

gboolean asrStartFile(DBusGProxy *p, const char *uid, const char *filename, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "asr_start_file", G_TYPE_STRING, uid, G_TYPE_STRING, filename, G_TYPE_INT, behaviour,
                G_TYPE_INVALID);
}

gboolean asrStartString(DBusGProxy *p, const char *uid, const char *grammar, const char *lang, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "asr_start_string", G_TYPE_STRING, uid, G_TYPE_STRING, grammar, G_TYPE_STRING, lang,
                G_TYPE_INT, behaviour, G_TYPE_INVALID);
}

/* --- webcam ------------------------------------------------------------------------ */

gboolean webcamTakeSnapshot(DBusGProxy *p, const char *id, const char *snapURL, int behaviour)
{
    RESULT_CALL(p, CALL_FAILED, "takeSnapshot", G_TYPE_STRING, id, G_TYPE_STRING, snapURL, G_TYPE_INT, behaviour,
                G_TYPE_INVALID);
}
