// SPDX-License-Identifier: MIT
/*
 * karotz.button: the rabbit's head button.
 *
 *   karotz.button.addListener(function (type) { ...; return true; });
 *
 * The listener is called for every buttonCallback message the controller
 * sends with the name of the press type ("SIMPLE", "DOUBLE", "TRIPLE",
 * "MULTIPLE", "LONG_START", "LONG_STOP"). It keeps listening only while it
 * returns boolean true; any other return value removes it.
 */
#include "karotzvm.h"

namespace {

/* ButtonMsgCallback. */
class ButtonMsgCallback : public JsListener {
public:
    explicit ButtonMsgCallback(v8::Handle<v8::Function> fn) : JsListener(fn) {}

    /* Messages without buttonCallback are ignored (the listener
     * stays). The function is called directly, with the function itself as
     * receiver (poll_controller() has opened the HandleScope and entered
     * the application's context). A listener that throws stays registered;
     * the exception is reported by call_listener(). */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_buttoncallback())
            return true;
        v8::Handle<v8::Value> argv[1];
        argv[0] = v8::String::New(pm::ButtonCallback_Type_Name(msg.buttoncallback().type()).c_str());
        v8::Handle<v8::Value> result = function->Call(function, 1, argv);
        if (result.IsEmpty())
            return true;
        return result->IsBoolean() && result->BooleanValue();
    }
};

} // namespace

/* addListener(function). */
static v8::Handle<v8::Value> js_button_add_listener(const v8::Arguments &args)
{
    if (args.Length() != 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");
    KarotzData *karotz = current_karotz();
    karotz->add_listener(new ButtonMsgCallback(v8::Handle<v8::Function>::Cast(args[0])));
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> button_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "addListener", js_button_add_listener);
    return tmpl;
}
