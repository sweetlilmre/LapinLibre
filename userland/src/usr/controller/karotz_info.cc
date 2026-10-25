// SPDX-License-Identifier: MIT
/*
 * The Karotz's details for the platform (KarotzInfoResponse): MAC and IP
 * addresses, flash usage, firmware versions and language.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/statfs.h>

#include "controller.h"

/* The first line of a file (at most size - 1 bytes) without its line end, or NULL. */
static void read_first_line(const char *path, int size, char **value)
{
    FILE *file = fopen(path, "r");

    if (file == NULL) {
        *value = NULL;
        return;
    }
    *value = (char *)malloc(size);
    if (fgets(*value, size, file) == NULL) {
        /* An empty file gives "". */
        (*value)[0] = '\0';
    }
    (*value)[strcspn(*value, "\r\n")] = '\0';
    fclose(file);
}

/* ifconfig and route by full path: they are in /sbin, which a controller
 * started from a login shell may not have on its PATH (the address then
 * comes back empty). The other tools are in /bin. */
void get_mac_address(char **mac)
{
    if (system("/sbin/ifconfig wlan0 | head -n1 | awk -F' ' '{ print $5 }' > /tmp/mac_addr") != 0) {
        /* The status is awk's: a failure shows as an empty file, read as "". */
    }
    read_first_line("/tmp/mac_addr", 18, mac);
}

/* The address of the default route's interface. */
void get_ip_address(char **ip)
{
    if (system("/sbin/ifconfig `/sbin/route | head -n3 | tail -n1 | awk -F' ' '{ print $8 }'` | grep inet | awk -F: '{ print $2 "
               "}' | awk '{ print $1 }' > /tmp/ip_addr") != 0) {
        /* The status is awk's: a failure shows as an empty file, read as "". */
    }
    read_first_line("/tmp/ip_addr", 16, ip);
}

/* Used and total size of /usr in KiB (block counts truncated to 20 bits, times 4), or -1. */
#define BLOCK_COUNT_MASK 0xfffff
#define KIB_PER_BLOCK_SHIFT 2 /* 4 KiB blocks */
void get_flash_usage(int *used, int *size)
{
    struct statfs stats;

    if (statfs("/usr", &stats) == 0) {
        *used = ((stats.f_blocks - stats.f_bavail) & BLOCK_COUNT_MASK) << KIB_PER_BLOCK_SHIFT;
        *size = (stats.f_blocks & BLOCK_COUNT_MASK) << KIB_PER_BLOCK_SHIFT;
    } else {
        *used = -1;
        *size = -1;
    }
}

static void add_info(pm::KarotzInfoResponse *response, const char *key, const char *value)
{
    pm::KeyValue *info = response->add_infos();

    info->set_key(key);
    info->set_value(value);
}

void karotz_info_fill(pm::PlatformMessage *msg)
{
    pm::KarotzInfoResponse *response = msg->mutable_karotzinfo_response();
    char number[50];
    char *value;
    int used = 0, size = 0;

    get_mac_address(&value);
    if (value != NULL) {
        add_info(response, "mac_address", value);
        free(value);
    }
    get_flash_usage(&used, &size);
    if (used != -1 && size != -1) {
        sprintf(number, "%d", size);
        add_info(response, "set_hard_drive_size", number);
        sprintf(number, "%d", used);
        add_info(response, "set_hard_drive_used", number);
    }
    get_ip_address(&value);
    if (value != NULL) {
        add_info(response, "ip_address", value);
        free(value);
    }
    read_first_line("/etc/conf/rootfs_version", 20, &value);
    if (value != NULL) {
        add_info(response, "rootfs_version", value);
        free(value);
    }
    read_first_line("/etc/conf/rootfs_status", 10, &value);
    if (value == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Rootfs status error");
    } else {
        add_info(response, "rootfs_status", value);
        free(value);
    }
    read_first_line("/etc/conf/sys_version", 20, &value);
    if (value != NULL) {
        add_info(response, "sys_version", value);
        free(value);
    }
    value = controller_get_config(global_controller, "lang", "en-US");
    add_info(response, "lang", value);
    free(value);
}
