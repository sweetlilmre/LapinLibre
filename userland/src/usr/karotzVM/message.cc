// SPDX-License-Identifier: MIT
/*
 * karotz.message: the rabbit's message box (messages between Karotz owners).
 *
 *   karotz.message.list(function (messages) { ... });
 *   karotz.message.get({a: "<message id>", ...}, function (messages) { ... });
 *   karotz.message.delete({...ids...}, callback);
 *   karotz.message.markAsRead({...ids...}, callback);
 *   karotz.message["new"]({uuid: "<contact uuid>", objects: ["<idDB>", ...]},
 *                         "text" [, function]);
 *
 * Each call sends a PlatformMessage with a karotzMessageQuery (action LIST, GET,
 * DELETE, READ or NEW). For get/delete/markAsRead the ids are the values of
 * the first argument's own enumerable properties. The reply's
 * karotzMessageResponse becomes an Array of
 *   { header: { id, date, status, from, to }, content: { type, content, header } }
 * handed to the callback (from/to are contact objects, see contact.cc).
 * new() sends without waiting for a reply and logs the message at debug level.
 */
#include <string>

#include "karotzvm.h"

/* MessageCallbackFunction: the reply to list/get/delete/
 * markAsRead. Logs the Response code, then calls the stored function with
 * the Array of messages and frees it. */
static void message_reply(void *data, const pm::PlatformMessage &reply)
{
    StoredFunction *callback = static_cast<StoredFunction *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "MessageCallbackFunction");
    if (!reply.has_response()) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "MessageCallbackFunction: has_response false");
    } else if (reply.response().has_code()) {
        if (reply.response().code() == pm::OK)
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "MessageCallbackFunction: response: OK");
        else if (reply.response().code() == pm::ERROR)
            g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "MessageCallbackFunction: response: ERROR");
    }

    /* Quirk: the log text describes "no karotzMessageResponse, or an empty
     * list", but the test is &&: only a reply WITHOUT a karotzMessageResponse
     * is refused (a missing response's list is always empty), and a
     * karotzMessageResponse with an empty list calls back with an empty
     * Array. A refused reply keeps the function registered, so a later reply
     * with the same correlationId still reaches it. The controller sends
     * such a reply for a query from an application that is not the
     * interactive one (a Response ERROR, while the query still goes on to
     * the platform). Whether && or || was meant, and what the platform
     * itself replied, is not known; both are kept as they are. */
    if (!reply.has_karotzmessageresponse() && reply.karotzmessageresponse().karotzmessagelist_size() == 0) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING,
              "MessageCallbackFunction: has_karotzmessageresponse false. abort");
        return;
    }

    FreshContext context;

    const pm::KarotzMessageResponse &response = reply.karotzmessageresponse();
    v8::Handle<v8::Array> messages = v8::Array::New(response.karotzmessagelist_size());
    for (int i = 0; i < response.karotzmessagelist_size(); i++) {
        v8::Handle<v8::Object> entry = v8::Object::New();
        v8::Handle<v8::Object> header = v8::Object::New();
        v8::Handle<v8::Object> content = v8::Object::New();
        /* Works on a copy of the message. */
        pm::KarotzMessage message(response.karotzmessagelist(i));

        if (message.has_header()) {
            const pm::KarotzMessageHeader &h = message.header();
            header->Set(v8::String::New("id"), v8::String::New(h.id().c_str()));
            header->Set(v8::String::New("date"), v8::Number::New((double)h.date()));
            header->Set(v8::String::New("status"), v8::Number::New((double)(unsigned)h.status()));
            if (h.has_from())
                header->Set(v8::String::New("from"), contact_to_js(h.from()));
            if (h.has_to())
                header->Set(v8::String::New("to"), contact_to_js(h.to()));
        }
        if (message.has_content()) {
            const pm::KarotzMessageContent &c = message.content();
            content->Set(v8::String::New("type"), v8::String::New(pm::KarotzMessageContent_Type_Name(c.type()).c_str()));
            content->Set(v8::String::New("content"), v8::String::New(c.content().c_str()));
            if (c.has_header())
                content->Set(v8::String::New("header"), v8::String::New(c.header().c_str()));
        }
        entry->Set(v8::String::New("header"), header);
        entry->Set(v8::String::New("content"), content);
        messages->Set(v8::Number::New((double)i), entry);
    }

    /* The receiver is the function itself. */
    v8::Handle<v8::Value> argv[1] = { messages };
    (*callback)->Call(*callback, 1, argv);
    release_function(callback);
    /* The handler is removed with the function, so a repeated reply finds
     * nothing. */
    reply_done();
}

/* karotz.message.list(callback). */
static v8::Handle<v8::Value> js_message_list(const v8::Arguments &args)
{
    /* The first argument must be the callback (a non-function would be
     * called as one when the reply arrives). Quirk: arguments after the
     * callback are accepted and ignored, list(fn, extra) included, as they
     * always have been; whether that was meant is not known. */
    if (args.Length() < 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::KarotzMessageQuery *query = msg->mutable_karotzmessagequery();
    query->set_action(pm::KarotzMessageQuery_TypeAction_LIST);
    StoredFunction *callback = store_function(v8::Handle<v8::Function>::Cast(args[0]));
    link_send_with_callback(karotz->socket, *msg, new ReplyCallback(message_reply, callback, release_stored_function));
    delete msg;
    return v8::Undefined();
}

/* get, delete and markAsRead: the three are the same code with a different
 * action. args: (object of ids, callback); the ids are the values
 * (ToString) of the object's enumerable properties. */
static v8::Handle<v8::Value> message_query_ids(const v8::Arguments &args, pm::KarotzMessageQuery_TypeAction action)
{
    if (!(args.Length() == 2 && args[1]->IsFunction() && args[0]->IsObject())) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "Wrong arguments");
        return js_throw("Wrong arguments");
    }

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    v8::Local<v8::Array> keys = args[0]->ToObject()->GetPropertyNames();
    StoredFunction *callback = store_function(v8::Handle<v8::Function>::Cast(args[1]));
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::KarotzMessageQuery *query = msg->mutable_karotzmessagequery();
    query->set_action(action);
    for (unsigned i = 0; i < keys->Length(); i++) {
        v8::Local<v8::Value> key = keys->Get(v8::Integer::New(i));
        if (key.IsEmpty())
            continue;
        v8::String::AsciiValue id(args[0]->ToObject()->Get(key)->ToString());
        query->add_id(*id);
    }
    link_send_with_callback(karotz->socket, *msg, new ReplyCallback(message_reply, callback, release_stored_function));
    delete msg;
    return v8::Undefined();
}

/* karotz.message.get(ids, callback). */
static v8::Handle<v8::Value> js_message_get(const v8::Arguments &args)
{
    return message_query_ids(args, pm::KarotzMessageQuery_TypeAction_GET);
}

/* karotz.message.delete(ids, callback). */
static v8::Handle<v8::Value> js_message_delete(const v8::Arguments &args)
{
    return message_query_ids(args, pm::KarotzMessageQuery_TypeAction_DELETE);
}

/* karotz.message.markAsRead(ids, callback). */
static v8::Handle<v8::Value> js_message_mark_as_read(const v8::Arguments &args)
{
    return message_query_ids(args, pm::KarotzMessageQuery_TypeAction_READ);
}

/* karotz.message.new(to, text [, callback]). to is
 * { uuid: "<recipient>", objects: ["<idDB>", ...] }. With a callback, the
 * reply's ReturnCode name ("OK", "ERROR") reaches it, once. */
static v8::Handle<v8::Value> js_message_new(const v8::Arguments &args)
{
    if (args.Length() == 3 && !args[2]->IsFunction())
        return js_throw("Wrong arguments");
    if (args.Length() < 2)
        return js_throw("Wrong arguments");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::KarotzMessageQuery *query = msg->mutable_karotzmessagequery();
    query->set_action(pm::KarotzMessageQuery_TypeAction_NEW);
    pm::KarotzMessage *message = query->mutable_message();
    pm::Contact *to = message->mutable_header()->mutable_to();

    /* msg is freed on the two argument errors below. */
    if (!args[0]->IsObject()) {
        delete msg;
        return js_throw("Arg[0] Must be object");
    }

    v8::Local<v8::Object> recipient = args[0]->ToObject();
    v8::String::AsciiValue uuid(recipient->Get(v8::String::New("uuid"))->ToString());
    to->set_uuid(*uuid);

    /* A recipient without an "objects" Array throws. Each element goes
     * through AsciiValue (ToString). */
    v8::Local<v8::Value> objects_value = recipient->Get(v8::String::New("objects"));
    if (!objects_value->IsArray()) {
        delete msg;
        return js_throw("Arg[0].objects Must be array");
    }
    v8::Handle<v8::Array> objects = v8::Handle<v8::Array>::Cast(objects_value);
    for (unsigned i = 0; i < objects->Length(); i++) {
        v8::Local<v8::Value> object = objects->Get(v8::Integer::New(i));
        pm::Contact_Object *contact_object = to->add_objects();
        v8::String::AsciiValue iddb(object);
        contact_object->set_iddb(*iddb);
    }

    /* The message text is the second argument. */
    if (!js_is_string(args[1])) {
        delete msg;
        return js_throw("Arg[1] Must be string");
    }

    pm::KarotzMessageContent *content = message->mutable_content();
    content->set_type(pm::KarotzMessageContent_Type_TEXT);
    v8::String::AsciiValue text(args[1]->ToString());
    content->set_content(*text);

    if (args.Length() == 3)
        link_send_with_callback(karotz->socket, *msg,
                                new ReplyCallback(event_reply,
                                                  new NamedFunction("MESSAGE", v8::Handle<v8::Function>::Cast(args[2]),
                                                                    REPLY_END_FIRST),
                                                  release_named_function));
    else
        link_send(karotz->socket, *msg);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "message: %s", msg->ShortDebugString().c_str());
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> message_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "list", js_message_list);
    js_set_function(tmpl, "get", js_message_get);
    js_set_function(tmpl, "delete", js_message_delete);
    js_set_function(tmpl, "new", js_message_new);
    js_set_function(tmpl, "markAsRead", js_message_mark_as_read);
    return tmpl;
}
