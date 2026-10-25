// SPDX-License-Identifier: MIT
/*
 * Applying a configuration from the platform: download the applications
 * (zip archives from url_app) into /usr/karotz/apps, remove those no longer
 * configured, write the speech grammar URL and switch language.
 */
#include <ctype.h>
#include <dirent.h>
#include <fstream>
#include <netdb.h>
#include <set>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <curl/curl.h>
#include <libxml/parser.h>
#include <libxml/xpath.h>

#include "controller.h"

#define APPS_DIR "/usr/karotz/apps/"

static xmlXPathObjectPtr evaluate_xpath(xmlDocPtr doc, const char *expression)
{
    xmlXPathContextPtr context = xmlXPathNewContext(doc);
    xmlXPathObjectPtr result = xmlXPathEvalExpression((const xmlChar *)expression, context);
    xmlXPathFreeContext(context);
    return result;
}

/* An application's descriptor.xml. */
class Descriptor {
public:
    /* A descriptor that can't be parsed leaves doc NULL, and version() then
     * says NULL, so one corrupt descriptor doesn't stop the controller. */
    explicit Descriptor(const char *path) : root(NULL), versions(NULL)
    {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "loading descriptor");
        doc = xmlReadFile(path, NULL, 0);
        if (doc == NULL)
            return;
        root = xmlDocGetRootElement(doc);
        load_version();
    }

    /* Frees the document and the version query. */
    ~Descriptor()
    {
        if (doc != NULL)
            xmlFreeDoc(doc);
        if (versions != NULL)
            xmlXPathFreeObject(versions);
    }

    /* The <version> text, or NULL if the descriptor couldn't be read or has
     * none. */
    const char *version() const
    {
        if (versions == NULL || versions->nodesetval == NULL || versions->nodesetval->nodeNr < 1)
            return NULL;
        xmlNodePtr node = versions->nodesetval->nodeTab[0];
        if (node->children == NULL || node->children->content == NULL)
            return NULL;
        return (const char *)node->children->content;
    }

private:
    void load_version() { versions = evaluate_xpath(doc, "/descriptor/version"); }

    xmlDocPtr doc;
    xmlNodePtr root;
    xmlXPathObjectPtr versions;

    Descriptor(const Descriptor &);
    Descriptor &operator=(const Descriptor &);
};

/*
 * Download and unpack an application unless the installed descriptor has the
 * configured version, then store its configuration as config.bin. The
 * descriptor object and the configuration's buffer are freed.
 */
/*
 * An application key names a directory under APPS_DIR and goes into the
 * unzip shell command, so it may only hold letters, digits, '-', '_' and
 * '.', without "..": otherwise a key such as "x; rm -rf /usr" would run as
 * root, and "../../etc" would reach outside the applications folder. Real
 * keys are UUIDs.
 */
static bool safe_apikey(const std::string &key)
{
    if (key.empty() || key.size() > 64 || key.find("..") != std::string::npos)
        return false;
    for (size_t i = 0; i < key.size(); i++) {
        unsigned char c = (unsigned char)key[i];
        if (!isalnum(c) && c != '-' && c != '_' && c != '.')
            return false;
    }
    return true;
}

static void download_app(const char *url_app, const pm::ConfigApp &app)
{
    char path[255];
    char url[255];
    gboolean download = TRUE;

    if (!safe_apikey(app.apikey())) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "download_app: unsafe application key ignored: %s", app.apikey().c_str());
        return;
    }
    memset(url, 0, sizeof url);
    /* The archive is /tmp/file plus six characters, made by mkstemp() so
     * there is no race on the name. */
    char archive[] = "/tmp/fileXXXXXX";
    FILE *output = fdopen(mkstemp(archive), "w");
    snprintf(path, sizeof path, APPS_DIR "%s/descriptor.xml", app.apikey().c_str());
    FILE *descriptor_file = fopen(path, "r");
    if (descriptor_file == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "download_app: no descriptor: download");
    } else {
        fclose(descriptor_file);
        Descriptor *descriptor = new Descriptor(path);
        const char *version = descriptor->version();
        if (version == NULL) {
            /* Downloaded again, which replaces it. */
            g_log(NULL, G_LOG_LEVEL_WARNING, "download_app: unreadable descriptor or no version: download again");
        } else {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "download_app: version: %s", version);
            if (strcmp(version, app.version().c_str()) == 0) {
                g_log(NULL, G_LOG_LEVEL_DEBUG, "download_app: the installed version is the latest, not downloading");
                download = FALSE;
            }
        }
        delete descriptor;
    }
    if (download) {
        snprintf(url, sizeof url, "%s/file/%s/%s/get", url_app, app.apikey().c_str(), app.version().c_str());
        g_log(NULL, G_LOG_LEVEL_DEBUG, "calling url: %s", url);
        CURL *curl = curl_easy_init();
        curl_easy_setopt(curl, CURLOPT_URL, url);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, fwrite);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, output);
        curl_easy_setopt(curl, CURLOPT_USERAGENT, "libcurl-agent/1.0 karotz");
        curl_easy_perform(curl);
        curl_easy_cleanup(curl);
        fflush(output);
        snprintf(url, sizeof url, APPS_DIR "%s", app.apikey().c_str());
        int result = mkdir(url, 0775);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "mkdir returned %d", result);
        snprintf(url, sizeof url, "/bin/unzip -o %s -d " APPS_DIR "%s", archive, app.apikey().c_str());
        g_log(NULL, G_LOG_LEVEL_DEBUG, "executing: %s", url);
        if (system(url) != 0) {
            /* Logged, otherwise ignored: a broken
             * unpack is downloaded again at the next configuration, once its
             * descriptor doesn't read. */
            g_log(NULL, G_LOG_LEVEL_WARNING, "download_app: unzip failed for %s", app.apikey().c_str());
        }
    }
    remove(archive);
    fclose(output);
    size_t size = app.ByteSize();
    void *data = malloc(size);
    app.SerializeToArray(data, size);
    snprintf(url, sizeof url, APPS_DIR "%s/config.bin", app.apikey().c_str());
    FILE *config = fopen(url, "w");
    fwrite(data, size, 1, config);
    fclose(config);
    free(data);
}

/* /tmp/apps.abnf: the URL of the account's speech grammar, by IP address. */
static void write_grammar_url(ControllerObject *controller)
{
    char *lang = controller_get_config(controller, "lang", "en-US");
    char *domain = controller_get_config(controller, "domain_app", "127.0.0.1");
    char *id = controller_get_account(controller, "id", "");
    struct hostent *host = gethostbyname(domain);

    if (host == NULL) {
        /* Free them before returning. */
        free(id);
        free(domain);
        free(lang);
        return;
    }
    std::ofstream file;
    file.open("/tmp/apps.abnf", std::ios::out);
    if (!file.bad()) {
        file << "http://";
        const char *address = host->h_addr_list[0];
        if (address != NULL) {
            for (int i = 0; i < host->h_length; i++) {
                if (i > 0)
                    file << ".";
                file << (unsigned)(unsigned char)address[i];
            }
        }
        file << "/asr/grammar/" << id << ".abnf";
        file.close();
    }
    free(id);
    free(domain);
    free(lang);
}

/* Install the configuration's applications and language (UPDATING_CONF). */
void controller_load_configuration(ControllerObject *controller)
{
    GError *error = NULL;
    std::set<std::string> former;
    char path[255];

    g_log(NULL, G_LOG_LEVEL_DEBUG, "Loading configuration");
    if (!controller->sleeping)
        ledPulse(controller->led, NULL, LED_ORANGE, LED_OFF, PULSE_PERIOD, LED_FOREVER);
    char *url_app = g_key_file_get_value(controller->karotz_conf, "karotz", "url_app", &error);
    if (url_app == NULL || *url_app == '\0') {
        url_app = strdup("http://127.0.0.1:8080");
        g_clear_error(&error);
    }

    DIR *dir = opendir(APPS_DIR);
    if (dir == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "remove_old_apps: cannot open, creating it: " APPS_DIR);
        if (mkdir(APPS_DIR, 0775) == -1) {
            free(url_app);
            return;
        }
    } else {
        struct dirent *entry;
        while ((entry = readdir(dir)) != NULL) {
            if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0)
                g_log(NULL, G_LOG_LEVEL_DEBUG, "remove_old_apps: skipping . or ..");
            else
                former.insert(std::string(entry->d_name));
        }
        closedir(dir);
    }

    int n = controller->config->configapp_size();
    for (int i = 0; i < n; i++) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Download application %d/%d", i + 1, n);
        pm::ConfigApp app(controller->config->configapp(i));
        download_app(url_app, app);
        std::set<std::string>::iterator it = former.find(std::string(controller->config->configapp(i).apikey().c_str()));
        if (it == former.end())
            g_log(NULL, G_LOG_LEVEL_DEBUG, "Application was not installed before");
        else
            former.erase(it);
    }
    free(url_app);

    /* rmdir only removes empty directories, so this mostly logs errors. */
    g_log(NULL, G_LOG_LEVEL_DEBUG, "remove_old_apps: removing the applications no longer configured");
    for (std::set<std::string>::iterator it = former.begin(); it != former.end(); ++it) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "remove_old_apps set: rmdir: %s", it->c_str());
        snprintf(path, sizeof path, APPS_DIR "%s", it->c_str());
        if (rmdir(path) == -1)
            g_log(NULL, G_LOG_LEVEL_DEBUG, "remove_old_apps rmdir error");
    }

    write_grammar_url(controller);
    if (controller->config->has_lang()) {
        char *lang = controller_get_config(controller, "lang", "en-US");
        if (strcmp(lang, controller->config->lang().c_str()) != 0) {
            controller_set_config(controller, "lang", controller->config->lang().c_str());
            char *new_lang = controller_get_config(controller, "lang", "en-US");
            if (!controller_load_lang(controller, new_lang))
                g_log(NULL, G_LOG_LEVEL_WARNING, "Error: Karotz couldn't load language");
            free(new_lang);
        }
        free(lang);
    }
    controller->config_pending = FALSE;
}
