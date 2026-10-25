// SPDX-License-Identifier: MIT
/*
 * karotz.ears: the rabbit's two ears.
 *
 *   karotz.ears.move(left, right [, callback])
 *   karotz.ears.moveRelative(left, right [, callback])
 *   karotz.ears.reset([callback])
 *   karotz.ears.resetRight([callback])
 *   karotz.ears.resetLeft([callback])
 *   karotz.ears.addListener(listener)
 *
 * move/moveRelative send an Ears message with the positions (a null
 * position leaves that ear alone; relative = true for moveRelative); their
 * callback gets the ReturnCode name of the reply and of later events.
 * The resets send Ears { reset = true, right/left = 1 }; their callback gets
 * (codeName, left, right). addListener registers a function called for
 * every EarsCallback event (ear moved by hand, blocked, ...) as
 * listener(typeName, step, length); it stays registered only while it
 * returns true.
 */
#include "karotzvm.h"

/* ---- move / moveRelative ------------------------------------------------ */

/* move (relative = false) and moveRelative (relative = true) are
 * identical but for set_relative. */
static v8::Handle<v8::Value> ears_move(const v8::Arguments &args, bool relative)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 3 && args[2]->IsFunction())
        callback = new NamedFunction("EARS", v8::Handle<v8::Function>::Cast(args[2]));
    else if (args.Length() != 2)
        return js_throw("Wrong arguments");

    /* The callback's NamedFunction is freed when this throws. */
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL) {
        delete callback;
        return v8::Undefined();  /* the exception is pending */
    }

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Ears *ears = msg->mutable_ears();
    /* null leaves an ear where it is; anything else (undefined included,
     * which gives 0) is converted with Int32Value. */
    if (!args[0]->IsNull())
        ears->set_left(args[0]->Int32Value());
    if (!args[1]->IsNull())
        ears->set_right(args[1]->Int32Value());
    if (relative)
        ears->set_relative(true);

    /* A move gets one Event when it ends (done, cut short or blocked), or
     * only a Response ERROR. Not a move of both ears: when both are blocked
     * at once ears-daemon sends CANCELLED twice, so it is kept. */
    if (callback != NULL && !(ears->has_left() && ears->has_right()))
        callback->end = REPLY_END_EVENT_OR_ERROR;

    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(event_reply, callback, release_named_function));
    delete msg;
    return v8::Undefined();
}

static v8::Handle<v8::Value> js_ears_move(const v8::Arguments &args)
{
    return ears_move(args, false);
}

static v8::Handle<v8::Value> js_ears_move_relative(const v8::Arguments &args)
{
    return ears_move(args, true);
}

/* ---- reset --------------------------------------------------------------- */

/* Reply handler of the resets: callback(codeName, left, right).
 * codeName is the Event's code when the reply has an Event, else the
 * Response's, else ""; left/right come from the reply's Ears (0 when
 * absent). The function is called with a fresh context entered. The
 * StoredFunction is never released (later replies call it again). */
static void ears_reset_reply(void *data, const pm::PlatformMessage &reply)
{
    StoredFunction *callback = static_cast<StoredFunction *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "EarsResetCallback");

    v8::Handle<v8::Value> argv[3];
    if (reply.has_event())
        argv[0] = v8::String::New(pm::ReturnCode_Name(reply.event().code()).c_str());
    else if (reply.has_response())
        argv[0] = v8::String::New(pm::ReturnCode_Name(reply.response().code()).c_str());
    else
        argv[0] = v8::String::New("");

    if (reply.has_ears()) {
        const pm::Ears &ears = reply.ears();
        argv[1] = v8::Number::New(ears.has_left() ? static_cast<double>(ears.left()) : 0.0);
        argv[2] = v8::Number::New(ears.has_right() ? static_cast<double>(ears.right()) : 0.0);
    } else {
        argv[1] = v8::Number::New(0.0);
        argv[2] = v8::Number::New(0.0);
    }
    js_call(*callback, 3, argv);
    /* The function is freed after the last reply. A reset ends with one
     * Event (TERMINATED with the positions, or CANCELLED by a newer
     * request), or gets only a Response ERROR. */
    if (reply_is_last(REPLY_END_EVENT_OR_ERROR, reply)) {
        release_function(callback);
        reply_done();
    }
}

/* reset / resetRight / resetLeft ([callback]). which is 'B'
 * (both), 'R' or 'L'. Unlike the other functions the interactive check
 * comes before the argument check. */
static v8::Handle<v8::Value> ears_reset(const v8::Arguments &args, char which)
{
    StoredFunction *callback = NULL;

    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();

    switch (args.Length()) {
    case 0:
        break;
    case 1:
        if (!args[0]->IsFunction())
            return js_throw("Wrong arguments");
        callback = store_function(v8::Handle<v8::Function>::Cast(args[0]));
        break;
    default:
        return js_throw("Wrong arguments");
    }

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Ears *ears = msg->mutable_ears();
    ears->set_reset(true);
    if (which == 'R') {
        ears->set_right(1);
    } else if (which == 'L') {
        ears->set_left(1);
    } else {
        ears->set_right(1);
        ears->set_left(1);
    }

    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(ears_reset_reply, callback, release_stored_function));
    delete msg;
    return v8::Undefined();
}

static v8::Handle<v8::Value> js_ears_reset(const v8::Arguments &args)
{
    return ears_reset(args, 'B');
}

static v8::Handle<v8::Value> js_ears_reset_right(const v8::Arguments &args)
{
    return ears_reset(args, 'R');
}

static v8::Handle<v8::Value> js_ears_reset_left(const v8::Arguments &args)
{
    return ears_reset(args, 'L');
}

/* ---- addListener ---------------------------------------------------------- */

/* EarsMsgCallback: a listener(typeName, step, length) for EarsCallback
 * events. */
class EarsMsgCallback : public JsListener {
public:
    explicit EarsMsgCallback(v8::Handle<v8::Function> fn) : JsListener(fn) {}

    /* Other messages keep the listener (true). The listener is
     * called with itself as receiver and no context of its own; it stays
     * registered only if it returns the boolean true. */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_earscallback())
            return true;

        const pm::EarsCallback &event = msg.earscallback();
        v8::Handle<v8::Value> argv[3];
        argv[0] = v8::String::New(pm::EarsCallback_Type_Name(event.type()).c_str());
        argv[1] = v8::Integer::New(event.has_step() ? event.step() : 0);
        argv[2] = v8::Integer::New(event.has_length() ? event.length() : 0);
        v8::Handle<v8::Value> result = function->Call(function, 3, argv);
        /* A listener that threw (empty result) stays registered; the
         * exception is reported by call_listener(). */
        if (result.IsEmpty())
            return true;
        return result->IsBoolean() && result->BooleanValue();
    }
};

/* addListener(listener). */
static v8::Handle<v8::Value> js_ears_add_listener(const v8::Arguments &args)
{
    if (args.Length() != 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");

    /* No interactive-mode check here. */
    KarotzData *karotz = current_karotz();
    karotz->add_listener(new EarsMsgCallback(v8::Handle<v8::Function>::Cast(args[0])));
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> ears_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "move", js_ears_move);
    js_set_function(tmpl, "moveRelative", js_ears_move_relative);
    js_set_function(tmpl, "reset", js_ears_reset);
    js_set_function(tmpl, "resetRight", js_ears_reset_right);
    js_set_function(tmpl, "resetLeft", js_ears_reset_left);
    js_set_function(tmpl, "addListener", js_ears_add_listener);
    return tmpl;
}
