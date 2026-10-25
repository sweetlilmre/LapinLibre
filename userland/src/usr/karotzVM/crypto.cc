// SPDX-License-Identifier: MIT
/*
 * SHA-1, HMAC-SHA1 and base64 (see crypto.h).
 */
#include <ctype.h>
#include <string.h>

#include "crypto.h"

/* FIPS 180 SHA-1: the initial hash value and the round constants. */
#define SHA1_H0 0x67452301
#define SHA1_H1 0xefcdab89
#define SHA1_H2 0x98badcfe
#define SHA1_H3 0x10325476
#define SHA1_H4 0xc3d2e1f0
#define SHA1_K_00_19 0x5a827999
#define SHA1_K_20_39 0x6ed9eba1
#define SHA1_K_40_59 0x8f1bbcdc
#define SHA1_K_60_79 0xca62c1d6

/* RFC 2104 HMAC pad bytes. */
#define HMAC_IPAD 0x36
#define HMAC_OPAD 0x5c

static inline uint32_t rol(uint32_t value, unsigned bits)
{
    return (value << bits) | (value >> (32 - bits));
}

/* Starts a new hash (the buffer is left as it is). */
void Sha1::reset()
{
    state[0] = SHA1_H0;
    state[1] = SHA1_H1;
    state[2] = SHA1_H2;
    state[3] = SHA1_H3;
    state[4] = SHA1_H4;
    count[0] = 0;
    count[1] = 0;
}

/* One 64-byte block. */
void Sha1::transform(const unsigned char block[64])
{
    uint32_t w[80];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)block[4 * i] << 24 | (uint32_t)block[4 * i + 1] << 16 |
               (uint32_t)block[4 * i + 2] << 8 | (uint32_t)block[4 * i + 3];
    for (int i = 16; i < 80; i++)
        w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);

    uint32_t a = state[0], b = state[1], c = state[2], d = state[3], e = state[4];
    for (int i = 0; i < 80; i++) {
        uint32_t f, k;
        if (i < 20) {
            f = ((c ^ d) & b) ^ d;
            k = SHA1_K_00_19;
        } else if (i < 40) {
            f = b ^ c ^ d;
            k = SHA1_K_20_39;
        } else if (i < 60) {
            f = ((b | c) & d) | (b & c);
            k = SHA1_K_40_59;
        } else {
            f = b ^ c ^ d;
            k = SHA1_K_60_79;
        }
        uint32_t t = rol(a, 5) + f + e + w[i] + k;
        e = d;
        d = c;
        c = rol(b, 30);
        b = a;
        a = t;
    }
    state[0] += a;
    state[1] += b;
    state[2] += c;
    state[3] += d;
    state[4] += e;
}

/* Adds data to the hash. */
void Sha1::update(const unsigned char *data, size_t length)
{
    uint32_t used = (count[0] >> 3) & 63;
    uint32_t len = (uint32_t)length;

    count[0] += len << 3;
    if (count[0] < (len << 3))
        count[1]++;
    count[1] += len >> 29;

    uint32_t i;
    if (used + len > 63) {
        i = 64 - used;
        memcpy(buffer + used, data, i);
        transform(buffer);
        for (; i + 63 < len; i += 64)
            transform(data + i);
        used = 0;
    } else {
        i = 0;
    }
    memcpy(buffer + used, data + i, len - i);
}

/* Appends 0x80, zeros up to 56 mod 64, then the bit
 * count, and extracts the digest. The state is not wiped afterwards;
 * every use calls reset() first. */
void Sha1::final()
{
    unsigned char final_count[8];
    for (unsigned i = 0; i < 8; i++)
        final_count[i] = (unsigned char)(count[i >= 4 ? 0 : 1] >> ((3 - (i & 3)) * 8));

    static const unsigned char pad80 = 0x80, pad00 = 0;
    update(&pad80, 1);
    while ((count[0] & 504) != 448)
        update(&pad00, 1);
    update(final_count, 8);

    for (unsigned i = 0; i < 20; i++)
        hash[i] = (unsigned char)(state[i >> 2] >> ((3 - (i & 3)) * 8));
}

/* The digest of the last final(). */
void Sha1::digest(unsigned char out[20]) const
{
    memcpy(out, hash, 20);
}

/*
 * HMAC-SHA1 (RFC 2104) of text with key, into digest.
 * A key longer than 64 bytes is replaced by its SHA-1, the inner hash is
 * SHA1((key ^ 0x36..) || text), the result SHA1((key ^ 0x5c..) || inner).
 * The buffer is sized to the text, so any length of text can be signed.
 */
void hmac_sha1(const unsigned char *text, size_t text_length,
               const unsigned char *key, size_t key_length,
               unsigned char digest[20])
{
    unsigned char sha1_key[64];
    unsigned char ipad[64];
    unsigned char opad[64];
    unsigned char inner[20];
    Sha1 sha1;

    memset(sha1_key, 0, sizeof sha1_key);
    memset(ipad, HMAC_IPAD, sizeof ipad);
    memset(opad, HMAC_OPAD, sizeof opad);

    if ((int)key_length <= 64) {
        memcpy(sha1_key, key, key_length);
    } else {
        sha1.reset();
        sha1.update(key, key_length);
        sha1.final();
        sha1.digest(sha1_key);
    }

    /* inner: (key ^ ipad) || text */
    for (int i = 0; i < 64; i++)
        ipad[i] ^= sha1_key[i];
    unsigned char *append1 = new unsigned char[64 + text_length];
    memcpy(append1, ipad, 64);
    memcpy(append1 + 64, text, text_length);
    sha1.reset();
    sha1.update(append1, text_length + 64);
    sha1.final();
    sha1.digest(inner);
    delete[] append1;

    /* outer: (key ^ opad) || inner */
    for (int i = 0; i < 64; i++)
        opad[i] ^= sha1_key[i];
    unsigned char append2[64 + 20];
    memcpy(append2, opad, 64);
    memcpy(append2 + 64, inner, 20);
    sha1.reset();
    sha1.update(append2, sizeof append2);
    sha1.final();
    sha1.digest(digest);
}

/* A std::string, built by the static initialiser. */
static const std::string base64_chars =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* base64_encode */
std::string base64_encode(const unsigned char *data, size_t length)
{
    std::string result;
    unsigned char in[3];
    unsigned char out[4];
    int i = 0;

    while (length--) {
        in[i++] = *data++;
        if (i == 3) {
            out[0] = (in[0] & 0xfc) >> 2;
            out[1] = ((in[0] & 0x03) << 4) + ((in[1] & 0xf0) >> 4);
            out[2] = ((in[1] & 0x0f) << 2) + ((in[2] & 0xc0) >> 6);
            out[3] = in[2] & 0x3f;
            for (i = 0; i < 4; i++)
                result += base64_chars[out[i]];
            i = 0;
        }
    }
    if (i != 0) {
        for (int j = i; j < 3; j++)
            in[j] = 0;
        out[0] = (in[0] & 0xfc) >> 2;
        out[1] = ((in[0] & 0x03) << 4) + ((in[1] & 0xf0) >> 4);
        out[2] = ((in[1] & 0x0f) << 2) + ((in[2] & 0xc0) >> 6);
        out[3] = in[2] & 0x3f;
        for (int j = 0; j < i + 1; j++)
            result += base64_chars[out[j]];
        while (i++ < 3)
            result += '=';
    }
    return result;
}

/* is_base64 */
static bool is_base64(unsigned char c)
{
    return isalnum(c) || c == '+' || c == '/';
}

/* base64_decode. Quirk: the padding positions
 * of a short last group are looked up as '\0', i.e. npos truncated to
 * 0xff, which only affects bytes that are not output. Not called by
 * anything at the moment. */
std::string base64_decode(const std::string &encoded)
{
    size_t remaining = encoded.size();
    size_t pos = 0;
    int i = 0;
    unsigned char in[4];
    unsigned char out[3];
    std::string result;

    while (remaining-- && encoded[pos] != '=' && is_base64(encoded[pos])) {
        in[i++] = encoded[pos];
        pos++;
        if (i == 4) {
            for (i = 0; i < 4; i++)
                in[i] = (unsigned char)base64_chars.find(in[i]);
            out[0] = (in[0] << 2) + ((in[1] & 0x30) >> 4);
            out[1] = ((in[1] & 0x0f) << 4) + ((in[2] & 0x3c) >> 2);
            out[2] = ((in[2] & 0x03) << 6) + in[3];
            for (i = 0; i < 3; i++)
                result += out[i];
            i = 0;
        }
    }
    if (i != 0) {
        for (int j = i; j < 4; j++)
            in[j] = 0;
        for (int j = 0; j < 4; j++)
            in[j] = (unsigned char)base64_chars.find(in[j]);
        out[0] = (in[0] << 2) + ((in[1] & 0x30) >> 4);
        out[1] = ((in[1] & 0x0f) << 4) + ((in[2] & 0x3c) >> 2);
        out[2] = ((in[2] & 0x03) << 6) + in[3];
        for (int j = 0; j < i - 1; j++)
            result += out[j];
    }
    return result;
}
