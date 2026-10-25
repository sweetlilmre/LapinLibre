// SPDX-License-Identifier: MIT
/*
 * Running one application: the JavaScript environment karotzVM builds for it
 * and the run itself.
 *
 * The global object holds log(), exit(), setTimeout(), ping(), include(),
 * signHMACSHA1(), uuid(), timestamp(), the objects karotz, file, http and
 * social, the strings cmdline, appName and instanceName, launchType (how the
 * application was started: {name}, or for an RFID tag or a voice command the
 * details) and params (the application's configuration from config.bin).
 * karotz holds connect(), start(), stop(), ping(), info(), random() and one
 * object per access granted in descriptor.xml.
 */
#include <errno.h>
#include <sstream>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "karotzvm.h"

/* An application must be in interactive mode this long after starting. */
#define AUTOKILL_NOT_INTERACTIVE_MS 10000

/* ---- global functions --------------------------------------------------- */

/* log(text) */
static v8::Handle<v8::Value> js_log(const v8::Arguments &args)
{
    if (args.Length() != 1)
        return js_throw("Wrong arguments");
    v8::String::AsciiValue text(args[0]->ToString());
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "JSLOG: %s", *text);
    return v8::Undefined();
}

/* exit(): ends the main loop, and with it the child. */
static v8::Handle<v8::Value> js_exit(const v8::Arguments &args)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "EXIT");
    if (args.Length() != 0)
        return js_throw("Wrong arguments");
    g_main_loop_quit(current_karotz()->loop);
    return v8::Undefined();
}

/*
 * A setTimeout callback runs again for as long as it returns true. It
 * runs in the application's context, and an exception is reported and ends
 * the timer.
 */
static gboolean timeout_expired(gpointer data)
{
    StoredFunction *fn = static_cast<StoredFunction *>(data);
    /* The handles this run of the callback makes are freed when it returns
     * (see karotzvm.h). */
    v8::HandleScope scope;
    FreshContext context;
    v8::TryCatch try_catch;
    v8::Handle<v8::Value> result = (*fn)->Call(*fn, 0, NULL);

    if (result.IsEmpty()) {
        report_exception(try_catch);
    } else if (result->IsBoolean() && result->BooleanValue()) {
        return TRUE;
    }
    release_function(fn);
    return FALSE;
}

/* setTimeout(ms, fn) */
static v8::Handle<v8::Value> js_set_timeout(const v8::Arguments &args)
{
    if (args.Length() != 2 || !args[0]->IsNumber() || !args[1]->IsFunction())
        return js_throw("Must be setTimeout(int,function)");
    StoredFunction *fn = store_function(args[1]);
    int delay = args[0]->Int32Value();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "SetTimeout: %d", delay);
    g_timeout_add(delay, timeout_expired, fn);
    return v8::Undefined();
}

/* timestamp(): the time as a decimal string. */
static v8::Handle<v8::Value> js_timestamp(const v8::Arguments &)
{
    char text[255];

    sprintf(text, "%d", (int)time(NULL));
    return v8::String::New(text);
}

/* uuid() */
static v8::Handle<v8::Value> js_uuid(const v8::Arguments &)
{
    char uuid[37];

    make_uuid(uuid);
    return v8::String::New(uuid);
}

/* karotz.random(): rand() % 10000000. */
static v8::Handle<v8::Value> js_random(const v8::Arguments &)
{
    int value = rand() % 10000000;
    v8::Handle<v8::Value> result = v8::Integer::New(value);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "rand: %d", value);
    return result;
}

/* ---- scripts ------------------------------------------------------------ */

/* A file that cannot be stat'ed or opened gives NULL, after logging
 * strerror. */
static char *read_whole_file(const char *path)
{
    struct stat info;
    FILE *file = fopen(path, "r");

    if (stat(path, &info) != 0 || file == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_CRITICAL, "%s", strerror(errno));
        if (file != NULL)
            fclose(file);
        return NULL;
    }
    char *text = new char[info.st_size + 1];
    fread(text, info.st_size, 1, file);
    fclose(file);
    text[info.st_size] = '\0';
    return text;
}

/* <app folder>/<app>/<name> as a string, empty if it can't be read. */
static v8::Handle<v8::String> load_source(KarotzData *karotz, const char *name)
{
    char path[255];

    snprintf(path, sizeof path, "%s/%s/%s", karotz->options->app_folder, karotz->app_name.c_str(), name);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "loading source: %s", path);
    char *text = read_whole_file(path);
    if (text == NULL)
        return v8::Handle<v8::String>();
    v8::Handle<v8::String> source = v8::String::New(text);
    delete[] text;
    return source;
}

static const char *to_cstring(const v8::String::Utf8Value &value)
{
    return *value != NULL ? *value : "<string conversion failed>";
}

/* file:line: exception, the source line, a ^^^ marker under the
 * culprit and the stack trace. Without a message only the exception is
 * logged. */
void report_exception(v8::TryCatch &try_catch)
{
    v8::String::Utf8Value exception(try_catch.Exception());
    const char *exception_text = to_cstring(exception);
    v8::Handle<v8::Message> message = try_catch.Message();

    if (message.IsEmpty()) {
        g_log(KVM_LOG, G_LOG_LEVEL_CRITICAL, "%s\n", exception_text);
        return;
    }
    v8::String::Utf8Value file(message->GetScriptResourceName());
    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s:%i: %s", to_cstring(file), message->GetLineNumber(), exception_text);
    v8::String::Utf8Value line(message->GetSourceLine());
    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s", to_cstring(line));

    char marker[1024];
    int start = message->GetStartColumn();
    int i;
    for (i = 0; i < start; i++)
        marker[i] = ' ';
    int end = message->GetEndColumn();
    for (i = start; i < end; i++)
        marker[i] = '^';
    marker[i] = '\0';
    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s", marker);

    v8::String::Utf8Value stack(try_catch.StackTrace());
    if (stack.length() > 0)
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s", to_cstring(stack));
}

/*
 * include(file): runs another script of the application in the
 * current context. An included file that is missing or has a syntax
 * error throws in the including script.
 */
static v8::Handle<v8::Value> js_include(const v8::Arguments &args)
{
    if (args.Length() != 1 || !js_is_string(args[0]))
        return js_throw("Must contain only one file");
    v8::String::AsciiValue name(args[0]->ToString());
    if (path_refused(*name)) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "access denied: %s", *name);
        return v8::ThrowException(v8::String::Concat(v8::String::New("access denied: "), args[0]->ToString()));
    }
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Include: %s", *name);
    v8::Handle<v8::Context> context = v8::Context::GetCurrent();
    KarotzData *karotz = current_karotz();
    v8::Context::Scope scope(context);
    v8::Handle<v8::String> source = load_source(karotz, *name);
    if (source.IsEmpty())
        return v8::ThrowException(v8::String::Concat(v8::String::New("cannot read: "), args[0]->ToString()));
    v8::TryCatch try_catch;
    v8::Handle<v8::Script> script = v8::Script::Compile(source, v8::String::New(*name));
    if (script.IsEmpty() || script->Run().IsEmpty())
        return try_catch.ReThrow();
    return v8::Undefined();
}

/* ---- params ------------------------------------------------------------- */

/*
 * config.bin is a serialized ConfigApp. params gets one object per
 * configured instance, named after it, with the instance's parameters plus
 * accessToken and uuid when set, and "descriptor" with the application's
 * own parameters when there are any. A missing, unreadable, short-read or unparsable file gives {}.
 * The read buffer is freed here.
 */
static v8::Handle<v8::Object> load_params(const char *path)
{
    struct stat info;

    if (stat(path, &info) != 0) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "no params loaded: %s", strerror(errno));
        return v8::Object::New();
    }
    char *data = new char[info.st_size];
    FILE *file = fopen(path, "r");
    if (file == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s", strerror(errno));
        delete[] data;
        return v8::Object::New();
    }
    if (info.st_size > 0 && fread(data, info.st_size, 1, file) != 1) {
        /* A short read: no parameters (rather than parsing what wasn't read). */
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "no params loaded: short read of %s", path);
        delete[] data;
        fclose(file);
        return v8::Object::New();
    }
    fclose(file);

    pm::ConfigApp config;
    bool parsed = config.ParseFromArray(data, info.st_size);
    delete[] data;
    if (!parsed) {
        /* What did parse is incomplete: no parameters rather than some. */
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "no params loaded: %s doesn't parse; missing: %s", path,
              config.InitializationErrorString().c_str());
        return v8::Object::New();
    }
    int instances = config.configinstance_size();
    v8::Handle<v8::Object> params = v8::Object::New();
    for (int i = 0; i < instances; i++) {
        pm::ConfigInstance instance(config.configinstance(i));
        int count = instance.params_size();
        v8::Handle<v8::Object> values = v8::Object::New();
        for (int j = 0; j < count; j++) {
            v8::Handle<v8::String> key = v8::String::New(instance.params(j).key().c_str());
            v8::Handle<v8::String> value = v8::String::New(instance.params(j).value().c_str());
            values->Set(key, value);
        }
        if (instance.has_accesstoken()) {
            v8::Handle<v8::String> key = v8::String::New("accessToken");
            v8::Handle<v8::String> value = v8::String::New(instance.accesstoken().c_str());
            values->Set(key, value);
        }
        if (instance.has_uuid()) {
            v8::Handle<v8::String> key = v8::String::New("uuid");
            v8::Handle<v8::String> value = v8::String::New(instance.uuid().c_str());
            values->Set(key, value);
        }
        params->Set(v8::String::New(instance.name().c_str()), values);
    }
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "create parameters");
    int count = config.parameters_size();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "parameters size: %d", count);
    if (count > 0) {
        v8::Handle<v8::Object> values = v8::Object::New();
        for (int i = 0; i < count; i++) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "create parameters key: %s", config.parameters(i).key().c_str());
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "create parameters value: %s", config.parameters(i).value().c_str());
            v8::Handle<v8::String> key = v8::String::New(config.parameters(i).key().c_str());
            v8::Handle<v8::String> value = v8::String::New(config.parameters(i).value().c_str());
            values->Set(key, value);
        }
        params->Set(v8::String::New("descriptor"), values);
    }
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "create parameters done");
    return params;
}

/* ---- launchType ----------------------------------------------------------- */

/* launchType.semantic cannot be assigned. */
static void ignore_assignment(v8::Local<v8::String>, v8::Local<v8::Value>, const v8::AccessorInfo &)
{
}

/* launchType.semantic, parsed from rawSemantic on each read. The objects
 * are made in the application's context, where the accessor runs, rather
 * than in a new context per read (see FreshContext for what such contexts
 * cost). */
static v8::Handle<v8::Value> get_semantic(v8::Local<v8::String>, const v8::AccessorInfo &info)
{
    v8::Local<v8::Object> holder = info.Holder();
    v8::Handle<v8::Object> parsed = v8::Object::New();
    v8::String::Utf8Value raw(holder->Get(v8::String::New("rawSemantic"))->ToString());
    asr_parse_semantic(*raw, parsed);
    return parsed->Get(v8::String::New("semantic"));
}

/*
 * The launch type the controller gave. "RFID;<id>;<app>;<type>;
 * <pict>;<color>;<data>" (an RFID tag) and "ASR;<text>;<semantic>" (a voice
 * command) are taken apart (fields after a missing ';' are empty);
 * anything else is just {name}.
 */
static void fill_launch_type(v8::Handle<v8::ObjectTemplate> tmpl, const std::string &launch_type)
{
    if (launch_type.compare(0, 4, "RFID") == 0) {
        std::string tag, id, app, type, pict, color, data;
        tmpl->Set(v8::String::New("name"), v8::String::New("RFID"));
        std::istringstream fields(launch_type, std::ios_base::in);
        std::getline(fields, tag, ';');
        std::getline(fields, id, ';');
        tmpl->Set(v8::String::New("id"), v8::String::New(id.c_str()));
        std::getline(fields, app, ';');
        tmpl->Set(v8::String::New("app"), v8::String::New(app.c_str()));
        std::getline(fields, type, ';');
        tmpl->Set(v8::String::New("type"), v8::String::New(type.c_str()));
        std::getline(fields, pict, ';');
        tmpl->Set(v8::String::New("pict"), v8::String::New(pict.c_str()));
        std::getline(fields, color, ';');
        tmpl->Set(v8::String::New("color"), v8::String::New(color.c_str()));
        std::getline(fields, data, ';');
        tmpl->Set(v8::String::New("data"), v8::String::New(data.c_str()));
    } else if (launch_type.compare(0, 3, "ASR") == 0) {
        std::string tag, text, semantic;
        /* Entered, left and disposed of. */
        v8::Persistent<v8::Context> context = v8::Context::New();
        context->Enter();
        tmpl->Set(v8::String::New("name"), v8::String::New("ASR"));
        std::istringstream fields(launch_type, std::ios_base::in);
        std::getline(fields, tag, ';');
        std::getline(fields, text, ';');
        tmpl->Set(v8::String::New("text"), v8::String::New(text.c_str()));
        std::getline(fields, semantic, ';');
        semantic = "<semantic>" + semantic + "</semantic>";
        tmpl->SetAccessor(v8::String::New("semantic"), get_semantic, ignore_assignment);
        tmpl->Set(v8::String::New("rawSemantic"), v8::String::New(semantic.c_str()));
        tmpl->Set(v8::String::New("confident"), v8::Number::New(50));
        context->Exit();
        context.Dispose();
    } else {
        tmpl->Set(v8::String::New("name"), v8::String::New(launch_type.c_str()));
    }
}

/* ---- the environment ----------------------------------------------------- */

static v8::Handle<v8::ObjectTemplate> global_template()
{
    v8::Handle<v8::ObjectTemplate> global = v8::ObjectTemplate::New();

    js_set_function(global, "log", js_log);
    js_set_function(global, "exit", js_exit);
    js_set_function(global, "setTimeout", js_set_timeout);
    js_set_function(global, "ping", js_ping);
    global->SetInternalFieldCount(1); /* the KarotzData */
    return global;
}

/* One object per granted access; social (always) and http go on
 * the global object, the others on karotz. */
static void add_access_objects(v8::Handle<v8::ObjectTemplate> global, v8::Handle<v8::ObjectTemplate> karotz,
                               const Descriptor *descriptor)
{
    static const struct {
        const char *access;
        v8::Handle<v8::ObjectTemplate> (*build)();
    } objects[] = {
        {"led", led_template},       {"ears", ears_template},
        {"tts", tts_template},       {"webcam", webcam_template},
        {"asr", asr_template},       {"multimedia", multimedia_template},
        {"button", button_template}, {"rfid", rfid_template},
        {"k2k", k2k_template},       {"message", message_template},
        {"contact", contact_template}, {"serial", serial_template},
        {"dock", dock_template},
    };

    for (size_t i = 0; i < sizeof objects / sizeof objects[0]; i++) {
        if (descriptor->has_access(objects[i].access))
            karotz->Set(v8::String::New(objects[i].access), objects[i].build());
    }
    global->Set(v8::String::New("social"), social_template());
    if (descriptor->has_access("http"))
        global->Set(v8::String::New("http"), http_template());
}

static Descriptor *load_descriptor(KarotzData *karotz)
{
    char path[255];

    snprintf(path, sizeof path, "%s/%s/descriptor.xml", karotz->options->app_folder, karotz->app_name.c_str());
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "searching descriptor: %s", path);
    return new Descriptor(path);
}

/* Builds the environment and runs main.js. The context is
 * disposed of afterwards; the callbacks the script left keep it alive. */
static void app_load(KarotzData *karotz)
{
    v8::Handle<v8::ObjectTemplate> global = global_template();
    v8::Handle<v8::ObjectTemplate> karotz_object = v8::ObjectTemplate::New();
    Descriptor *descriptor = load_descriptor(karotz);
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "descriptor loaded");
    add_access_objects(global, karotz_object, descriptor);
    karotz->descriptor = descriptor;

    js_set_function(karotz_object, "ping", js_ping);
    js_set_function(karotz_object, "connect", js_karotz_connect);
    js_set_function(karotz_object, "start", js_karotz_start);
    js_set_function(karotz_object, "stop", js_karotz_stop);
    js_set_function(karotz_object, "info", js_karotz_info);
    srand(time(NULL));
    js_set_function(karotz_object, "random", js_random);

    js_set_function(global, "include", js_include);
    js_set_function(global, "signHMACSHA1", js_sign_hmac_sha1);
    js_set_function(global, "uuid", js_uuid);
    js_set_function(global, "timestamp", js_timestamp);
    global->Set(v8::String::New("karotz"), karotz_object);
    global->Set(v8::String::New("file"), file_template());
    global->Set(v8::String::New("cmdline"), v8::String::New(karotz->cmdline.c_str()));
    global->Set(v8::String::New("instanceName"), v8::String::New(karotz->instance_name.c_str()));
    global->Set(v8::String::New("appName"), v8::String::New(karotz->app_name.c_str()));
    v8::Handle<v8::ObjectTemplate> launch_type = v8::ObjectTemplate::New();
    fill_launch_type(launch_type, karotz->launch_type);
    global->Set(v8::String::New("launchType"), launch_type);

    v8::Persistent<v8::Context> context = v8::Context::New(NULL, global);
    context->Enter();
    context->Global()->SetPointerInInternalField(0, karotz);
    set_current_karotz(karotz);
    set_app_context(context);

    char path[255];
    snprintf(path, sizeof path, "%s/%s/config.bin", karotz->options->app_folder, karotz->app_name.c_str());
    v8::Handle<v8::Object> params = load_params(path);
    context->Global()->Set(v8::String::New("params"), params);

    v8::Handle<v8::String> source = load_source(karotz, "main.js");
    v8::TryCatch try_catch;
    try_catch.SetCaptureMessage(true);
    try_catch.SetVerbose(true);
    v8::Handle<v8::Script> script;
    if (source.IsEmpty())
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "no main.js"); /* the autokill ends the child */
    else
        script = v8::Script::Compile(source, v8::String::New("main.js"));
    if (source.IsEmpty()) {
    } else if (script.IsEmpty()) {
        report_exception(try_catch);
    } else {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "running app: %s", karotz->app_name.c_str());
        v8::Handle<v8::Value> result = script->Run();
        if (result.IsEmpty())
            report_exception(try_catch);
    }
    /* Left, then disposed of: the copy kept by set_app_context() keeps the
     * context alive for the main loop. */
    context->Exit();
    context.Dispose();
}

static gboolean autokill_if_not_interactive(gpointer data)
{
    if (static_cast<KarotzData *>(data)->interactive_id == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "/!\\ AUTOKILL, application still not interactive after %dms",
              AUTOKILL_NOT_INTERACTIVE_MS);
        exit(-1);
    }
    return FALSE;
}

void app_run(KarotzData *karotz)
{
    karotz->loop = g_main_loop_new(NULL, FALSE);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "running app: %s/%s", karotz->options->app_folder, karotz->app_name.c_str());
    v8::HandleScope scope;
    v8::V8::SetCaptureStackTraceForUncaughtExceptions(true, 10, v8::StackTrace::kOverview);
    app_load(karotz);
    g_timeout_add(AUTOKILL_NOT_INTERACTIVE_MS, autokill_if_not_interactive, karotz);
    g_main_loop_run(karotz->loop);
    v8::V8::Dispose();
    g_main_loop_unref(karotz->loop);
}
