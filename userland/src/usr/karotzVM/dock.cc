// SPDX-License-Identifier: MIT
/*
 * karotz.dock: the Karotz dock (the motorised base) and its LED.
 *
 *   karotz.dock.light(color [, callback])
 *   karotz.dock.fade(color, period [, callback])
 *   karotz.dock.pulse(color, period, pulse [, callback])
 *   karotz.dock.move(direction [, time [, callback]])
 *   karotz.dock.addListener(function (status, change) { ...; return true; })
 *
 * The LED functions are karotz.led's, but the Led goes inside a Dock
 * message. move sends Dock { move { cmd, time } } with direction "UP",
 * "DOWN", "RIGHT", "LEFT" or "STOP". Callbacks get the ReturnCode name of
 * the reply and of later events with the same correlationId.
 */
#include <string.h>

#include "karotzvm.h"

/* Sends msg, with event_reply(callback) as reply handler when there is a
 * callback, then deletes it (the tail every dock function shares). */
static void send_dock_message(KarotzData *karotz, pm::PlatformMessage *msg, NamedFunction *callback)
{
    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(event_reply, callback, release_named_function));
    delete msg;
}

/* ---- the dock's LED ------------------------------------------------------- */

/* light(color [, callback]). */
static v8::Handle<v8::Value> js_dock_light(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 2 && args[1]->IsFunction())
        callback = new NamedFunction("DOCKLED", v8::Handle<v8::Function>::Cast(args[1]), REPLY_END_FIRST);
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
    pm::Led_Light *light = msg->mutable_dock()->mutable_led()->mutable_light();
    light->set_color(*color);
    send_dock_message(karotz, msg, callback);
    return v8::Undefined();
}

/* fade(color, period [, callback]). */
static v8::Handle<v8::Value> js_dock_fade(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 3 && args[2]->IsFunction())
        callback = new NamedFunction("DOCKLED", v8::Handle<v8::Function>::Cast(args[2]), REPLY_END_FIRST);
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
    pm::Led_Fade *fade = msg->mutable_dock()->mutable_led()->mutable_fade();
    fade->set_color(*color);
    fade->set_period(period);
    send_dock_message(karotz, msg, callback);
    return v8::Undefined();
}

/* pulse(color, period, pulse [, callback]). */
static v8::Handle<v8::Value> js_dock_pulse(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 4 && args[3]->IsFunction())
        callback = new NamedFunction("DOCKLED", v8::Handle<v8::Function>::Cast(args[3]), REPLY_END_FIRST);
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
    pm::Led_Pulse *pulse = msg->mutable_dock()->mutable_led()->mutable_pulse();
    pulse->set_color(*color);
    pulse->set_period(period);
    pulse->set_pulse(pulse_time);
    send_dock_message(karotz, msg, callback);
    return v8::Undefined();
}

/* ---- move ------------------------------------------------------------------ */

/* move(direction [, time [, callback]]). The argument checks fall
 * through from the longest form to the shortest, collecting "wrong". */
static v8::Handle<v8::Value> js_dock_move(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;
    bool wrong = false;
    /* Quirk: with only the direction, the message still carries a time,
     * 0. The field is optional with default 0, so to the controller this is
     * the same as no time. No daemon here serves the dock, so what a time
     * of 0 means to one is not known. */
    uint32_t time = 0;
    pm::Dock_Type cmd = static_cast<pm::Dock_Type>(0);

    switch (args.Length()) {
    case 3:
        if (args[2]->IsFunction()) {
            /* The callback is the third argument (args[1] is the time). */
            callback = new NamedFunction("DOCKMOVE", v8::Handle<v8::Function>::Cast(args[2]), REPLY_END_FIRST);
        } else {
            wrong = true;
        }
        /* fall through */
    case 2:
        if (args[1]->IsNumber())
            time = args[1]->Uint32Value();
        else
            wrong = true;
        /* fall through */
    case 1:
        if (!js_is_string(args[0]))
            wrong = true;
        break;
    default:
        wrong = true;
        break;
    }
    if (wrong) {
        delete callback;
        return js_throw("Wrong arguments");
    }

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL) {
        delete callback;
        return v8::Undefined();  /* the exception is pending */
    }

    v8::String::AsciiValue direction_value(args[0]->ToString());
    const char *direction = *direction_value;
    if (strcmp(direction, "UP") == 0)
        cmd = pm::Dock_Type_UP;
    else if (strcmp(direction, "DOWN") == 0)
        cmd = pm::Dock_Type_DOWN;
    else if (strcmp(direction, "RIGHT") == 0)
        cmd = pm::Dock_Type_RIGHT;
    else if (strcmp(direction, "LEFT") == 0)
        cmd = pm::Dock_Type_LEFT;
    else if (strcmp(direction, "STOP") == 0)
        cmd = pm::Dock_Type_STOP;
    else {
        /* An unknown direction throws and sends nothing (the generated
         * setter would abort the VM on an invalid Dock_Type). */
        delete callback;
        return js_throw("Wrong 1st arguments");
    }

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Dock_Move *move = msg->mutable_dock()->mutable_move();
    move->set_cmd(cmd);
    move->set_time(time);
    send_dock_message(karotz, msg, callback);
    return v8::Undefined();
}

/* ---- the dock listener ------------------------------------------------------- */

/* DockMsgCallback: a listener(status, change) for Dock messages. */
class DockMsgCallback : public JsListener {
public:
    explicit DockMsgCallback(v8::Handle<v8::Function> fn) : JsListener(fn) {}

    /* Other messages keep the listener (true). The listener is
     * called with itself as receiver and no context of its own; it stays
     * registered only if it returns the boolean true. */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_dock())
            return true;

        v8::Handle<v8::Value> argv[2];
        argv[0] = v8::Integer::New(msg.dock().status());
        argv[1] = v8::Integer::New(msg.dock().change());
        v8::Handle<v8::Value> result = function->Call(function, 2, argv);
        /* A listener that threw (empty result) stays registered; the
         * exception is reported by call_listener(). */
        if (result.IsEmpty())
            return true;
        return result->IsBoolean() && result->BooleanValue();
    }
};

/* addListener(listener): listener(status, change) for each Dock message the controller sends
 * (the dock's status signal). */
static v8::Handle<v8::Value> js_dock_add_listener(const v8::Arguments &args)
{
    if (args.Length() != 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");

    /* No interactive-mode check here. */
    KarotzData *karotz = current_karotz();
    karotz->add_listener(new DockMsgCallback(v8::Handle<v8::Function>::Cast(args[0])));
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> dock_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "light", js_dock_light);
    js_set_function(tmpl, "fade", js_dock_fade);
    js_set_function(tmpl, "pulse", js_dock_pulse);
    js_set_function(tmpl, "move", js_dock_move);
    js_set_function(tmpl, "addListener", js_dock_add_listener);
    return tmpl;
}
