// SPDX-License-Identifier: MIT
/*
 * MD5 for the platform authentication: libcrypto's MD5() when the OpenSSL
 * headers are available; without them (libssl-dev) GLib's checksum, which
 * gives the same digest, stands in.
 */
#include <stdio.h>
#include <string.h>

#include "controller.h"

/* Compilers without __has_include (the rabbit's gcc 4.3) come with OpenSSL. */
#ifdef __has_include
#if __has_include(<openssl/md5.h>)
#define HAVE_OPENSSL_MD5 1
#endif
#else
#define HAVE_OPENSSL_MD5 1
#endif

#ifdef HAVE_OPENSSL_MD5
#define OPENSSL_SUPPRESS_DEPRECATED
#include <openssl/md5.h>

/* Lower-case hex of md5(text). */
void md5_hex(const char *text, char hex[33])
{
    unsigned char digest[16] = { 0 };

    MD5((const unsigned char *)text, strlen(text), digest);
    for (int i = 0; i < 16; i++)
        sprintf(hex + i * 2, "%02x", digest[i]);
}
#else
void md5_hex(const char *text, char hex[33])
{
    gchar *digest = g_compute_checksum_for_string(G_CHECKSUM_MD5, text, -1);

    memcpy(hex, digest, 33);
    g_free(digest);
}
#endif
