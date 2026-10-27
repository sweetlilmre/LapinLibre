// SPDX-License-Identifier: MIT
/*
 * voice-daemon: text-to-speech and speech recognition for the rabbit, as
 * com.mindscape.karotz.Voice (/com/mindscape/karotz/Voice).
 *
 * TTS: tts_speak(id, text, lang) forks a child that POSTs the text, wrapped
 * in SSML-style breaks, to the speech server configured for the language in
 * /usr/etc/conf/voice.conf ([tts] <lang>=<url>) and pipes the answer (MP3)
 * into "madplay -a 1.5 -". When the child ends, or tts_stop kills it,
 * tts_event(id, 4) is signalled. Only one text is spoken at a time.
 *
 * ASR: asr_start_string(uid, grammar, lang) and asr_start_file(uid, file)
 * connect over TCP to one of the recognition servers in [asr] (server<n>,
 * nbserver, port; the first one is picked at random, the next ones in turn
 * when a connection fails), send the grammar, then stream the microphone
 * (/dev/audio, OSS, mu-law 16 kHz mono) from an idle callback until the
 * server answers. Every message on the socket is a 4-byte big-endian length
 * followed by a serialized lapinlibre.asr.AsrMsg (asr-message.proto).
 * The answer becomes asr_event(uid, words, semantic, score), or
 * asr_event(uid, "<nomatch>", words, 2) when the server didn't recognise
 * anything; asr_event(uid, "<error_server_timeout>", "cancel", 2) reports a
 * session that couldn't start. asr_stop drops the session silently.
 */
#include <sstream>
#include <string>

#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/soundcard.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include <curl/curl.h>
#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include "asr-message.pb.h"
#include "karotz-marshal.h"
extern "C" {
#include "karotz-messages.h"
}

using lapinlibre::asr::AsrMsg;
using lapinlibre::asr::Response;

#define VOICE_CONF "/usr/etc/conf/voice.conf"
/*
 * How long a connection to a TTS or ASR server may take. Without a limit,
 * a server that doesn't answer at all (a host or firewall that drops the
 * packets, rather than refusing) is given up only when the kernel stops
 * retrying, about 189 s later. TTS connects in its child, ASR from the main
 * loop (the asr_start_* call is answered once the session has started), so
 * the daemon keeps answering while either waits.
 */
#define TTS_CONNECT_TIMEOUT_S 10L
#define ASR_CONNECT_TIMEOUT_S 5
#define AUDIO_DEVICE "/dev/audio"

/* Raw bytes read from /dev/audio per round; every second one is sent. */
#define AUDIO_READ_SIZE 2048
#define AUDIO_CHUNK_SIZE 1024

/* The status reported in tts_event (both for a finished and a stopped text). */
#define TTS_EVENT_DONE 4
#define TTS_EVENT_ERROR 2  /* ReturnCode ERROR */
/* The score reported with "<nomatch>" and errors in asr_event. */
#define ASR_SCORE_ERROR 2

enum {
    SIGNAL_ASR_EVENT,
    SIGNAL_TTS_EVENT,
    SIGNAL_REGISTERED,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    /* TTS */
    GString *tts_id;   /* id of the current (or last) text */
    pid_t tts_pid;     /* the speaking child, 0 when idle */
    guint tts_watch;   /* its child watch */
    /* ASR */
    GString *asr_uid;  /* uid of the current (or last) session */
    int asr_socket;    /* connection to the server, 0 when no session */
    int audio_fd;      /* /dev/audio, 0 when closed */
    char *asr_server;  /* host of the server in use */
    int asr_port;
    guint asr_source;  /* the idle callback streaming the audio */
    /* While a session connects (asr_context set): the method's pending reply,
     * the connection's watch and time limit, the servers tried so far, and
     * the session's grammar (inline in asr_lang) or grammar file. */
    DBusGMethodInvocation *asr_context;
    guint connect_watch;
    guint connect_timeout;
    int connect_attempt;
    char *asr_grammar;
    char *asr_lang;
    char *asr_file;
    int server_index;  /* the [asr] server<n> in use */
    int server_count;  /* nbserver */
    GKeyFile *conf;    /* voice.conf */
} VoiceObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} VoiceObjectClass;

extern "C" {
GType voice_object_get_type(void);
gboolean voice_object_tts_speak(VoiceObject *self, const char *id, const char *text, const char *lang,
                                gint behaviour, gboolean *result, GError **error);
gboolean voice_object_tts_stop(VoiceObject *self, const char *id, gint behaviour, gboolean *result,
                               GError **error);
gboolean voice_object_asr_start_file(VoiceObject *self, const char *uid, const char *filename, gint behaviour,
                                     DBusGMethodInvocation *context);
gboolean voice_object_asr_start_string(VoiceObject *self, const char *uid, const char *grammar,
                                       const char *lang, gint behaviour, DBusGMethodInvocation *context);
gboolean voice_object_asr_stop(VoiceObject *self, const char *id, gint behaviour, gboolean *result,
                               GError **error);
}

#define VOICE_TYPE_OBJECT (voice_object_get_type())

G_DEFINE_TYPE(VoiceObject, voice_object, G_TYPE_OBJECT)

#include "voice-daemon-glue.h"

static VoiceObjectClass *klass_of(VoiceObject *self)
{
    return (VoiceObjectClass *)G_OBJECT_GET_CLASS(self);
}

/* --- text to speech ------------------------------------------------------ */

static int tts_init(VoiceObject *self)
{
    self->tts_id = g_string_sized_new(40);
    self->tts_pid = 0;
    self->tts_watch = 0;
    return 0;
}

/* The speaking child has ended. */
static void tts_child_exited(GPid pid, gint status, gpointer data)
{
    VoiceObject *self = (VoiceObject *)data;
    /* Done, or ERROR when the child failed (the speech couldn't be fetched,
     * or curl or madplay couldn't be started). */
    int code = WIFEXITED(status) && WEXITSTATUS(status) == 0 ? TTS_EVENT_DONE : TTS_EVENT_ERROR;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS: the child process %d ended", pid);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS: the child's recorded pid was %d", self->tts_pid);
    self->tts_pid = 0;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %d", "ttsPidCallBack", status);
    g_signal_emit(self, klass_of(self)->signals[SIGNAL_TTS_EVENT], 0, self->tts_id->str, code);
}

/* The child: fetch the speech for `text` from `url` and play it. Never returns. */
static void tts_child(const char *url, const char *text) G_GNUC_NORETURN;
static void tts_child(const char *url, const char *text)
{
    CURL *curl;
    FILE *player;
    GString *buffer;

    curl = curl_easy_init();
    if (curl == NULL)
        exit(-2);
    player = popen("madplay -a 1.5 -", "w");
    if (player == NULL)
        exit(-1);
    buffer = g_string_sized_new(40);
    g_string_assign(buffer, url);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "tts url: %s", url);
    curl_easy_setopt(curl, CURLOPT_URL, buffer->str); /* libcurl copies it */
    /* The same buffer is reused for the body, which libcurl does not copy. */
    g_string_printf(buffer, "<s><break time=\"200ms\" />%s<break time=\"150ms\" /></s>", text);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, buffer->str);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)buffer->len);
    /* Default write callback: the answer is fwrite()n into madplay's stdin. */
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, player);
    /* An HTTP error status (e.g. 404) is a failure too, not speech for madplay. */
    curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, TTS_CONNECT_TIMEOUT_S);
    CURLcode fetched = curl_easy_perform(curl);
    if (fetched != CURLE_OK)
        g_log(NULL, G_LOG_LEVEL_WARNING, "TTS: fetching the speech failed: %s (curl error %d)",
              curl_easy_strerror(fetched), (int)fetched);
    curl_easy_cleanup(curl);
    pclose(player);
    exit(fetched == CURLE_OK ? 0 : 3); /* tts_child_exited() reports the status */
}

/* Start speaking `text` in `lang`. Returns 0, -4 if already speaking, -6 for
 * a language without a server. */
static int tts_speak(VoiceObject *self, const char *id, const char *text, const char *lang)
{
    char key[3];
    GError *error = NULL;
    char *url;

    if (self->tts_pid != 0)
        return -4;
    g_string_assign(self->tts_id, id);
    if (strnlen(lang, 3) < 2) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "wrong lang (%s)", lang);
        return -6;
    }
    /* The server is looked up by the language's first two letters. */
    key[0] = g_ascii_tolower(lang[0]);
    key[1] = g_ascii_tolower(lang[1]);
    key[2] = '\0';
    url = g_key_file_get_string(self->conf, "tts", key, &error);
    if (url == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "wrong lang (%s)", lang);
        g_clear_error(&error);
        return -6;
    }
    self->tts_pid = fork();
    /* Logged by both the parent and the child (id 0). */
    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS FORK ##### id:%d", self->tts_pid);
    if (self->tts_pid == 0)
        tts_child(url, text);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS FORK ##### id:%d", self->tts_pid);
    self->tts_watch = g_child_watch_add(self->tts_pid, tts_child_exited, self);
    g_free(url);
    return 0;
}

/* Stop the text being spoken, if any, and signal it as done. */
static void tts_cancel(VoiceObject *self)
{
    int status;
    pid_t reaped;

    if (self->tts_pid == 0)
        return;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS CANCEL pid:%d", self->tts_pid);
    g_source_remove(self->tts_watch);
    self->tts_watch = 0;
    if (system("killall madplay") < 0) {
        /* killall fails when none is running: nothing to do */
    }
    kill(self->tts_pid, SIGTERM);
    /* wait() reaps whichever child ends first, which can only be the TTS
     * child: madplay is its own child, and system() reaps its own. */
    reaped = wait(&status);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS CANCEL pid wait: %d", reaped);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS CANCEL status: %d", status);
    self->tts_pid = 0;
    g_signal_emit(self, klass_of(self)->signals[SIGNAL_TTS_EVENT], 0, self->tts_id->str, TTS_EVENT_DONE);
}

/* --- speech recognition: servers ----------------------------------------- */

/* The host of [asr] server<index>, or NULL. */
static char *asr_server_host(VoiceObject *self)
{
    std::ostringstream key;

    key << "server" << self->server_index;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "using server key %s", key.str().c_str());
    return g_key_file_get_string(self->conf, "asr", key.str().c_str(), NULL);
}

/* Pick the first server at random. */
static void asr_pick_random_server(VoiceObject *self)
{
    srand(time(NULL));
    self->server_index = rand() % self->server_count;
    self->asr_server = asr_server_host(self);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "asrServer %s", self->asr_server);
}

/* Move on to the next server. */
static void asr_next_server(VoiceObject *self)
{
    self->server_index = (self->server_index + 1) % self->server_count;
    g_free(self->asr_server);
    self->asr_server = asr_server_host(self);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "asrServer %s", self->asr_server);
}

/* Read the [asr] settings. Returns 0, or 3 if there's no server. */
static int asr_init(VoiceObject *self)
{
    self->asr_socket = 0;
    self->asr_uid = g_string_sized_new(40);
    self->asr_port = g_key_file_get_integer(self->conf, "asr", "port", NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "port %d", self->asr_port);
    self->server_count = g_key_file_get_integer(self->conf, "asr", "nbserver", NULL);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "nbserver %d", self->server_count);
    if (self->server_count <= 0) {
        /* No (or no valid) nbserver: no server, as below (the picks would
         * divide by zero). */
        g_log(NULL, G_LOG_LEVEL_WARNING, "no nbserver in voice.conf [asr]");
        self->asr_server = NULL;
        return 3;
    }
    asr_pick_random_server(self);
    if (self->asr_server == NULL) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "no server in voice.conf found");
        return 3;
    }
    return 0;
}

/*
 * The connection of a starting session, made from the main loop so the
 * daemon keeps answering meanwhile: each server is tried once at most, in
 * turn, each for up to ASR_CONNECT_TIMEOUT_S. asr_connected(self, 0) runs
 * once one has connected (asr_socket blocking again), asr_connected(self, -2)
 * when none could be reached.
 */
static void asr_connected(VoiceObject *self, int ret);
static void asr_connect_next(VoiceObject *self);

/* Drop the attempt's watch and time limit. */
static void asr_connect_unwatch(VoiceObject *self)
{
    if (self->connect_watch != 0)
        g_source_remove(self->connect_watch);
    if (self->connect_timeout != 0)
        g_source_remove(self->connect_timeout);
    self->connect_watch = 0;
    self->connect_timeout = 0;
}

/* The attempt failed (errno says why): close it and try the next server. */
static void asr_connect_failed(VoiceObject *self)
{
    g_log(NULL, G_LOG_LEVEL_WARNING, "connect failed on '%s:%d': %s", self->asr_server, self->asr_port,
          strerror(errno));
    asr_connect_unwatch(self);
    close(self->asr_socket);
    self->asr_socket = 0;
    asr_next_server(self);
    asr_connect_next(self);
}

/* The attempt connected: the socket is made blocking again. */
static void asr_connect_done(VoiceObject *self)
{
    int flags = fcntl(self->asr_socket, F_GETFL, 0);

    asr_connect_unwatch(self);
    if (flags < 0 || fcntl(self->asr_socket, F_SETFL, flags & ~O_NONBLOCK) < 0) {
        asr_connect_failed(self);
        return;
    }
    asr_connected(self, 0);
}

/* The socket is writable or failed: the connection's outcome is known. */
static gboolean asr_connect_ready(GIOChannel *channel, GIOCondition condition, gpointer data)
{
    VoiceObject *self = (VoiceObject *)data;
    int error = 0;
    socklen_t length = sizeof error;

    (void)channel;
    (void)condition;
    self->connect_watch = 0;
    if (getsockopt(self->asr_socket, SOL_SOCKET, SO_ERROR, &error, &length) < 0)
        error = errno;
    if (error != 0) {
        errno = error;
        asr_connect_failed(self);
    } else {
        asr_connect_done(self);
    }
    return FALSE;
}

/* Nothing answered in time. */
static gboolean asr_connect_late(gpointer data)
{
    VoiceObject *self = (VoiceObject *)data;

    self->connect_timeout = 0;
    errno = ETIMEDOUT;
    asr_connect_failed(self);
    return FALSE;
}

/* Start the next attempt, or report that no server could be reached. */
static void asr_connect_next(VoiceObject *self)
{
    struct sockaddr_in address;
    struct hostent *host;
    GIOChannel *channel;
    int flags;

    for (;;) {
        self->connect_attempt++;
        g_log(NULL, G_LOG_LEVEL_DEBUG, "try connection %d/%d", self->connect_attempt, self->server_count);
        if (self->connect_attempt > self->server_count) {
            asr_connected(self, -2);
            return;
        }
        /* A failed attempt's socket is closed (and asr_socket reset) before
         * the next one. */
        self->asr_socket = socket(AF_INET, SOCK_STREAM, 0);
        host = gethostbyname(self->asr_server);
        if (host == NULL) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "nslookup failed on '%s'", self->asr_server);
            close(self->asr_socket);
            self->asr_socket = 0;
            asr_next_server(self);
            continue;
        }
        memset(&address, 0, sizeof address);
        address.sin_family = AF_INET;
        memcpy(&address.sin_addr, host->h_addr_list[0], 4);
        address.sin_port = htons((uint16_t)self->asr_port);
        flags = fcntl(self->asr_socket, F_GETFL, 0);
        if (flags < 0 || fcntl(self->asr_socket, F_SETFL, flags | O_NONBLOCK) < 0) {
            asr_connect_failed(self);
            return;
        }
        if (connect(self->asr_socket, (const struct sockaddr *)&address, sizeof address) == 0) {
            asr_connect_done(self);
            return;
        }
        if (errno != EINPROGRESS) {
            asr_connect_failed(self);
            return;
        }
        channel = g_io_channel_unix_new(self->asr_socket);
        self->connect_watch = g_io_add_watch(channel, (GIOCondition)(G_IO_OUT | G_IO_ERR | G_IO_HUP),
                                             asr_connect_ready, self);
        g_io_channel_unref(channel);
        self->connect_timeout = g_timeout_add_seconds(ASR_CONNECT_TIMEOUT_S, asr_connect_late, self);
        return;
    }
}

/* --- speech recognition: messages ----------------------------------------- */

/* Send a length-prefixed message. Returns 0 or -1. */
static int asr_send(int sock, const AsrMsg &message)
{
    uint32_t size = message.ByteSize();
    uint32_t length = htonl(size);
    char *buffer = (char *)malloc(size + 4);
    int sent = 0;

    memcpy(buffer, &length, 4);
    message.SerializeToArray(buffer + 4, size);
    while (sent < (int)(size + 4)) {
        ssize_t n = send(sock, buffer + sent, size + 4 - sent, 0);
        if (n == -1) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "send failed: %s", strerror(errno));
            free(buffer);
            return -1;
        }
        if (sent != 0)
            g_log(NULL, G_LOG_LEVEL_DEBUG, "send retry, %d bytes sent so far", sent);
        sent += n;
    }
    free(buffer);
    return 0;
}

/* Receive a length-prefixed message; NULL if the connection fails. */
static AsrMsg *asr_receive(int sock)
{
    uint32_t length;
    uint32_t size;
    uint32_t got;
    char *buffer;
    AsrMsg *message;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "reading a message on socket %d\n", sock);
    for (got = 0; got < 4;) {
        ssize_t n = recv(sock, (char *)&length + got, 4 - got, 0);
        if (n < 1) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to receive bytes from server: %s", strerror(errno));
            return NULL;
        }
        got += n;
    }
    size = ntohl(length);
    buffer = (char *)malloc(size);
    for (got = 0; got < size;) {
        ssize_t n = recv(sock, buffer + got, size - got, 0);
        if (n < 1) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to receive bytes message from server: %s", strerror(errno));
            free(buffer);
            return NULL;
        }
        got += n;
    }
    message = new AsrMsg;
    if (!message->ParseFromArray(buffer, size)) {
        /* Unreadable: no answer, as when the server hangs up (asr_report(NULL)). */
        g_log(NULL, G_LOG_LEVEL_WARNING, "ASR: the server's answer can't be read (%u bytes)", size);
        delete message;
        free(buffer);
        return NULL;
    }
    free(buffer);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "message received:\n%s", message->DebugString().c_str());
    return message;
}

/* Has the server answered? 0 if the socket is readable, 1 if not (after up
 * to 2 ms), -1 on error. */
static int asr_poll_socket(VoiceObject *self)
{
    fd_set readable;
    struct timeval timeout;

    FD_ZERO(&readable);
    FD_SET(self->asr_socket, &readable);
    timeout.tv_sec = 0;
    timeout.tv_usec = 2000;
    if (select(self->asr_socket + 1, &readable, NULL, NULL, &timeout) < 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Failed to select socket :(");
        return -1;
    }
    return FD_ISSET(self->asr_socket, &readable) ? 0 : 1;
}

/* --- speech recognition: sessions ----------------------------------------- */

/* Set /dev/audio up for recording: mu-law, mono, 16 kHz. Returns 0 or a
 * negative step number. */
static int audio_setup(int fd)
{
    int value = AFMT_MU_LAW;

    if (ioctl(fd, SNDCTL_DSP_RESET, 0) < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Unable to reset OSS driver.");
        return -1;
    }
    if (ioctl(fd, SNDCTL_DSP_SETFMT, &value) < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Unable to set format");
        return -2;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "accepted format=%d\n", value);
    value = 0;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "expected stereo=%d\n", 0);
    if (ioctl(fd, SNDCTL_DSP_STEREO, &value) < 0)
        return -3;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "accepted stereo=%d\n", value);
    value = 16000;
    if (ioctl(fd, SNDCTL_DSP_SPEED, &value) < 0)
        return -4;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "speed=%d\n", value);
    return 0;
}

/* Signal the server's answer. */
static void asr_report(VoiceObject *self, const AsrMsg *answer)
{
    guint signal = klass_of(self)->signals[SIGNAL_ASR_EVENT];

    if (answer == NULL) {
        g_signal_emit(self, signal, 0, self->asr_uid->str, "<nomatch>", "connection lost", ASR_SCORE_ERROR);
        return;
    }
    if (answer->has_response()) {
        const Response &response = answer->response();

        if (response.code() == lapinlibre::asr::OK) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "emit ok");
            g_signal_emit(self, signal, 0, self->asr_uid->str, response.words().c_str(),
                          response.semantic().c_str(), response.score());
        } else {
            std::string words("");

            g_log(NULL, G_LOG_LEVEL_DEBUG, "emit fail");
            if (response.has_words())
                words = response.words();
            g_signal_emit(self, signal, 0, self->asr_uid->str, "<nomatch>", words.c_str(), ASR_SCORE_ERROR);
        }
    }
    /* An answer without a response signals nothing. */
    g_log(NULL, G_LOG_LEVEL_DEBUG, "emit done");
}

/* Idle callback: send the next chunk of audio, and end the session once the
 * server has answered. */
static gboolean asr_stream(gpointer data)
{
    VoiceObject *self = (VoiceObject *)data;
    unsigned char raw[AUDIO_READ_SIZE];
    unsigned char chunk[AUDIO_CHUNK_SIZE];
    AsrMsg *message;
    AsrMsg *answer = NULL;
    int ready;
    int i;

    /* After a short or failed read the unread audio is silence, so no stale
     * stack bytes are sent as audio. */
    ssize_t n = read(self->audio_fd, raw, sizeof raw);
    if (n < 0)
        n = 0;
    memset(raw + n, 0, sizeof raw - n);
    /* Keep every second byte. */
    for (i = 0; i < AUDIO_CHUNK_SIZE; i++)
        chunk[i] = raw[i * 2];
    message = new AsrMsg;
    message->set_audio(chunk, sizeof chunk);
    asr_send(self->asr_socket, *message);
    delete message;

    ready = asr_poll_socket(self);
    if (ready == 1)
        return TRUE;
    if (ready == -1)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "dead socket");
    else
        answer = asr_receive(self->asr_socket);
    asr_report(self, answer);
    delete answer;

    close(self->audio_fd);
    self->audio_fd = 0;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "close sock");
    close(self->asr_socket);
    self->asr_socket = 0;
    return FALSE;
}

/* Answer the pending asr_start_* call: TRUE when the session started. */
static void asr_reply(VoiceObject *self, int ret)
{
    gboolean result = ret == 0;

    dbus_g_method_return(self->asr_context, result);
    self->asr_context = NULL;
    g_free(self->asr_grammar);
    g_free(self->asr_lang);
    g_free(self->asr_file);
    self->asr_grammar = self->asr_lang = self->asr_file = NULL;
}

/* End the session, if any, without a signal. A session still connecting
 * stops trying, and its asr_start_* call is answered FALSE. */
static int asr_cancel(VoiceObject *self)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "asrCancel CANCEL");
    if (self->asr_context != NULL) {
        asr_connect_unwatch(self);
        if (self->asr_socket != 0)
            close(self->asr_socket);
        self->asr_socket = 0;
        asr_reply(self, -2);
        return 0;
    }
    if (self->asr_socket != 0) {
        close(self->audio_fd);
        self->audio_fd = 0;
        close(self->asr_socket);
        self->asr_socket = 0;
        g_source_remove(self->asr_source);
    }
    return 0;
}

/* Start a session with either an inline `grammar` in `lang` or the grammar in
 * `filename`, answering `context` once it has started or failed: the
 * connection is made from the main loop (asr_connect_next), the rest in
 * asr_connected. A session already running is answered FALSE at once. */
static void asr_start(VoiceObject *self, const char *uid, const char *grammar, const char *lang,
                      const char *filename, DBusGMethodInvocation *context)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "ASR Go\n");
    if (self->asr_socket != 0 || self->asr_context != NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "asr session already present\n");
        dbus_g_method_return(context, FALSE);
        return;
    }
    g_string_assign(self->asr_uid, uid);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "ASR uuid %s\n", self->asr_uid->str);
    self->asr_context = context;
    self->asr_grammar = g_strdup(grammar);
    self->asr_lang = g_strdup(lang);
    self->asr_file = g_strdup(filename);
    self->connect_attempt = 0;
    asr_connect_next(self);
}

/* The session's connection is made (ret 0) or no server could be reached
 * (-2): send the grammar and start streaming, or fail with -3 if /dev/audio
 * can't be used, -6 if the grammar file can't be read. Answers the call. */
static void asr_connected(VoiceObject *self, int ret)
{
    std::string text;
    AsrMsg *message;

    if (ret != 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "asr server connection FAIL");
        goto fail;
    }
    self->audio_fd = open(AUDIO_DEVICE, O_RDONLY);
    if (self->audio_fd == -1) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "asr server cant open /dev/audio");
        ret = -3;
        goto fail;
    }
    if (audio_setup(self->audio_fd) != 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "asr server cant set /dev/audio parameters");
        ret = -3;
        goto fail;
    }

    if (self->asr_file == NULL) {
        if (g_strstr_len(self->asr_grammar, 10, "http") != NULL) {
            /* A grammar URL is passed on as is. */
            text = self->asr_grammar;
        } else {
            /* A grammar rule is wrapped into an ABNF grammar. */
            text += "#ABNF 1.0 UTF-8;\r\nlanguage ";
            text += self->asr_lang;
            text += ";\r\nmode voice;\r\ntag-format <semantics/1.0>;\r\nroot $App;\r\npublic $App = ";
            text += self->asr_grammar;
            text += ";\r\n";
        }
    } else {
        char buffer[100];
        size_t n;
        FILE *file = fopen(self->asr_file, "r");

        if (file == NULL) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "asr server can't open %s\n", self->asr_file);
            ret = -6;
            goto fail;
        }
        while ((int)(n = fread(buffer, 1, sizeof buffer, file)) > 0)
            text.append(buffer, n);
        fclose(file);
    }

    message = new AsrMsg;
    message->set_grammar(text);
    asr_send(self->asr_socket, *message);
    delete message;
    self->asr_source = g_idle_add(asr_stream, self);
    asr_reply(self, 0);
    return;

fail:
    if (self->asr_socket != 0)
        close(self->asr_socket);
    self->asr_socket = 0;
    /* A failed open leaves -1 here, which is not closed. */
    if (self->audio_fd > 0)
        close(self->audio_fd);
    self->audio_fd = 0;
    g_signal_emit(self, klass_of(self)->signals[SIGNAL_ASR_EVENT], 0, self->asr_uid->str,
                  "<error_server_timeout>", "cancel", ASR_SCORE_ERROR);
    asr_reply(self, ret);
}

/* --- D-Bus methods ---------------------------------------------------------- */

/* A failure is answered as a reply with result FALSE (the methods return
 * TRUE: FALSE would mean a D-Bus error, and none is set). `behaviour` is
 * ignored. */

gboolean voice_object_tts_speak(VoiceObject *self, const char *id, const char *text, const char *lang,
                                gint behaviour, gboolean *result, GError **error)
{
    int ret;

    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "SPEAK: %s\n", text);
    ret = tts_speak(self, id, text, lang);
    *result = ret == 0;
    return TRUE;
}

gboolean voice_object_tts_stop(VoiceObject *self, const char *id, gint behaviour, gboolean *result,
                               GError **error)
{
    (void)id;
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "TTS STOP\n");
    tts_cancel(self);
    *result = TRUE;
    return TRUE;
}

/* asr_start_file and asr_start_string answer once the session has started
 * or failed (org.freedesktop.DBus.GLib.Async), so the daemon answers other
 * calls while the session connects. */
gboolean voice_object_asr_start_file(VoiceObject *self, const char *uid, const char *filename, gint behaviour,
                                     DBusGMethodInvocation *context)
{
    (void)behaviour;
    asr_start(self, uid, NULL, NULL, filename, context);
    return TRUE;
}

gboolean voice_object_asr_start_string(VoiceObject *self, const char *uid, const char *grammar,
                                       const char *lang, gint behaviour, DBusGMethodInvocation *context)
{
    (void)behaviour;
    asr_start(self, uid, grammar, lang, NULL, context);
    return TRUE;
}

gboolean voice_object_asr_stop(VoiceObject *self, const char *id, gint behaviour, gboolean *result,
                               GError **error)
{
    (void)id;
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "ASR STOP\n");
    asr_cancel(self);
    *result = TRUE;
    return TRUE;
}

/* --- object ----------------------------------------------------------------- */

static void voice_object_class_init(VoiceObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    klass->signals[SIGNAL_TTS_EVENT] =
        g_signal_new("tts_event", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL, karotz_smarshal_VOID__STRING_INT,
                     G_TYPE_NONE, 2, G_TYPE_STRING, G_TYPE_INT);
    klass->signals[SIGNAL_ASR_EVENT] =
        g_signal_new("asr_event", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                     karotz_smarshal_VOID__STRING_STRING_STRING_INT, G_TYPE_NONE, 4, G_TYPE_STRING, G_TYPE_STRING,
                     G_TYPE_STRING, G_TYPE_INT);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    g_return_if_fail(klass != NULL);
}

static void voice_object_init(VoiceObject *self)
{
    GError *error = NULL;
    int ret;

    dbus_g_object_type_install_info(VOICE_TYPE_OBJECT, &dbus_glib_voice_object_object_info);
    g_return_if_fail(self != NULL);
    self->conf = g_key_file_new();
    if (!g_key_file_load_from_file(self->conf, VOICE_CONF, G_KEY_FILE_NONE, &error)) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "%s", error->message);
        exit(-1);
    }
    ret = asr_init(self);
    if (ret != 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "ASR init FAIL%d\n", ret);
        exit(-1);
    }
    ret = tts_init(self);
    if (ret != 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "TTS init FAIL %d\n", ret);
        exit(-1);
    }
}

int main(void)
{
    int log_priority;
    GError *error = NULL;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    GMainLoop *loop;
    VoiceObject *voice;
    guint request_result;

    log_priority = getenv("KAROTZ_DEBUG") == NULL ? LOG_NOTICE : LOG_DEBUG;
    init_karotz_log("Voice", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");

    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Failed to open connection to bus");
        for (;;)
            ;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Successfully connected to %s bus\n", "SYSTEM");

    voice = (VoiceObject *)g_object_new(VOICE_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Voice", G_OBJECT(voice));

    bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, "com.mindscape.karotz.Voice", 0, &request_result, &error)) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Failed to get name");
        for (;;)
            ;
    }
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }

    g_signal_emit(voice, klass_of(voice)->signals[SIGNAL_REGISTERED], 0);
    g_main_loop_run(loop);
    return 0;
}
