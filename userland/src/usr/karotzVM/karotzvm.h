/* SPDX-License-Identifier: MIT */
/*
 * karotzVM: runs the Karotz's JavaScript applications on V8 2.5.
 *
 * The controller starts one karotzVM ("karotzVM --app_folder=/usr/karotz/apps")
 * and writes one line per application launch to its standard input:
 * "<app> [<launch type> [<instance>]]". For each line karotzVM forks a child
 * (at most three at a time) that loads /usr/karotz/apps/<app>/descriptor.xml,
 * builds the JavaScript API the descriptor grants (karotz.led, karotz.ears,
 * ...), runs main.js and then a GLib main loop. The application itself opens
 * the link to the controller with karotz.connect(host, port) (the
 * controller's TCP server, port 9123) and asks for interactive mode with
 * karotz.start(callback, data); every API call then is a protobuf
 * message (4-byte big-endian length + lapinlibre.platform.PlatformMessage) on
 * that socket, and the controller's replies and events come back the same
 * way.
 *
 * Declarations shared by karotzVM's modules: one file per area and one file
 * per JavaScript object.
 */
#ifndef KAROTZVM_H
#define KAROTZVM_H

#include <string>
#include <time.h>

#include <glib.h>
#include <v8.h>

#include "platform-message.pb.h"

namespace pm = lapinlibre::platform;

/* Every message is logged in this domain (see log_handler in main.cc). */
#define KVM_LOG "KarotzVM"

/* ---- command line (main.cc) -------------------------------------------- */

/* The options: --debug=<level> (messages up to level 1 << level are
 * printed on stderr, default 7 = everything) and --app_folder=<dir>. */
struct Options {
    int debug;
    const char *app_folder;
};

/* ---- messages received from the controller ---------------------------- */

/* Something that wants to see the messages from the controller (button presses, RFID tags, ...):
 * MessageListener. It gets every message but an interactiveMode one, replies included, and looks
 * for its own kind. on_message returns false to be removed (and deleted). */
class MessageListener {
public:
    virtual ~MessageListener() {}
    virtual bool on_message(const pm::PlatformMessage &msg) = 0;
};

/* A JavaScript listener function behind a MessageListener. */
class JsListener : public MessageListener {
public:
    explicit JsListener(v8::Handle<v8::Function> fn) : function(v8::Persistent<v8::Function>::New(fn)) {}

protected:
    v8::Persistent<v8::Function> function;
};

/* ---- the running application (karotz_data.cc) -------------------------- */

/* KarotzData: the application this process runs, reachable from
 * JavaScript through internal field 0 of the global object. It is itself the
 * listener the receive loop hands unsolicited messages to, and passes them
 * on to the listeners the application registered. */
class KarotzData : public MessageListener {
public:
    KarotzData();
    ~KarotzData();

    /* "<app> [<launch type> [<instance>]]". */
    void set_cmdline(const char *line);
    void add_listener(MessageListener *listener);
    /* Every listener in turn; those returning false are dropped. */
    bool on_message(const pm::PlatformMessage &msg);

    int socket;              /* the controller link, -1 until karotz.connect */
    char *interactive_id;    /* set once interactive mode is granted */
    GMainLoop *loop;
    GList *listeners;        /* of MessageListener* */
    std::string app_name;
    std::string launch_type;
    std::string instance_name;
    std::string cmdline;
    Options *options;
    class Descriptor *descriptor;

private:
    KarotzData(const KarotzData &);
    KarotzData &operator=(const KarotzData &);
};

/* The application this process runs (see js.cc). */
KarotzData *current_karotz();
/* Set once, by app_load(). */
void set_current_karotz(KarotzData *karotz);

/* ---- descriptor.xml (descriptor.cc) ------------------------------------ */

/* An application's descriptor: the parsed document and the result of the
 * XPath /descriptor/accesses/access. A descriptor that cannot be
 * read makes the process exit(0). */
class Descriptor {
public:
    explicit Descriptor(const char *path);
    ~Descriptor();
    /* Whether <access>name</access> is granted. */
    bool has_access(const char *name) const;

private:
    void *document;  /* xmlDocPtr */
    void *root;      /* xmlNodePtr */
    void *accesses;  /* xmlXPathObjectPtr */

    Descriptor(const Descriptor &);
    Descriptor &operator=(const Descriptor &);
};

/* ---- the controller link (controller_link.cc) -------------------------- */

/* A reply handler waiting for the message whose id a reply carries as its
 * correlationId: handler(data, reply). A registered handler stays until its
 * request's last reply, so a second reply with the same correlationId calls
 * it again: a request can get a Response and then Events. A one-shot
 * handler (once) is removed, and the ReplyCallback deleted, after its first
 * call; others when they call reply_done(); the rest age out after
 * ten hours. */
typedef void (*ReplyHandler)(void *data, const pm::PlatformMessage &reply);
/* Frees a handler's data when the handler is dropped before its last
 * reply: aged out, or replaced by a request whose id compares equal. */
typedef void (*ReplyRelease)(void *data);
/* Called by a handler that has had its last reply and freed its data (for
 * requests with several replies, the last one recognisable only by its
 * content): it is removed like a one-shot handler once it returns. */
void reply_done(void);
struct ReplyCallback {
    ReplyCallback(ReplyHandler handler, void *data, ReplyRelease release, bool once = false)
        : handler(handler), data(data), release(release), stamp(0), once(once), running(false),
          dropped(false) {}
    ReplyHandler handler;
    void *data;
    ReplyRelease release;  /* NULL: nothing to free */
    time_t stamp;
    bool once;
    bool running;         /* its handler is being called */
    bool dropped;         /* taken out of the table while running: freed after */
};

/* A new message with a fresh UUID as id; interactive_id is set
 * when not NULL. */
pm::PlatformMessage *link_new_message(const char *interactive_id);
/* Logs and sends a message: 4-byte big-endian length + the message. A
 * failed send() logs and returns -1. */
int link_send(int socket, const pm::PlatformMessage &msg);
/* Registers callback under the message's id, then sends it. */
void link_send_with_callback(int socket, const pm::PlatformMessage &msg, ReplyCallback *callback);

/* ---- JavaScript helpers (js.cc) ---------------------------------------- */

/* ThrowException(String::New(message)). */
v8::Handle<v8::Value> js_throw(const char *message);
/* The current KarotzData, or NULL after throwing "Must be in interactive
 * mode" when interactive mode has not been granted yet. */
KarotzData *interactive_karotz();
/*
 * Most reply handlers and listeners enter a context before building the
 * callback's arguments (so those objects belong to that context), call the
 * function with the function itself as receiver, then leave it. A
 * FreshContext does the first and last steps for the duration of a scope.
 *
 * It enters the application's own context rather than a new one. With a
 * new context created and disposed for every reply, V8 2.5 loses track of
 * the application's context after about eighty of them: the bindings find
 * no application, the scripts' globals stop resolving, and the program
 * crashes. The arguments belong to the application's context, which the
 * callbacks run in.
 *
 * The callbacks don't open a HandleScope themselves: the main loop's two
 * ways into JavaScript do, poll_controller() for each message from the
 * controller (reply handlers and listeners) and timeout_expired() for each
 * setTimeout callback, so the handles made while handling an event are
 * freed when it is done instead of piling up in app_run()'s scope for as
 * long as the application runs.
 */
/* The application's context, kept for the callbacks (set by app_load()). */
v8::Handle<v8::Context> app_context();
void set_app_context(v8::Handle<v8::Context> context);
/* Log an exception caught by try_catch (file:line, the source line, a
 * marker under the culprit and the stack trace). */
void report_exception(v8::TryCatch &try_catch);

class FreshContext {
public:
    FreshContext() : context(app_context()) { context->Enter(); }
    ~FreshContext() { context->Exit(); }

private:
    v8::Handle<v8::Context> context;

    FreshContext(const FreshContext &);
    FreshContext &operator=(const FreshContext &);
};

/* fn->Call(fn, argc, argv) inside a FreshContext (for callbacks whose
 * arguments are primitives or were built in a FreshContext already). */
v8::Handle<v8::Value> js_call(v8::Persistent<v8::Function> &fn, int argc, v8::Handle<v8::Value> argv[]);
/* Value::IsString() as V8's inline version tests it. */
bool js_is_string(v8::Handle<v8::Value> value);
/* Adds name -> FunctionTemplate(callback) to an object template. */
void js_set_function(v8::Handle<v8::ObjectTemplate> tmpl, const char *name, v8::InvocationCallback callback);

/* A JavaScript function kept for later with a name for the logs. */
/* Which reply is a request's last, from
 * what the controller and the daemons send for it: its handler and its data
 * are freed after that reply, and a later reply finds no correlation.
 * Requests whose last reply can't be told are kept until they age out. */
enum ReplyEnd {
    REPLY_END_NEVER,          /* not recognisable: kept */
    REPLY_END_FIRST,          /* the only reply */
    REPLY_END_EVENT,          /* the Event; a Response ERROR may or may not have one after it */
    REPLY_END_EVENT_OR_ERROR, /* the Event, or a Response ERROR, which has none after it */
};
bool reply_is_last(ReplyEnd end, const pm::PlatformMessage &reply);

struct NamedFunction {
    NamedFunction(const char *name, v8::Handle<v8::Function> fn, ReplyEnd end = REPLY_END_NEVER);
    ~NamedFunction();
    v8::Persistent<v8::Function> function;
    char *name;
    ReplyEnd end;  /* for event_reply and the other reply handlers */
};

/* A JavaScript function kept for later (a heap-allocated Persistent). */
typedef v8::Persistent<v8::Function> StoredFunction;
StoredFunction *store_function(v8::Handle<v8::Value> fn);
/* Disposes and deletes it. */
void release_function(StoredFunction *fn);
/* ReplyRelease functions for a reply handler's NamedFunction and
 * StoredFunction. */
void release_named_function(void *fn);
void release_stored_function(void *fn);

/* ---- shared reply handlers (events.cc) --------------------------------- */

/* For a NamedFunction: logs "Event: <name>", then calls it with the
 * ReturnCode name of the reply's Response code and of its Event code, when
 * present. Used by led, ears.move, dock and most multimedia calls. */
void event_reply(void *named_function, const pm::PlatformMessage &reply);

/* http.cc: the HTTP POST and GET behind http.post/get and the social
 * objects, returning {header, content}. */
v8::Handle<v8::Value> http_post_args(v8::Handle<v8::Value> argv[], int argc);
v8::Handle<v8::Value> http_get_args(v8::Handle<v8::Value> argv[], int argc);

/* contact.cc: a Contact as a JavaScript object, also used by
 * karotz.message. */
v8::Handle<v8::Value> contact_to_js(const pm::Contact &contact);

/* asr.cc: parses <semantic> XML into properties of an object. */
void asr_parse_semantic(const char *xml, v8::Handle<v8::Object> into);

/* ---- utilities (util.cc) ------------------------------------------------ */

/* A lower-case random UUID, 36 characters + NUL. */
void make_uuid(char out[37]);
/* True if a file name is refused: anything naming descriptor.xml,
 * or holding characters other than letters, digits, '_', '-', '/' and '.'
 * (a '.' only when not right after another '.'). */
bool path_refused(const char *name);

/* ---- the application (app.cc) ------------------------------------------ */

/* Runs one application in the forked child until its main loop ends. */
void app_run(KarotzData *karotz);

/* ---- JavaScript objects (one file each) --------------------------------- */

/* Object templates for karotz.<name>, each built when the descriptor grants
 * the access of the same name; social (a global) always, and http (a global) with its access. */
v8::Handle<v8::ObjectTemplate> led_template();        /* led.cc */
v8::Handle<v8::ObjectTemplate> ears_template();       /* ears.cc */
v8::Handle<v8::ObjectTemplate> tts_template();        /* tts.cc */
v8::Handle<v8::ObjectTemplate> webcam_template();     /* webcam.cc */
v8::Handle<v8::ObjectTemplate> asr_template();        /* asr.cc */
v8::Handle<v8::ObjectTemplate> multimedia_template(); /* multimedia.cc */
v8::Handle<v8::ObjectTemplate> button_template();     /* button.cc */
v8::Handle<v8::ObjectTemplate> rfid_template();       /* rfid.cc */
v8::Handle<v8::ObjectTemplate> k2k_template();        /* k2k.cc */
v8::Handle<v8::ObjectTemplate> message_template();    /* message.cc */
v8::Handle<v8::ObjectTemplate> contact_template();    /* contact.cc */
v8::Handle<v8::ObjectTemplate> serial_template();     /* serial.cc */
v8::Handle<v8::ObjectTemplate> dock_template();       /* dock.cc */
v8::Handle<v8::ObjectTemplate> social_template();     /* social.cc */
v8::Handle<v8::ObjectTemplate> http_template();       /* http.cc */
v8::Handle<v8::ObjectTemplate> file_template();       /* file.cc (global "file") */

/* Functions on the karotz object itself and on the global object. */
v8::Handle<v8::Value> js_karotz_connect(const v8::Arguments &args); /* controller_link.cc */
v8::Handle<v8::Value> js_karotz_start(const v8::Arguments &args);   /* controller_link.cc */
v8::Handle<v8::Value> js_karotz_stop(const v8::Arguments &args);    /* controller_link.cc */
v8::Handle<v8::Value> js_karotz_info(const v8::Arguments &args);    /* info.cc */
v8::Handle<v8::Value> js_ping(const v8::Arguments &args);           /* info.cc */
v8::Handle<v8::Value> js_sign_hmac_sha1(const v8::Arguments &args); /* social.cc */

#endif
