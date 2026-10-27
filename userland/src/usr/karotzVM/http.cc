// SPDX-License-Identifier: MIT
/*
 * http (a global, beside karotz): synchronous HTTP requests through libcurl.
 *
 *   http.get(url [, headers])                 -> body string
 *   http.get2(url [, headers])                -> {header, content}
 *   http.post(url, params [, headers [, isForm]])  -> body string
 *   http.post2(url, params [, headers [, isForm]]) -> {header, content}
 *
 * headers is a map {"Name": "value"} or an array of such maps; every entry
 * becomes a "Name: value" request header. post sends params either
 * urlencoded ("k1=v1&k2=v2", the default) or, with isForm true, as a
 * multipart form (string values, objects carrying a hidden "token", or
 * file-like objects whose internal field 1 is a path). A urlencoded post of
 * exactly one property whose value is a file-like object sends that file's
 * content as the body. The response body is converted to UTF-8 when the
 * first 200 bytes say encoding="..." or the headers say charset=... .
 * The calls block the whole VM until the transfer is over, so a server that
 * doesn't answer can't hold it for long: the connection must be made within
 * CONNECT_TIMEOUT_S, and a transfer slower than 1 byte a second for
 * STALL_TIMEOUT_S fails.
 */
#include <ctype.h>
#include <errno.h>
#include <iconv.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <fstream>
#include <string>
#include <vector>

#include <curl/curl.h>
#include <openssl/ssl.h>
#include <openssl/x509_vfy.h>

#include "karotzvm.h"

/* A growing buffer filled by the curl callbacks: {data, length}. */
struct CurlBuffer {
    char *data;
    size_t length;
};

/* The urlencoded body built by genCurlPostSimple (malloc'ed). length
 * stays -1 for a "k=v&..." string (curl then uses
 * strlen) and is the file size when a file is sent. */
struct PostData {
    char *data;
    int length;
};

/* CURLOPT_WRITEFUNCTION / HEADERFUNCTION, appends to a CurlBuffer
 * and keeps it NUL-terminated. */
static size_t curl_append(void *ptr, size_t size, size_t nmemb, void *userdata)
{
    CurlBuffer *buffer = static_cast<CurlBuffer *>(userdata);
    size_t new_length = buffer->length + size * nmemb;

    buffer->data = static_cast<char *>(realloc(buffer->data, new_length + 1));
    if (buffer->data == NULL) {
        g_log(KVM_LOG, G_LOG_LEVEL_CRITICAL, "realloc() failed");
        exit(1);
    }
    memcpy(buffer->data + buffer->length, ptr, size * nmemb);
    buffer->data[new_length] = '\0';
    buffer->length = new_length;
    return size * nmemb;
}

#define POST_INITIAL_SIZE 0x2000
#define CHARSET_SCAN_LENGTH 200 /* how far into the body to look for encoding= */
#define CONNECT_TIMEOUT_S 10     /* to make the connection */
#define STALL_TIMEOUT_S 30       /* below 1 byte a second, the transfer fails */

/* HTTPS servers' certificates are checked against the CA certificates in
 * CA_DIR (one file per certificate, named by its subject hash), or in
 * $SSL_CERT_DIR when that is set. A "no" in HTTPS_CHECK_FILE (the install's
 * HTTPS_CHECK setting) turns the checks off. */
#define CA_DIR "/etc/ssl/certs"
#define HTTPS_CHECK_FILE "/usr/etc/conf/https_check"
/* The rabbit's clock starts at 1970 at every boot until ntpd sets it: before
 * 2026-01-01 it counts as not set, and certificates' dates aren't checked. */
#define CLOCK_SET_AFTER 1767225600L

/* Whether HTTPS_CHECK_FILE leaves the certificate checks on. */
static bool https_check_on(void)
{
    char text[8] = "";
    FILE *f = fopen(HTTPS_CHECK_FILE, "r");
    if (f == NULL)
        return true;
    size_t n = fread(text, 1, sizeof text - 1, f);
    fclose(f);
    text[n] = '\0';
    return strncmp(text, "no", 2) != 0;
}

/* CURLOPT_SSL_CTX_FUNCTION while the clock isn't set: the chain and the
 * name are still checked, the dates aren't. */
static CURLcode skip_cert_dates(CURL *curl, void *ssl_ctx, void *userdata)
{
    (void)curl;
    (void)userdata;
    X509_VERIFY_PARAM_set_flags(SSL_CTX_get0_param(static_cast<SSL_CTX *>(ssl_ctx)), X509_V_FLAG_NO_CHECK_TIME);
    return CURLE_OK;
}

/* The certificate options of a request: checked against the CA directory
 * (curl's default CA file is left out), or not at all. */
static void set_cert_checks(CURL *curl)
{
    if (!https_check_on()) {
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 0L);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 0L);
        return;
    }
    const char *dir = getenv("SSL_CERT_DIR");
    curl_easy_setopt(curl, CURLOPT_CAINFO, (char *)NULL);
    curl_easy_setopt(curl, CURLOPT_CAPATH, dir != NULL && dir[0] != '\0' ? dir : CA_DIR);
    if (time(NULL) < CLOCK_SET_AFTER) {
        g_log(KVM_LOG, G_LOG_LEVEL_INFO, "the clock isn't set: certificate dates not checked");
        curl_easy_setopt(curl, CURLOPT_SSL_CTX_FUNCTION, skip_cert_dates);
    }
}

/*
 * genCurlPostSimple: "k1=v1&k2=v2" with curl_escape'd keys and
 * values (Utf8Value of their ToString()), or, for a single file-like
 * object, the file's content. NULL (nothing allocated) when that file can't
 * be read.
 */
static PostData *gen_curl_post_simple(v8::Handle<v8::Object> params)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "genCurlPostSimple");
    int used = 0;
    unsigned allocated = POST_INITIAL_SIZE;
    char *data = static_cast<char *>(malloc(POST_INITIAL_SIZE));
    *data = '\0';
    PostData *post = static_cast<PostData *>(malloc(sizeof(PostData)));

    v8::Local<v8::Array> keys = params->GetPropertyNames();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Post params: %d", keys->Length());
    post->length = -1;

    for (unsigned i = 0; i < keys->Length(); i++) {
        v8::Local<v8::Value> key = keys->Get(v8::Integer::New(i));
        if (key.IsEmpty())
            continue;
        v8::Local<v8::Value> value = params->Get(key);

        if (value->IsObject() && keys->Length() == 1) {
            /* A single file-like object: its content is the body. */
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "genCurlPostSimple: value->IsObject() && arrayKey->Length() == 1");
            v8::Handle<v8::Object> object = v8::Handle<v8::Object>::Cast(value);
            if (object->InternalFieldCount() > 1) {
                v8::String::AsciiValue path(v8::Handle<v8::String>::Cast(object->GetInternalField(1)));
                std::ifstream file(*path, std::ios::in);
                if (!file) {
                    /* Missing or unreadable: nothing is posted, and the
                     * caller throws. */
                    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "http.post: can't read %s: %s", *path, strerror(errno));
                    free(data);
                    free(post);
                    return NULL;
                }
                file.seekg(0, std::ios::end);
                int size = (int)(long long)file.tellg();
                file.seekg(0, std::ios::beg);
                /* (the only property, so nothing was appended before) */
                data = static_cast<char *>(realloc(data, size + allocated));
                /* The file is the body: read into the start of the buffer
                 * and NUL-terminated for the logs. curl is given the file
                 * size, so content holding NUL bytes is posted whole. */
                file.read(data, size);
                if (size >= 0)
                    data[size] = '\0';
                file.close();
                post->length = size;
                break;
            }
        } else {
            v8::String::Utf8Value name(key->ToString());
            v8::String::Utf8Value text(value->ToString());
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "%s => %s", *name, *text);
            char *escaped_name = curl_escape(*name, 0);
            char *escaped_text = curl_escape(*text, 0);
            size_t name_length = strlen(escaped_name);
            size_t text_length = strlen(escaped_text);
            size_t needed = used + name_length + text_length + 3;
            if (needed > allocated) {
                allocated = needed;
                g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "postSize realloc: %u bytes", allocated);
                data = static_cast<char *>(realloc(data, allocated));
            }
            /* sprintf(data, "%s%s=%s&", data, name, value) */
            char *end = data + used;
            memcpy(end, escaped_name, name_length);
            end += name_length;
            *end++ = '=';
            memcpy(end, escaped_text, text_length);
            end += text_length;
            *end++ = '&';
            *end = '\0';
            used += name_length + text_length + 2;
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "postSize: %d", used);
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "data: %d bytes", (int)strlen(data));
            curl_free(escaped_name);
            curl_free(escaped_text);
        }
    }

    if (post->length == -1) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "postSize: %d", used);
        /* Drops the trailing '&'. Without any parameter data is "". */
        if (used > 0)
            data[used - 1] = '\0';
    }
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "http data the end: %d bytes", (int)strlen(data));
    post->data = data;
    return post;
}

/* One part of a multipart form: a value, or the path of a file to upload. */
struct FormPart {
    std::string name;
    std::string value;
    bool file;
};

/*
 * A multipart form's parts, in the order of the properties. String values
 * are sent as they are; for object values, a hidden "token" string (e.g.
 * social.facebook) is sent as the value, otherwise internal field 1 of an
 * object with more than one internal field (a file or webcam resource) names
 * a file to upload. http_call() builds the curl form from them.
 */
static std::vector<FormPart> *gen_curl_form(v8::Handle<v8::Object> params)
{
    std::vector<FormPart> *form = new std::vector<FormPart>;

    v8::Local<v8::Array> keys = params->GetPropertyNames();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Post params: %d", keys->Length());
    for (unsigned i = 0; i < keys->Length(); i++) {
        v8::Local<v8::Value> key = keys->Get(v8::Integer::New(i));
        if (key.IsEmpty())
            continue;
        v8::Local<v8::Value> value = params->Get(key);

        if (!value->IsObject()) {
            v8::String::Utf8Value name(key->ToString());
            v8::String::Utf8Value text(value->ToString());
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "%s => %s", *name, *text);
            form->push_back(FormPart{ *name, *text, false });
            continue;
        }

        v8::Handle<v8::Object> object = v8::Handle<v8::Object>::Cast(value);
        v8::String::Utf8Value name(key->ToString());
        v8::Local<v8::Value> token = object->GetHiddenValue(v8::String::New("token"));
        if (!token.IsEmpty() && js_is_string(token)) {
            v8::String::AsciiValue token_text(token->ToString());
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "TOKEN: set (%d characters)", (int)strlen(*token_text));
            form->push_back(FormPart{ *name, *token_text, false });
        } else {
            int fields = object->InternalFieldCount();
            if (fields > 1) {
                v8::String::AsciiValue path(v8::Handle<v8::String>::Cast(object->GetInternalField(1)));
                form->push_back(FormPart{ *name, *path, true });
            }
        }
    }
    return form;
}

/*
 * The curl form of the parts (NULL for none, as no form at all). A file that
 * can't be read gives its error, before anything is sent.
 */
static CURLcode curl_form(CURL *curl, const std::vector<FormPart> &parts, curl_mime **mime)
{
    *mime = NULL;
    for (const FormPart &p : parts) {
        if (*mime == NULL)
            *mime = curl_mime_init(curl);
        curl_mimepart *part = curl_mime_addpart(*mime);
        curl_mime_name(part, p.name.c_str());
        CURLcode result = p.file ? curl_mime_filedata(part, p.value.c_str())
                                 : curl_mime_data(part, p.value.c_str(), CURL_ZERO_TERMINATED);
        if (result != CURLE_OK)
            return result;
    }
    return CURLE_OK;
}

/* Appends "Name: value" for every property of a header map. */
static curl_slist *add_header_map(v8::Handle<v8::Object> map, curl_slist *list)
{
    std::string header;

    v8::Local<v8::Array> keys = map->GetPropertyNames();
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "Headers: %d", keys->Length());
    for (unsigned i = 0; i < keys->Length(); i++) {
        v8::Local<v8::Value> key = keys->Get(v8::Integer::New(i));
        if (key.IsEmpty())
            continue;
        v8::Local<v8::Value> value = map->Get(key);
        v8::String::AsciiValue name(key->ToString());
        v8::String::AsciiValue text(value->ToString());
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "HEADER %s => %s", *name, *text);
        header.assign(*name);
        header += ": ";
        header += *text;
        list = curl_slist_append(list, header.c_str());
    }
    return list;
}

/* The request headers of a map or of an array of maps (non-object
 * array items are skipped); NULL without headers. */
static curl_slist *build_headers(v8::Handle<v8::Object> headers)
{
    curl_slist *list = NULL;

    if (headers.IsEmpty())
        return NULL;
    if (!headers->IsArray()) {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "header map");
        list = add_header_map(headers, list);
    } else {
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "header array");
        v8::Handle<v8::Array> array = v8::Handle<v8::Array>::Cast(headers);
        for (unsigned i = 0; i < array->Length(); i++) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "array [%d]", i);
            v8::Local<v8::Value> item = headers->Get(i);
            if (item->IsObject())
                list = add_header_map(item->ToObject(), list);
        }
    }
    return list;
}

/*
 * The size of data converted with cd, measured by
 * converting it into a scratch window that is reused and thrown away (E2BIG
 * only means the window is full: the input pointer carries on where it
 * stopped), plus what a stateful charset writes at the end. cd is reset
 * afterwards. 0, or -1 for input the converter rejects.
 */
static int utf8_size(iconv_t cd, char *data, size_t length, size_t *size)
{
    char scratch[256];
    char *in = data;
    size_t in_left = length;
    size_t total = 0;
    int result = 0;

    while (in_left > 0) {
        char *out = scratch;
        size_t out_left = sizeof scratch;
        size_t r = iconv(cd, &in, &in_left, &out, &out_left);
        total += sizeof scratch - out_left;
        if (r == (size_t)-1 && errno != E2BIG) {
            result = -1;
            break;
        }
    }
    if (result == 0) {
        char *out = scratch;
        size_t out_left = sizeof scratch;
        if (iconv(cd, NULL, NULL, &out, &out_left) == (size_t)-1)
            result = -1;
        total += sizeof scratch - out_left;
    }
    iconv(cd, NULL, NULL, NULL, NULL);
    *size = total;
    return result;
}

/*
 * httpCall: performs the request (a POST when post is not NULL: a PostData,
 * or with is_form the FormPart list of a multipart form, a GET when that
 * list is empty) and returns {header, content} when full is 1, otherwise the content
 * string. HTTPS certificates are checked as set_cert_checks says. On a curl error the content is
 * "curl error". A body that announces another charset is converted to
 * UTF-8 with libiconv (failures are reported with printf on stdout and
 * leave the body as it is).
 */
static v8::Handle<v8::Value> http_call(const char *url, curl_slist *headers, void *post, bool is_form, int full)
{
    CurlBuffer body = { NULL, 0 };
    CurlBuffer header = { NULL, 0 };
    char to_code[6];
    memcpy(to_code, "UTF-8", 6);
    /* A failed curl_easy_init counts as a curl error. */
    CURLcode result = CURLE_FAILED_INIT;
    char error_text[CURL_ERROR_SIZE] = "";

    CURL *curl = curl_easy_init();
    if (curl != NULL) {
        set_cert_checks(curl);
        curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, error_text);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, (long)CONNECT_TIMEOUT_S);
        curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, 1L);
        curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, (long)STALL_TIMEOUT_S);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curl_append);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
        curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, curl_append);
        curl_easy_setopt(curl, CURLOPT_WRITEHEADER, &header);
        curl_easy_setopt(curl, CURLOPT_URL, url);
        curl_easy_setopt(curl, CURLOPT_HEADER, 0L);
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_mime *mime = NULL;
        CURLcode form = CURLE_OK;
        if (post != NULL && is_form)
            form = curl_form(curl, *static_cast<std::vector<FormPart> *>(post), &mime);
        if (post != NULL && (!is_form || mime != NULL)) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "datapost != NULL");
            if (!is_form) {
                PostData *data = static_cast<PostData *>(post);
                g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "httpCall isForm = false: %s", data->data);
                g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "httpCall datapost.len: %d", data->length);
                curl_easy_setopt(curl, CURLOPT_POSTFIELDS, data->data);
                if (data->length != 0)
                    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)data->length);
            } else {
                curl_easy_setopt(curl, CURLOPT_MIMEPOST, mime);
            }
        }
        result = form == CURLE_OK ? curl_easy_perform(curl) : form;
        curl_easy_cleanup(curl);
        curl_mime_free(mime);
    }

    if (result == CURLE_OK) {
        /* Zeroed, so an empty sscanf result gives an empty charset. */
        unsigned char charset_buffer[100];
        memset(charset_buffer, 0, sizeof charset_buffer);
        unsigned char *charset = charset_buffer;

        size_t scan = CHARSET_SCAN_LENGTH;
        if (body.length < CHARSET_SCAN_LENGTH)
            scan = body.length;
        char *found = g_strstr_len(body.data, scan, "encoding");
        int skip = 9; /* encoding= */
        const char *end = body.data + body.length; /* the terminating NUL */
        if (found == NULL) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "encoding not in data");
            found = g_strstr_len(header.data, header.length, "charset");
            skip = 8; /* charset= */
            end = header.data + header.length;
        }
        /* Data ending with the bare word has nothing after it to skip
         * past, so the word is ignored. */
        if (found != NULL && found + skip > end) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "encoding at the very end: ignored");
            found = NULL;
        }
        if (found == NULL) {
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "encoding not in header");
        } else {
            found += skip;
            sscanf(found, "%50s", (char *)charset_buffer);
            /* Upper-cased, without surrounding quotes. */
            unsigned char c = charset_buffer[0];
            if (c == '"' || c == '\'')
                charset = charset_buffer + 1;
            else
                charset_buffer[0] = (unsigned char)toupper(c);
            for (int i = 1; (c = charset_buffer[i]) != 0; i++) {
                if (c == '"' || c == '\'') {
                    charset_buffer[i] = 0;
                    break;
                }
                charset_buffer[i] = (unsigned char)toupper(c);
            }
            g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "encoding: %s", (char *)charset);

            if (strcmp((char *)charset, "UTF-8") == 0) {
                g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "already UTF-8");
            } else {
                const char *from_code = (const char *)charset;
                char *in = body.data;
                size_t in_left = body.length;
                size_t out_left = 0;
                char *converted = NULL;
                char *out = NULL;
                iconv_t cd = iconv_open(to_code, from_code);
                /* The output is measured first (utf8_size), then the body
                 * is converted into exactly that much, so text of any mix of
                 * accented characters fits. */
                bool ok = false;
                if (cd == (iconv_t)-1) {
                    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "Failed to iconv_open %s to %s.\n", from_code, to_code);
                } else if (utf8_size(cd, body.data, body.length, &out_left) == 0) {
                    converted = static_cast<char *>(malloc(out_left ? out_left : 1));
                    if (converted == NULL) {
                        g_log(KVM_LOG, G_LOG_LEVEL_CRITICAL, "malloc fail");
                        exit(1);
                    }
                    out = converted;
                    ok = iconv(cd, &in, &in_left, &out, &out_left) != (size_t)-1 &&
                         iconv(cd, NULL, NULL, &out, &out_left) != (size_t)-1;
                }
                if (cd == (iconv_t)-1) {
                    /* reported above */
                } else if (!ok) {
                    /* Bad input: the body is left unconverted and the
                     * descriptor closed. */
                    g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "Fail to convert characters to new code set in remaining %d out remaining %d.\n",
                           (int)in_left, (int)out_left);
                    iconv_close(cd);
                } else {
                    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "in remaining %d out remaining %d.\n", (int)in_left, (int)out_left);
                    if (iconv_close(cd) == -1)
                        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "Fail to iconv_close.");
                    out_left = out - converted;
                    free(body.data);
                    body.data = converted;
                    body.length = out_left;
                    converted = NULL;
                }
                if (converted != NULL)
                    free(converted);
            }
        }
    } else {
        g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "curl error code: %d", result);
        if (result == CURLE_PEER_FAILED_VERIFICATION || result == CURLE_SSL_CACERT_BADFILE)
            g_log(KVM_LOG, G_LOG_LEVEL_WARNING, "certificate check failed: %s", error_text);
        body.data = static_cast<char *>(realloc(body.data, 11));
        if (body.data == NULL) {
            g_log(KVM_LOG, G_LOG_LEVEL_CRITICAL, "realloc() failed");
            exit(1);
        }
        body.length = 10;
        snprintf(body.data, 11, "curl error");
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "curl error: %s", body.data);
    }

    v8::Handle<v8::Value> value;
    if (full == 1) {
        v8::Local<v8::Object> object = v8::Object::New();
        object->Set(v8::String::New("header"), v8::String::New(header.data, header.length));
        object->Set(v8::String::New("content"), v8::String::New(body.data, body.length));
        value = object;
    } else {
        value = v8::String::New(body.data, body.length);
    }
    free(body.data);
    free(header.data);
    return value;
}

/*
 * httppost(argv, argc): argv[0] url string, argv[1] params object,
 * argv[2] optional headers object, argv[3] optional isForm boolean (only
 * when argc is exactly 4). Returns {header, content}. Also used by the
 * secure posts of social.cc (hence not static).
 */
v8::Handle<v8::Value> http_post_args(v8::Handle<v8::Value> argv[], int argc)
{
    v8::Handle<v8::Object> headers;
    bool is_form = false;

    if (argc < 2 || !js_is_string(argv[0]) || !argv[1]->IsObject())
        return js_throw("Signature error function");
    if (argc >= 3 && argv[2]->IsObject())
        headers = argv[2]->ToObject();
    if (argc == 4 && argv[3]->IsBoolean())
        is_form = argv[3]->ToBoolean()->Value();

    v8::String::AsciiValue url(argv[0]->ToString());
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "isForm %d", is_form);
    void *post;
    if (!is_form) {
        post = gen_curl_post_simple(argv[1]->ToObject());
        if (post == NULL)
            return js_throw("file not found");  /* the file to post can't be read */
    } else {
        post = gen_curl_form(argv[1]->ToObject());
    }
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "httppost to %s...", *url);
    curl_slist *list = build_headers(headers);
    v8::Handle<v8::Value> result = http_call(*url, list, post, is_form, 1);
    if (!is_form) {
        /* The data and the struct. */
        free(static_cast<PostData *>(post)->data);
        free(post);
    } else {
        delete static_cast<std::vector<FormPart> *>(post);
    }
    curl_slist_free_all(list);
    return result;
}

/*
 * httpget(argv, argc): argv[0] url string, argv[1] optional headers
 * object (only when argc is exactly 2). Returns {header, content}. Also
 * used by the secure gets of social.cc.
 */
v8::Handle<v8::Value> http_get_args(v8::Handle<v8::Value> argv[], int argc)
{
    v8::Handle<v8::Object> headers;

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "httpget...");
    if (argc < 1 || !js_is_string(argv[0]))
        return js_throw("Signature error function");
    if (argc == 2 && argv[1]->IsObject())
        headers = argv[1]->ToObject();

    v8::String::AsciiValue url(argv[0]->ToString());
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "httpget to %s...", *url);
    curl_slist *list = build_headers(headers);
    v8::Handle<v8::Value> result = http_call(*url, list, NULL, false, 1);
    curl_slist_free_all(list);
    return result;
}

/* post2: at most four arguments to httppost. */
static v8::Handle<v8::Value> js_http_post2(const v8::Arguments &args)
{
    v8::Handle<v8::Value> argv[4];
    int argc = args.Length();
    if (argc > 4)
        argc = 4;
    for (int i = 0; i < argc; i++)
        argv[i] = args[i];
    return http_post_args(argv, argc);
}

/* post: the content of post2. After an error (an argument error throws,
 * and returns undefined) the result is passed on as it is, so the exception
 * reaches the application. */
static v8::Handle<v8::Value> js_http_post(const v8::Arguments &args)
{
    v8::Handle<v8::Value> result = js_http_post2(args);
    if (result.IsEmpty() || !result->IsObject())
        return result;
    return result->ToObject()->Get(v8::String::New("content"));
}

/* get2: at most two arguments to httpget. */
static v8::Handle<v8::Value> js_http_get2(const v8::Arguments &args)
{
    v8::Handle<v8::Value> argv[2];
    int argc = args.Length();
    if (argc > 2)
        argc = 2;
    for (int i = 0; i < argc; i++)
        argv[i] = args[i];
    return http_get_args(argv, argc);
}

/* get: the content of get2 (an error is passed on as post's is). */
static v8::Handle<v8::Value> js_http_get(const v8::Arguments &args)
{
    v8::Handle<v8::Value> result = js_http_get2(args);
    if (result.IsEmpty() || !result->IsObject())
        return result;
    return result->ToObject()->Get(v8::String::New("content"));
}

v8::Handle<v8::ObjectTemplate> http_template()
{
    v8::Handle<v8::ObjectTemplate> tmpl = v8::ObjectTemplate::New();
    js_set_function(tmpl, "get", js_http_get);
    js_set_function(tmpl, "post", js_http_post);
    js_set_function(tmpl, "get2", js_http_get2);
    js_set_function(tmpl, "post2", js_http_post2);
    return tmpl;
}
