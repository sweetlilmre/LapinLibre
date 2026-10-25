// SPDX-License-Identifier: MIT
/*
 * karotz.k2k: Karotz-to-Karotz voice calls (SIP).
 *
 *   karotz.k2k.account(sipId, sipDomain, sipPassword, stunServer);
 *   karotz.k2k.call(address);
 *   karotz.k2k.answer(accept);
 *   karotz.k2k.hangup();
 *   karotz.k2k.addListener(function (event) { ... return true; });
 *
 * call/answer/hangup/account each send a PlatformMessage with the matching k2k
 * sub-message, without a reply handler. addListener registers a listener
 * that sees every k2kCallback the controller sends and calls the function
 * with { type: "INCOMING" | "BEGINNING" | "TERMINATED", correspondant }; the
 * listener stays registered only while the function returns true.
 */
#include "karotzvm.h"

/* K2kMsgCallback: a listener for the k2kCallback messages of a call. */
class K2kMsgCallback : public JsListener {
public:
    explicit K2kMsgCallback(v8::Handle<v8::Function> fn) : JsListener(fn) {}

    /* Messages without a k2kCallback are ignored (the listener
     * stays). Otherwise the function is called with {type, correspondant}
     * and the listener stays only if it returned the boolean true. */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_k2kcallback())
            return true;

        FreshContext context;
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "K2k: K2kMsgCallback");

        const pm::K2kCallback &event = msg.k2kcallback();
        v8::Handle<v8::Object> object = v8::Object::New();
        object->Set(v8::String::New("type"), v8::String::New(pm::K2kCallback_Type_Name(event.type()).c_str()));
        object->Set(v8::String::New("correspondant"), v8::String::New(event.correspondant().c_str()));

        /* The receiver is the function itself. */
        v8::Handle<v8::Value> argv[1] = { object };
        v8::Handle<v8::Value> result = function->Call(function, 1, argv);
        /* A listener that threw (empty result) stays registered; the
         * exception is reported by call_listener(). */
        if (result.IsEmpty())
            return true;
        return result->IsBoolean() && result->BooleanValue();
    }
};

/* karotz.k2k.addListener(fn). No interactive-mode check, as in every
 * addListener. */
static v8::Handle<v8::Value> js_k2k_add_listener(const v8::Arguments &args)
{
    if (!(args.Length() == 1 && args[0]->IsFunction()))
        return js_throw("Wrong arguments");

    KarotzData *karotz = current_karotz();
    karotz->add_listener(new K2kMsgCallback(v8::Handle<v8::Function>::Cast(args[0])));
    return v8::Undefined();
}

/* karotz.k2k.account(sipId, sipDomain, sipPassword, stunServer). */
static v8::Handle<v8::Value> js_k2k_account(const v8::Arguments &args)
{
    if (args.Length() != 4)
        return js_throw("Wrong arguments");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::K2k_Account *account = msg->mutable_k2k()->mutable_account();
    v8::String::Utf8Value sip_id(args[0]->ToString());
    v8::String::Utf8Value sip_domain(args[1]->ToString());
    v8::String::Utf8Value sip_password(args[2]->ToString());
    v8::String::Utf8Value stun_server(args[3]->ToString());
    account->set_sipid(*sip_id);
    account->set_sipdomain(*sip_domain);
    account->set_sippassword(*sip_password);
    account->set_stunserver(*stun_server);
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

/* karotz.k2k.hangup(). */
static v8::Handle<v8::Value> js_k2k_hangup(const v8::Arguments &args)
{
    if (args.Length() != 0)
        return js_throw("Wrong arguments");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_k2k()->mutable_hangup();
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

/* karotz.k2k.answer(accept); accept goes through ToBoolean. */
static v8::Handle<v8::Value> js_k2k_answer(const v8::Arguments &args)
{
    if (args.Length() != 1)
        return js_throw("Wrong arguments");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::K2k_Answer *answer = msg->mutable_k2k()->mutable_answer();
    answer->set_accept(args[0]->ToBoolean()->Value());
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

/* karotz.k2k.call(address). */
static v8::Handle<v8::Value> js_k2k_call(const v8::Arguments &args)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "call");
    if (args.Length() != 1)
        return js_throw("Wrong arguments");

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* interactive_karotz() has thrown */

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::K2k_Call *call = msg->mutable_k2k()->mutable_call();
    v8::String::Utf8Value address(args[0]->ToString());
    call->set_address(*address);
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> k2k_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "call", js_k2k_call);
    js_set_function(tmpl, "answer", js_k2k_answer);
    js_set_function(tmpl, "hangup", js_k2k_hangup);
    js_set_function(tmpl, "account", js_k2k_account);
    js_set_function(tmpl, "addListener", js_k2k_add_listener);
    return tmpl;
}
