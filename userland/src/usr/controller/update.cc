// SPDX-License-Identifier: MIT
/*
 * Firmware update check, done when the Karotz goes to sleep: the versions
 * published at url_update are compared with the installed ones and the
 * Karotz reboots (to update) when a newer one exists.
 *
 * Version files hold "YY.MM.DD.HH..." stamps; the published files carry it
 * on their fourth line.
 */
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <curl/curl.h>

#include "controller.h"

/* Two decimal digits. */
static int two_digits(char tens, char units)
{
    char text[3];

    sprintf(text, "%c%c", tens, units);
    text[2] = '\0';
    return atoi(text);
}

/* TRUE when `available` (YY.MM.DD.HH) is newer than `installed`. */
static gboolean is_newer(const char *installed, const char *available)
{
    int a_year = two_digits(available[0], available[1]);
    int a_month = two_digits(available[3], available[4]);
    int a_day = two_digits(available[6], available[7]);
    int a_hour = two_digits(available[9], available[10]);
    int i_year = two_digits(installed[0], installed[1]);
    int i_month = two_digits(installed[3], installed[4]);
    int i_day = two_digits(installed[6], installed[7]);
    int i_hour = two_digits(installed[9], installed[10]);

    if (i_year < a_year)
        return TRUE;
    if (a_year < i_year)
        return FALSE;
    if (i_month < a_month)
        return TRUE;
    if (a_month < i_month)
        return FALSE;
    if (i_day < a_day)
        return TRUE;
    if (a_day == i_day && i_hour < a_hour)
        return TRUE;
    return FALSE;
}

/* Fetch <url_update>/rootfs_version and /yaffs_version into /tmp. */
static gboolean download_versions(ControllerObject *controller)
{
    char yaffs_url[255];
    char rootfs_url[255];
    CURL *curl;
    FILE *rootfs;
    FILE *yaffs;
    char *base;
    CURLcode result;

    memset(rootfs_url, 0, sizeof rootfs_url);
    memset(yaffs_url, 0, sizeof yaffs_url);
    curl = curl_easy_init();
    if (curl == NULL)
        return FALSE;
    base = controller_get_config(controller, "url_update", "http://127.0.0.1:1");
    snprintf(rootfs_url, sizeof rootfs_url, "%s/rootfs_version", base);
    snprintf(yaffs_url, sizeof yaffs_url, "%s/yaffs_version", base);
    free(base);
    rootfs = fopen("/tmp/rootfs_version", "w");
    yaffs = fopen("/tmp/yaffs_version", "w");
    if (rootfs == NULL || yaffs == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Error occurred during rootfs/yaffs tmp file creation");
        curl_easy_cleanup(curl);
        return FALSE;
    }
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "libcurl-agent/1.0 karotz");
    /* An HTTP error page (a 404 for a missing file) is a
     * failed download, not a version file. */
    curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
    curl_easy_setopt(curl, CURLOPT_URL, rootfs_url);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, fwrite);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, rootfs);
    result = curl_easy_perform(curl);
    if (result != CURLE_OK) {
        curl_easy_cleanup(curl);
        g_log(NULL, G_LOG_LEVEL_WARNING, "Error occurred during downloading rootfs_version file");
        /* Both files are closed. */
        fclose(rootfs);
        fclose(yaffs);
        return FALSE;
    }
    fflush(rootfs);
    fclose(rootfs);
    curl_easy_setopt(curl, CURLOPT_URL, yaffs_url);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, fwrite);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, yaffs);
    /* The second download's result is kept, so a failed yaffs_version is
     * noticed. */
    result = curl_easy_perform(curl);
    fflush(yaffs);
    fclose(yaffs);
    if (result != CURLE_OK) {
        curl_easy_cleanup(curl);
        g_log(NULL, G_LOG_LEVEL_WARNING, "Error occurred during downloading yaffs_version file");
        return FALSE;
    }
    curl_easy_cleanup(curl);
    return TRUE;
}

/* A version as "YY.MM.DD.HH" ("12.07.19.00"). */
static gboolean is_version(const char *v)
{
    for (int i = 0; i < 11; i++) {
        if (i == 2 || i == 5 || i == 8 ? v[i] != '.' : !isdigit((unsigned char)v[i]))
            return FALSE;
    }
    return TRUE;
}

/* The 4th line of a version file into version[12]; FALSE if it has fewer. */
static gboolean fourth_line(FILE *file, char version[12])
{
    char line[258];

    for (int i = 0; i < 4; i++) {
        if (fgets(line, 255, file) == NULL)
            return FALSE;
    }
    strncpy(version, line, 11);
    version[11] = '\0';
    return TRUE;
}

/* Always returns TRUE. */
gboolean controller_check_update(ControllerObject *controller)
{
    char installed_sys[12] = "";
    char installed_rootfs[12] = "";
    char available_yaffs[12] = "";
    char available_rootfs[12] = "";
    FILE *rootfs, *yaffs, *rootfs_version, *sys_version;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "Checking Karotz update");
    if (!download_versions(controller))
        return TRUE;
    rootfs = fopen("/tmp/rootfs_version", "r");
    yaffs = fopen("/tmp/yaffs_version", "r");
    rootfs_version = fopen("/etc/conf/rootfs_version", "r");
    sys_version = fopen("/etc/conf/sys_version", "r");
    /* Nothing is compared unless all four opened (a missing one ends the
     * check silently). The versions are only compared when all four were
     * also read and look like versions: a short or empty yaffs_version (a
     * failed download) must not cause a reboot for nothing at every check. */
    gboolean all_open = rootfs != NULL && yaffs != NULL && rootfs_version != NULL && sys_version != NULL;
    gboolean ok = all_open && fgets(installed_rootfs, sizeof installed_rootfs, rootfs_version) != NULL &&
                  fgets(installed_sys, sizeof installed_sys, sys_version) != NULL &&
                  fourth_line(rootfs, available_rootfs) && fourth_line(yaffs, available_yaffs) &&
                  is_version(installed_rootfs) && is_version(installed_sys) &&
                  is_version(available_rootfs) && is_version(available_yaffs);
    /* Every file that opened is closed, whichever are missing. */
    FILE *files[] = { rootfs, yaffs, rootfs_version, sys_version };
    for (size_t i = 0; i < sizeof files / sizeof files[0]; i++) {
        if (files[i] != NULL)
            fclose(files[i]);
    }
    if (!all_open)
        return TRUE;
    if (!ok) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "update check: version files unreadable or malformed, skipped");
        return TRUE;
    }
    gboolean rootfs_newer = is_newer(installed_rootfs, available_rootfs);
    gboolean yaffs_newer = is_newer(installed_sys, available_yaffs);
    if (rootfs_newer || yaffs_newer) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "REBOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOT !");
        /* By full path, as for ifconfig (karotz_info.cc): PATH may lack /sbin. */
        if (system("/sbin/reboot") != 0)
            g_log(NULL, G_LOG_LEVEL_WARNING, "reboot failed");
    }
    return TRUE;
}
