// SPDX-License-Identifier: MIT
/*
 * Helpers around V8 shared by the bindings: the current application, the
 * standard exceptions, calling back into JavaScript, kept functions.
 *
 * They cover the pointer from the global's internal field, the IsString
 * check, the named function holder, and the context + Function::Call
 * sequence that every reply handler uses.
 */
#include <stdlib.h>
#include <string.h>

#include "karotzvm.h"

/*
 * The application this process runs: each karotzVM child runs exactly one.
 * It is kept here rather than taken from internal field 0 of the current
 * context's global object, because the current context in an API call is
 * not always the application's (its global then has no such field): after
 * enough reply callbacks the pointer would come back NULL and the next call
 * crash. The pointer is also stored in the global.
 */
static KarotzData *the_karotz;

void set_current_karotz(KarotzData *karotz)
{
    the_karotz = karotz;
}

KarotzData *current_karotz()
{
    return the_karotz;
}

v8::Handle<v8::Value> js_throw(const char *message)
{
    return v8::ThrowException(v8::String::New(message));
}

KarotzData *interactive_karotz()
{
    KarotzData *karotz = current_karotz();

    if (karotz->interactive_id == NULL) {
        js_throw("Must be in interactive mode");
        return NULL;
    }
    return karotz;
}

static v8::Persistent<v8::Context> the_app_context;

void set_app_context(v8::Handle<v8::Context> context)
{
    the_app_context = v8::Persistent<v8::Context>::New(context);
}

v8::Handle<v8::Context> app_context()
{
    return the_app_context;
}

/*
 * Reply handlers enter the application's context (see FreshContext in
 * karotzvm.h) and call the function with the function itself as receiver
 * (so "this" in a callback is the callback). The function runs in the
 * context it was created in.
 */
v8::Handle<v8::Value> js_call(v8::Persistent<v8::Function> &fn, int argc, v8::Handle<v8::Value> argv[])
{
    FreshContext context;
    return fn->Call(fn, argc, argv);
}

/* V8's inline Value::IsString(): a heap object whose map's instance type is
 * below FIRST_NONSTRING_TYPE (0x80). */
bool js_is_string(v8::Handle<v8::Value> value)
{
    return value->IsString();
}

void js_set_function(v8::Handle<v8::ObjectTemplate> tmpl, const char *name, v8::InvocationCallback callback)
{
    tmpl->Set(v8::String::New(name), v8::FunctionTemplate::New(callback));
}

NamedFunction::NamedFunction(const char *name, v8::Handle<v8::Function> fn, ReplyEnd end)
    : function(v8::Persistent<v8::Function>::New(fn)), name(strdup(name)), end(end)
{
}

NamedFunction::~NamedFunction()
{
    function.Dispose();
    function.Clear();
    free(name);
}

StoredFunction *store_function(v8::Handle<v8::Value> fn)
{
    return new StoredFunction(v8::Persistent<v8::Function>::New(v8::Handle<v8::Function>::Cast(fn)));
}

void release_function(StoredFunction *fn)
{
    fn->Dispose();
    fn->Clear();
    delete fn;
}

void release_named_function(void *fn)
{
    delete static_cast<NamedFunction *>(fn);
}

void release_stored_function(void *fn)
{
    release_function(static_cast<StoredFunction *>(fn));
}
