// SPDX-License-Identifier: MIT
/*
 * The link to the controller: a TCP connection the application opens with
 * karotz.connect(host, port) (the controller's server, port 9123), carrying
 * PlatformMessage protobuf messages each preceded by its length (4 bytes, big
 * endian). A GLib idle callback polls the socket; a message that doesn't
 * parse is logged and dropped; a reply (a message with a
 * correlationId) goes to the handler registered under the id of the message
 * it answers, and every message but an interactiveMode one, replies
 * included, to the application's listeners. Interactive
 * mode, which every API call needs, is asked for with karotz.start() and
 * given up with karotz.stop() or by the controller.
 */
#include <errno.h>
#include <netdb.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <string>
#include <vector>

#include <google/protobuf/descriptor.h>

#include "karotzvm.h"

/* How long karotzVM survives the controller ending interactive mode. */
#define AUTOKILL_AFTER_STOP_MS 300000
/* Reply handlers older than this are dropped when a new one is added. */
#define CALLBACK_LIFETIME_S 36000

/* ---- messages ----------------------------------------------------------- */

pm::PlatformMessage *link_new_message(const char *interactive_id)
{
    char id[37];

    make_uuid(id);
    pm::PlatformMessage *msg = new pm::PlatformMessage;
    msg->set_id(id);
    if (interactive_id != NULL)
        msg->set_interactiveid(interactive_id);
    return msg;
}

/* A failed send() is logged and gives -1. */
int link_send(int socket, const pm::PlatformMessage &msg)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Sending MSG\n%s", msg.DebugString().c_str());
    int size = msg.ByteSize();
    unsigned char *buffer = static_cast<unsigned char *>(malloc(size + 4));
    uint32_t length = htonl(size);
    memcpy(buffer, &length, 4);
    msg.SerializeToArray(buffer + 4, size);
    for (int sent = 0; sent < size + 4;) {
        ssize_t n = send(socket, buffer + sent, size + 4 - sent, 0);
        if (n == -1) {
            g_log(KVM_LOG, G_LOG_LEVEL_CRITICAL, "send failed: %s", strerror(errno));
            free(buffer);
            return -1;
        }
        sent += n;
    }
    free(buffer);
    return 0;
}

/* ---- reply handlers ----------------------------------------------------- */

/*
 * Message id -> ReplyCallback, keyed by the whole id string. The table has
 * no lock: the VM is single-threaded.
 */
static GHashTable *callbacks;

/* A handler dropped before its last reply, with its data. */
static void destroy_callback(ReplyCallback *callback)
{
    if (callback->release != NULL && callback->data != NULL)
        callback->release(callback->data);
    delete callback;
}

/* A handler taken out of the table (its key already freed) before its
 * last reply: freed now, or after its call if it is being called. */
static void drop_callback(ReplyCallback *callback)
{
    if (callback->running)
        callback->dropped = true;
    else
        destroy_callback(callback);
}

/* Aged-out handlers are taken out of the table and freed with their id
 * and data. GLib no longer touches a key or value once this returns TRUE. */
static gboolean callback_expired(gpointer key, gpointer value, gpointer)
{
    ReplyCallback *callback = static_cast<ReplyCallback *>(value);

    if (time(NULL) - callback->stamp <= CALLBACK_LIFETIME_S)
        return FALSE;
    free(key);
    drop_callback(callback);
    return TRUE;
}

static void register_callback(char *id, ReplyCallback *callback)
{
    gpointer old_key, old_value;

    if (callbacks == NULL)
        callbacks = g_hash_table_new(g_str_hash, g_str_equal);
    g_hash_table_foreach_remove(callbacks, callback_expired, NULL);
    callback->stamp = time(NULL);
    /* An id already registered (a repeated UUID) replaces its handler;
     * the old handler and its id are freed. */
    if (g_hash_table_lookup_extended(callbacks, id, &old_key, &old_value)) {
        g_hash_table_steal(callbacks, old_key);
        free(old_key);
        drop_callback(static_cast<ReplyCallback *>(old_value));
    }
    g_hash_table_insert(callbacks, id, callback);
}

static ReplyCallback *find_callback(const pm::PlatformMessage &reply)
{
    if (callbacks == NULL)
        return NULL;
    return static_cast<ReplyCallback *>(g_hash_table_lookup(callbacks, reply.correlationid().c_str()));
}

/* Set by reply_done() from inside a handler: remove it afterwards. */
static bool callback_done;

void reply_done(void)
{
    callback_done = true;
}

static void call_callback(ReplyCallback *callback, const pm::PlatformMessage &reply)
{
    callback_done = false;
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "reply callback handler");
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "function pointer: %p", (void *)callback->handler);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "callback data: %p", callback->data);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "response: %p", (const void *)&reply);
    callback->running = true;
    {
        /* An exception the application's callback throws is reported, as
         * a listener's or a timer's is, and the application goes on. */
        v8::TryCatch try_catch;
        callback->handler(callback->data, reply);
        if (try_catch.HasCaught())
            report_exception(try_catch);
    }
    callback->running = false;
    if (callback->dropped) {
        /* Taken out of the table by a request the handler made. */
        if (callback->once || callback_done)
            delete callback;
        else
            destroy_callback(callback);
    } else if (callback->once || callback_done) {
        /* After the handler, not in it: the handler mustn't free what its
         * caller is using. The key is the strdup()'ed message id. */
        gpointer key, value;
        if (g_hash_table_lookup_extended(callbacks, reply.correlationid().c_str(), &key, &value) &&
            value == callback) {
            g_hash_table_remove(callbacks, key);
            free(key);
            delete callback;
        }
    }
}

/* The handler stays registered unless it is one-shot, see ReplyCallback. */
void link_send_with_callback(int socket, const pm::PlatformMessage &msg, ReplyCallback *callback)
{
    register_callback(strdup(msg.id().c_str()), callback);
    link_send(socket, msg);
}

/* ---- receiving ---------------------------------------------------------- */

/*
 * Waits 1 ms for the socket; returns 1 if nothing came, 0 after
 * reading one message's bytes into `bytes` (parse_message() checks them).
 * An interrupted select() counts as nothing yet; a closed connection or a
 * failed read or select ends the application, which can do nothing without
 * the controller. The length is read byte by byte, so it may arrive in
 * pieces.
 */
static void connection_lost(const char *what)
{
    g_log(KVM_LOG, G_LOG_LEVEL_CRITICAL, "%s, the application ends", what);
    exit(0);
}

static int receive_frame(int socket, std::string *bytes)
{
    fd_set readable;
    struct timeval timeout;

    FD_ZERO(&readable);
    FD_SET(socket, &readable);
    timeout.tv_sec = 0;
    timeout.tv_usec = 1000;
    if (select(socket + 1, &readable, NULL, NULL, &timeout) < 0) {
        if (errno == EINTR)
            return 1;
        connection_lost("Failed to select socket");
    }
    if (!FD_ISSET(socket, &readable))
        return 1;

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "message received");
    unsigned char head[4];
    for (unsigned received = 0; received < 4;) {
        ssize_t n = recv(socket, head + received, 4 - received, 0);
        if (n < 1)
            connection_lost("Failed to receive bytes length from server");
        received += n;
    }
    uint32_t length = (uint32_t)head[0] << 24 | (uint32_t)head[1] << 16 | (uint32_t)head[2] << 8 | head[3];
    bytes->resize(length);
    for (uint32_t received = 0; received < length;) {
        ssize_t n = recv(socket, &(*bytes)[received], length - received, 0);
        if (n < 1)
            connection_lost("Failed to receive bytes from server");
        received += n;
    }
    return 0;
}

/*
 * What a message is, for the log, without its values (they can hold
 * credentials): its id and correlation id, and the names of the other
 * fields it has.
 */
static std::string message_trace(const pm::PlatformMessage &msg)
{
    std::vector<const google::protobuf::FieldDescriptor *> fields;
    std::string names;

    msg.GetReflection()->ListFields(msg, &fields);
    for (size_t i = 0; i < fields.size(); i++) {
        if (fields[i]->number() == pm::PlatformMessage::kIdFieldNumber ||
            fields[i]->number() == pm::PlatformMessage::kCorrelationIdFieldNumber)
            continue;
        names += (names.empty() ? "" : ", ") + fields[i]->name();
    }
    return "id \"" + msg.id() + "\", correlationId \"" + msg.correlationid() + "\", fields: " +
           (names.empty() ? std::string("none") : names);
}

/*
 * Parse a received message. One that doesn't parse (malformed bytes, or a
 * required field missing) is logged with what can trace it back, and
 * false is returned: it must not reach the reply handlers or listeners,
 * since what did parse is incomplete and missing fields read as their
 * defaults (poll_controller() gives a waiting reply handler ERROR in its
 * place). No answer is sent: the controller's messages aren't answered.
 */
static bool parse_message(const std::string &bytes, pm::PlatformMessage *msg)
{
    if (!msg->ParseFromString(bytes)) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "malformed message (%d bytes) not handled: %s; missing: %s",
              (int)bytes.size(), message_trace(*msg).c_str(), msg->InitializationErrorString().c_str());
        return false;
    }
    /* Social credentials stay out of the log. */
    if (!msg->has_social())
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Received MSG\n%s", msg->DebugString().c_str());
    return true;
}

static gboolean autokill(gpointer)
{
    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "interactive mode stopped, autokill");
    exit(0);
}

/*
 * An idle callback for as long as the program runs: each received
 * message goes to its reply handler, if it is a reply, and then, unless it
 * is about interactive mode, to the application's listeners. The
 * controller ending interactive mode schedules the end of the program.
 */
static gboolean poll_controller(gpointer data)
{
    KarotzData *karotz = static_cast<KarotzData *>(data);
    std::string bytes;
    pm::PlatformMessage msg;
    /* The handles the reply handlers and listeners make for this message
     * are freed when it is done (see karotzvm.h). */
    v8::HandleScope scope;

    if (receive_frame(karotz->socket, &bytes) != 0)
        return TRUE;
    bool parsed = parse_message(bytes, &msg);
    /* The reply handlers and listeners run in the application's context
     * (the one their functions were created in), entered here once for
     * the message. Reporting an exception they throw needs it too: reading
     * the exception's message (its source line and position) runs V8's own
     * JavaScript in the entered context, and crashes without one. */
    FreshContext context;
    if (!parsed) {
        /* A malformed reply to a request still waiting: its handler gets a
         * Response ERROR in its place, so the callback ends instead of
         * waiting. Nothing else reaches the handlers or listeners. */
        ReplyCallback *callback = msg.has_correlationid() ? find_callback(msg) : NULL;
        if (callback != NULL) {
            pm::PlatformMessage error;
            error.set_id(msg.has_id() ? msg.id() : "malformed");
            error.set_correlationid(msg.correlationid());
            if (msg.has_interactiveid())
                error.set_interactiveid(msg.interactiveid());
            error.mutable_response()->set_code(pm::ERROR);
            error.mutable_response()->set_description("malformed reply");
            g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "the malformed reply's handler gets ERROR");
            call_callback(callback, error);
        }
        return TRUE;
    }
    if (msg.has_correlationid()) {
        ReplyCallback *callback = find_callback(msg);
        if (callback == NULL)
            g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "NO CORRELATION FOUND");
        else
            call_callback(callback, msg);
    }
    if (!msg.has_interactivemode()) {
        karotz->on_message(msg);
    } else if (msg.interactivemode().action() == pm::InteractiveMode_Action_STOP) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "interactive mode stopped, autokill in %dms", AUTOKILL_AFTER_STOP_MS);
        g_timeout_add(AUTOKILL_AFTER_STOP_MS, autokill, NULL);
    }
    return TRUE;
}

/* The socket is closed when the connection fails. */
static int connect_controller(const char *host, int port, KarotzData *karotz)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "connecting to: %s:%d", host, port);
    struct hostent *entry = gethostbyname(host);
    if (entry == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "nslookup failed on '%s'", host);
        close(fd);
        return -1;
    }
    struct sockaddr_in address;
    memset(&address, 0, sizeof address);
    address.sin_family = AF_INET;
    memcpy(&address.sin_addr, entry->h_addr_list[0], 4);
    address.sin_port = htons(port);
    if (connect(fd, reinterpret_cast<struct sockaddr *>(&address), sizeof address) < 0) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "connect failed on '%s:%d': %s", host, port, strerror(errno));
        close(fd);
        return -1;
    }
    karotz->socket = fd;
    g_idle_add(poll_controller, karotz);
    return 0;
}

/*
 * karotz.connect(host, port). Quirk: an optional third argument is accepted
 * only when it is NOT an object: an object there is refused ("Wrong
 * arguments"), anything else is converted with ToObject() and ignored.
 * Nothing reads the third argument and what it is for is not known, so the
 * check is kept as it is until an application or a server shows its use.
 */
v8::Handle<v8::Value> js_karotz_connect(const v8::Arguments &args)
{
    if (args.Length() < 2 || !js_is_string(args[0]) || !args[1]->IsInt32())
        return js_throw("Wrong arguments");
    if (args.Length() == 3 && !args[2]->IsObject())
        args[2]->ToObject();
    else if (args.Length() != 2)
        return js_throw("Wrong arguments");

    KarotzData *karotz = current_karotz();
    v8::String::AsciiValue host(args[0]->ToString());
    int port = args[1]->Int32Value();
    if (connect_controller(*host, port, karotz) == 0)
        return v8::Undefined();
    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "connection to the controller failed");
    return js_throw("connection refused");
}

/* ---- interactive mode --------------------------------------------------- */

namespace {
/* InteractiveData: what karotz.start() keeps until the
 * controller answers. */
struct InteractiveData {
    InteractiveData(const char *id, KarotzData *karotz, v8::Handle<v8::Function> fn, v8::Handle<v8::Object> data)
        : function(v8::Persistent<v8::Function>::New(fn)), karotz(karotz), id(strdup(id))
    {
        if (!data.IsEmpty())
            this->data = v8::Persistent<v8::Object>::New(data);
    }
    ~InteractiveData()
    {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "clean InteractiveData");
        function.Dispose();
        function.Clear();
        if (!data.IsEmpty())
            data.Dispose();
        data.Clear();
        free(id);
    }

    v8::Persistent<v8::Function> function;
    v8::Persistent<v8::Object> data;
    KarotzData *karotz;
    char *id;
};
}

/* The start callback gets the data object given to karotz.start(),
 * if any. Unlike the other callbacks, no context is created around it. */
static void call_interactive_function(InteractiveData *interactive)
{
    if (!interactive->data.IsEmpty()) {
        v8::Handle<v8::Value> argv[1] = {interactive->data};
        interactive->function->Call(interactive->function, 1, argv);
    } else {
        interactive->function->Call(interactive->function, 0, NULL);
    }
}

/*
 * The controller's answer to karotz.start(). A refusal ends the
 * program; on success the application gets its interactive id and its
 * callback is called. The handler is registered one-shot, so a repeated
 * answer finds no correlation and the freed InteractiveData is not used.
 */
static void interactive_callback(void *data, const pm::PlatformMessage &reply)
{
    InteractiveData *interactive = static_cast<InteractiveData *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "InteractiveCallback...");
    if (reply.has_response()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Response...");
        if (reply.response().has_code()) {
            if (reply.response().code() != pm::OK) {
                g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Interactive mode is not valid: EXIT !");
                exit(1);
            }
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "KarotzData: %p", (void *)interactive->karotz);
            interactive->karotz->interactive_id = strdup(interactive->id);
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "calling interactiveMode function");
            call_interactive_function(interactive);
        }
    }
    delete interactive;
}

/* ReplyRelease for an InteractiveData that never got its answer. */
static void release_interactive(void *data)
{
    delete static_cast<InteractiveData *>(data);
}

/*
 * karotz.start(callback [, data]): asks the controller for
 * interactive mode under a new id. The old form karotz.start(callback, data,
 * id) passes the id, which is copied from its String::AsciiValue before
 * that is destroyed. The callback alone gets a new id too: an empty id is
 * the controller's "no application" state, so it would answer OK without
 * handing the rabbit over, and no event would reach the application.
 */
v8::Handle<v8::Value> js_karotz_start(const v8::Arguments &args)
{
    v8::Handle<v8::Object> data;
    char uuid[37];
    std::string legacy_id;
    const char *id = "";

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "starting interactiveMode");
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "check arguments");
    if (args.Length() < 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");
    if (args.Length() == 2 && args[1]->IsObject()) {
        data = args[1]->ToObject();
        make_uuid(uuid);
        id = uuid;
    } else if (args.Length() == 3 && args[1]->IsObject()) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "DEPRECATED karotz.start with an id: use karotz.start(callback, data)");
        data = args[1]->ToObject();
        v8::String::AsciiValue value(args[2]->ToString());
        legacy_id = *value;
        id = legacy_id.c_str();
    } else if (args.Length() != 1) {
        return js_throw("Wrong arguments");
    } else {
        make_uuid(uuid);
        id = uuid;
    }

    KarotzData *karotz = current_karotz();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "KarotzData: %p", (void *)karotz);
    if (karotz->socket == -1)
        return js_throw("Connect first");
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "starting interactiveMode: %s", id);
    InteractiveData *interactive =
        new InteractiveData(id, karotz, v8::Handle<v8::Function>::Cast(args[0]), data);
    pm::PlatformMessage *msg = link_new_message(NULL);
    pm::InteractiveMode *mode = msg->mutable_interactivemode();
    mode->set_interactiveid(id);
    mode->set_action(pm::InteractiveMode_Action_START);
    ReplyCallback *callback = new ReplyCallback(interactive_callback, interactive, release_interactive, true); /* one-shot */
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "function pointer: %p", (void *)interactive_callback);
    link_send_with_callback(karotz->socket, *msg, callback);
    delete msg;
    return v8::Undefined();
}

/* karotz.stop(): tells the controller interactive mode is over;
 * nothing changes locally. */
v8::Handle<v8::Value> js_karotz_stop(const v8::Arguments &args)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "stop interactiveMode");
    if (args.Length() != 0)
        return js_throw("Wrong arguments");
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "stop interactiveMode: %s", karotz->interactive_id);
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_interactivemode()->set_action(pm::InteractiveMode_Action_STOP);
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}
