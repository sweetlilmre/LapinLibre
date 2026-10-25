// SPDX-License-Identifier: MIT
/*
 * karotz.contact: the owner's contact list.
 *
 *   karotz.contact.get(function (contacts) { ... });
 *
 * get() sends a PlatformMessage carrying an empty getcontactList; the controller's
 * reply carries a contactList, which the callback receives as an Array of
 * contact objects:
 *   { firstname, lastname, alias, uuid,  (each only when set)
 *     objects: [ { id, name }, ... ] }
 *
 * The contact -> JavaScript converter is also used by
 * karotz.message for the "from" and "to" of a message header, so it lives
 * here and is not static (message.cc declares it extern).
 */
#include "karotzvm.h"

/* A Contact as a JavaScript object, properties in this order:
 * firstname, lastname, alias, uuid (each only when present), then objects
 * (always, an Array of { id, name }). */
v8::Handle<v8::Value> contact_to_js(const pm::Contact &contact)
{
    v8::Handle<v8::Object> result = v8::Object::New();

    if (contact.has_firstname())
        result->Set(v8::String::New("firstname"), v8::String::New(contact.firstname().c_str()));
    if (contact.has_lastname())
        result->Set(v8::String::New("lastname"), v8::String::New(contact.lastname().c_str()));
    if (contact.has_alias())
        result->Set(v8::String::New("alias"), v8::String::New(contact.alias().c_str()));
    if (contact.has_uuid())
        result->Set(v8::String::New("uuid"), v8::String::New(contact.uuid().c_str()));

    v8::Handle<v8::Array> objects = v8::Array::New(contact.objects_size());
    for (unsigned i = 0; i < (unsigned)contact.objects_size(); i++) {
        v8::Handle<v8::Object> object = v8::Object::New();
        /* The proto field is idDB, the JavaScript property "id". */
        object->Set(v8::String::New("id"), v8::String::New(contact.objects(i).iddb().c_str()));
        /* "name" is set only on objects that have one. */
        if (contact.objects(i).has_name())
            object->Set(v8::String::New("name"), v8::String::New(contact.objects(i).name().c_str()));
        objects->Set(v8::Number::New((double)i), object);
    }
    result->Set(v8::String::New("objects"), objects);
    return result;
}

/* getContactCallbackFunction: calls the stored function with the
 * Array of contacts (empty when the reply has no contactList), then frees
 * it. */
static void contact_get_reply(void *data, const pm::PlatformMessage &reply)
{
    StoredFunction *callback = static_cast<StoredFunction *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "getContactCallbackFunction");
    FreshContext context;

    v8::Handle<v8::Array> contacts;
    if (!reply.has_contactlist()) {
        contacts = v8::Array::New(0);
    } else {
        const pm::ContactList &list = reply.contactlist();
        contacts = v8::Array::New(list.contacts_size());
        for (int i = 0; i < list.contacts_size(); i++)
            contacts->Set(v8::Number::New((double)i), contact_to_js(list.contacts(i)));
    }

    /* The receiver is the function itself. */
    v8::Handle<v8::Value> argv[1] = { contacts };
    (*callback)->Call(*callback, 1, argv);
    release_function(callback);
    /* The handler is removed with the function, so a repeated reply finds
     * nothing. */
    reply_done();
}

/* karotz.contact.get(callback). */
static v8::Handle<v8::Value> js_contact_get(const v8::Arguments &args)
{
    if (!(args.Length() == 1 && args[0]->IsFunction()))
        return js_throw("Wrong arguments");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    StoredFunction *callback = store_function(v8::Handle<v8::Function>::Cast(args[0]));
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_getcontactlist();
    link_send_with_callback(karotz->socket, *msg, new ReplyCallback(contact_get_reply, callback, release_stored_function));
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> contact_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "get", js_contact_get);
    return tmpl;
}
