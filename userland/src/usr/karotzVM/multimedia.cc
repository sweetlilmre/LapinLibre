// SPDX-License-Identifier: MIT
/*
 * karotz.multimedia: the music player and the microphone recorder.
 *
 *   play(url [, callback])            url: a string (a "./file" is taken
 *                                     relative to the application's folder)
 *                                     or an object whose internal field 1
 *                                     holds the path (the "data" of a record
 *                                     or webcam event)
 *   pause/resume/stop/next/previous([callback])
 *   record([callback])
 *   artist/folder/genre/playlist/song(callback)
 *   addListener(listener)
 *
 * Every call but addListener sends a Multimedia message to the controller.
 * The play..previous callbacks receive the name of the ReturnCode of the
 * Response and of the Event (one string per call); the record callback
 * receives {type} for the Response and {type, description, data} for the
 * Event, data being an object whose internal field 1 holds
 * "/tmp/record.flac"; the list callbacks receive the first entry of the
 * first non-empty list of the reply. Listeners receive the name of each
 * MultimediaCallback action (PLAY, STOP, ...) and stay registered only while
 * they return true.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "karotzvm.h"

/* ---- reply handlers ----------------------------------------------------- */

/* Calls the function with the function itself as receiver. */
static void call_callback(NamedFunction *callback, v8::Handle<v8::Value> argument)
{
    v8::Handle<v8::Value> argv[1] = { argument };
    callback->function->Call(callback->function, 1, argv);
}

/* record callback(response) with {type} (type only when the
 * Response has a code; its description is not passed). */
static void record_response(NamedFunction *callback, const pm::Response &response)
{
    FreshContext context;
    v8::Handle<v8::Object> result = v8::Object::New();
    if (response.has_code())
        result->Set(v8::String::New("type"), v8::String::New(pm::ReturnCode_Name(response.code()).c_str()));
    call_callback(callback, result);
}

/* record callback(event) with {type, description, data}; type and
 * description only when present, data always: an instance of a template with
 * two internal fields, field 1 = "/tmp/record.flac" (field 0 left unset). */
static void record_event(NamedFunction *callback, const pm::Event &event)
{
    FreshContext context;
    v8::Handle<v8::Object> result = v8::Object::New();
    if (event.has_code())
        result->Set(v8::String::New("type"), v8::String::New(pm::ReturnCode_Name(event.code()).c_str()));
    if (event.has_description())
        result->Set(v8::String::New("description"), v8::String::New(event.description().c_str()));

    v8::Handle<v8::ObjectTemplate> data_template = v8::ObjectTemplate::New();
    data_template->SetInternalFieldCount(2);
    v8::Handle<v8::Object> data = data_template->NewInstance();
    data->SetInternalField(1, v8::String::New("/tmp/record.flac"));
    result->Set(v8::String::New("data"), data);

    call_callback(callback, result);
}

/* Reply handler of record (data: the NamedFunction).
 * The NamedFunction is freed after the recording's one Event, or a Response
 * ERROR. */
static void record_reply(void *data, const pm::PlatformMessage &reply)
{
    NamedFunction *callback = static_cast<NamedFunction *>(data);

    if (reply.has_response()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "has_response");
        record_response(callback, reply.response());
    }
    if (reply.has_event()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "has_event");
        record_event(callback, reply.event());
    }
    if (reply_is_last(callback->end, reply)) {
        delete callback;
        reply_done();
    }
}

/* Reply handler of the list requests (data: the StoredFunction).
 * Calls it with the FIRST entry of the first non-empty list, looked for in
 * the order artist, folder, playlist, genre, song, then frees it. The
 * function is called directly (poll_controller() has entered the
 * application's context).
 * Only the first entry is passed; the controller puts the whole list in
 * that one entry, the names each followed by ':' ("a:b:c:"). Quirk: when
 * there is no list (the controller's ERROR when multimedia-daemon fails),
 * the function is freed and not called, so the application is never told;
 * whether it should be is not known. The handler is registered one-shot,
 * so a second reply with the same correlationId finds nothing. */
static void multimedia_list_reply(void *data, const pm::PlatformMessage &reply)
{
    StoredFunction *callback = static_cast<StoredFunction *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "MULTIMEDIA: MultimedialistCallback");
    v8::Handle<v8::Value> argv[1];
    const pm::Multimedia &multimedia = reply.multimedia();
    if (multimedia.artistlist_size() > 0) {
        argv[0] = v8::String::New(multimedia.artistlist(0).c_str());
    } else if (multimedia.folderlist_size() > 0) {
        argv[0] = v8::String::New(multimedia.folderlist(0).c_str());
    } else if (multimedia.playlistlist_size() > 0) {
        argv[0] = v8::String::New(multimedia.playlistlist(0).c_str());
    } else if (multimedia.genrelist_size() > 0) {
        argv[0] = v8::String::New(multimedia.genrelist(0).c_str());
    } else if (multimedia.songlist_size() > 0) {
        argv[0] = v8::String::New(multimedia.songlist(0).c_str());
    } else {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "MULTIMEDIA: MultimedialistCallback: no list");
        release_function(callback);
        return;
    }
    (*callback)->Call(*callback, 1, argv);
    release_function(callback);
}

/* ---- listener ----------------------------------------------------------- */

/* MultimediaMsgCallback: a listener registered with addListener. */
class MultimediaMsgCallback : public JsListener {
public:
    explicit MultimediaMsgCallback(v8::Handle<v8::Function> fn) : JsListener(fn) {}

    /* Messages without a multimediaCallback are ignored (the
     * listener stays). Otherwise the function is called directly (in the
     * context poll_controller() has entered) with the action's name and
     * stays registered only if it returns true (a boolean, not merely
     * truthy). */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_multimediacallback())
            return true;
        v8::Handle<v8::Value> argv[1];
        argv[0] = v8::String::New(pm::MultimediaCallback_TypeAction_Name(msg.multimediacallback().action()).c_str());
        v8::Handle<v8::Value> result = function->Call(function, 1, argv);
        /* A listener that threw (empty result) stays registered; the
         * exception is reported by call_listener(). */
        if (result.IsEmpty())
            return true;
        return result->IsBoolean() && result->BooleanValue();
    }
};

/* ---- helpers ------------------------------------------------------------ */

/* A malloc'ed copy of text[from..to] (both included), or NULL when
 * text is NULL or from > to. Exits when out of memory. */
static char *substring(const char *text, unsigned from, unsigned to)
{
    char *copy = NULL;
    if (text != NULL && from <= to) {
        copy = static_cast<char *>(malloc(to - from + 2));
        if (copy == NULL) {
            fwrite("out of memory\n", 1, 14, stderr);
            exit(1);
        }
        unsigned i;
        for (i = from; i <= to; i++)
            copy[i - from] = text[i];
        copy[i - from] = '\0';
    }
    return copy;
}

/* A new message for the current interactive session. */
static pm::PlatformMessage *new_message(KarotzData *karotz)
{
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    return msg;
}

/* Sends msg, with event_reply when there is a callback, and deletes it. */
static void send_and_delete(KarotzData *karotz, pm::PlatformMessage *msg, NamedFunction *callback)
{
    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(event_reply, callback, release_named_function));
    delete msg;
}

/* ---- play ----------------------------------------------------------------- */

/* play(url [, callback]).
 * The url argument: a string is converted with ToString; an object with at
 * least two internal fields gives its internal field 1 (e.g. the "data" of a
 * record or webcam event); anything else gives no string at all.
 * A url starting with "./" (and longer than 2 characters) is a file of the
 * application: "/usr/karotz/apps/<app>/<rest>", at most 254 characters.
 * Anything that gives no string throws "Wrong arguments" once in
 * interactive mode, after logging "play[(null)]". The rest is freed, and so
 * is the NamedFunction when the interactive-mode check throws. */
static v8::Handle<v8::Value> js_multimedia_play(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    /* Kept (REPLY_END_NEVER): multimedia-daemon sends TERMINATED for the
     * latest play on every pause, stop and end, so more can always come. */
    if (args.Length() == 2 && args[1]->IsFunction())
        callback = new NamedFunction("MutimediaPlay", v8::Handle<v8::Function>::Cast(args[1]));
    else if (args.Length() != 1)
        return js_throw("Wrong arguments");

    v8::Handle<v8::String> url_string;
    if (js_is_string(args[0])) {
        url_string = args[0]->ToString();
    } else if (args[0]->IsObject()) {
        v8::Handle<v8::Object> object = args[0]->ToObject();
        if (object->InternalFieldCount() > 1)
            url_string = v8::Handle<v8::String>::Cast(object->GetInternalField(1));
    }
    v8::String::AsciiValue url(url_string);

    KarotzData *karotz = current_karotz();
    if (karotz->interactive_id == NULL) {
        delete callback;
        return js_throw("Must be in interactive mode");
    }

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "MULTIMEDIA: play[%s]", *url);
    const char *ascii = *url;
    if (ascii == NULL) {
        /* No url string. */
        delete callback;
        return js_throw("Wrong arguments");
    }
    pm::PlatformMessage *msg = new_message(karotz);
    pm::Multimedia::Play *play = msg->mutable_multimedia()->mutable_play();
    if (ascii[0] == '.' && ascii[1] == '/' && strlen(ascii) > 2) {
        char *rest = substring(ascii, 2, strlen(ascii) - 1);
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "MULTIMEDIA: app-relative path[%s]", rest);
        char buffer[512];
        snprintf(buffer, sizeof buffer, "%s/%s/%s", karotz->options->app_folder, karotz->app_name.c_str(), rest);
        free(rest);
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "MULTIMEDIA: buffer play[%s]", buffer);
        play->set_url(buffer);
    } else {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "MULTIMEDIA: *ascii play[%s]", ascii);
        play->set_url(ascii);
    }
    send_and_delete(karotz, msg, callback);
    return v8::Undefined();
}

/* ---- pause, resume, stop, next, previous ------------------------------- */

/* The commands without parameters. */
enum Command { PAUSE, RESUME, STOP, NEXT, PREVIOUS };

/* pause, resume, stop, next and previous: command([callback]). The
 * NamedFunction is freed when the interactive-mode check throws. */
static v8::Handle<v8::Value> multimedia_command(const v8::Arguments &args, Command command,
                                                const char *callback_name, const char *log)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 1 && args[0]->IsFunction())
        callback = new NamedFunction(callback_name, v8::Handle<v8::Function>::Cast(args[0]), REPLY_END_FIRST);
    else if (args.Length() != 0)
        return js_throw("Wrong arguments");

    KarotzData *karotz = current_karotz();
    if (karotz->interactive_id == NULL) {
        delete callback;
        return js_throw("Must be in interactive mode");
    }

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "%s", log);
    pm::PlatformMessage *msg = new_message(karotz);
    pm::Multimedia *multimedia = msg->mutable_multimedia();
    switch (command) {
    case PAUSE:    multimedia->mutable_pause(); break;
    case RESUME:   multimedia->mutable_resume(); break;
    case STOP:     multimedia->mutable_stop(); break;
    case NEXT:     multimedia->mutable_next(); break;
    case PREVIOUS: multimedia->mutable_previous(); break;
    }
    send_and_delete(karotz, msg, callback);
    return v8::Undefined();
}

static v8::Handle<v8::Value> js_multimedia_pause(const v8::Arguments &args)
{
    return multimedia_command(args, PAUSE, "MutimediaPause", "MULTIMEDIA: pause");
}

static v8::Handle<v8::Value> js_multimedia_resume(const v8::Arguments &args)
{
    return multimedia_command(args, RESUME, "MultimediaResume", "MULTIMEDIA: resume");
}

static v8::Handle<v8::Value> js_multimedia_stop(const v8::Arguments &args)
{
    return multimedia_command(args, STOP, "MutimediaStop", "MULTIMEDIA: stop");
}

static v8::Handle<v8::Value> js_multimedia_next(const v8::Arguments &args)
{
    return multimedia_command(args, NEXT, "MultimediaNext", "MULTIMEDIA: next");
}

static v8::Handle<v8::Value> js_multimedia_previous(const v8::Arguments &args)
{
    return multimedia_command(args, PREVIOUS, "MultimediaPrevious", "MULTIMEDIA: previous");
}

/* ---- artist, folder, genre, playlist, song ---------------------------- */

/* The list requests. */
enum ListRequest { GET_ARTIST, GET_FOLDER, GET_GENRE, GET_PLAYLIST, GET_SONG };

/* artist, folder, genre, playlist and song: request(callback); the
 * callback is mandatory. */
static v8::Handle<v8::Value> multimedia_list(const v8::Arguments &args, ListRequest request, const char *log)
{
    if (!(args.Length() == 1 && args[0]->IsFunction()))
        return js_throw("Wrong arguments");

    KarotzData *karotz = current_karotz();
    if (karotz->interactive_id == NULL)
        return js_throw("Must be in interactive mode");

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "%s", log);
    StoredFunction *callback = store_function(args[0]);
    pm::PlatformMessage *msg = new_message(karotz);
    pm::Multimedia *multimedia = msg->mutable_multimedia();
    switch (request) {
    case GET_ARTIST:   multimedia->mutable_getartist(); break;
    case GET_FOLDER:   multimedia->mutable_getfolder(); break;
    case GET_GENRE:    multimedia->mutable_getgenre(); break;
    case GET_PLAYLIST: multimedia->mutable_getplaylist(); break;
    case GET_SONG:     multimedia->mutable_getsong(); break;
    }
    link_send_with_callback(karotz->socket, *msg, new ReplyCallback(multimedia_list_reply, callback, release_stored_function, true)); /* one-shot */
    delete msg;
    return v8::Undefined();
}

static v8::Handle<v8::Value> js_multimedia_artist(const v8::Arguments &args)
{
    return multimedia_list(args, GET_ARTIST, "MULTIMEDIA: GetArtist");
}

static v8::Handle<v8::Value> js_multimedia_folder(const v8::Arguments &args)
{
    return multimedia_list(args, GET_FOLDER, "MULTIMEDIA: GetFolder");
}

static v8::Handle<v8::Value> js_multimedia_genre(const v8::Arguments &args)
{
    return multimedia_list(args, GET_GENRE, "MULTIMEDIA: GetGenre");
}

static v8::Handle<v8::Value> js_multimedia_playlist(const v8::Arguments &args)
{
    return multimedia_list(args, GET_PLAYLIST, "MULTIMEDIA: GetPlaylist");
}

static v8::Handle<v8::Value> js_multimedia_song(const v8::Arguments &args)
{
    return multimedia_list(args, GET_SONG, "MULTIMEDIA: GetSong");
}

/* ---- addListener, record ------------------------------------------------ */

/* addListener(listener). No interactive-mode check. */
static v8::Handle<v8::Value> js_multimedia_add_listener(const v8::Arguments &args)
{
    if (!(args.Length() == 1 && args[0]->IsFunction()))
        return js_throw("Wrong arguments");

    KarotzData *karotz = current_karotz();
    karotz->add_listener(new MultimediaMsgCallback(v8::Handle<v8::Function>::Cast(args[0])));
    return v8::Undefined();
}

/* record([callback]): sends record with an empty url (the
 * recording lands in /tmp/record.flac, which the event's data names).
 * The NamedFunction is freed when the interactive-mode check throws. */
static v8::Handle<v8::Value> js_multimedia_record(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 1 && args[0]->IsFunction())
        callback = new NamedFunction("MutimediaRecord", v8::Handle<v8::Function>::Cast(args[0]), REPLY_END_EVENT_OR_ERROR);
    else if (args.Length() != 0)
        return js_throw("Wrong arguments");

    KarotzData *karotz = current_karotz();
    if (karotz->interactive_id == NULL) {
        delete callback;
        return js_throw("Must be in interactive mode");
    }

    pm::PlatformMessage *msg = new_message(karotz);
    msg->mutable_multimedia()->mutable_record()->set_url("");
    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(record_reply, callback, release_named_function));
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> multimedia_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "play", js_multimedia_play);
    js_set_function(tmpl, "pause", js_multimedia_pause);
    js_set_function(tmpl, "resume", js_multimedia_resume);
    js_set_function(tmpl, "stop", js_multimedia_stop);
    js_set_function(tmpl, "next", js_multimedia_next);
    js_set_function(tmpl, "previous", js_multimedia_previous);
    js_set_function(tmpl, "artist", js_multimedia_artist);
    js_set_function(tmpl, "folder", js_multimedia_folder);
    js_set_function(tmpl, "genre", js_multimedia_genre);
    js_set_function(tmpl, "playlist", js_multimedia_playlist);
    js_set_function(tmpl, "song", js_multimedia_song);
    js_set_function(tmpl, "addListener", js_multimedia_add_listener);
    js_set_function(tmpl, "record", js_multimedia_record);
    return tmpl;
}
