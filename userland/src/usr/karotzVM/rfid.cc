// SPDX-License-Identifier: MIT
/*
 * karotz.rfid: the RFID reader in the rabbit's nose.
 *
 *   karotz.rfid.addListener(function (tag) { ...; return true; });
 *
 * The listener is called for every rfidCallback message the controller sends
 * with an object describing the tag: always "id", then, when the message
 * carries them, "direction" ("IN"/"OUT"), "app" (number), "type" (the type
 * name, "FLAT", "NANOZTAG", "ZTAMPS", ...), "pict" (number), "color" (the
 * colour name, "RED", "BLUE", ...) and "content" (string). It keeps
 * listening only while it returns boolean true.
 */
#include "karotzvm.h"

namespace {

/* RfidMsgCallback. */
class RfidMsgCallback : public JsListener {
public:
    explicit RfidMsgCallback(v8::Handle<v8::Function> fn) : JsListener(fn) {}

    /* Messages without rfidCallback are ignored (the listener
     * stays). */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_rfidcallback())
            return true;
        FreshContext context;
        const pm::RfidCallback &rfid = msg.rfidcallback();
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "RFID: RfidMsgCallback");
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "RFID: RfidMsgCallback: %s", rfid.id().c_str());

        v8::Handle<v8::Object> tag = v8::Object::New();
        tag->Set(v8::String::New("id"), v8::String::New(rfid.id().c_str()));
        if (rfid.has_direction())
            tag->Set(v8::String::New("direction"),
                     v8::String::New(pm::RfidCallback_Dir_Name(rfid.direction()).c_str()));
        if (rfid.has_app())
            tag->Set(v8::String::New("app"), v8::Number::New(rfid.app()));
        if (rfid.has_type())
            tag->Set(v8::String::New("type"),
                     v8::String::New(pm::RfidCallback_Type_Name(rfid.type()).c_str()));
        if (rfid.has_pict())
            tag->Set(v8::String::New("pict"), v8::Number::New(rfid.pict()));
        if (rfid.has_color())
            tag->Set(v8::String::New("color"),
                     v8::String::New(pm::RfidCallback_Col_Name(rfid.color()).c_str()));
        /* Quirk: content is a bytes field but is passed through c_str(), so
         * it stops at the first NUL byte (and is decoded as UTF-8). The
         * controller fills it from rfid-daemon's D-Bus string of hex
         * blocks, which can't hold a NUL. */
        if (rfid.has_content())
            tag->Set(v8::String::New("content"), v8::String::New(rfid.content().c_str()));

        v8::Handle<v8::Value> argv[1] = { tag };
        v8::Handle<v8::Value> result = function->Call(function, 1, argv);
        /* A listener that threw (empty result) stays registered; the
         * exception is reported by call_listener(). */
        if (result.IsEmpty())
            return true;
        return result->IsBoolean() && result->BooleanValue();
    }
};

} // namespace

/* addListener(function). */
static v8::Handle<v8::Value> js_rfid_add_listener(const v8::Arguments &args)
{
    if (args.Length() != 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");
    KarotzData *karotz = current_karotz();
    karotz->add_listener(new RfidMsgCallback(v8::Handle<v8::Function>::Cast(args[0])));
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> rfid_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "addListener", js_rfid_add_listener);
    return tmpl;
}
