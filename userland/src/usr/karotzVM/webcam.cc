// SPDX-License-Identifier: MIT
/*
 * karotz.webcam: takes a picture.
 *
 *   karotz.webcam.photo(url [, callback])
 *
 * Sends a Webcam message (photo = url) to the controller. The optional
 * callback receives one object per part of the reply: for the Response
 * {type: <ReturnCode name>}, for the Event {type, description, data}, where
 * data is an object whose internal field 1 holds "/tmp/picture.jpg" (the
 * form karotz.multimedia.play and the file functions accept for a local
 * file). For "direct" the controller sends the JPEG in the reply itself: it
 * is written to /tmp/picture.jpg, and the Response's object has data too.
 */
#include <stdio.h>

#include "karotzvm.h"

#define PICTURE "/tmp/picture.jpg"

/* The data object: an instance of a template with two internal fields, field 1 = the picture's
 * path (field 0 left unset). */
static v8::Handle<v8::Object> picture_data()
{
    v8::Handle<v8::ObjectTemplate> data_template = v8::ObjectTemplate::New();
    data_template->SetInternalFieldCount(2);
    v8::Handle<v8::Object> data = data_template->NewInstance();
    data->SetInternalField(1, v8::String::New(PICTURE));
    return data;
}

/* A direct picture's JPEG into PICTURE; FALSE when it can't be written. */
static gboolean write_picture(const std::string &jpeg)
{
    FILE *f = fopen(PICTURE, "wb");
    if (f == NULL)
        return FALSE;
    gboolean ok = fwrite(jpeg.data(), 1, jpeg.size(), f) == jpeg.size();
    return fclose(f) == 0 && ok;
}

/* Calls the function with the function itself as receiver. */
static void call_callback(NamedFunction *callback, v8::Handle<v8::Value> argument)
{
    v8::Handle<v8::Value> argv[1] = { argument };
    callback->function->Call(callback->function, 1, argv);
}

/* callback(response) with {type} (type only when the Response has a
 * code; its description is not passed), and data when a direct picture came with it. */
static void webcam_response(NamedFunction *callback, const pm::Response &response, gboolean with_picture)
{
    FreshContext context;
    v8::Handle<v8::Object> result = v8::Object::New();
    if (response.has_code())
        result->Set(v8::String::New("type"), v8::String::New(pm::ReturnCode_Name(response.code()).c_str()));
    if (with_picture)
        result->Set(v8::String::New("data"), picture_data());
    call_callback(callback, result);
}

/* callback(event) with {type, description, data}; type and
 * description only when present, data always: an instance of a template with
 * two internal fields, field 1 = "/tmp/picture.jpg" (field 0 left unset). */
static void webcam_event(NamedFunction *callback, const pm::Event &event)
{
    FreshContext context;
    v8::Handle<v8::Object> result = v8::Object::New();
    if (event.has_code())
        result->Set(v8::String::New("type"), v8::String::New(pm::ReturnCode_Name(event.code()).c_str()));
    if (event.has_description())
        result->Set(v8::String::New("description"), v8::String::New(event.description().c_str()));

    result->Set(v8::String::New("data"), picture_data());

    call_callback(callback, result);
}

/* Reply handler for photo (data: the NamedFunction "WEBCAM"). The
 * NamedFunction is freed after the photo's last reply, when it can be told
 * (see js_webcam_photo()). */
static void webcam_reply(void *data, const pm::PlatformMessage &reply)
{
    NamedFunction *callback = static_cast<NamedFunction *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "EventCallbackWebcam Webcam");
    if (reply.has_response()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "has_response Webcam");
        gboolean picture = FALSE;
        if (reply.has_webcamdirectresponse() && reply.webcamdirectresponse().has_data()) {
            picture = write_picture(reply.webcamdirectresponse().data());
            if (!picture)
                g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "webcam: can't write the direct picture to %s", PICTURE);
        }
        webcam_response(callback, reply.response(), picture);
    }
    if (reply.has_event()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "has_event Webcam");
        webcam_event(callback, reply.event());
    }
    if (reply_is_last(callback->end, reply)) {
        delete callback;
        reply_done();
    }
}

/* photo(url [, callback]). The NamedFunction is created before the
 * interactive-mode check, and freed when that check throws. */
static v8::Handle<v8::Value> js_webcam_photo(const v8::Arguments &args)
{
    NamedFunction *callback = NULL;

    if (args.Length() == 2 && args[1]->IsFunction())
        callback = new NamedFunction("WEBCAM", v8::Handle<v8::Function>::Cast(args[1]));
    else if (args.Length() != 1)
        return js_throw("Wrong arguments");

    KarotzData *karotz = current_karotz();
    if (karotz->interactive_id == NULL) {
        delete callback;
        return js_throw("Must be in interactive mode");
    }

    v8::String::AsciiValue url(args[0]->ToString());
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "WEBCAM: photo url[%s]", *url);

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_webcam()->set_photo(*url);
    /* The controller's replies, by url: "file" and an upload address (both
     * taken by the webcam-daemon) get a Response and the daemon's Event,
     * or a Response ERROR with or without one; "video" gets a Response per
     * frame until it is stopped, and "videoStop" nothing at all (both
     * kept); "direct" gets one Response. */
    if (callback != NULL) {
        if (strcmp(*url, "direct") == 0)
            callback->end = REPLY_END_FIRST;
        else if (strcmp(*url, "video") != 0 && strcmp(*url, "videoStop") != 0)
            callback->end = REPLY_END_EVENT;
    }
    if (callback == NULL)
        link_send(karotz->socket, *msg);
    else
        link_send_with_callback(karotz->socket, *msg, new ReplyCallback(webcam_reply, callback, release_named_function));
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> webcam_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "photo", js_webcam_photo);
    return tmpl;
}
