// SPDX-License-Identifier: MIT
/*
 * Small utilities: UUIDs and the file-name check of include(), file.read()
 * and the asr grammar files.
 */
#include <ctype.h>
#include <string.h>

#include <uuid/uuid.h>

#include "karotzvm.h"

/* uuid_generate + uuid_unparse, then lower-cased in place
 * (e2fsprogs' uuid_unparse already gives lower case). */
void make_uuid(char out[37])
{
    uuid_t uuid;

    uuid_generate(uuid);
    uuid_unparse(uuid, out);
    for (int i = 0; out[i] != '\0'; i++)
        out[i] = (char)tolower((unsigned char)out[i]);
}

/* A name is refused when it mentions descriptor.xml anywhere, or
 * holds a character other than [A-Za-z0-9_/-] or a '.' that does not follow
 * another '.' (so "../x" is refused but "a/./b" and ".x" are accepted). */
bool path_refused(const char *name)
{
    char previous = '\0';

    if (strstr(name, "descriptor.xml") != NULL)
        return true;
    for (int i = 0; name[i] != '\0'; i++) {
        unsigned char c = (unsigned char)name[i];
        bool allowed = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                       (c == '.' && previous != '.') || c == '_' || c == '-' || c == '/';
        if (!allowed)
            return true;
        previous = name[i];
    }
    return false;
}
