// SPDX-License-Identifier: MIT
/*
 * karotz.asr: speech recognition.
 *
 *   karotz.asr.string(grammar, lang, function (result) { ... });
 *       Sends a PlatformMessage with asr { dictionary: [grammar], lang: lang } and
 *       calls the function once with {<semantic tags...>, text, confident}
 *       when a reply carrying asrCallback arrives. The semantic XML of the
 *       first recognition is turned into properties of the result object
 *       (one per leaf element, nested objects for elements with children).
 *   karotz.asr.file(name [, function])
 *       The grammar is the application's file `name` (relative to its
 *       folder; names outside it are refused): sends asr { file: <the
 *       application's folder>/<name> } and, given a function, calls it as
 *       string() does.
 */
#include "karotzvm.h"

#include <stdio.h>
#include <string.h>

#include <libxml/parser.h>
#include <libxml/tree.h>

/* For each element among node and its following siblings, sets
 * into[name] to the element's text content when it has no child elements
 * (nothing when that content is NULL), or to an object built recursively
 * from its children. */
static void semantic_to_object(xmlNodePtr node, v8::Handle<v8::Object> into)
{
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "node type: print_element_names");
    for (xmlNodePtr cur = node; cur != NULL; cur = cur->next) {
        if (cur->type != XML_ELEMENT_NODE)
            continue;
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "node type: Element, name: %s\n", cur->name);
        if (xmlChildElementCount(cur) == 0) {
            /* The content is fetched once for the test, the log and the
             * value, and freed afterwards. */
            xmlChar *content = xmlNodeGetContent(cur);
            if (content == NULL) {
                g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "node type: Element, children, content: null\n");
            } else {
                g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "node type: Element, content: %s\n", content);
                into->Set(v8::String::New(reinterpret_cast<const char *>(cur->name)),
                          v8::String::New(reinterpret_cast<const char *>(content)));
                xmlFree(content);
            }
        } else {
            v8::Handle<v8::Object> child = v8::Object::New();
            semantic_to_object(cur->children, child);
            into->Set(v8::String::New(reinterpret_cast<const char *>(cur->name)), child);
        }
    }
}

/* parseSemantic. */
void asr_parse_semantic(const char *xml, v8::Handle<v8::Object> into)
{
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: parseSemantic: size:%d", (int)strlen(xml));
    xmlDocPtr doc = xmlReadMemory(xml, strlen(xml), "noname.xml", NULL, 0);
    if (doc == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: parseSemantic: Failed to parse document");
        return;
    }
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: parseSemantic: succeed parse document");
    semantic_to_object(xmlDocGetRootElement(doc), into);
    xmlFreeDoc(doc);
}

/* Reply handler of asr.string; data is the StoredFunction. Every
 * reply is logged; only one carrying asrCallback calls (and then frees) the
 * function. */
static void asr_callback(void *data, const pm::PlatformMessage &reply)
{
    StoredFunction *fn = static_cast<StoredFunction *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback");
    if (!reply.has_response()) {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: has_response false");
    } else if (reply.response().has_code()) {
        if (reply.response().code() == pm::OK)
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: response: OK");
        else if (reply.response().code() == pm::ERROR)
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: response: ERROR");
    }

    if (!reply.has_asrcallback()) {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: has_asrcallback false");
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback end");
        return;
    }

    const pm::AsrCallback &asr = reply.asrcallback();
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: has_asrcallback true. recognition_size: %d",
          asr.recognition_size());
    if (asr.recognition_size() < 1) {
        g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: recognition_size = 0");
    } else {
        const pm::AsrCallback_Recognition &first = asr.recognition(0);
        if (!first.has_text())
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: recognition(0).has_text() false");
        else
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: text: %s", first.text().c_str());
        if (!first.has_confident())
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: recognition(0).has_confident() false");
        else
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: confident: %d", first.confident());
        if (!first.has_semantic()) {
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: recognition(0).has_semantic() false");
        } else {
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: semantic: %s", first.semantic().c_str());
            g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: AsrCallback: semantic size: %d",
                  (int)strlen(first.semantic().c_str()));
        }
    }

    /* The object is built in a fresh context. When nothing was recognised
     * the callback gets an empty result (no text, confidence 0, no semantic
     * properties). */
    FreshContext context;
    v8::Handle<v8::Object> result = v8::Object::New();
    if (asr.recognition_size() > 0) {
        asr_parse_semantic(asr.recognition(0).semantic().c_str(), result);
        result->Set(v8::String::New("text"), v8::String::New(asr.recognition(0).text().c_str()));
        result->Set(v8::String::New("confident"), v8::Number::New(asr.recognition(0).confident()));
    } else {
        result->Set(v8::String::New("text"), v8::String::New(""));
        result->Set(v8::String::New("confident"), v8::Number::New(0));
    }

    /* The return value is ignored and the function freed. The handler is
     * removed after this reply, so a second asrCallback reply with the same
     * correlationId finds nothing. */
    v8::Handle<v8::Value> argv[1] = { result };
    (*fn)->Call(*fn, 1, argv);
    release_function(fn);
    reply_done();
}

/* string(grammar, lang, callback). */
static v8::Handle<v8::Value> js_asr_string(const v8::Arguments &args)
{
    if (args.Length() != 3 || !args[2]->IsFunction())
        return js_throw("Wrong arguments");
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* "Must be in interactive mode" thrown */

    v8::String::Utf8Value grammar(args[0]->ToString());
    v8::String::Utf8Value lang(args[1]->ToString());
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: grammar[%s]", *grammar);

    StoredFunction *callback = store_function(args[2]);
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    pm::Asr *asr = msg->mutable_asr();
    asr->add_dictionary(*grammar);
    asr->set_lang(*lang);
    link_send_with_callback(karotz->socket, *msg, new ReplyCallback(asr_callback, callback, release_stored_function));
    delete msg;
    return v8::Undefined();
}

/* file(name [, callback]): the grammar in the application's file `name` (relative to its
 * folder). Accepts one argument, or two when the last is a function, which gets the result as
 * string()'s does. */
static v8::Handle<v8::Value> js_asr_file(const v8::Arguments &args)
{
    if (!(args.Length() == 2 && args[args.Length() - 1]->IsFunction()) && args.Length() != 1)
        return js_throw("Wrong arguments");

    v8::String::Utf8Value name(args[0]->ToString());
    if (path_refused(*name)) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "access denied: %s", *name);
        return v8::ThrowException(v8::String::Concat(v8::String::New("access denied: "), args[0]->ToString()));
    }
    KarotzData *karotz = interactive_karotz();
    if (karotz == NULL)
        return v8::Undefined();  /* "Must be in interactive mode" thrown */

    char path[512];
    snprintf(path, sizeof path, "%s/%s/%s", karotz->options->app_folder, karotz->app_name.c_str(), *name);
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "ASR: grammarFile[%s]", path);
    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->set_interactiveid(karotz->interactive_id);
    msg->mutable_asr()->set_file(path);
    if (args.Length() == 2)
        link_send_with_callback(karotz->socket, *msg,
                                new ReplyCallback(asr_callback, store_function(args[1]), release_stored_function));
    else
        link_send(karotz->socket, *msg);
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> asr_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "file", js_asr_file);
    js_set_function(tmpl, "string", js_asr_string);
    return tmpl;
}
