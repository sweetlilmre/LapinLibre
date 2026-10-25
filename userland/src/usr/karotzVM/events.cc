// SPDX-License-Identifier: MIT
/*
 * The reply handler shared by the calls that only report success or failure
 * (led, ears.move, dock, most of multimedia, tts...): the controller answers
 * with a Response, and may later send an Event for the same message (e.g.
 * when a sound has finished); each reaches the application's callback as the
 * name of its ReturnCode ("OK", "ERROR", "TERMINATED", ...).
 */
#include "karotzvm.h"

/* The application's context is entered around the call by poll_controller(). */
static void call_with_code(NamedFunction *callback, pm::ReturnCode code)
{
    v8::Handle<v8::Value> argv[1] = {v8::String::New(pm::ReturnCode_Name(code).c_str())};

    callback->function->Call(callback->function, 1, argv);
}

bool reply_is_last(ReplyEnd end, const pm::PlatformMessage &reply)
{
    bool error = reply.has_response() && reply.response().has_code() && reply.response().code() != pm::OK;

    switch (end) {
    case REPLY_END_FIRST:
        return true;
    case REPLY_END_EVENT:
        return reply.has_event();
    case REPLY_END_EVENT_OR_ERROR:
        return reply.has_event() || error;
    default:
        return false;
    }
}

/* The callback hears the Response and any later Events. The handler and
 * the NamedFunction are freed after the request's last reply, when it can be
 * told (NamedFunction::end). */
void event_reply(void *named_function, const pm::PlatformMessage &reply)
{
    NamedFunction *callback = static_cast<NamedFunction *>(named_function);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Event: %s", callback->name);
    if (reply.has_response() && reply.response().has_code()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "calling response function");
        call_with_code(callback, reply.response().code());
    }
    if (reply.has_event() && reply.event().has_code()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "calling event function");
        call_with_code(callback, reply.event().code());
    }
    if (reply_is_last(callback->end, reply)) {
        delete callback;
        reply_done();
    }
}
