/* SPDX-License-Identifier: MIT */
/*
 * multimedia-daemon: music playback and microphone recording for D-Bus
 * clients, as com.mindscape.karotz.Multimedia (/com/mindscape/karotz/Multimedia).
 *
 * Playback has two back ends:
 *  - karotz-player, for plain paths and URLs: the first play forks
 *    "karotz-player -include /etc/conf/karotz-player.conf <url>" (with -playlist for
 *    .asx/.m3u/.pls); later plays, pause and resume are slave commands written
 *    to the FIFO /tmp/karotz-player.ctrl (karotz-player.conf names it with input=file=),
 *    and stop kills the process.
 *  - MPD, for "command::argument" URLs (playlist::, title::, dir::, artist::,
 *    genre::, allsong::, plus mode:: and lock:: settings), through libmpd on
 *    127.0.0.1:6600. The daemon starts mpd itself if it can't connect, and
 *    checks the connection every second.
 * The get_* methods list the MPD database ("a:b:c:" strings).
 *
 * record(id, url) captures 16 kHz mono from the OSS device /dev/audio and pipes
 * it to flac (/tmp/record.flac) for up to 90 s, then emits
 * multi_recordfinished(id, 4). Playback state changes are reported with
 * multi_callback(action) (1 play, 2 stop, 3 pause, 5 database changed, 6 an
 * external MPD client appeared) and, on stop or pause,
 * multi_playfinished(id, 4).
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/ioctl.h>
#include <sys/soundcard.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>
#include <libmpd/libmpd.h>

#include "karotz-common.h"
#include "karotz-messages.h"
#include "karotz-marshal.h"

/*
 * Extensions in the rabbit's libmpd 0.17.0 that stock libmpd lacks (so the
 * daemon only links against the rabbit's), declared here: a
 * genre list like mpd_database_get_artists(), the number of other
 * MPD clients, and the status-changed flag that reports a change of it.
 */
MpdData *mpd_database_get_genres(MpdObj *mi);
int mpd_status_get_count_externalClient(MpdObj *mi);
#define MPD_CST_EXTERNAL_CLIENT 0x10000

#define MPD_HOST "127.0.0.1"
#define MPD_PORT 6600
#define MPD_PASSWORD "karotz_admin"

#define PLAYER_CTRL "/tmp/karotz-player.ctrl"
#define RECORD_TIMEOUT_MS 90000
#define RECORD_COMMAND \
    "flac - --endian=little --channels=1 --bps=16 --sample-rate=16000 --sign=unsigned   -o /tmp/record.flac -f"

/* Playback state (status_changed() and the API keep it in `state`). */
enum {
    STATE_STOPPED = 0,
    STATE_MPD_PLAYING = 1,
    STATE_MPD_PAUSED = 2,
    STATE_PLAYER_PLAYING = 3,
    STATE_PLAYER_PAUSED = 4,
};

/* Argument of the multi_callback signal. */
enum {
    ACTION_PLAY = 1,
    ACTION_STOP = 2,
    ACTION_PAUSE = 3,
    ACTION_DATABASE_CHANGED = 5,
    ACTION_EXTERNAL_CLIENT = 6,
};

/* Second argument of multi_playfinished / multi_recordfinished. */
#define EVENT_ERROR 2
#define EVENT_DONE 4

enum {
    SIGNAL_REGISTERED,
    SIGNAL_PLAYFINISHED,
    SIGNAL_RECORDFINISHED,
    SIGNAL_CALLBACK,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    DBusGConnection *bus;   /* set once the name is owned */
    DBusGProxy *bus_proxy;
    GString *uid;           /* id of the last successful play, for multi_playfinished */
    MpdObj *mpd;
    int player_pid;        /* -1 if player isn't running */
    GString *record_uuid;   /* id of the running recording, "" if none */
    FILE *record_pipe;      /* flac's stdin */
    guint record_timeout;
    int audio_fd;           /* /dev/audio, 0 once a recording is stopped */
    int skip_silence;       /* still skipping the device's initial silence */
} MultimediaObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} MultimediaObjectClass;

GType multimedia_object_get_type(void);
#define MULTIMEDIA_TYPE_OBJECT (multimedia_object_get_type())

G_DEFINE_TYPE(MultimediaObject, multimedia_object, G_TYPE_OBJECT)

gboolean multimedia_object_play(MultimediaObject *self, const char *id, char *url, int behaviour, gboolean *result,
                                GError **error);
gboolean multimedia_object_pause(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                 GError **error);
gboolean multimedia_object_next(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                GError **error);
gboolean multimedia_object_previous(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                    GError **error);
gboolean multimedia_object_resume(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                  GError **error);
gboolean multimedia_object_stop(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                GError **error);
gboolean multimedia_object_record(MultimediaObject *self, const char *id, const char *url, int behaviour,
                                  gboolean *result, GError **error);
gboolean multimedia_object_get_artist(MultimediaObject *self, char **list, gboolean *result, GError **error);
gboolean multimedia_object_get_folder(MultimediaObject *self, char **list, gboolean *result, GError **error);
gboolean multimedia_object_get_playlist(MultimediaObject *self, char **list, gboolean *result, GError **error);
gboolean multimedia_object_get_genre(MultimediaObject *self, char **list, gboolean *result, GError **error);
gboolean multimedia_object_get_song(MultimediaObject *self, char **list, gboolean *result, GError **error);

#include "multimedia-daemon-glue.h"

static MultimediaObject *multimedia;
static int state;                    /* STATE_* */
static int saved_volume = 100;       /* volume restored before MPD plays */
static int output_enabled;           /* lock::no enabled the MPD output: track volume changes */
static int external_clients;         /* last mpd_status_get_count_externalClient() */
static int player_cmd_failures;     /* failed writes to PLAYER_CTRL */
static guint8 record_gain;           /* recording gain, lowered when samples clip */
static int record_failed;            /* a write to the encoder failed during this recording */

/* ---- signals -------------------------------------------------------------- */

static MultimediaObjectClass *object_class(void)
{
    return (MultimediaObjectClass *)G_OBJECT_GET_CLASS(multimedia);
}

static void send_play_finished(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sendSignal_playFinished. uid: %s", multimedia->uid->str);
    g_signal_emit(multimedia, object_class()->signals[SIGNAL_PLAYFINISHED], 0, multimedia->uid->str, EVENT_DONE);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sendSignal_playFinished. done");
}

static void send_action(int action)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sendSignal_action");
    g_signal_emit(multimedia, object_class()->signals[SIGNAL_CALLBACK], 0, action);
}

/* Record a new playback state and report it. */
static void api_new_status(int status)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_newStatus: %d", status);
    state = status;
    if (status == STATE_STOPPED || status == STATE_MPD_PAUSED || status == STATE_PLAYER_PAUSED) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_newStatus signal play finished");
        send_play_finished();
    }
    switch (state) {
    case STATE_STOPPED:
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_newStatus signal action stop");
        send_action(ACTION_STOP);
        break;
    case STATE_MPD_PLAYING:
    case STATE_PLAYER_PLAYING:
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_newStatus signal action play");
        send_action(ACTION_PLAY);
        break;
    case STATE_MPD_PAUSED:
    case STATE_PLAYER_PAUSED:
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_newStatus signal action pause");
        send_action(ACTION_PAUSE);
        break;
    default:
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_newStatus signal action state default: %d", state);
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_newStatus end");
}

/* ---- MPD ------------------------------------------------------------------ */

static void sound_save(MpdObj *mpd)
{
    int volume = mpd_status_get_volume(mpd);

    if (output_enabled)
        saved_volume = volume;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sound save: %d", volume);
}

static void sound_load(MpdObj *mpd)
{
    mpd_status_set_volume(mpd, saved_volume);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "sound load: %d", saved_volume);
}

static int mpd_error(MpdObj *mpd, int id, char *msg, void *data)
{
    (void)mpd;
    (void)data;
    /* libmpd disconnects on a non-zero return: an MPD error is logged and
     * the connection kept (check_mpd() reconnects if it is lost). */
    g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[31;01mError \x1b[0m\x1b[32;06m%i:\x1b[0m '%s'\n", id, msg);
    return 0;
}

static void mpd_status_changed(MpdObj *mpd, ChangedStatusType what, void *data)
{
    int count;

    (void)data;
    if (what & MPD_CST_SONGID) {
        mpd_Song *song = mpd_playlist_get_current_song(mpd);
        if (song != NULL)
            g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mSong:\x1b[0m %s - %s - %s\n", song->artist, song->title,
                  song->file);
    }
    if (what & MPD_CST_STATE) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mState:\x1b[0m");
        switch (mpd_player_get_state(mpd)) {
        case MPD_PLAYER_PLAY:
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Playing\n");
            api_new_status(STATE_MPD_PLAYING);
            break;
        case MPD_PLAYER_PAUSE:
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Paused\n");
            api_new_status(STATE_MPD_PAUSED);
            break;
        case MPD_PLAYER_STOP:
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Stopped\n");
            api_new_status(STATE_STOPPED);
            break;
        default:
            break;
        }
    }
    if (what & MPD_CST_REPEAT)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mRepeat:\x1b[0m %s\n", mpd_player_get_repeat(mpd) ? "On" : "Off");
    if (what & MPD_CST_RANDOM)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mRandom:\x1b[0m %s\n", mpd_player_get_random(mpd) ? "On" : "Off");
    if (what & MPD_CST_VOLUME) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mVolume: sound :\x1b[0m %03i%%\n", mpd_status_get_volume(mpd));
        sound_save(mpd);
    }
    if (what & MPD_CST_CROSSFADE)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mX-Fade:\x1b[0m %i sec.\n", mpd_status_get_crossfade(mpd));
    if (what & MPD_CST_UPDATING) {
        if (mpd_status_db_is_updating(mpd))
            g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mStarted updating DB\x1b[0m\n");
        else
            g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mUpdating DB finished\x1b[0m\n");
    }
    if (what & MPD_CST_DATABASE) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "\x1b[32;06mDatabased changed\x1b[0m\n");
        send_action(ACTION_DATABASE_CHANGED);
    }
    if (what & MPD_CST_PLAYLIST)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mPlaylist changed\x1b[0m\n");
    if (what & MPD_CST_AUDIO)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mAudio Changed\x1b[0m\n");
    if (what & MPD_CST_TOTAL_TIME) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[32;06mTotal song time changed:\x1b[0m %02i:%02i\n",
              mpd_status_get_total_song_time(mpd) / 60, mpd_status_get_total_song_time(mpd) % 60);
    }
    if (what & MPD_CST_PERMISSION)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "\x1b[33;06mPermission:\x1b[0m Changed\n");

    count = external_clients;
    if (what & MPD_CST_EXTERNAL_CLIENT) {
        count = mpd_status_get_count_externalClient(mpd);
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "ExternalCount: old_count :%d  ; new_count :%d\n", external_clients, count);
        if (external_clients == 0 && count == 1)
            send_action(ACTION_EXTERNAL_CLIENT);
    }
    external_clients = count;
}

/* An mpd start_mpd() started has ended: GLib's child watch has reaped it. */
static void mpd_child_ended(GPid pid, gint status, gpointer data)
{
    (void)status;
    (void)data;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd end process: %d", (int)pid);
    g_spawn_close_pid(pid);
}

/* An mpd start_mpd() started is left this long to answer before it may be killed and started
 * again: check_mpd() retries the connection every second, and the system build's mpd takes about 2 s to
 * load its libraries on the rabbit, so killing it at the first failed connect looped. */
#define MPD_START_GRACE 10
static time_t mpd_started;

static int start_mpd(void)
{
    pid_t pid;

    if (mpd_started != 0 && time(NULL) - mpd_started < MPD_START_GRACE) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "startMpd: still starting, not restarted\n");
        return 1;
    }
    mpd_started = time(NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "startMpd\n");
    if (system("killall mpd") < 0) {
        /* killall fails when none is running: nothing to do */
    }
    pid = fork();
    if (pid == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "startMpd CHILD\n");
        execlp("mpd", "mpd", "/etc/mpd/mpd.conf", (char *)NULL);
        /* Only reached if the exec failed. _exit(), not exit(): the child
         * mustn't run the daemon's atexit handlers or flush its stdio
         * buffers, which belong to the parent. */
        g_log(NULL, G_LOG_LEVEL_WARNING, "execlp ERROR\n");
        _exit(1);
    }
    if (pid > 0)
        g_child_watch_add(pid, mpd_child_ended, NULL);
    return 1;
}

/* A new libmpd object for the local mpd; starts mpd if it can't connect. */
static MpdObj *mpd_control_connect(void)
{
    MpdObj *mpd;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd_control_connect. mpd_control_connect\n");
    mpd = mpd_new(MPD_HOST, MPD_PORT, MPD_PASSWORD);
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_connect. mpd_object == NULL. abort\n");
        mpd_free(NULL);
        return NULL;
    }
    if (mpd_connect(mpd) == MPD_OK) {
        mpd_send_password(mpd);
        mpd_signal_connect_error(mpd, mpd_error, NULL);
        mpd_signal_connect_status_changed(mpd, mpd_status_changed, NULL);
    } else {
        start_mpd();
    }
    return mpd;
}

/* Frees the object; returns NULL for the caller to store. */
static MpdObj *mpd_control_disconnect(MpdObj *mpd)
{
    if (mpd == NULL)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd_control_disconnect. mpd_object == NULL. abort\n");
    else
        mpd_free(mpd);
    return NULL;
}

/* lock::yes disables MPD's output at full volume; anything else enables it and restores the volume. */
static int mpd_control_set_output(MpdObj *mpd, int enable)
{
    int ret;

    if (!enable) {
        output_enabled = 0;
        mpd_status_set_volume(mpd, 100);
        ret = mpd_server_set_output_device(mpd, 0, 0);
    } else {
        output_enabled = 1;
        ret = mpd_server_set_output_device(mpd, 0, 1);
        sound_load(mpd);
    }
    return ret;
}

/* Replace MPD's playlist with the songs queued so far and play it. */
static void mpd_play_queued(MpdObj *mpd)
{
    mpd_playlist_clear(mpd);
    mpd_playlist_queue_commit(mpd);
    sound_load(mpd);
    mpd_player_play(mpd);
}

/* Queue every song of `data` (freeing the list), logging each with `what`. */
static void mpd_queue_songs(MpdObj *mpd, MpdData *data, const char *what)
{
    do {
        if (data->type == MPD_DATA_TYPE_SONG) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd_control_play_%s - file: %s\n", what, data->song->file);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd_control_play_%s - title: %s\n", what, data->song->title);
            mpd_playlist_queue_add(mpd, data->song->file);
        }
        data = mpd_data_get_next(data);
    } while (data != NULL);
}

static int mpd_control_play_playlist(MpdObj *mpd, const char *name)
{
    MpdData *data;
    int found = 0;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_play_playlist: %s\n", name);
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_play_playlist. mpd_object == NULL. abort\n");
        return -1;
    }
    data = mpd_database_playlist_list(mpd);
    if (data != NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "search playlist: %s\n", name);
        do {
            if (data->type == MPD_DATA_TYPE_PLAYLIST) {
                g_log(NULL, G_LOG_LEVEL_DEBUG, "existing playlist: %s\n", data->playlist->path);
                if (strcmp(name, data->playlist->path) == 0) {
                    g_log(NULL, G_LOG_LEVEL_DEBUG, "playlist found!\n");
                    found = 1;
                    break;
                }
            }
            data = mpd_data_get_next(data);
        } while (data != NULL);
    }
    if (!found) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "playlist not found!\n");
        return -1;
    }
    mpd_playlist_clear(mpd);
    mpd_playlist_queue_load(mpd, data->playlist->path);
    mpd_data_free(data); /* frees the rest of the list */
    mpd_playlist_queue_commit(mpd);
    sound_load(mpd);
    mpd_player_play(mpd);
    return 1;
}

static int mpd_control_play_dir(MpdObj *mpd, const char *dir)
{
    MpdData *data;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_play_dir: %s\n", dir);
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_play_dir. mpd_object == NULL. abort\n");
        return -1;
    }
    data = mpd_database_get_directory_recursive(mpd, dir);
    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_play_dir. directory not found. abort\n");
        return -1;
    }
    mpd_queue_songs(mpd, data, "dir");
    mpd_play_queued(mpd);
    return 1;
}

/* Play the songs whose tag `tag` is exactly `value` (artist::, genre::, title::). */
static int mpd_control_play_search(MpdObj *mpd, int tag, const char *value, const char *what,
                                   const char *not_found)
{
    MpdData *data;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_play_%s: %s\n", what, value);
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_play_%s. mpd_object == NULL. abort\n", what);
        return -1;
    }
    mpd_database_search_start(mpd, TRUE);
    mpd_database_search_add_constraint(mpd, tag, value);
    data = mpd_database_search_commit(mpd);
    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_play_%s. %s not found. abort\n", what, not_found);
        return -1;
    }
    mpd_queue_songs(mpd, data, what);
    mpd_play_queued(mpd);
    return 1;
}

static int mpd_control_play_allsong(MpdObj *mpd)
{
    MpdData *data;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_play_allsong\n");
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_play_allsong. mpd_object == NULL. abort\n");
        return -1;
    }
    data = mpd_database_get_complete(mpd);
    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_play_allsong. get all song error. abort\n");
        return -1;
    }
    mpd_queue_songs(mpd, data, "allsong");
    mpd_play_queued(mpd);
    return 1;
}

static int mpd_control_pause(MpdObj *mpd)
{
    int ret;

    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_pause. mpd_object == NULL. abort\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_pause\n");
    ret = mpd_player_pause(mpd);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd_control_pause done. rtn:%d\n", ret);
    return 0;
}

static int mpd_control_next(MpdObj *mpd)
{
    int ret;

    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_next. mpd_object == NULL. abort\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_next\n");
    sound_load(mpd);
    mpd_player_play(mpd);
    ret = mpd_player_next(mpd);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd_control_next done. rtn:%d\n", ret);
    return 0;
}

static int mpd_control_previous(MpdObj *mpd)
{
    int ret;

    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_previous. mpd_object == NULL. abort\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_previous\n");
    sound_load(mpd);
    mpd_player_play(mpd);
    ret = mpd_player_prev(mpd);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "mpd_control_previous done. rtn:%d\n", ret);
    return 0;
}

/* mpd_player_pause() toggles, so it also resumes. */
static int mpd_control_resume(MpdObj *mpd)
{
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_resume. mpd_object == NULL. abort\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_resume\n");
    sound_load(mpd);
    mpd_player_pause(mpd);
    return 0;
}

static int mpd_control_stop(MpdObj *mpd)
{
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "mpd_control_stop. mpd_object == NULL. abort\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mpd_control_stop\n");
    mpd_player_stop(mpd);
    return 0;
}

/* mode::normal|random|repeat|norepeat; -1 for anything else. */
static int mpd_control_mode(MpdObj *mpd, const char *mode)
{
    if (strcmp(mode, "normal") == 0)
        mpd_player_set_random(mpd, 0);
    else if (strcmp(mode, "random") == 0)
        mpd_player_set_random(mpd, 1);
    else if (strcmp(mode, "repeat") == 0)
        mpd_player_set_repeat(mpd, 1);
    else if (strcmp(mode, "norepeat") == 0)
        mpd_player_set_repeat(mpd, 0);
    else
        return -1;
    return 0;
}

/* Copy `list` into a newly allocated *out for the D-Bus reply and free `list`. */
static int copy_list(GString *list, char **out, const char *copy_message)
{
    *out = g_malloc(strlen(list->str) + 1);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s", copy_message);
    strcpy(*out, list->str);
    g_string_free(list, TRUE);
    return 0;
}

/* An empty reply when libmpd returned no list. The GString is freed. */
static int empty_list(GString *list, char **out)
{
    *out = g_malloc(strlen(list->str) + 1);
    strcpy(*out, list->str);
    g_string_free(list, TRUE);
    return 0;
}

static int api_get_artist(MpdObj *mpd, char **out)
{
    GString *list = g_string_new("");
    MpdData *data;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "multimedia_api_getArtist\n");
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "multimedia_api_getArtist. mpd_object == NULL. abort\n");
        return -1;
    }
    data = mpd_database_get_artists(mpd);
    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_getArtist. MPD returned no artists: replying with an empty list\n");
        return empty_list(list, out);
    }
    do {
        if (data->type == MPD_DATA_TYPE_TAG && data->tag_type == MPD_TAG_ITEM_ARTIST && data->tag != NULL &&
            data->tag[0] != '\0')
            g_string_append_printf(list, "%s:", data->tag);
        data = mpd_data_get_next(data);
    } while (data != NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_getArtist - artistlist: %s\n", list->str);
    return copy_list(list, out, "multimedia_api_getArtist - artistlist copy\n");
}

/* Append every directory under `path` that has songs below it, depth first. */
static void get_subfolder(MpdObj *mpd, char *path, GString **list)
{
    MpdData *data = mpd_database_get_directory(mpd, path);

    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "get_subfolder. MPD returned nothing for this folder\n");
        return;
    }
    do {
        /* The recursive listing only tests for songs, and is freed at once. */
        if (data->type == MPD_DATA_TYPE_DIRECTORY && data->directory != NULL) {
            MpdData *songs = mpd_database_get_directory_recursive(mpd, data->directory);
            if (songs != NULL) {
                mpd_data_free(songs);
                g_string_append_printf(*list, "%s:", data->directory);
                get_subfolder(mpd, data->directory, list);
            }
        }
        data = mpd_data_get_next(data);
    } while (data != NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "get_subfolder - folders so far: %s\n", (*list)->str);
}

static int api_get_folder(MpdObj *mpd, char **out)
{
    GString *list = g_string_new("");

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "multimedia_api_getFolder\n");
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "multimedia_api_getFolder. mpd_object == NULL. abort\n");
        return -1;
    }
    get_subfolder(mpd, "", &list);
    return copy_list(list, out, "multimedia_api_getFolder - folderList copy\n");
}

static int api_get_playlist(MpdObj *mpd, char **out)
{
    GString *list = g_string_new("");
    MpdData *data;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "multimedia_api_getPlaylist\n");
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "multimedia_api_getPlaylist. mpd_object == NULL. abort\n");
        return -1;
    }
    data = mpd_database_playlist_list(mpd);
    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG,
              "multimedia_api_getPlaylist. MPD returned no playlists: replying with an empty list\n");
        return empty_list(list, out);
    }
    do {
        if (data->type == MPD_DATA_TYPE_PLAYLIST && data->playlist->path != NULL && data->playlist->path[0] != '\0')
            g_string_append_printf(list, "%s:", data->playlist->path);
        data = mpd_data_get_next(data);
    } while (data != NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_getPlaylist - playlistList: %s\n", list->str);
    return copy_list(list, out, "multimedia_api_getPlaylist - playlistList copy\n");
}

static int api_get_song(MpdObj *mpd, char **out)
{
    GString *list = g_string_new("");
    MpdData *data;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "multimedia_api_getSong\n");
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "multimedia_api_getSong. mpd_object == NULL. abort\n");
        return -1;
    }
    data = mpd_database_get_complete(mpd);
    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_getSong. MPD returned no songs: replying with an empty list\n");
        return empty_list(list, out);
    }
    do {
        if (data->type == MPD_DATA_TYPE_SONG && data->song->title != NULL && data->song->title[0] != '\0')
            g_string_append_printf(list, "%s:", data->song->title);
        data = mpd_data_get_next(data);
    } while (data != NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_getSong - songList: %s\n", list->str);
    return copy_list(list, out, "multimedia_api_getSong - songList copy\n");
}

static int api_get_genre(MpdObj *mpd, char **out)
{
    GString *list = g_string_new("");
    MpdData *data;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "multimedia_api_getGenre\n");
    if (mpd == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "multimedia_api_getGenre. mpd_object == NULL. abort\n");
        return -1;
    }
    data = mpd_database_get_genres(mpd);
    if (data == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_getGenre. MPD returned no genres: replying with an empty list\n");
        return empty_list(list, out);
    }
    do {
        if (data->type == MPD_DATA_TYPE_TAG && data->tag_type == MPD_TAG_ITEM_GENRE && data->tag != NULL &&
            data->tag[0] != '\0')
            g_string_append_printf(list, "%s:", data->tag);
        data = mpd_data_get_next(data);
    } while (data != NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia_api_getGenre - genreList: %s\n", list->str);
    return copy_list(list, out, "multimedia_api_getGenre - genreList copy\n");
}

/* ---- children --------------------------------------------------------------- */

/*
 * The daemon's own children, karotz-player and the mpd it starts, each have
 * a GLib child watch: GLib reaps that pid and calls back in the
 * main loop, where logging and the D-Bus signals of a state change are safe.
 * (Logging or emitting from a SIGCHLD handler crashed the daemon when it
 * interrupted a log message or a D-Bus send.) system() and popen() children
 * have no watch, so system() and pclose() reap them, and pclose() gets flac's
 * exit status. A stop waits for the player itself, after removing its watch.
 */
static guint player_watch;  /* the current player's child watch, 0 for none */

/* player has ended (its waitpid status): the player is stopped. */
static void player_ended(int status)
{
    if (WIFEXITED(status))
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: player end process: %d", multimedia->player_pid);
    else
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: player not exited properly: bug\n");
    multimedia->player_pid = -1;
    api_new_status(STATE_STOPPED);
}

/* A player has ended and GLib has reaped it: if it is still the current one, the player is
 * stopped. (An earlier one, replaced before it ended, needs nothing more.) */
static void player_child_ended(GPid pid, gint status, gpointer data)
{
    (void)data;
    if (multimedia != NULL && multimedia->player_pid == pid) {
        player_watch = 0;
        player_ended(status);
    }
    g_spawn_close_pid(pid);
}

/* ---- player -------------------------------------------------------------- */

static int is_playlist(const char *path)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: isPlaylist path: %s\n", path);
    if (strstr(path, ".asx") == NULL && strstr(path, ".m3u") == NULL && strstr(path, ".pls") == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: isPlaylist FALSE");
        return 0;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: isPlaylist TRUE");
    return 1;
}

static pid_t start_player(const char *path)
{
    pid_t pid = fork();

    if (pid == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: startPlayer CHILD\n");
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: startPlayer CHILD. path: %s\n", path);
        if (!is_playlist(path))
            execlp("karotz-player", "karotz-player", "-include", "/etc/conf/karotz-player.conf", path, (char *)NULL);
        else
            execlp("karotz-player", "karotz-player", "-include", "/etc/conf/karotz-player.conf", "-playlist", path, (char *)NULL);
        /* Only reached if the exec failed (see start_mpd()). */
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: execlp karotz-player ERROR\n");
        _exit(1);
    }
    return pid;
}

/* 1 if `path` exists or could be created as a FIFO. */
static int verify_cmd_file(const char *path)
{
    int fd;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: verify_cmd_file - start: %s\n", path);
    fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd != -1) {
        close(fd);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: verify_cmd_file - file ok\n");
        return 1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: verify_cmd_file - fd == NULL\n");
    if (mkfifo(path, 0666) == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: verify_cmd_file - mkfifo\n");
        fd = open(path, O_RDONLY | O_NONBLOCK);
        if (fd != -1) {
            close(fd);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: verify_cmd_file -created file ok\n");
            return 1;
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: verify_cmd_file -created file not ok\n");
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: verify_cmd_file - file NOT ok\n");
    return 0;
}

static int player_control_init(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: player_control_init\n");
    if (system("killall karotz-player") < 0) {
        /* killall fails when none is running: nothing to do */
    }
    return 0;
}

#define PLAYER_GONE (-2)  /* send_player_cmd(): the player had ended */

/*
 * 1 if the current player has ended (or there is none): it is then reaped
 * here and stopped. Its child watch can't do it while a command waits for
 * the player, since the watch runs in the main loop this blocks.
 */
static int player_reap_if_ended(void)
{
    pid_t pid = multimedia->player_pid;
    int status = 0;

    if (pid <= 0)
        return 1;
    /* This wait may reap it, so GLib mustn't (it would warn of ECHILD). */
    if (player_watch != 0) {
        g_source_remove(player_watch);
        player_watch = 0;
    }
    if (waitpid(pid, &status, WNOHANG) == pid) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: player %d had ended before its command", pid);
        player_ended(status);
        return 1;
    }
    player_watch = g_child_watch_add(pid, player_child_ended, NULL);
    return 0;
}

/*
 * Write a slave command to player's FIFO. The player holds the FIFO open
 * while it runs; with no reader (a player still starting, or one that has
 * ended) the open is retried every 50 ms for up to 3 s, and a player that
 * has ended is reaped. 0, -1 on failure, PLAYER_GONE if the player had ended.
 */
static int send_player_cmd(const char *cmd)
{
    int fd, tries;
    size_t len = strlen(cmd);

    if (verify_cmd_file(PLAYER_CTRL) != 1) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: send_player_cmd - pipe /tmp/karotz-player.ctrl open error\n");
        if (++player_cmd_failures > 5) {
            player_cmd_failures = 0;
            player_control_init();
        }
        return -1;
    }
    for (tries = 0; (fd = open(PLAYER_CTRL, O_WRONLY | O_NONBLOCK)) < 0; tries++) {
        if (errno != ENXIO || tries >= 60) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: send_player_cmd - no player reads %s: %s",
                  PLAYER_CTRL, strerror(errno));
            return -1;
        }
        if (player_reap_if_ended())
            return PLAYER_GONE;
        g_usleep(50000);
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) & ~O_NONBLOCK);
    if (write(fd, cmd, len) != (ssize_t)len)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: send_player_cmd - write to %s: %s", PLAYER_CTRL,
              strerror(errno));
    close(fd);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: send_player_cmd - pipe /tmp/karotz-player.ctrl write done\n");
    return 0;
}

/* Start player on `path`, or have the running one load it. Trims `path` in place. */
static int player_control_play(char *path, int *pid)
{
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Multimedia: player_control_play path: %s\n", path);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: player_control_play player_pid: %d\n", *pid);
    if (verify_cmd_file(PLAYER_CTRL) != 1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: send_player_cmd - pipe /tmp/karotz-player.ctrl NOT OK\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: send_player_cmd - pipe /tmp/karotz-player.ctrl OK\n");
    path = g_strchug(g_strchomp(path));
    if (*pid != -1) {
        GString *cmd = g_string_new("");
        if (!is_playlist(path))
            g_string_append_printf(cmd, "loadfile ");
        else
            g_string_append_printf(cmd, "loadlist ");
        g_string_append_printf(cmd, "'%s' 0\n", path);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: player_control_play cmd_play: %s\n", cmd->str);
        int sent = send_player_cmd(cmd->str);
        g_string_free(cmd, TRUE);
        if (sent != PLAYER_GONE)
            return 0;
    }
    /* No player, or the last one had ended: a new one plays it. */
    *pid = start_player(path);
    if (*pid > 0)
        player_watch = g_child_watch_add(*pid, player_child_ended, NULL);
    return 0;
}

/*
 * Kill player and wait for it (for it alone: another child, such as a
 * recording's encoder, may still be running); the player is then stopped.
 * 0, or -1 without a pid.
 */
static int player_control_stop(pid_t pid)
{
    int status = 0;
    pid_t waited;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: player_control_stop\n");
    /* Only a real pid is signalled: the pid is -1 when player has already
     * gone (its child watch resets it), and kill(-1, SIGTERM) would
     * signal every process on the rabbit. Without a pid the caller falls
     * back to killall karotz-player. */
    if (pid <= 0)
        return -1;
    /* This wait reaps it, so GLib mustn't (it would warn of ECHILD). */
    if (player_watch != 0) {
        g_source_remove(player_watch);
        player_watch = 0;
    }
    kill(pid, SIGTERM);
    waited = waitpid(pid, &status, 0);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "player_control_stop: wait return: %d", waited);
    if (waited != pid)
        g_log(NULL, G_LOG_LEVEL_WARNING, "player_control_stop: waitpid(%d): %s", pid, strerror(errno));
    player_ended(status);
    return 0;
}

static int player_control_pause(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: player_control_pause\n");
    return send_player_cmd("pause\n") == 0 ? 0 : -1;
}

/* ---- the multimedia API --------------------------------------------------- */

/* Position of `needle` in `haystack`, or -1. */
static int str_pos(const char *haystack, const char *needle)
{
    const char *p;

    if (haystack == NULL || needle == NULL)
        return -1;
    p = strstr(haystack, needle);
    return p != NULL ? (int)(p - haystack) : -1;
}

/* Newly malloc'd copy of s[start..end] (end included); NULL unless start < end. */
static char *substring(const char *s, unsigned start, unsigned end)
{
    char *out;
    unsigned i;

    if (s == NULL || start >= end)
        return NULL;
    out = malloc(end - start + 2);
    if (out == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: out of memory\n");
        exit(1);
    }
    for (i = start; i <= end; i++)
        out[i - start] = s[i];
    out[i - start] = '\0';
    return out;
}

static void api_init(MultimediaObject *self)
{
    state = STATE_STOPPED;
    self->mpd = NULL;
    player_control_init();
    self->mpd = mpd_control_connect();
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - init done\n");
}

/*
 * The "command::argument" URLs, by their exact names (as the platform
 * documented them and the published applications use them). A play starts
 * MPD; a setting (mode, lock) changes how it plays and is not a play. Every
 * command needs its argument but allsong, which takes none ("allsong::").
 */
typedef enum { COMMAND_PLAY, COMMAND_SETTING } CommandKind;

static const struct {
    const char *name;
    CommandKind kind;
    int needs_argument;
} COMMANDS[] = {
    {"playlist", COMMAND_PLAY, 1}, {"title", COMMAND_PLAY, 1}, {"dir", COMMAND_PLAY, 1},
    {"artist", COMMAND_PLAY, 1},   {"genre", COMMAND_PLAY, 1}, {"allsong", COMMAND_PLAY, 0},
    {"mode", COMMAND_SETTING, 1},  {"lock", COMMAND_SETTING, 1},
};

/* What api_play() did. */
#define PLAY_FAILED -1
#define PLAY_PLAYER 0 /* player started */
#define PLAY_MPD 1     /* MPD plays */
#define PLAY_SETTING 2 /* a setting was applied: nothing plays */

/* mode::normal|random|repeat|norepeat, lock::yes|no: PLAY_SETTING, or PLAY_FAILED. */
static int api_setting(MultimediaObject *self, const char *name, const char *arg)
{
    int ret;

    if (strcmp(name, "mode") == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - mode\n");
        ret = mpd_control_mode(self->mpd, arg);
    } else if (strcmp(arg, "yes") == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - mpd lock\n");
        ret = mpd_control_set_output(self->mpd, 0);
    } else if (strcmp(arg, "no") == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - mpd unlock\n");
        ret = mpd_control_set_output(self->mpd, 1);
    } else {
        ret = -1;
    }
    if (ret != 0)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: API - %s::%s failed or unknown\n", name, arg);
    return ret == 0 ? PLAY_SETTING : PLAY_FAILED;
}

/* An MPD play command (arg is NULL for allsong): PLAY_MPD, or PLAY_FAILED. */
static int api_mpd_play(MultimediaObject *self, const char *name, const char *arg)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - %s\n", name);
    if (strcmp(name, "playlist") == 0)
        return mpd_control_play_playlist(self->mpd, arg);
    if (strcmp(name, "title") == 0)
        return mpd_control_play_search(self->mpd, MPD_TAG_ITEM_TITLE, arg, "song", "title");
    if (strcmp(name, "dir") == 0)
        return mpd_control_play_dir(self->mpd, arg);
    if (strcmp(name, "artist") == 0)
        return mpd_control_play_search(self->mpd, MPD_TAG_ITEM_ARTIST, arg, "artist", "artist");
    if (strcmp(name, "genre") == 0)
        return mpd_control_play_search(self->mpd, MPD_TAG_ITEM_GENRE, arg, "genre", "genre");
    return mpd_control_play_allsong(self->mpd);
}

/*
 * Play `url`: "command::argument" (COMMANDS) goes to MPD, anything else to
 * player. Returns PLAY_PLAYER, PLAY_MPD, PLAY_SETTING or PLAY_FAILED (an
 * unknown command, a missing argument, a play while player plays, or a
 * failure).
 */
static int api_play(MultimediaObject *self, char *url)
{
    int pos, ret;
    unsigned i;
    char *arg;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - play\n");
    pos = str_pos(url, "::");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia API - pos:%d", pos);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "multimedia API - strlen(path):%d", (int)strlen(url));

    if (pos < 1) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - path for Player\n");
        if (state == STATE_MPD_PLAYING) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: API - already busy with mpd - abort\n");
            return -1;
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - Player pid before: %d\n", self->player_pid);
        /* The playing state is entered only if player started and has a
         * pid. A failed start (no FIFO, no fork) would leave it "playing"
         * without an player, and a stop would then kill pid -1: every
         * process on the rabbit. An player that ends at once (its exec
         * failed) is reaped later, in the main loop, which stops the player. */
        int started = player_control_play(url, &self->player_pid);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - Player pid after: %d\n", self->player_pid);
        if (started != 0 || self->player_pid <= 0) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: API - player didn't start\n");
            return -1;
        }
        api_new_status(STATE_PLAYER_PLAYING);
        return PLAY_PLAYER;
    }

    for (i = 0; i < G_N_ELEMENTS(COMMANDS); i++)
        if (strlen(COMMANDS[i].name) == (size_t)pos && strncmp(url, COMMANDS[i].name, pos) == 0)
            break;
    if (i == G_N_ELEMENTS(COMMANDS)) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: API - '%s': unknown command, refused\n", url);
        return PLAY_FAILED;
    }
    arg = substring(url, pos + 2, strlen(url)); /* NULL when nothing follows "::" */
    if (arg == NULL && COMMANDS[i].needs_argument) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Multimedia: API - '%s': no argument after '::', refused\n", url);
        return PLAY_FAILED;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - argument:%s\n", arg != NULL ? arg : "");
    if (COMMANDS[i].kind == COMMAND_SETTING) {
        ret = api_setting(self, COMMANDS[i].name, arg);
    } else if (state == STATE_PLAYER_PLAYING) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - already busy with player - abort\n");
        ret = PLAY_FAILED;
    } else {
        ret = api_mpd_play(self, COMMANDS[i].name, arg);
    }
    free(arg);
    return ret;
}

static int api_pause(MultimediaObject *self)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - pause\n");
    if (state == STATE_MPD_PLAYING) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - mpd pause\n");
        if (mpd_control_pause(self->mpd) != 0)
            return -1;
        api_new_status(STATE_MPD_PAUSED);
    } else if (state == STATE_PLAYER_PLAYING) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - player pause\n");
        if (player_control_pause() == -1) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - player pause error = kill\n");
            if (system("killall karotz-player") < 0) {
                /* killall fails when none is running: nothing to do */
            }
            api_new_status(STATE_STOPPED);
            return -1;
        }
        api_new_status(STATE_PLAYER_PAUSED);
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - pause. bad current state: %d\n", state);
    }
    return 0;
}

/* For player, the state changes once the killed player is reaped (player_control_stop()). */
static int api_stop(MultimediaObject *self)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - stop\n");
    if (state == STATE_MPD_PLAYING || state == STATE_MPD_PAUSED) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - mpd stop\n");
        if (mpd_control_stop(self->mpd) != 0)
            return -1;
        api_new_status(STATE_STOPPED);
    } else if (state == STATE_PLAYER_PLAYING || state == STATE_PLAYER_PAUSED) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - player stop = kill\n");
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - pid: %d\n", self->player_pid);
        if (player_control_stop(self->player_pid) == -1) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - stop: killall player\n");
            if (system("killall karotz-player") < 0) {
                /* killall fails when none is running: nothing to do */
            }
        }
        self->player_pid = -1;
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - stop done\n");
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - stop. bad current state: %d\n", state);
    }
    return 0;
}

static int api_resume(MultimediaObject *self)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - resume\n");
    if (state == STATE_MPD_PAUSED) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - mpd resume\n");
        if (mpd_control_resume(self->mpd) != 0)
            return -1;
        api_new_status(STATE_MPD_PLAYING);
    } else if (state == STATE_PLAYER_PAUSED) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - player resume\n");
        if (player_control_pause() == -1) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - player resume error = kill\n");
            if (system("killall karotz-player") < 0) {
                /* killall fails when none is running: nothing to do */
            }
            api_new_status(STATE_STOPPED);
            return -1;
        }
        api_new_status(STATE_PLAYER_PLAYING);
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - resume. bad current state: %d\n", state);
    }
    return 0;
}

/* next/previous only act on MPD; during player playback they do nothing
 * and report success (the D-Bus result is TRUE). */
static int api_next(MultimediaObject *self)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - multimedia_api_next\n");
    if (state == STATE_MPD_PAUSED || state == STATE_MPD_PLAYING || state == STATE_STOPPED)
        return mpd_control_next(self->mpd);
    return 0;
}

static int api_previous(MultimediaObject *self)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - multimedia_api_previous\n");
    if (state == STATE_MPD_PAUSED || state == STATE_MPD_PLAYING || state == STATE_STOPPED)
        return mpd_control_previous(self->mpd);
    return 0;
}

/* ---- recording ------------------------------------------------------------ */

static void record_init(MultimediaObject *self)
{
    self->record_uuid = g_string_sized_new(40);
    self->record_pipe = NULL;
    self->record_timeout = 0;
}

/* OSS setup: unsigned 16-bit little endian, mono, 16 kHz. Results are ignored by the caller. */
static int audio_setup(int fd)
{
    int arg = AFMT_U16_LE;

    if (ioctl(fd, SNDCTL_DSP_RESET, 0) < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Unable to reset OSS driver.");
        return -1;
    }
    if (ioctl(fd, SNDCTL_DSP_SETFMT, &arg) < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Unable to set format");
        return -2;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "accepted format=%d\n", arg);
    arg = 0;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "expected stereo=%d\n", 0);
    if (ioctl(fd, SNDCTL_DSP_STEREO, &arg) < 0)
        return -3;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "accepted stereo=%d\n", arg);
    arg = 16000;
    if (ioctl(fd, SNDCTL_DSP_SPEED, &arg) < 0)
        return -4;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "speed=%d\n", arg);
    return 0;
}

/* Unsigned 16-bit samples: silence is the midpoint; above LOUD the gain drops. */
#define SAMPLE_SILENCE 0x8000u
#define SAMPLE_LOUD 0xe600

/*
 * Offset of the first sample that isn't silence (SAMPLE_SILENCE) while the start of
 * the recording is being skipped, else 0; -1 if the whole block is silence.
 */
static int skip_initial_silence(const guint8 *buf, int len, MultimediaObject *self)
{
    int i = 0;

    if (self->skip_silence) {
        while (i < len && buf[i] == (SAMPLE_SILENCE & 0xff) && (i + 1 >= len || buf[i + 1] == SAMPLE_SILENCE >> 8))
            i += 2;
    }
    if (i == len)
        return -1;
    self->skip_silence = 0;
    return i;
}

static gboolean record_stop(gpointer data);

/* Idle callback: move one block from /dev/audio to flac, amplified. */
static gboolean record_pump(gpointer data)
{
    MultimediaObject *self = data;
    guint8 buf[1024];
    int skip, i;
    ssize_t n;

    if (self->audio_fd == 0)
        return FALSE;
    /* After a short or failed read, the buffer is silence (the unsigned
     * midpoint, little-endian) from the first incomplete sample on, so no
     * stale stack bytes go into the recording. */
    n = read(self->audio_fd, buf, sizeof buf);
    if (n < 0)
        n = 0;
    for (i = (int)n & ~1; i < (int)sizeof buf; i += 2) {
        buf[i] = SAMPLE_SILENCE & 0xff;
        buf[i + 1] = SAMPLE_SILENCE >> 8;
    }
    skip = skip_initial_silence(buf, sizeof buf, self);
    if (skip >= 0) {
        for (i = 0; i < (int)sizeof buf; i += 2) {
            /* Amplify around the unsigned midpoint (unsigned 32-bit arithmetic,
             * truncated to 16 bits); lower the gain while samples come out loud. */
            unsigned sample = (unsigned)(buf[i] | buf[i + 1] << 8) * record_gain + (1u - record_gain) * SAMPLE_SILENCE;
            buf[i] = (guint8)sample;
            buf[i + 1] = (guint8)(sample >> 8);
            if (sample > SAMPLE_LOUD && record_gain > 1)
                record_gain--;
        }
        if (fwrite(buf + skip, sizeof buf - skip, 1, self->record_pipe) != 1) {
            /* The encoder has gone (SIGPIPE is ignored, so the write fails
             * instead of killing the daemon): the recording ends as failed. */
            g_log(NULL, G_LOG_LEVEL_WARNING, "Record: write to the encoder failed: %s\n", strerror(errno));
            record_failed = 1;
            record_stop(self);
            return FALSE;
        }
    }
    return TRUE;
}

/* Stop the recording: pad flac's input with silence, close everything, report. Also the timeout callback. */
static gboolean record_stop(gpointer data)
{
    MultimediaObject *self = data;
    char silence[1000];
    int i, status, code = EVENT_DONE;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "Record Stop\n");
    if (self->record_timeout != 0) {
        g_source_remove(self->record_timeout);
        self->record_timeout = 0;
    }
    if (self->record_pipe == NULL) {
        /* Nothing is recording. */
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Record Stop: no recording\n");
        return FALSE;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Record Stop %s\n", self->record_uuid->str);
    /* The padding is silence, the midpoint. 0x00 is the most negative
     * unsigned sample: a second of it at full scale would end every
     * recording in a click. */
    for (i = 0; i < (int)sizeof silence; i += 2) {
        silence[i] = (char)(SAMPLE_SILENCE & 0xff);
        silence[i + 1] = (char)(SAMPLE_SILENCE >> 8);
    }
    for (i = 0; i < 32 && !record_failed; i++) {
        if (fwrite(silence, sizeof silence, 1, self->record_pipe) != 1)
            record_failed = 1;
    }
    close(self->audio_fd);
    self->audio_fd = 0;
    status = pclose(self->record_pipe);
    self->record_pipe = NULL;
    /* flac's exit status: anything but 0 (missing, or it failed to write
     * the file) means there is no good recording. The daemon never reaps
     * flac itself (its child watches are for the player and mpd only), so pclose()
     * gets the status; ECHILD (status unknown, only a failed write tells)
     * is kept as a safety net. */
    if (status == -1 && errno == ECHILD)
        status = 0;
    if (record_failed || status != 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Record Stop: the recording failed (encoder status %d)\n", status);
        code = EVENT_ERROR;
    }
    g_signal_emit(self, object_class()->signals[SIGNAL_RECORDFINISHED], 0, self->record_uuid->str, code);
    g_string_assign(self->record_uuid, "");
    return FALSE;
}

/* Starts a recording; -1 (nothing started, nothing left open) if
 * /dev/audio can't be opened or set up, or the encoder can't be started. */
static int record_go(MultimediaObject *self, const char *id)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Record Go %s\n", id);
    self->audio_fd = open("/dev/audio", O_RDONLY);
    if (self->audio_fd < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Record Go: can't open /dev/audio: %s\n", strerror(errno));
        self->audio_fd = 0; /* 0 is "no recording" for record_pump() */
        return -1;
    }
    /* A device left in another format or rate would record noise that flac
     * then encodes as 16 kHz, 16-bit: no recording rather than a garbled one. */
    if (audio_setup(self->audio_fd) < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Record Go: /dev/audio setup failed\n");
        close(self->audio_fd);
        self->audio_fd = 0;
        return -1;
    }
    self->record_pipe = popen(RECORD_COMMAND, "w");
    if (self->record_pipe == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Record Go: can't start the encoder: %s\n", strerror(errno));
        close(self->audio_fd);
        self->audio_fd = 0;
        return -1;
    }
    g_string_assign(self->record_uuid, id);
    record_gain = 10;
    record_failed = 0;
    g_idle_add(record_pump, self);
    self->record_timeout = g_timeout_add(RECORD_TIMEOUT_MS, record_stop, self);
    self->skip_silence = 1;
    return 0;
}

/* ---- D-Bus methods -------------------------------------------------------- */

gboolean multimedia_object_play(MultimediaObject *self, const char *id, char *url, int behaviour, gboolean *result,
                                GError **error)
{
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "play url: %s :: id:%s", url, id);
    switch (api_play(self, url)) {
    case PLAY_PLAYER:
    case PLAY_MPD:
        /* The play's id, for its playFinished. ("" when nobody waits for it.) */
        *result = TRUE;
        g_string_assign(self->uid, id);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia play: obj. uid: %s", self->uid->str);
        break;
    case PLAY_SETTING:
        /* Not a play: the playing request keeps its id. */
        *result = TRUE;
        break;
    default:
        *result = FALSE;
        break;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "play: end");
    return TRUE;
}

gboolean multimedia_object_pause(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                 GError **error)
{
    (void)id;
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "pause");
    *result = api_pause(self) == 0;
    return TRUE;
}

gboolean multimedia_object_stop(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                GError **error)
{
    (void)id;
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "stop");
    if (self->record_pipe != NULL)
        record_stop(self);
    *result = api_stop(self) == 0;
    return TRUE;
}

gboolean multimedia_object_resume(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                  GError **error)
{
    (void)id;
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "resume");
    *result = api_resume(self) == 0;
    return TRUE;
}

gboolean multimedia_object_next(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                GError **error)
{
    (void)id;
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "next");
    *result = api_next(self) == 0;
    return TRUE;
}

gboolean multimedia_object_previous(MultimediaObject *self, const char *id, int behaviour, gboolean *result,
                                    GError **error)
{
    (void)id;
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "previous");
    *result = api_previous(self) == 0;
    return TRUE;
}

gboolean multimedia_object_get_artist(MultimediaObject *self, char **list, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "getArtist");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - multimedia_api_getArtist\n");
    *result = api_get_artist(self->mpd, list) == 0;
    return TRUE;
}

gboolean multimedia_object_get_folder(MultimediaObject *self, char **list, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "getFolder");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - multimedia_api_getFolder\n");
    *result = api_get_folder(self->mpd, list) == 0;
    return TRUE;
}

gboolean multimedia_object_get_playlist(MultimediaObject *self, char **list, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "getPlaylist");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - multimedia_api_getPlaylist\n");
    *result = api_get_playlist(self->mpd, list) == 0;
    return TRUE;
}

gboolean multimedia_object_get_genre(MultimediaObject *self, char **list, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "getGenre");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - multimedia_api_getGenre\n");
    *result = api_get_genre(self->mpd, list) == 0;
    return TRUE;
}

gboolean multimedia_object_get_song(MultimediaObject *self, char **list, gboolean *result, GError **error)
{
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "getSong");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Multimedia: API - multimedia_api_getSong\n");
    *result = api_get_song(self->mpd, list) == 0;
    return TRUE;
}

gboolean multimedia_object_record(MultimediaObject *self, const char *id, const char *url, int behaviour,
                                  gboolean *result, GError **error)
{
    (void)behaviour;
    (void)error;
    *result = TRUE;
    if (self->record_pipe != NULL)
        record_stop(self);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "record (on %s)", url);
    *result = record_go(self, id) == 0;
    return TRUE;
}

/* ---- object and main ------------------------------------------------------ */

/* Every second: reconnect to MPD if the status can't be read and the connection is gone. */
static gboolean check_mpd(gpointer data)
{
    MultimediaObject *self = data;

    if (mpd_status_update(self->mpd) != MPD_OK) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "multimediaStatusCallback - not ok\n");
        if (mpd_check_connected(self->mpd) != TRUE) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "multimediaStatusCallback - connection KO. reconnecting...\n");
            self->mpd = mpd_control_disconnect(self->mpd);
            self->mpd = mpd_control_connect();
        }
    }
    return TRUE;
}

static void multimedia_object_class_init(MultimediaObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_LAST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_PLAYFINISHED] = g_signal_new("multi_playfinished", type, G_SIGNAL_RUN_LAST, 0, NULL,
                                                       NULL, karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, 2,
                                                       G_TYPE_STRING, G_TYPE_INT);
    klass->signals[SIGNAL_RECORDFINISHED] = g_signal_new("multi_recordfinished", type, G_SIGNAL_RUN_LAST, 0, NULL,
                                                         NULL, karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, 2,
                                                         G_TYPE_STRING, G_TYPE_INT);
    klass->signals[SIGNAL_CALLBACK] = g_signal_new("multi_callback", type, G_SIGNAL_RUN_LAST, 0, NULL, NULL,
                                                   g_cclosure_marshal_VOID__INT, G_TYPE_NONE, 1, G_TYPE_INT);
    g_return_if_fail(klass != NULL);
}

static void multimedia_object_init(MultimediaObject *multimedia)
{
    multimedia->bus_proxy = NULL;
    dbus_g_object_type_install_info(MULTIMEDIA_TYPE_OBJECT, &dbus_glib_multimedia_object_object_info);
    g_return_if_fail(multimedia != NULL);
    multimedia->uid = g_string_sized_new(40);
    multimedia->player_pid = -1;
    api_init(multimedia);
    record_init(multimedia);
}

int main(void)
{
    int log_priority = LOG_DEBUG;
    GError *error = NULL;
    DBusGConnection *bus;
    GMainLoop *loop;
    guint request_result = 0;

    init_karotz_log("Multimedia", &log_priority);
    g_type_init();
    /* A write to an encoder or a FIFO whose reader has gone fails (EPIPE)
     * instead of killing the daemon. libdbus ignores SIGPIPE too once the
     * bus connection is opened; this doesn't depend on it, and covers the
     * time before. The children (mpd, player, flac) inherit it, as they
     * always have from libdbus. */
    signal(SIGPIPE, SIG_IGN);
    loop =g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");

    /* Neither failure stops the daemon at once (and the error is never
     * printed), but either leaves request_result at 0, so it then exits
     * (status 0) at the name check below. */
    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to open connection to bus");
    else
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Successfully connected to %s bus\n", "SYSTEM");

    multimedia = g_object_new(MULTIMEDIA_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Multimedia", G_OBJECT(multimedia));
    multimedia->bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                      "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(multimedia->bus_proxy, "com.mindscape.karotz.Multimedia", 0,
                                           &request_result, &error))
        g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to get name");

    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "Got result code %u from requesting name", request_result);
        return 0;
    }
    multimedia->bus = bus;
    g_timeout_add_seconds(1, check_mpd, multimedia);
    g_signal_emit(multimedia, object_class()->signals[SIGNAL_REGISTERED], 0);
    g_main_loop_run(loop);
    return 0;
}
