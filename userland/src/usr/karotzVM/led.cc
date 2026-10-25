// SPDX-License-Identifier: MIT
/*
 * karotz.led: the rabbit's belly LED.
 *
 *   karotz.led.light(color [, callback])
 *   karotz.led.fade(color, period [, callback])
 *   karotz.led.pulse(color, period, pulse [, callback])
 *
 * color is an "RRGGBB" string, period and pulse are milliseconds. Each call
 * sends a PlatformMessage carrying a Led (light / fade / pulse); with a callback the
 * controller's reply (and later events with the same correlationId) calls
 * callback(codeName), codeName being the ReturnCode name ("OK",
 * "TERMINATED", ...).
 */
#include "karotzvm.h"

/* Sends msg, with event_reply(callback) as reply handler when there is a
 * callback, then deletes it (the tail every LED function shares). */
static void send_led_message(KarotzData *karotz, pm::PlatformMessage *msg, NamedFunction *callback)
{
    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(event_reply, callback, release_named_function));
    delete msg;
}

/* light(color [, callback]). */
static v8::Handle<v8::Value> js_led_light(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 2 && args[1]->IsFunction())
        /* led-daemon sends the Event after a Response ERROR too (a bad
         * colour), so only the Event is the last reply. */
        callback = new NamedFunction("LED", v8::Handle<v8::Function>::Cast(args[1]), REPLY_END_EVENT);
    else if (args.Length() != 1)
        return js_throw("Wrong arguments");

    /* The callback's NamedFunction is freed when this throws. */
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL) {
        delete callback;
        return v8::Undefined();  /* the exception is pending */
    }

    v8::String::AsciiValue color(args[0]->ToString());
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "LED Light: color[%s]", *color);

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Led_Light *light = msg->mutable_led()->mutable_light();
    light->set_color(*color);
    send_led_message(karotz, msg, callback);
    return v8::Undefined();
}

/* fade(color, period [, callback]). */
static v8::Handle<v8::Value> js_led_fade(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 3 && args[2]->IsFunction())
        callback = new NamedFunction("LED", v8::Handle<v8::Function>::Cast(args[2]), REPLY_END_EVENT_OR_ERROR);
    else if (args.Length() != 2)
        return js_throw("Wrong arguments");

    /* The callback's NamedFunction is freed when this throws. */
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL) {
        delete callback;
        return v8::Undefined();
    }

    v8::String::AsciiValue color(args[0]->ToString());
    uint32_t period = args[1]->Uint32Value();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "LED Fade: color[%s] period[%d]", *color, period);

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Led_Fade *fade = msg->mutable_led()->mutable_fade();
    fade->set_color(*color);
    fade->set_period(period);
    send_led_message(karotz, msg, callback);
    return v8::Undefined();
}

/* pulse(color, period, pulse [, callback]). */
static v8::Handle<v8::Value> js_led_pulse(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 4 && args[3]->IsFunction())
        callback = new NamedFunction("LED", v8::Handle<v8::Function>::Cast(args[3]), REPLY_END_EVENT_OR_ERROR);
    else if (args.Length() != 3)
        return js_throw("Wrong arguments");

    /* The callback's NamedFunction is freed when this throws. */
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL) {
        delete callback;
        return v8::Undefined();
    }

    v8::String::AsciiValue color(args[0]->ToString());
    uint32_t period = args[1]->Uint32Value();
    uint32_t pulse_time = args[2]->Uint32Value();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "LED Pulse: color[%s] period[%d] pulse[%d]", *color, period,
          pulse_time);

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Led_Pulse *pulse = msg->mutable_led()->mutable_pulse();
    pulse->set_color(*color);
    pulse->set_period(period);
    pulse->set_pulse(pulse_time);
    send_led_message(karotz, msg, callback);
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> led_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "light", js_led_light);
    js_set_function(tmpl, "fade", js_led_fade);
    js_set_function(tmpl, "pulse", js_led_pulse);
    return tmpl;
}
