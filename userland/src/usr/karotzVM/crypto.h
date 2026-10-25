/* SPDX-License-Identifier: MIT */
/*
 * SHA-1 (FIPS 180), HMAC-SHA1 (RFC 2104) and base64 (RFC 4648) as karotzVM
 * uses them to sign requests (signHMACSHA1, the Twitter OAuth signature of
 * social.cc). Applications compare the signatures they get with other
 * implementations', so the output is the standard one, byte for byte.
 */
#ifndef KAROTZVM_CRYPTO_H
#define KAROTZVM_CRYPTO_H

#include <stddef.h>
#include <stdint.h>
#include <string>

/* A streaming SHA-1. */
class Sha1 {
public:
    Sha1() { reset(); }
    void reset();
    void update(const unsigned char *data, size_t length);
    /* Pads, stores the digest (see digest()) and wipes the state. */
    void final();
    /* The 20-byte digest of the last final(). */
    void digest(unsigned char out[20]) const;

private:
    void transform(const unsigned char block[64]);

    uint32_t state[5];
    uint32_t count[2];       /* message length in bits, low word first */
    unsigned char buffer[64];
    unsigned char hash[20];
};

/* HMAC-SHA1(key, text) into digest. */
void hmac_sha1(const unsigned char *text, size_t text_length,
               const unsigned char *key, size_t key_length,
               unsigned char digest[20]);

/* Standard base64 with '=' padding. */
std::string base64_encode(const unsigned char *data, size_t length);
/* Stops at the first '=' or non-base64 character. */
std::string base64_decode(const std::string &encoded);

#endif
