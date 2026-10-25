// SPDX-License-Identifier: MIT
/*
 * karotz.info and ping (karotz.ping and the global ping).
 *
 *   karotz.info(function (info) { ... });   info: { <key>: "<value>", ... }
 *   ping();  ping(function (code) { ... });
 *
 * info() sends a PlatformMessage with an empty karotzinfo_query; the reply's
 * karotzinfo_response infos (key/value pairs) become the properties of the
 * object handed to the callback. ping() sends an empty ping; with a
 * callback, the reply's Response code and Event code are passed to it as
 * strings ("OK", ...).
 */
#include "karotzvm.h"

/* InfoKarotzCallbackFunction: calls the stored function with an
 * object holding every key/value of the reply's karotzinfo_response, then
 * frees it. Without a karotzinfo_response nothing is called and the
 * function is kept. */
static void info_reply(void *data, const pm::PlatformMessage &reply)
{
    StoredFunction *callback = static_cast<StoredFunction *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "InfoKarotzCallbackFunction");
    if (!reply.has_karotzinfo_response()) {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "InfoKarotzCallbackFunction: has_karotzinfo_response false");
        return;
    }

    FreshContext context;

    v8::Handle<v8::Object> info = v8::Object::New();
    const pm::KarotzInfoResponse &response = reply.karotzinfo_response();
    int count = response.infos_size();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "InfoKarotzCallbackFunction has_karotzinfo size: %d", count);
    for (int i = 0; i < count; i++) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "karotzinfo key: %s", response.infos(i).key().c_str());
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "karotzinfo value: %s", response.infos(i).value().c_str());
        info->Set(v8::String::New(response.infos(i).key().c_str()), v8::String::New(response.infos(i).value().c_str()));
    }

    /* The receiver is the function itself. */
    v8::Handle<v8::Value> argv[1] = { info };
    (*callback)->Call(*callback, 1, argv);
    release_function(callback);
    /* The handler is removed with the function, so a repeated reply finds
     * nothing. */
    reply_done();
}

/* karotz.info(callback). */
v8::Handle<v8::Value> js_karotz_info(const v8::Arguments &args)
{
    if (!(args.Length() == 1 && args[0]->IsFunction()))
        return js_throw("Wrong arguments");

    /* The reply handler gets a plain stored function. */
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ConfigKarotzGet");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    StoredFunction *callback = store_function(v8::Handle<v8::Function>::Cast(args[0]));
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_karotzinfo_query();
    link_send_with_callback(karotz->socket, *msg, new ReplyCallback(info_reply, callback, release_stored_function));
    delete msg;
    return v8::Undefined();
}

/* ping([callback]) (karotz.ping and the global ping). */
v8::Handle<v8::Value> js_ping(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 1 && args[0]->IsFunction()) {
        /* Made before the interactive mode check. */
        callback = new NamedFunction("PingFunction", v8::Handle<v8::Function>::Cast(args[0]), REPLY_END_FIRST);
    } else if (args.Length() != 0) {
        return js_throw("Wrong arguments");
    }

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL) {
        delete callback;
        return v8::Undefined();  /* interactive_karotz() has thrown */
    }

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "PingFunction");
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_ping();
    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(event_reply, callback, release_named_function));
    delete msg;
    return v8::Undefined();
}
