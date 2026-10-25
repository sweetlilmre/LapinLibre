/* SPDX-License-Identifier: MIT */
/*
 * rfid-daemon: report RFID tags laid on the rabbit's nose as D-Bus signals on
 * com.mindscape.karotz.Rfid (/com/mindscape/karotz/Rfid).
 *
 * The reader is a contactless coupler with the NXP MFRC522/PN512 register
 * map on /dev/i2c-0 (slave address 1), driven in ISO 14443-B mode to talk to
 * ST SRI512/SRIX tags. Every 100 ms the daemon sends INITIATE; a tag that
 * answers is SELECTed, its UID and all 16 blocks plus the system block are
 * read, and tag_in(uid, blocks, app_code, type_code, pict_code, color_code)
 * is emitted. Tags already reported are SELECTed again on each poll and
 * tag_out(uid) is emitted once one stops answering. A CRC error on INITIATE
 * (several tags answering at once) starts the 16-slot PCALL16/SLOT_MARKER
 * anti-collision loop.
 *
 * /etc/conf/rfid_conf.xml can ask for block 7 (the tag's product codes:
 * application, type, picture and colour) to be written into each new tag
 * before it is read; the nose LED then pulses cyan if the tag reads back
 * what the configuration asked for and red otherwise.
 *
 * getTags returns ";uid:blocks" for every tag on the reader.
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <syslog.h>
#include <unistd.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <expat.h>
#include <glib-object.h>

#include "karotz-common.h"
#include "karotz-messages.h"
#include "karotz-marshal.h"

#define CONF_PATH "/etc/conf/rfid_conf.xml"
#define I2C_DEVICE "/dev/i2c-0"
#define I2C_SLAVE 0x0703
#define READER_ADDRESS 1
#define POLL_MS 100

enum {
    SIGNAL_REGISTERED,
    SIGNAL_TAG_IN,
    SIGNAL_TAG_OUT,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    GSList *tags; /* RfidTag *, newest first */
    int write_in_tag;
    int app_code;
    int type_code;
    int pict_code;
    int color_code;
    DBusGProxy *led;
    DBusGConnection *bus;
} RfidObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} RfidObjectClass;

GType rfid_object_get_type(void);
#define RFID_TYPE_OBJECT (rfid_object_get_type())

G_DEFINE_TYPE(RfidObject, rfid_object, G_TYPE_OBJECT)

gboolean rfid_object_get_tags(RfidObject *self, char **tags, gboolean *result, GError **error);

#include "rfid-daemon-glue.h"

/* One tag on the reader. */
typedef struct {
    char chip_id;  /* the 8-bit id the tag picked in answer to INITIATE */
    GString *uid;  /* 16 hex digits, most significant byte first */
    GString *blocks; /* "%08X:" for blocks 0-15 and the system block 255 */
    int app_code;  /* decoded from block 7 */
    int type_code;
    int pict_code;
    int color_code;
} RfidTag;

/* ------------------------------------------------------------------------ */
/* I2C access to the coupler                                                 */

/* Coupler registers (MFRC522/PN512 numbering). */
enum {
    REG_COMMAND = 0x01,
    REG_COM_IRQ = 0x04,
    REG_ERROR = 0x06,
    REG_FIFO_DATA = 0x09,
    REG_FIFO_LEVEL = 0x0a,
    REG_CONTROL = 0x0c,
    REG_BIT_FRAMING = 0x0d,
    REG_TX_MODE = 0x12,
    REG_RX_MODE = 0x13,
    REG_TX_CONTROL = 0x14,
    REG_TX_ASK = 0x15,
    REG_RX_THRESHOLD = 0x18,
    REG_DEMOD = 0x19,
    REG_TYPE_B = 0x1e,
    REG_RF_CFG = 0x26,
    REG_GS_N = 0x27,
    REG_CW_GS_P = 0x28,
    REG_MOD_GS_P = 0x29,
    REG_T_MODE = 0x2a,
    REG_T_PRESCALER = 0x2b,
    REG_T_RELOAD_HI = 0x2c,
    REG_T_RELOAD_LO = 0x2d,
};

enum { CMD_TRANSCEIVE = 0x0c, CMD_SOFT_RESET = 0x0f };

/* ComIrqReg bits. */
enum { IRQ_TIMER = 0x01, IRQ_ERROR = 0x02, IRQ_RX = 0x20, IRQ_CLEAR_ALL = 0x7f };

/* Register bits and values (PN512 data sheet names). */
#define CONTROL_INITIATOR    0x10  /* ControlReg: act as reader */
#define FIFO_LEVEL_FLUSH     0x80  /* FIFOLevelReg: FlushBuffer */
#define BIT_FRAMING_START    0x80  /* BitFramingReg: StartSend */
#define MODE_CRC_EN          0x80  /* TxModeReg/RxModeReg: TxCRCEn/RxCRCEn */
#define MODE_FRAMING_14443B  0x03  /* TxModeReg/RxModeReg: ISO 14443-B framing */
#define TX_CONTROL_INV_TX2   0x80  /* TxControlReg: InvTx2RFOn */
#define TX_CONTROL_TX2_EN    0x02  /* TxControlReg: Tx2RFEn */
#define TX_CONTROL_TX1_EN    0x01  /* TxControlReg: Tx1RFEn */
#define RX_THRESHOLD_VALUE   0x55  /* MinLevel 5, CollLevel 5 */
#define DEMOD_VALUE          0x4d  /* AddIQ 1, TauRcv 3, TauSync 1 */
#define RF_CFG_RX_GAIN_48DB  0x70  /* RFCfgReg: RxGain 48 dB */
#define GS_N_VALUE           0xff  /* CWGsN 15, ModGsN 15: maximum n-driver conductance */
#define CW_GS_P_VALUE        0x3f  /* CWGsP 63: maximum p-driver conductance */
#define MOD_GS_P_VALUE       0x08  /* ModGsP 8: the modulation index for type B */

/* ErrorReg bits. */
enum {
    ERR_WRITE = 0x80,     /* WrErr */
    ERR_TEMP = 0x40,      /* TempErr */
    ERR_BUFFER_OVFL = 0x10,
    ERR_COLL = 0x08,
    ERR_CRC = 0x04,
    ERR_PARITY = 0x02,
    ERR_PROTOCOL = 0x01,
};

/* The timer: TModeReg's TAuto bit and the high nibble of the prescaler.
 * Prescaler 0x2a6: 13.56 MHz / (2 x 678 + 1) = 10 kHz, one tick per 100 us. */
#define T_MODE_TAUTO         0x80
#define T_MODE_PRESCALER_HI  0x0f
#define TIMER_PRESCALER      0x2a6
#define TIMER_TICK_US        100
#define RX_TIMEOUT_US        3000

/* Why transceive() failed. */
enum {
    STATUS_OK = 0, /* also: nothing received */
    STATUS_FIFO = 1, /* nothing to send */
    STATUS_TX_LENGTH = 2, /* the FIFO didn't take the whole frame */
    STATUS_RX_OVERFLOW = 3, /* answer longer than the buffer */
    STATUS_TIMEOUT = 4,
    STATUS_WRITE_ERROR = 5,
    STATUS_TEMP_ERROR = 6,
    STATUS_BUFFER_OVERFLOW = 7,
    STATUS_COLLISION = 8,
    STATUS_CRC = 9, /* what several SRI tags answering at once look like */
    STATUS_PARITY = 10,
    STATUS_PROTOCOL = 11,
};

static int i2c_fd;
static unsigned char i2c_buf[2]; /* shared transfer buffer */

static ssize_t i2c_write(const void *buf, size_t len)
{
    return write(i2c_fd, buf, len);
}

static ssize_t i2c_read(void *buf, size_t len)
{
    return read(i2c_fd, buf, len);
}

static void i2c_open(void)
{
    i2c_fd = open(I2C_DEVICE, O_RDWR);
    if (i2c_fd < 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "Can't open device " I2C_DEVICE);
        exit(1);
    }
    if (ioctl(i2c_fd, I2C_SLAVE, READER_ADDRESS) < 0)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Can't set slave address");
}

static void write_reg(unsigned char reg, unsigned char value)
{
    i2c_buf[0] = reg;
    i2c_buf[1] = value;
    if (i2c_write(i2c_buf, 2) == -1)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Write failed");
}

static unsigned char read_reg(unsigned char reg)
{
    i2c_buf[0] = reg;
    if (i2c_write(i2c_buf, 1) == -1)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Write failed");
    if (i2c_read(i2c_buf, 1) == -1)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Read failed");
    return i2c_buf[0];
}

/* Program the coupler's timer for a receive timeout of `us` microseconds. */
static void set_timeout(unsigned int us)
{
    unsigned char mode;
    unsigned int reload = us / TIMER_TICK_US;

    mode = read_reg(REG_T_MODE);
    write_reg(REG_T_MODE, mode | T_MODE_TAUTO);
    write_reg(REG_T_PRESCALER, TIMER_PRESCALER & 0xff);
    mode = read_reg(REG_T_MODE);
    write_reg(REG_T_MODE, (mode & ~T_MODE_PRESCALER_HI) | (TIMER_PRESCALER >> 8));
    write_reg(REG_T_RELOAD_HI, (reload >> 8) & 0xff);
    write_reg(REG_T_RELOAD_LO, reload & 0xff);
}

/* Soft-reset the coupler and set it up for ISO 14443-B at 106 kbit/s. */
static void reader_init(void)
{
    write_reg(REG_COMMAND, CMD_SOFT_RESET);
    write_reg(REG_CONTROL, CONTROL_INITIATOR);
    write_reg(REG_TX_MODE, MODE_CRC_EN | MODE_FRAMING_14443B);
    write_reg(REG_RX_MODE, MODE_CRC_EN | MODE_FRAMING_14443B);
    write_reg(REG_TX_CONTROL, TX_CONTROL_INV_TX2 | TX_CONTROL_TX2_EN | TX_CONTROL_TX1_EN);
    write_reg(REG_RX_THRESHOLD, RX_THRESHOLD_VALUE);
    write_reg(REG_DEMOD, DEMOD_VALUE);
    write_reg(REG_RF_CFG, RF_CFG_RX_GAIN_48DB);
    write_reg(REG_GS_N, GS_N_VALUE);
    write_reg(REG_CW_GS_P, CW_GS_P_VALUE);
    write_reg(REG_MOD_GS_P, MOD_GS_P_VALUE);
    write_reg(REG_TYPE_B, 0);
    write_reg(REG_TX_ASK, 0);
    set_timeout(RX_TIMEOUT_US);
    write_reg(REG_COMMAND, CMD_TRANSCEIVE);
}

static int error_status(unsigned char error)
{
    if (error & ERR_WRITE)
        return STATUS_WRITE_ERROR;
    if (error & ERR_TEMP)
        return STATUS_TEMP_ERROR;
    if (error & ERR_BUFFER_OVFL)
        return STATUS_BUFFER_OVERFLOW;
    if (error & ERR_COLL)
        return STATUS_COLLISION;
    if (error & ERR_CRC)
        return STATUS_CRC;
    if (error & ERR_PARITY)
        return STATUS_PARITY;
    if (error & ERR_PROTOCOL)
        return STATUS_PROTOCOL;
    return STATUS_OK;
}

/*
 * Send `tx` to the tag and, if `wait`, collect up to `rx_max` answer bytes
 * into `rx`. Returns the number of bytes received (or 1 when not waiting),
 * 0 on failure with the reason in *status.
 */
static unsigned char transceive(int *status, const unsigned char *tx, unsigned char tx_len,
                                unsigned char *rx, unsigned char rx_max, gboolean wait)
{
    unsigned char i, n, irq;

    *status = STATUS_OK;
    if (tx_len == 0) {
        *status = STATUS_FIFO;
        return 0;
    }
    write_reg(REG_FIFO_LEVEL, FIFO_LEVEL_FLUSH);
    for (i = 0; i < tx_len; i++)
        write_reg(REG_FIFO_DATA, tx[i]);
    if (read_reg(REG_FIFO_LEVEL) != tx_len) {
        *status = STATUS_TX_LENGTH;
        return 0;
    }
    write_reg(REG_COMMAND, CMD_TRANSCEIVE);
    write_reg(REG_COM_IRQ, IRQ_CLEAR_ALL);
    write_reg(REG_BIT_FRAMING, BIT_FRAMING_START);
    if (!wait)
        return 1;

    /* Busy-wait for the answer, an error or the timer. */
    do {
        irq = read_reg(REG_COM_IRQ);
        if ((irq & IRQ_RX) || (irq & IRQ_TIMER))
            break;
    } while (!(irq & IRQ_ERROR));
    write_reg(REG_COM_IRQ, IRQ_CLEAR_ALL);

    if (irq & IRQ_ERROR) {
        *status = error_status(read_reg(REG_ERROR));
        return 0;
    }
    if (irq & IRQ_TIMER) {
        *status = STATUS_TIMEOUT;
        return 0;
    }
    n = read_reg(REG_FIFO_LEVEL);
    if (n == 0) {
        *status = STATUS_OK;
        return 0;
    }
    if (n > rx_max) {
        *status = STATUS_RX_OVERFLOW;
        return 0;
    }
    for (i = 0; i < n; i++)
        rx[i] = read_reg(REG_FIFO_DATA);
    return n;
}

/* ------------------------------------------------------------------------ */
/* SRI512 / SRIX commands                                                    */

/* Command codes (ST SRI512/SRIX4K data sheets). */
#define SRI_INITIATE            0x06  /* followed by 0x00 */
#define SRI_PCALL16             0x06  /* followed by 0x04 */
#define SRI_INITIATE_PARAM      0x00
#define SRI_PCALL16_PARAM       0x04
#define SRI_SLOT_MARKER         0x06  /* | slot << 4 */
#define SRI_READ_BLOCK          0x08
#define SRI_WRITE_BLOCK         0x09
#define SRI_GET_UID             0x0b
#define SRI_SELECT              0x0e

#define SRI_UID_SIZE            8
#define SRI_WRITE_WAIT_US       500
#define SRI_BLOCK_SYSTEM        0xff  /* the system area */
#define SRI_BLOCKS              16    /* SRI512: 16 blocks of 4 bytes */
#define SRI_SLOTS               16    /* PCALL16 and SLOT_MARKER slots */

/* The LED answer to a verified (or failed) block 7 write, in ms. */
#define VERIFY_PULSE_PERIOD     500
#define VERIFY_PULSE_LENGTH     500

static unsigned char sri_initiate(int *status, unsigned char *chip_id)
{
    static const unsigned char cmd[2] = { SRI_INITIATE, SRI_INITIATE_PARAM };
    unsigned char buf[2];

    memcpy(buf, cmd, 2);
    return transceive(status, buf, 2, chip_id, 1, TRUE);
}

static unsigned char sri_pcall16(int *status, unsigned char *chip_id)
{
    static const unsigned char cmd[2] = { SRI_PCALL16, SRI_PCALL16_PARAM };
    unsigned char buf[2];

    memcpy(buf, cmd, 2);
    return transceive(status, buf, 2, chip_id, 1, TRUE);
}

static unsigned char sri_slot_marker(unsigned char slot, int *status, unsigned char *chip_id)
{
    unsigned char cmd = (unsigned char)(slot << 4 | SRI_SLOT_MARKER);

    return transceive(status, &cmd, 1, chip_id, 1, TRUE);
}

static unsigned char sri_select(int *status, unsigned char chip_id, unsigned char *answer)
{
    unsigned char cmd[2] = { SRI_SELECT, chip_id };

    return transceive(status, cmd, 2, answer, 1, TRUE);
}

static unsigned char sri_get_uid(int *status, unsigned char *uid)
{
    unsigned char cmd = SRI_GET_UID;

    return transceive(status, &cmd, 1, uid, SRI_UID_SIZE, TRUE);
}

static unsigned char sri_write_block(int *status, unsigned char block, const unsigned char *data, unsigned char len)
{
    unsigned char cmd[6]; /* opcode, block, 4 data bytes */
    unsigned char i, ok = 0;

    if (len < 5) {
        memset(cmd, 0, sizeof cmd);
        cmd[0] = SRI_WRITE_BLOCK;
        /* Exactly len bytes are copied, so nothing past the caller's data
         * is read or past cmd written. */
        for (i = 0; i < len; i++)
            cmd[2 + i] = data[i];
        cmd[1] = block;
        ok = transceive(status, cmd, 6, NULL, 0, FALSE);
        usleep(SRI_WRITE_WAIT_US);
    }
    return ok;
}

static unsigned char sri_read_block(int *status, unsigned char block, unsigned char *data, unsigned char len)
{
    unsigned char cmd[2];
    unsigned char ok = 0;

    if (len < 5) {
        memset(data, 0, len);
        cmd[0] = SRI_READ_BLOCK;
        cmd[1] = block;
        ok = transceive(status, cmd, 2, data, len, TRUE);
    }
    return ok;
}

/* ------------------------------------------------------------------------ */
/* Block 7: the tag's product codes (application, type, picture, colour)    */

/*
 * Block 7, least significant byte first: bytes 2-3 the application code;
 * byte 1 the type code (high nibble) and the picture code's high bits (low
 * nibble); byte 0 the picture code's low three bits (bits 5-7) and the
 * colour code (bits 0-4).
 */
#define BLOCK7                  7
#define BLOCK7_TYPE_SHIFT       4
#define BLOCK7_TYPE_MASK        0xf0
#define BLOCK7_PICT_HI_MASK     0x0f
#define BLOCK7_PICT_LO_SHIFT    5
#define BLOCK7_PICT_LO_MASK     0xe0
#define BLOCK7_PICT_LO_BITS     7     /* the picture code's low three bits */
#define BLOCK7_COLOR_MASK       0x1f

static int block7_app_code(unsigned char lo, unsigned char hi)
{
    return hi * 0x100 + lo;
}

static int block7_type_code(unsigned char b1)
{
    return (b1 & BLOCK7_TYPE_MASK) >> BLOCK7_TYPE_SHIFT;
}

static int block7_pict_code(unsigned char b0, unsigned char b1)
{
    return (b1 & BLOCK7_PICT_HI_MASK) * 8 + ((b0 & BLOCK7_PICT_LO_MASK) >> BLOCK7_PICT_LO_SHIFT);
}

static int block7_color_code(unsigned char b0)
{
    return b0 & BLOCK7_COLOR_MASK;
}

/* Write the configured codes into block 7 of the selected tag. */
static void write_block7(RfidObject *self)
{
    unsigned char data[4] = { 0 };
    int status = 0;

    data[2] = (unsigned char)self->app_code;
    data[3] = (unsigned char)(self->app_code / 256);
    data[1] |= (unsigned char)(self->type_code << BLOCK7_TYPE_SHIFT);
    data[0] |= (unsigned char)((self->pict_code & BLOCK7_PICT_LO_BITS) << BLOCK7_PICT_LO_SHIFT);
    data[1] |= (unsigned char)(self->pict_code / 8);
    data[0] |= self->color_code & BLOCK7_COLOR_MASK;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: writeData - %02X%02X%02X%02X\n", data[3], data[2], data[1], data[0]);
    if (!sri_write_block(&status, BLOCK7, data, 4))
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: writeData - SRI512_WRITE_BLOCK failed - abort\n");
}

/* ------------------------------------------------------------------------ */
/* Configuration: <configure writeInTag="true|false"/>                       */
/*                <block7 app_code= type_code= pict_code= color_code=/>      */

static char conf_buf[0x2000];

static gboolean parse_code(const char *value, const char *name, int *code)
{
    char *end;
    long n = strtol(value, &end, 10);

    if (*end != '\0') {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: block7: error convertion: %s\n", name);
        return FALSE;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: block7: convertion: %s: %d\n", name, (int)n);
    *code = (int)n;
    return TRUE;
}

static void XMLCALL conf_start(void *data, const XML_Char *name, const XML_Char **attrs)
{
    RfidObject *self = *(RfidObject **)data;
    int i;

    if (strcmp(name, "configure") == 0) {
        for (i = 0; attrs[i] != NULL; i += 2) {
            if (strcmp(attrs[i], "writeInTag") == 0) {
                if (strcmp(attrs[i + 1], "false") == 0)
                    self->write_in_tag = 0;
                else if (strcmp(attrs[i + 1], "true") == 0)
                    self->write_in_tag = 1;
            }
        }
    } else if (strcmp(name, "block7") == 0) {
        for (i = 0; attrs[i] != NULL; i += 2) {
            if (strcmp(attrs[i], "app_code") == 0)
                parse_code(attrs[i + 1], "app_code", &self->app_code);
            else if (strcmp(attrs[i], "type_code") == 0)
                parse_code(attrs[i + 1], "type_code", &self->type_code);
            else if (strcmp(attrs[i], "pict_code") == 0)
                parse_code(attrs[i + 1], "pict_code", &self->pict_code);
            else if (strcmp(attrs[i], "color_code") == 0)
                parse_code(attrs[i + 1], "color_code", &self->color_code);
        }
    }
}

static void XMLCALL conf_end(void *data, const XML_Char *name)
{
    (void)data;
    (void)name;
}

static gboolean parse_rfid_conf(const char *path, RfidObject *self)
{
    RfidObject **user_data = malloc(sizeof *user_data);
    XML_Parser parser;
    FILE *f;
    size_t len;
    int done;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: parseRfidConf - Start\n");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: parseRfidConf - inXmlPath: %s\n\n", path);
    *user_data = self;
    parser = XML_ParserCreate("utf-8");
    if (parser == NULL) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "RFID: parseRfidConf - Couldn't allocate memory for parser\n");
        exit(-1);
    }
    XML_SetElementHandler(parser, conf_start, conf_end);
    XML_SetUserData(parser, user_data);

    /* The parser and user_data are freed on both return paths; a read or
     * parse error exits the daemon (-1) instead. */
    f = fopen(path, "r");
    if (f == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: parseRfidConf - error open xml file\n");
        XML_ParserFree(parser);
        free(user_data);
        return FALSE;
    }
    do {
        len = fread(conf_buf, 1, sizeof conf_buf, f);
        if (ferror(f)) {
            g_log(NULL, G_LOG_LEVEL_CRITICAL, "RFID: parseRfidConf - Read xml file error\n");
            exit(-1);
        }
        done = feof(f);
        if (!XML_Parse(parser, conf_buf, (int)len, done)) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: parseRfidConf - xml parse error at line %d:\n%s\n",
                  (int)XML_GetCurrentLineNumber(parser), XML_ErrorString(XML_GetErrorCode(parser)));
            exit(-1);
        }
    } while (!done);
    fclose(f);
    XML_ParserFree(parser);
    free(user_data);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: parseRfidConf - End\n");
    return TRUE;
}

/* ------------------------------------------------------------------------ */
/* Tag tracking                                                              */

static gboolean tag_known(RfidObject *self, GString *uid)
{
    GSList *l;

    for (l = self->tags; l != NULL; l = l->next)
        if (g_string_equal(uid, ((RfidTag *)l->data)->uid))
            return TRUE;
    return FALSE;
}

/* SELECT every known tag again; forget (and report) those that don't answer. */
static void check_tags(RfidObject *self)
{
    RfidObjectClass *klass = (RfidObjectClass *)G_OBJECT_GET_CLASS(self);
    unsigned char answer = 0; /* not reset between tags: harmless, a failed SELECT is dropped by !ok */
    GSList *l, *next;
    int status;

    for (l = self->tags; l != NULL; l = next) {
        RfidTag *tag = l->data;
        unsigned char ok = sri_select(&status, tag->chip_id, &answer);

        /*
         * l->next is read before g_slist_remove() can free the node.
         */
        next = l->next;
        if ((char)answer != tag->chip_id || !ok) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: CHECK: tag no longer present: %s\n", tag->uid->str);
            g_signal_emit(self, klass->signals[SIGNAL_TAG_OUT], 0, tag->uid->str);
            g_string_free(tag->uid, TRUE);
            g_string_free(tag->blocks, TRUE);
            self->tags = g_slist_remove(self->tags, tag);
            free(tag);
        }
    }
}

/* Read the tag that answered with `chip_id` and report it if it is new. */
static void read_tag(RfidObject *self, char chip_id)
{
    RfidObjectClass *klass = (RfidObjectClass *)G_OBJECT_GET_CLASS(self);
    unsigned char answer = 0, uid[8], data[4];
    RfidTag *tag;
    GString *uid_str, *blocks;
    unsigned int block;
    int status;

    if (!sri_select(&status, (unsigned char)chip_id, &answer) || chip_id != (char)answer) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "RFID: getTagInfo - SRI512_SELECT failed - abort\n");
        return;
    }
    memset(uid, 0, sizeof uid);
    if (!sri_get_uid(&status, uid)) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "RFID: getTagInfo - SRI512_GET_UID failed - abort\n");
        return;
    }
    tag = malloc(sizeof *tag);
    if (tag == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "RFID: getTagInfo - rfidTag == NULL. malloc failed - abort\n");
        return;
    }
    uid_str = g_string_new("");
    g_string_printf(uid_str, "%02X%02X%02X%02X%02X%02X%02X%02X",
                    uid[7], uid[6], uid[5], uid[4], uid[3], uid[2], uid[1], uid[0]);
    tag->uid = uid_str;
    tag->chip_id = chip_id;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: uid: %s", uid_str->str);

    if (tag_known(self, uid_str)) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: getTagInfo - tag already in list - abort\n");
        free(tag);
        g_string_free(uid_str, TRUE);
        return;
    }

    blocks = g_string_new("");
    memset(data, 0, sizeof data);
    if (self->write_in_tag == 1) {
        g_log(NULL, G_LOG_LEVEL_DEBUG,
              "RFID: parseRfidConf: app_code:%d  type_code:%d   pict_code:%d   color_code:%d\n",
              self->app_code, self->type_code, self->pict_code, self->color_code);
        write_block7(self);
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: Read begin\n");
    for (block = 0; block < SRI_BLOCKS; block++) {
        sri_read_block(&status, (unsigned char)block, data, 4); /* failures read as zeros */
        g_string_append_printf(blocks, "%02X%02X%02X%02X:", data[3], data[2], data[1], data[0]);
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: block %d: %02X%02X%02X%02X\n", block, data[3], data[2], data[1], data[0]);
        if (block == BLOCK7) {
            tag->app_code = block7_app_code(data[2], data[3]);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: app_code: %02X::%d\n", tag->app_code, tag->app_code);
            tag->type_code = block7_type_code(data[1]);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: type_code: %02X::%d\n", tag->type_code, tag->type_code);
            tag->pict_code = block7_pict_code(data[0], data[1]);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: pict_code: %02X::%d\n", tag->pict_code, tag->pict_code);
            tag->color_code = block7_color_code(data[0]);
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: color_code: %02X::%d\n", tag->color_code, tag->color_code);
        }
    }
    if (tag->app_code == 0 && tag->type_code == 0 && tag->pict_code == 0 && tag->color_code == 0) {
        /* Taken as a tag pulled away mid-read: the tag, its uid and the
         * blocks are freed. */
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: tag codes all zero, taken as pulled away mid-read: dropped");
        g_string_free(blocks, TRUE);
        g_string_free(uid_str, TRUE);
        free(tag);
        return;
    }

    sri_read_block(&status, SRI_BLOCK_SYSTEM, data, 4);
    g_string_append_printf(blocks, "%02X%02X%02X%02X:", data[3], data[2], data[1], data[0]);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: block 255: %02X%02X%02X%02X\n", data[3], data[2], data[1], data[0]);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: Read end\n");
    tag->blocks = blocks;

    if (self->write_in_tag == 1) {
        if (self->app_code == tag->app_code && self->type_code == tag->type_code &&
            self->pict_code == tag->pict_code && self->color_code == tag->color_code) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: Verify block 7 OK\n");
            ledPulse(self->led, NULL, LED_CYAN, LED_OFF, VERIFY_PULSE_PERIOD, VERIFY_PULSE_LENGTH);
        } else {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: Verify block 7 NOT ok\n");
            ledPulse(self->led, NULL, LED_RED, LED_OFF, VERIFY_PULSE_PERIOD, VERIFY_PULSE_LENGTH);
        }
    }

    self->tags = g_slist_prepend(self->tags, tag);
    g_signal_emit(self, klass->signals[SIGNAL_TAG_IN], 0, uid_str->str, blocks->str,
                  tag->app_code, tag->type_code, tag->pict_code, tag->color_code);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: Signal E_RFID_SIGNAL_TAG_IN emit\n");
}

/* Every POLL_MS: look for new tags, then check the known ones are still there. */
static gboolean poll_reader(gpointer data)
{
    RfidObject *self = data;
    unsigned char chip_id = 0;
    gboolean collision = FALSE;
    unsigned char slot;
    int status;

    if (sri_initiate(&status, &chip_id)) {
        read_tag(self, (char)chip_id);
    } else if (status == STATUS_CRC) {
        /* Several tags answered: let each pick one of 16 slots, until none collide. */
        do {
            collision = FALSE;
            if (sri_pcall16(&status, &chip_id))
                read_tag(self, (char)chip_id);
            else if (status == STATUS_CRC)
                collision = TRUE;
            for (slot = 1; slot < SRI_SLOTS; slot++) {
                if (sri_slot_marker(slot, &status, &chip_id))
                    read_tag(self, (char)chip_id);
                else if (status == STATUS_CRC)
                    collision = TRUE;
            }
        } while (collision);
    }
    check_tags(self);
    return TRUE;
}

/* ------------------------------------------------------------------------ */
/* D-Bus object                                                              */

gboolean rfid_object_get_tags(RfidObject *self, char **tags, gboolean *result, GError **error)
{
    GString *s = g_string_new("");
    GSList *l;

    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "RFID: getTags\n");
    /*
     * The list head is taken after check_tags(), so the walk sees the
     * updated list and no freed nodes when a tag has just gone.
     */
    check_tags(self);
    for (l = self->tags; l != NULL; l = l->next) {
        RfidTag *tag = l->data;

        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: getTags: adding a tag\n");
        g_string_append_printf(s, ";%s:%s", tag->uid->str, tag->blocks->str);
    }
    *tags = g_malloc(s->len + 1);
    if (*tags == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: getTags: out of memory for the reply\n");
        *result = FALSE;
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: strcpy tag\n");
        strcpy(*tags, s->str);
        *result = TRUE;
    }
    g_string_free(s, TRUE);
    return TRUE;
}

static void rfid_object_class_init(RfidObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_TAG_IN] = g_signal_new("tag_in", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                 karotz_smarshal_VOID__STRING_STRING_INT_INT_INT_INT, G_TYPE_NONE, 6,
                                                 G_TYPE_STRING, G_TYPE_STRING,
                                                 G_TYPE_INT, G_TYPE_INT, G_TYPE_INT, G_TYPE_INT);
    klass->signals[SIGNAL_TAG_OUT] = g_signal_new("tag_out", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                  g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1, G_TYPE_STRING);
    g_return_if_fail(klass != NULL);
}

static void rfid_object_init(RfidObject *self)
{
    GError *error = NULL;

    dbus_g_object_type_install_info(RFID_TYPE_OBJECT, &dbus_glib_rfid_object_object_info);
    self->tags = NULL;
    self->led = NULL;
    self->bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    dbus_load_proxy(&self->led, KAROTZ_PROXY_LED, self->bus);
    self->write_in_tag = 0;
    self->app_code = 0;
    self->type_code = 0;
    self->pict_code = 0;
    self->color_code = 0;
    parse_rfid_conf(CONF_PATH, self);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID: parseRfidConf: app_code:%d  type_code:%d   pict_code:%d   color_code:%d\n",
          self->app_code, self->type_code, self->pict_code, self->color_code);
    g_return_if_fail(self != NULL);
}

static void rfid_object_free_tags(RfidObject *self)
{
    g_slist_free(self->tags);
}

int main(void)
{
    int log_priority = LOG_NOTICE;
    GError *error = NULL;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    GMainLoop *loop;
    RfidObject *rfid;
    guint request_result = 0;

    init_karotz_log("Rfid", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");

    /* Unlike the other daemons, failures here are only logged. */
    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL)
        g_log(NULL, G_LOG_LEVEL_WARNING, "RFID - Failed to open connection to bus");
    else
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RFID - Successfully connected to %s bus\n", "SYSTEM");

    rfid = g_object_new(RFID_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Rfid", G_OBJECT(rfid));

    bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, "com.mindscape.karotz.Rfid", 0, &request_result, &error))
        g_log(NULL, G_LOG_LEVEL_WARNING, "RFID - Failed to get name");
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "RFID - Got result code %u from requesting name", request_result);
        exit(1);
    }

    i2c_open();
    reader_init();
    g_timeout_add(POLL_MS, poll_reader, rfid);
    g_signal_emit(rfid, ((RfidObjectClass *)G_OBJECT_GET_CLASS(rfid))->signals[SIGNAL_REGISTERED], 0);
    g_main_loop_run(loop);
    rfid_object_free_tags(rfid);
    return 0;
}
