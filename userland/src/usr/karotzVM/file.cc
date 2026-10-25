// SPDX-License-Identifier: MIT
/*
 * The global "file" object: files in the application's own folder.
 *
 *   file.read(name)          returns an object whose "text" property holds
 *                            the file's content (read anew at every access);
 *   file.write(name, text)   (over)writes the file with text.
 *
 * name is relative to <app_folder>/<app> (e.g. /usr/karotz/apps/<app>) and
 * refused (exception "access denied: <name>") when path_refused() says so.
 * Both arguments must be strings, otherwise "Signature error function". No
 * connection or interactive mode is needed and nothing goes to the
 * controller.
 */
#include "karotzvm.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

namespace {

/* A file loaded into memory: NUL-terminated data and its size. */
struct Resource {
    char *data;
    int size;
};

} // namespace

/* Loads a whole file; NULL (after logging strerror) when it cannot
 * be opened or stat'ed, in which case the Resource is freed and the FILE
 * closed. A loaded Resource is freed with free_resource(). */
static Resource *load_resource(const char *path)
{
    Resource *resource = new Resource;
    resource->data = NULL;
    resource->size = 0;
    FILE *f = fopen(path, "r");
    if (f == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s", strerror(errno));
        delete resource;
        return NULL;
    }
    struct stat st;
    if (stat(path, &st) != 0) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s", strerror(errno));
        fclose(f);
        delete resource;
        return NULL;
    }
    resource->data = new char[st.st_size + 1];
    /* The size is what was read: a file that shrank since stat() (or a read
     * error) gives the bytes there were, not undefined ones after them. */
    resource->size = (int)fread(resource->data, 1, st.st_size, f);
    fclose(f);
    resource->data[resource->size] = '\0';
    return resource;
}

/* Getter of "text". Internal field 0 is an External holding the
 * Resource (always NULL: the loaded file is never stored back, so the file
 * is read again at every access), internal field 1 the path. */
static v8::Handle<v8::Value> js_resource_text(v8::Local<v8::String>, const v8::AccessorInfo &info)
{
    v8::Local<v8::Object> holder = info.Holder();
    Resource *resource = static_cast<Resource *>(v8::Handle<v8::External>::Cast(holder->GetInternalField(0))->Value());
    if (resource == NULL) {
        v8::String::AsciiValue path(v8::Handle<v8::String>::Cast(holder->GetInternalField(1)));
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "path %s", *path);
        resource = load_resource(*path);
        if (resource == NULL)
            return v8::ThrowException(v8::String::Concat(v8::String::New("cannot open resource: "),
                                                         v8::String::New(*path)));
    }
    v8::Handle<v8::String> text = v8::String::New(resource->data);
    /* The Resource is never stored back (see above), so it ends here. */
    delete[] resource->data;
    delete resource;
    return text;
}

/* Setter of "text": assignments are ignored. */
static void js_resource_set_text(v8::Local<v8::String>, v8::Local<v8::Value>, const v8::AccessorInfo &)
{
}

/* getDataResource(path): the object read() returns. */
static v8::Handle<v8::Value> get_data_resource(const char *path)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "getDataResource(%s)", path);
    v8::HandleScope scope;
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    tmpl->SetInternalFieldCount(2);
    tmpl->SetAccessor(v8::String::New("text"), js_resource_text, js_resource_set_text);
    v8::Local<v8::Object> resource = tmpl->NewInstance();
    resource->SetInternalField(0, v8::External::New(NULL));
    resource->SetInternalField(1, v8::String::New(path));
    return scope.Close(resource);
}

/* "<app_folder>/<app>/<name>" (truncated to 511 characters). */
static void app_file_path(char out[512], KarotzData *karotz, const char *name)
{
    snprintf(out, 512, "%s/%s/%s", karotz->options->app_folder, karotz->app_name.c_str(), name);
}

/* The exception of a refused name (logged first). */
static v8::Handle<v8::Value> access_denied(const v8::Arguments &args, const char *name)
{
    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "access denied: %s", name);
    return v8::ThrowException(v8::String::Concat(v8::String::New("access denied: "), args[0]->ToString()));
}

/* read(name). */
static v8::Handle<v8::Value> js_file_read(const v8::Arguments &args)
{
    if (args.Length() != 1 || !js_is_string(args[0]))
        return js_throw("Signature error function");
    v8::String::AsciiValue name(args[0]->ToString());
    if (path_refused(*name))
        return access_denied(args, *name);
    char path[512];
    app_file_path(path, current_karotz(), *name);
    return get_data_resource(path);
}

/* write(name, text). The text is written as fopen(path, "w+") +
 * fputs (an AsciiValue: only the low byte of each character). A file that
 * cannot be created throws "cannot open resource". */
static v8::Handle<v8::Value> js_file_write(const v8::Arguments &args)
{
    if (args.Length() != 2 || !js_is_string(args[0]) || !js_is_string(args[1]))
        return js_throw("Signature error function");
    v8::String::AsciiValue name(args[0]->ToString());
    if (path_refused(*name))
        return access_denied(args, *name);
    char path[512];
    app_file_path(path, current_karotz(), *name);
    v8::String::AsciiValue text(args[1]->ToString());
    FILE *f = fopen(path, "w+");
    if (f == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "%s", strerror(errno));
        return v8::ThrowException(v8::String::Concat(v8::String::New("cannot open resource: "), args[0]->ToString()));
    }
    fputs(*text, f);
    fclose(f);
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> file_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "read", js_file_read);
    js_set_function(tmpl, "write", js_file_write);
    return tmpl;
}
