// SPDX-License-Identifier: MIT
/*
 * karotz.serial: the serial port on the rabbit's USB (through the
 * controller, which hands it to the serial daemon). Everything goes through
 * platform serial messages; karotzVM itself never touches a device.
 *
 *   karotz.serial.open__(port [, baud])
 *       Sends serial { open { port, baud } } (baud defaults to 9600; a
 *       second argument must be an Int32, otherwise "arg 2 is Integer
 *       baudrate"; with three or more arguments the extra ones, baud
 *       included, are ignored).
 *   karotz.serial.write__(port, base, data)
 *       Sends serial { write { port, data, base } } (no argument checks:
 *       missing arguments become "undefined").
 *   karotz.serial.addListener__(listener)
 *       listener is an object; for every serialCallback message the
 *       controller sends, listener.onMessage(listener, port, data) is called
 *       (with the function itself as receiver). It keeps listening only
 *       while it returns boolean true.
 *
 * The forms without "__", which applications call:
 *
 *   karotz.serial.open(port [, baud])
 *       As open__.
 *   karotz.serial.write(data [, base])
 *       Sends serial { write { port: <last opened port>, data, base } }, base
 *       "a" unless given; with three arguments, as write__(port, base, data).
 *       Throws "no port opened" before any open.
 *   karotz.serial.addListener(function [, base])
 *       Sends serialCallback { readline { port: <last opened port>, data: "",
 *       base } } (base "a" unless given), which puts the port in line mode,
 *       then calls function(data) for every line the controller sends back; it
 *       keeps listening unless the function returns boolean false. With an
 *       object, as addListener__. Throws "no port opened" before any open.
 *
 * The debug-log traces ("serial: adding a listener", "building onMessage
 * arguments", "arg0", "call onMessage") follow every listener call.
 */
#include "karotzvm.h"

#include <stdio.h>

/* The port of the last open, which write(data) and addListener(function) use. */
static std::string last_opened_port;

namespace {

/* SerialMsgCallback: keeps the listener object. */
class SerialMsgCallback : public MessageListener {
public:
    explicit SerialMsgCallback(v8::Handle<v8::Object> listener)
        : listener(v8::Persistent<v8::Object>::New(listener)) {}

    /* Messages without serialCallback are ignored (the listener
     * stays). onMessage is called directly (poll_controller() has opened the
     * HandleScope and entered the application's context). When the listener has no
     * onMessage function, or onMessage throws, the message is ignored (the
     * listener stays; call_listener() reports the exception). */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_serialcallback())
            return true;
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "call onMessage");
        v8::Handle<v8::Value> argv[3];
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "building onMessage arguments");
        argv[0] = v8::Local<v8::Object>::New(listener);
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "arg0");
        const pm::SerialCallback_Readline &line = msg.serialcallback().readline();
        argv[1] = v8::String::New(line.port().c_str());
        argv[2] = v8::String::New(line.data().c_str());
        v8::Local<v8::Value> on_message = listener->Get(v8::String::New("onMessage"));
        if (on_message.IsEmpty() || !on_message->IsFunction())
            return true;
        v8::Handle<v8::Function> handler = v8::Handle<v8::Function>::Cast(on_message);
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "call onMessage");
        v8::Handle<v8::Value> result = handler->Call(handler, 3, argv);
        if (result.IsEmpty())
            return true;
        return result->IsBoolean() && result->BooleanValue();
    }

private:
    v8::Persistent<v8::Object> listener;
};

/* SerialLineCallback: keeps the function given to addListener(function). */
class SerialLineCallback : public MessageListener {
public:
    explicit SerialLineCallback(v8::Handle<v8::Function> function)
        : function(v8::Persistent<v8::Function>::New(function)) {}

    /* Messages without serialCallback are ignored. The function gets the
     * line's data; an exception is reported by call_listener() and the
     * listener stays. */
    bool on_message(const pm::PlatformMessage &msg)
    {
        if (!msg.has_serialcallback())
            return true;
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "call onMessage");
        v8::Handle<v8::Value> argv[1] = { v8::String::New(msg.serialcallback().readline().data().c_str()) };
        v8::Handle<v8::Value> result = function->Call(function, 1, argv);
        if (result.IsEmpty())
            return true;
        return !(result->IsBoolean() && !result->BooleanValue());
    }

private:
    v8::Persistent<v8::Function> function;
};

/* The base argument at `index`, or "a". */
std::string base_argument(const v8::Arguments &args, int index)
{
    if (args.Length() <= index || args[index]->IsUndefined())
        return "a";
    v8::String::AsciiValue base(args[index]->ToString());
    return *base;
}

} // namespace

/* open__(port [, baud]). */
static v8::Handle<v8::Value> js_serial_open(const v8::Arguments &args)
{
    if (args.Length() < 1)
        return js_throw("Wrong arguments");
    unsigned baud = 9600;
    if (args.Length() == 2) {
        if (!args[1]->IsInt32())
            return js_throw("arg 2 is Integer baudrate");
        baud = args[1]->Uint32Value();
    }
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* "Must be in interactive mode" thrown */

    v8::String::AsciiValue port(args[0]->ToString());
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Serial_Open *request = msg->mutable_serial()->mutable_open();
    request->set_port(*port);
    request->set_baud(baud);
    last_opened_port = *port;
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

/* write__(port, base, data). */
static v8::Handle<v8::Value> js_serial_write(const v8::Arguments &args)
{
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* "Must be in interactive mode" thrown */

    v8::String::AsciiValue port(args[0]->ToString());
    v8::String::AsciiValue base(args[1]->ToString());
    v8::String::AsciiValue data(args[2]->ToString());
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Serial_Write *request = msg->mutable_serial()->mutable_write();
    request->set_port(*port);
    request->set_base(*base);
    request->set_data(*data);
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

/* addListener__(listener). Without an object it throws. */
static v8::Handle<v8::Value> js_serial_add_listener(const v8::Arguments &args)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "serial: adding a listener");
    if (args.Length() < 1 || !args[0]->IsObject())
        return js_throw("Wrong arguments");
    KarotzData *karotz = current_karotz();
    karotz->add_listener(new SerialMsgCallback(v8::Handle<v8::Object>::Cast(args[0])));
    return v8::Undefined();
}

/* write(data [, base]) to the last opened port; with three arguments, write__. */
static v8::Handle<v8::Value> js_serial_write_short(const v8::Arguments &args)
{
    if (args.Length() >= 3)
        return js_serial_write(args);
    if (args.Length() < 1)
        return js_throw("Wrong arguments");
    if (last_opened_port.empty())
        return js_throw("no port opened");
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* "Must be in interactive mode" thrown */

    v8::String::AsciiValue data(args[0]->ToString());
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Serial_Write *request = msg->mutable_serial()->mutable_write();
    request->set_port(last_opened_port);
    request->set_base(base_argument(args, 1));
    request->set_data(*data);
    link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

/* addListener(function [, base]): line mode on the last opened port, then function(data) per
 * line; with an object, addListener__. */
static v8::Handle<v8::Value> js_serial_add_listener_short(const v8::Arguments &args)
{
    if (args.Length() >= 1 && args[0]->IsObject() && !args[0]->IsFunction())
        return js_serial_add_listener(args);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "serial: adding a listener");
    if (args.Length() < 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");
    if (last_opened_port.empty())
        return js_throw("no port opened");
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* "Must be in interactive mode" thrown */

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::SerialCallback_Readline *readline = msg->mutable_serialcallback()->mutable_readline();
    readline->set_port(last_opened_port);
    readline->set_data("");
    readline->set_base(base_argument(args, 1));
    link_send(karotz->socket, *msg);
    delete msg;
    karotz->add_listener(new SerialLineCallback(v8::Handle<v8::Function>::Cast(args[0])));
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> serial_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "open__", js_serial_open);
    js_set_function(tmpl, "write__", js_serial_write);
    js_set_function(tmpl, "addListener__", js_serial_add_listener);
    js_set_function(tmpl, "open", js_serial_open);
    js_set_function(tmpl, "write", js_serial_write_short);
    js_set_function(tmpl, "addListener", js_serial_add_listener_short);
    return tmpl;
}
