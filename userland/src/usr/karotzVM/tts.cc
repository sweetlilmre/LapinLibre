// SPDX-License-Identifier: MIT
/*
 * karotz.tts: text to speech.
 *
 *   karotz.tts.start(text [, lang] [, callback])
 *   karotz.tts.stop([callback])
 *
 * Both send a Tts message (action START with text/lang, or STOP) to the
 * controller. The optional callback receives the name of the ReturnCode of
 * the controller's Response and of its Event (e.g. "OK", "TERMINATED"), one
 * string per call.
 */
#include "karotzvm.h"

/* Sends msg, with event_reply when there is a callback. */
static void tts_send(KarotzData *karotz, pm::PlatformMessage *msg, NamedFunction *callback)
{
    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(event_reply, callback, release_named_function));
}

/* start(text [, lang] [, callback]).
 *   3 arguments, the third a function: text, lang, callback;
 *   3 arguments, the third not a function: text, lang (no callback);
 *   2 arguments: text + callback when the second is a function, else text + lang;
 *   1 argument: text only.
 * The NamedFunction is created before the interactive-mode check, and freed
 * when that check throws. */
static v8::Handle<v8::Value> js_tts_start(const v8::Arguments &args)
{
    bool with_lang = false;
    NamedFunction *callback = NULL;

    if (args.Length() == 3) {
        with_lang = true;
        if (args[2]->IsFunction())
            callback = new NamedFunction("TTS", v8::Handle<v8::Function>::Cast(args[2]), REPLY_END_EVENT_OR_ERROR);
    } else if (args.Length() == 2) {
        if (!args[1]->IsFunction())
            with_lang = true;
        else
            callback = new NamedFunction("TTS", v8::Handle<v8::Function>::Cast(args[1]), REPLY_END_EVENT_OR_ERROR);
    } else if (args.Length() < 1 || args.Length() > 3) {
        return js_throw("Wrong arguments");
    }

    KarotzData *karotz = current_karotz();
    if (karotz->interactive_id == NULL) {
        delete callback;
        return js_throw("Must be in interactive mode");
    }

    v8::String::Utf8Value text(args[0]->ToString());
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "TTS: say[%s]", *text);

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Tts *tts = msg->mutable_tts();
    tts->set_action(pm::Tts::START);
    tts->set_text(*text);
    if (with_lang) {
        v8::String::Utf8Value lang(args[1]->ToString());
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "TTS: lang[%s]", *lang);
        tts->set_lang(*lang);
    }
    tts_send(karotz, msg, callback);
    delete msg;
    return v8::Undefined();
}

/* stop([callback]). As in start, the NamedFunction is freed when the
 * interactive-mode check throws. */
static v8::Handle<v8::Value> js_tts_stop(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "TtsFunctionStop start");
    if (args.Length() == 1 && args[0]->IsFunction()) {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "TtsFunctionStop arg ok");
        callback = new NamedFunction("TTS", v8::Handle<v8::Function>::Cast(args[0]), REPLY_END_FIRST);
    } else if (args.Length() != 0) {
        return js_throw("Wrong arguments");
    }

    KarotzData *karotz = current_karotz();
    if (karotz->interactive_id == NULL) {
        delete callback;
        return js_throw("Must be in interactive mode");
    }

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "TtsFunctionStop");
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_tts()->set_action(pm::Tts::STOP);
    tts_send(karotz, msg, callback);
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> tts_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "start", js_tts_start);
    js_set_function(tmpl, "stop", js_tts_stop);
    return tmpl;
}
