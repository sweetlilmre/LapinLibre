// SPDX-License-Identifier: MIT
/*
 * The global "social" object: access to the owner's Twitter, Facebook and
 * SoundCloud accounts, plus the global signHMACSHA1().
 *
 *   social.init(callback [, data])
 *       asks the controller for the account credentials (a PlatformMessage with an
 *       empty socialQuery, no interactiveId needed). When the reply arrives
 *       the credentials the descriptor grants (accesses "twitter",
 *       "facebook", "soundcloud") are installed on social, then
 *       callback(data) (or callback()) is called once:
 *         social.twitter.sign(baseString)    OAuth HMAC-SHA1 signature
 *         social.twitter.oauth_consumer_key, social.twitter.oauth_token
 *         social.facebook.get(url) / .post(url, params [, headers [, isForm]])
 *         social.soundcloud.get(url) / .post(url, params [, headers [, isForm]])
 *       get/post only accept https://<host>.facebook.com/... (resp.
 *       soundcloud.com) URLs and add the account's token (access_token /
 *       oauth_token) as a query parameter or post field; they return the
 *       response body like karotz.http.get/post.
 *   signHMACSHA1(text, key)  base64 of HMAC-SHA1(key, text)
 *
 * The secrets (consumer secret, token secret, tokens) are kept as hidden
 * values, invisible to JavaScript.
 *
 * The SHA-1 / HMAC / base64 code is in crypto.cc.
 */
#include <regex.h>
#include <stdio.h>
#include <string.h>

#include <string>
#include <vector>

#include <curl/curl.h>

#include "crypto.h"
#include "karotzvm.h"

/* ---- URL checks --------------------------------------------------------- */

/* The URL patterns, compiled on first use. */
static const char *const facebook_pattern = "^https://\\w*\\.facebook.com/.*$";
static const char *const soundcloud_pattern = "^https://\\w*\\.soundcloud.com/.*$";
static const char *const query_pattern = "\\?";
static regex_t facebook_regex;
static regex_t soundcloud_regex;
static regex_t query_regex;
static bool regexes_compiled = false;

/* Whether url matches pattern (one of the regexes above). A
 * pattern that fails to compile makes every check fail (and compiling is
 * tried again next time). */
static bool url_allowed(const char *url, regex_t *pattern)
{
    if (!regexes_compiled) {
        if (regcomp(&facebook_regex, facebook_pattern, REG_EXTENDED) != 0) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "bad pattern: '%s'\n", facebook_pattern);
            return false;
        }
        if (regcomp(&soundcloud_regex, soundcloud_pattern, REG_EXTENDED) != 0) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "bad pattern: '%s'\n", soundcloud_pattern);
            return false;
        }
        if (regcomp(&query_regex, query_pattern, REG_EXTENDED) != 0) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "bad pattern: '%s'\n", query_pattern);
            return false;
        }
        regexes_compiled = true;
    }
    int status = regexec(pattern, url, 0, NULL, 0);
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, " regexp match %d", status);
    return status == 0;
}

/* ---- secure requests ------------------------------------------------------ */

/* The hidden "token" of social.<account> (social.facebook,
 * social.soundcloud), as the wrappers fetch it before the request; an empty
 * handle until social.init has stored one. */
static v8::Local<v8::String> account_token(const char *account)
{
    v8::Local<v8::Object> social = v8::Context::GetCurrent()->Global()->Get(v8::String::New("social"))->ToObject();
    v8::Local<v8::Object> object = social->Get(v8::String::New(account))->ToObject();
    v8::Local<v8::Value> token = object->GetHiddenValue(v8::String::New("token"));
    if (token.IsEmpty())
        return v8::Local<v8::String>();
    return token->ToString();
}

/* Thrown by the wrappers when the account has no token yet. */
static v8::Handle<v8::Value> no_account(const char *account)
{
    char text[100];
    snprintf(text, sizeof text, "no %s account (call social.init first)", account);
    return js_throw(text);
}

/*
 * secureHttpPost: http post (url, params [, headers [, isForm]])
 * with token_name = token added to a clone of params, when url matches
 * pattern. Returns the content.
 */
static v8::Handle<v8::Value> secure_http_post(const v8::Arguments &args, regex_t *pattern,
                                              v8::Handle<v8::String> token, const char *token_name)
{
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "secureHttpPost");
    int argc = args.Length();
    if (argc < 2 || !js_is_string(args[0]) || !args[1]->IsObject())
        return js_throw("Signature error function");

    v8::String::AsciiValue url(args[0]->ToString());
    if (!url_allowed(*url, pattern))
        return js_throw("unauthorised url");

    /* The argument array holds all the arguments. argc is passed on
     * unchanged (so isForm needs exactly four). */
    std::vector<v8::Handle<v8::Value> > argv(argc);
    argv[0] = args[0];
    v8::Local<v8::Object> params = args[1]->ToObject()->Clone();
    params->Set(v8::String::New(token_name), token);
    argv[1] = params;
    for (int i = 2; i < argc; i++)
        argv[i] = args[i];
    v8::Handle<v8::Value> result = http_post_args(&argv[0], argc);
    /* When httppost threw (an undefined result), the exception reaches
     * the application. */
    if (result.IsEmpty() || !result->IsObject())
        return result;
    v8::Local<v8::Object> object = result->ToObject();
    return object->Get(v8::String::New("content"));
}

/*
 * secureHttpGet: http get of url + ('?' or '&') + token_name + "="
 * + token (not escaped), when url matches pattern. Further arguments
 * (headers) are ignored. Returns the content.
 */
static v8::Handle<v8::Value> secure_http_get(const v8::Arguments &args, regex_t *pattern,
                                             v8::Handle<v8::String> token, const char *token_name)
{
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "secureHttpGet");
    int argc = args.Length();
    if (argc < 1 || !js_is_string(args[0]))
        return js_throw("Signature error function");

    v8::String::AsciiValue url(args[0]->ToString());
    if (!url_allowed(*url, pattern))
        return js_throw("unauthorised url");

    int has_query = regexec(&query_regex, *url, 0, NULL, 0);
    std::string full_url(*url);
    if (has_query == 0)
        full_url += '&';
    else
        full_url += '?';
    full_url += token_name;
    full_url += "=";
    v8::String::AsciiValue token_text(token);
    full_url += *token_text;
    g_log(KVM_LOG, G_LOG_LEVEL_MESSAGE, "FacebookHttpGet URL %.*s", (int)strcspn(full_url.c_str(), "?"), full_url.c_str());

    v8::Handle<v8::Value> argv[1];
    argv[0] = v8::String::New(full_url.c_str());
    v8::Handle<v8::Value> result = http_get_args(argv, 1);
    v8::Local<v8::Object> object = result->ToObject();
    return object->Get(v8::String::New("content"));
}

/* social.facebook.get */
static v8::Handle<v8::Value> js_facebook_get(const v8::Arguments &args)
{
    v8::Local<v8::String> token = account_token("facebook");
    if (token.IsEmpty())
        return no_account("facebook");
    return secure_http_get(args, &facebook_regex, token, "access_token");
}

/* social.facebook.post */
static v8::Handle<v8::Value> js_facebook_post(const v8::Arguments &args)
{
    v8::Local<v8::String> token = account_token("facebook");
    if (token.IsEmpty())
        return no_account("facebook");
    return secure_http_post(args, &facebook_regex, token, "access_token");
}

/* social.soundcloud.get */
static v8::Handle<v8::Value> js_soundcloud_get(const v8::Arguments &args)
{
    v8::Local<v8::String> token = account_token("soundcloud");
    if (token.IsEmpty())
        return no_account("soundcloud");
    return secure_http_get(args, &soundcloud_regex, token, "oauth_token");
}

/* social.soundcloud.post */
static v8::Handle<v8::Value> js_soundcloud_post(const v8::Arguments &args)
{
    v8::Local<v8::String> token = account_token("soundcloud");
    if (token.IsEmpty())
        return no_account("soundcloud");
    return secure_http_post(args, &soundcloud_regex, token, "oauth_token");
}

/* ---- Twitter --------------------------------------------------------------- */

/* urlEncode: curl_easy_escape (to be released with curl_free), or NULL
 * when it fails (out of memory). */
static char *url_encode(const char *text)
{
    CURL *curl = curl_easy_init();
    char *encoded = curl_easy_escape(curl, text, strlen(text));
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "urlEncode: Encoded secret: %d characters\n", encoded ? (int)strlen(encoded) : 0);
    curl_easy_cleanup(curl);
    return encoded;
}

/*
 * social.twitter.sign(baseString): the OAuth 1.0 signature,
 * base64(HMAC-SHA1(urlencode(consumersecret) + "&" +
 * urlencode(tokensecret), baseString)).
 */
static v8::Handle<v8::Value> js_twitter_sign(const v8::Arguments &args)
{
    if (args.Length() != 1 || !js_is_string(args[0])) {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "TwitterSign Wrong arguments");
        return js_throw("Wrong arguments");
    }

    v8::Local<v8::Object> social = v8::Context::GetCurrent()->Global()->Get(v8::String::New("social"))->ToObject();
    v8::Local<v8::Object> twitter = social->Get(v8::String::New("twitter"))->ToObject();
    /* The secrets are hidden values social.init stores: until then there
     * are none (empty handles). */
    v8::Local<v8::Value> consumer_value = twitter->GetHiddenValue(v8::String::New("consumersecret"));
    v8::Local<v8::Value> token_value = twitter->GetHiddenValue(v8::String::New("tokensecret"));
    if (consumer_value.IsEmpty() || token_value.IsEmpty())
        return js_throw("TwitterSign: no Twitter account (call social.init first)");
    v8::String::AsciiValue consumer_secret(consumer_value->ToString());
    v8::String::AsciiValue token_secret(token_value->ToString());

    /* url_encode() fails only when out of memory: thrown rather than
     * appended (a NULL appended to a std::string crashes). */
    std::string key("");
    char *encoded = url_encode(*consumer_secret);
    if (encoded == NULL)
        return js_throw("TwitterSign: encoding failed");
    key.append(encoded);
    curl_free(encoded);
    key.append("&");
    encoded = url_encode(*token_secret);
    if (encoded == NULL)
        return js_throw("TwitterSign: encoding failed");
    key.append(encoded);
    curl_free(encoded);

    unsigned char digest[20];
    memset(digest, 0, sizeof digest);
    v8::String::AsciiValue text(args[0]->ToString());
    v8::String::AsciiValue key_text(v8::String::New(key.c_str()));
    hmac_sha1(reinterpret_cast<const unsigned char *>(*text), strlen(*text),
              reinterpret_cast<const unsigned char *>(*key_text), strlen(*key_text), digest);
    std::string signature = base64_encode(digest, 20);
    return v8::String::New(signature.c_str());
}

/* signHMACSHA1(text, key): base64(HMAC-SHA1(key, text)), both taken
 * as ASCII strings. */
v8::Handle<v8::Value> js_sign_hmac_sha1(const v8::Arguments &args)
{
    if (args.Length() != 2)
        return js_throw("SignHMACSHA1: Wrong arguments");

    unsigned char digest[20];
    memset(digest, 0, sizeof digest);
    v8::String::AsciiValue text(args[0]->ToString());
    v8::String::AsciiValue key(args[1]->ToString());
    hmac_sha1(reinterpret_cast<const unsigned char *>(*text), strlen(*text),
              reinterpret_cast<const unsigned char *>(*key), strlen(*key), digest);
    std::string signature = base64_encode(digest, 20);
    return v8::String::New(signature.c_str());
}

/* ---- social.init ----------------------------------------------------------- */

/* What init keeps until the reply. */
struct SocialInit {
    v8::Persistent<v8::Function> function;
    v8::Persistent<v8::Object> data;      /* empty without data */
    v8::Persistent<v8::Context> context;  /* the context init was called in */
};

/* ReplyRelease for a SocialInit that never got its reply. */
static void release_social_init(void *data)
{
    SocialInit *init = static_cast<SocialInit *>(data);

    init->function.Dispose();
    init->data.Dispose();
    init->context.Dispose();
    delete init;
}

/* parent[name] when it exists (as an object), otherwise a new
 * object stored there. */
static v8::Local<v8::Object> get_or_create(v8::Handle<v8::Object> parent, const char *name)
{
    v8::Local<v8::String> key = v8::String::New(name);
    if (!parent->Has(key)) {
        v8::Local<v8::Object> object = v8::Object::New();
        parent->Set(key, object);
        return object;
    }
    return parent->Get(key)->ToObject();
}

/* FunctionTemplate::New(callback)->GetFunction(). */
static v8::Local<v8::Function> new_function(v8::InvocationCallback callback)
{
    return v8::FunctionTemplate::New(callback)->GetFunction();
}

/*
 * The reply to init's socialQuery. Inside init's context, installs
 * the credentials of the reply's Social (when it has one) the descriptor
 * grants, calls the callback once (receiver: the function itself) and
 * frees everything. The handler is registered one-shot, so it is removed
 * after the first reply and a repeat finds no correlation. Repeated and
 * stray replies have not been tested hard yet.
 */
static void social_reply(void *data, const pm::PlatformMessage &reply)
{
    SocialInit *init = static_cast<SocialInit *>(data);

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "getSocialCallbackFunction");
    /* The scope gets a local handle of its own: init->context is disposed
     * of (and init freed) below, before the scope leaves the context. */
    v8::Local<v8::Context> context = v8::Local<v8::Context>::New(init->context);
    v8::Context::Scope context_scope(context);
    KarotzData *karotz = current_karotz();

    if (reply.has_social()) {
        v8::Local<v8::Object> social = init->context->Global()->Get(v8::String::New("social"))->ToObject();
        const pm::Social &credentials = reply.social();

        if (karotz->descriptor->has_access("twitter") &&
            credentials.has_twitterconsumerkey() && credentials.has_twitterconsumersecret() &&
            credentials.has_twittertoken() && credentials.has_twittertokensecret()) {
            v8::Local<v8::Object> twitter = get_or_create(social, "twitter");
            twitter->Set(v8::String::New("sign"), new_function(js_twitter_sign));
            twitter->Set(v8::String::New("oauth_consumer_key"),
                         v8::String::New(credentials.twitterconsumerkey().c_str()));
            twitter->Set(v8::String::New("oauth_token"), v8::String::New(credentials.twittertoken().c_str()));
            twitter->SetHiddenValue(v8::String::New("consumersecret"),
                                    v8::String::New(credentials.twitterconsumersecret().c_str()));
            twitter->SetHiddenValue(v8::String::New("tokensecret"),
                                    v8::String::New(credentials.twittertokensecret().c_str()));
        }

        if (karotz->descriptor->has_access("facebook") && credentials.has_facebooktoken()) {
            v8::Local<v8::Object> facebook = get_or_create(social, "facebook");
            facebook->SetHiddenValue(v8::String::New("token"), v8::String::New(credentials.facebooktoken().c_str()));
            facebook->Set(v8::String::New("get"), new_function(js_facebook_get));
            facebook->Set(v8::String::New("post"), new_function(js_facebook_post));
        }

        if (karotz->descriptor->has_access("soundcloud") && credentials.has_soundcloudtoken()) {
            v8::Local<v8::Object> soundcloud = get_or_create(social, "soundcloud");
            soundcloud->SetHiddenValue(v8::String::New("token"),
                                       v8::String::New(credentials.soundcloudtoken().c_str()));
            soundcloud->Set(v8::String::New("get"), new_function(js_soundcloud_get));
            soundcloud->Set(v8::String::New("post"), new_function(js_soundcloud_post));
        }
    }

    if (!init->data.IsEmpty()) {
        v8::Handle<v8::Value> argv[1];
        argv[0] = init->data;
        init->function->Call(init->function, 1, argv);
    } else {
        init->function->Call(init->function, 0, NULL);
    }

    init->function.Dispose();
    init->function.Clear();
    init->data.Dispose();
    init->data.Clear();
    init->context.Dispose();
    init->context.Clear();
    delete init;
}

/*
 * social.init(callback [, data]): sends a PlatformMessage with an empty
 * socialQuery (no interactiveId; interactive mode is not required) and
 * calls back from social_reply.
 */
static v8::Handle<v8::Value> js_social_init(const v8::Arguments &args)
{
    v8::Handle<v8::Object> data;

    if (args.Length() < 1 || !args[0]->IsFunction())
        return js_throw("Wrong arguments");
    if (args.Length() == 2 && args[1]->IsObject())
        data = args[1]->ToObject();
    else if (args.Length() != 1)
        return js_throw("Wrong arguments");

    v8::Local<v8::Context> context = v8::Context::GetCurrent();
    KarotzData *karotz = current_karotz();
    v8::Persistent<v8::Function> function = v8::Persistent<v8::Function>::New(v8::Handle<v8::Function>::Cast(args[0]));
    SocialInit *init = new SocialInit;
    init->function = function;
    init->data = v8::Persistent<v8::Object>::New(data);
    init->context = v8::Persistent<v8::Context>::New(context);

    pm::PlatformMessage *msg = link_new_message(NULL);
    msg->mutable_socialquery();
    link_send_with_callback(karotz->socket, *msg, new ReplyCallback(social_reply, init, release_social_init, true));
    delete msg;
    return v8::Undefined();
}

v8::Handle<v8::ObjectTemplate> social_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "init", js_social_init);
    return tmpl;
}
