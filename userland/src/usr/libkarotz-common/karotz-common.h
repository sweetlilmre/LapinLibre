/* SPDX-License-Identifier: MIT */
/*
 * libkarotz-common: helpers and the D-Bus client calls shared by the Karotz
 * programs. Each call takes a proxy from dbus_load_proxy() and returns
 * FALSE if the proxy is NULL or the call fails; otherwise most return the
 * daemon's boolean result.
 */
#ifndef KAROTZ_COMMON_H
#define KAROTZ_COMMON_H

#include <dbus/dbus-glib.h>
#include <glib.h>

/* Log `message` and the error at G_LOG_LEVEL_ERROR (fatal), then hang. */
void die(const char *message, GError *error) G_GNUC_NORETURN;

/* Print `message` and the error, free it, and exit(1) if `fatal`. */
void handle_error(const char *message, GError *error, int fatal);

void to_lower(char *s);

/* Write a new lower-case UUID into `out` (at least 37 bytes). Returns 1. */
int generate_uuid(char *out);

/* The services dbus_load_proxy() can connect to. */
typedef enum {
    KAROTZ_PROXY_LED = 0,
    KAROTZ_PROXY_EARS = 1,
    KAROTZ_PROXY_VOICE = 3,
    KAROTZ_PROXY_RFID = 4,
    KAROTZ_PROXY_BUTTON = 5,
    KAROTZ_PROXY_MULTIMEDIA = 6,
    KAROTZ_PROXY_CONTROLLER = 7,
    KAROTZ_PROXY_CHOREOGRAPHY_MANAGER = 9,
    KAROTZ_PROXY_WEBCAM = 10,
    KAROTZ_PROXY_SCHEDULER = 11,
    KAROTZ_PROXY_K2K = 12,
    KAROTZ_PROXY_SERIAL = 13,
    KAROTZ_PROXY_DOCK = 14,
} KarotzProxy;

/* Store a proxy for `which` in *proxy. Returns FALSE if `proxy` is NULL. */
gboolean dbus_load_proxy(DBusGProxy **proxy, KarotzProxy which, DBusGConnection *bus);

gboolean choreographyLaunchScript(DBusGProxy *p, const char *script, char **out, int arg);

gboolean controllerGetStatus(DBusGProxy *p, int *karotz, int *network, int *platform);
gboolean controllerTakeControl(DBusGProxy *p, gboolean *success, char **interactive_id);
gboolean controllerReleaseControl(DBusGProxy *p, const char *interactive_id);

gboolean dockLedLight(DBusGProxy *p, const char *id, const char *color);
gboolean dockLedFade(DBusGProxy *p, const char *id, const char *color, int period);
gboolean dockLedPulse(DBusGProxy *p, const char *id, const char *color1, const char *color2, int period, int length);
gboolean dockMove(DBusGProxy *p, const char *id, int a, int b);

gboolean earsReset(DBusGProxy *p, const char *id, const char *ears);
gboolean earsMove(DBusGProxy *p, const char *id, int l_position, int l_speed, int r_position, int r_speed);
gboolean earsMoveLeft(DBusGProxy *p, const char *id, int l_position, int l_speed);
gboolean earsMoveRight(DBusGProxy *p, const char *id, int r_position, int r_speed);
gboolean earsMoveStep(DBusGProxy *p, const char *id, int l_position, int l_speed, int r_position, int r_speed);
gboolean earsMoveLeftStep(DBusGProxy *p, const char *id, int l_position, int l_speed);
gboolean earsMoveRightStep(DBusGProxy *p, const char *id, int r_position, int r_speed);

gboolean k2kCanRing(DBusGProxy *p, gboolean can_ring);
gboolean k2kCall(DBusGProxy *p, const char *uri);
gboolean k2kAnswer(DBusGProxy *p, gboolean answer);
gboolean k2kHangup(DBusGProxy *p);
gboolean k2kUseAccount(DBusGProxy *p, const char *a, const char *b, const char *c, const char *d);

/* LED colours the programs use, as "RRGGBB". */
#define LED_OFF          "000000"
#define LED_RED          "FF0000"
#define LED_GREEN        "00FF00"
#define LED_BLUE         "0000FF"
#define LED_CYAN         "00FFFF"
#define LED_ORANGE       "FFA500"
#define LED_SEA_GREEN    "00FF9F"
#define LED_LIGHT_GREEN  "4FFF68"
/* ledPulse() length: pulse until the next LED command. */
#define LED_FOREVER      (-1)

gboolean ledLight(DBusGProxy *p, const char *id, const char *color);
gboolean ledFade(DBusGProxy *p, const char *id, const char *color, int period);
gboolean ledPulse(DBusGProxy *p, const char *id, const char *color1, const char *color2, int period, int length);

gboolean multimediaPlay(DBusGProxy *p, const char *id, const char *url, int behaviour);
gboolean multimediaPause(DBusGProxy *p, const char *id, int behaviour);
gboolean multimediaStop(DBusGProxy *p, const char *id, int behaviour);
gboolean multimediaResume(DBusGProxy *p, const char *id, int behaviour);
gboolean multimediaRecord(DBusGProxy *p, const char *id, const char *url, int behaviour);
gboolean multimediaNext(DBusGProxy *p, int behaviour);
gboolean multimediaPrevious(DBusGProxy *p, int behaviour);
gboolean multimediaGetArtist(DBusGProxy *p, char **artistList);
gboolean multimediaGetFolder(DBusGProxy *p, char **folderList);
gboolean multimediaGetPlaylist(DBusGProxy *p, char **playlistList);
gboolean multimediaGetGenre(DBusGProxy *p, char **genreList);
gboolean multimediaGetSong(DBusGProxy *p, char **songList);

gboolean rfidGetTags(DBusGProxy *p, char **tags);

gboolean schedulerAddDateEntry(DBusGProxy *p, const char *idScheduler, int year, int month, int day, int hour, int minute, int second);
gboolean schedulerAddWeeklyEntry(DBusGProxy *p, const char *idScheduler, int week, int hour, int minute, int second);
gboolean schedulerAddPeriodicEntry(DBusGProxy *p, const char *idScheduler, int period, int delta);
gboolean schedulerDeleteAllEntry(DBusGProxy *p);
gboolean schedulerDeleteEntry(DBusGProxy *p, const char *idScheduler);

gboolean serialOpen(DBusGProxy *p, const char *port, int baud);
gboolean serialWrite(DBusGProxy *p, const char *port, const char *data, const char *base);
gboolean serialClose(DBusGProxy *p, const char *port);
gboolean serialReadLineMode(DBusGProxy *p, const char *port, const char *base);

gboolean ttsSpeak(DBusGProxy *p, const char *id, const char *text, const char *lang, int behaviour);
gboolean ttsStop(DBusGProxy *p, const char *id, int behaviour);
gboolean asrStop(DBusGProxy *p, const char *id, int behaviour);
gboolean asrStartFile(DBusGProxy *p, const char *uid, const char *filename, int behaviour);
gboolean asrStartString(DBusGProxy *p, const char *uid, const char *grammar, const char *lang, int behaviour);

gboolean webcamTakeSnapshot(DBusGProxy *p, const char *id, const char *snapURL, int behaviour);

#endif
