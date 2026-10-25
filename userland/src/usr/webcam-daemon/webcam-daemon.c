/* SPDX-License-Identifier: MIT */
/*
 * webcam-daemon: take a snapshot with the rabbit's camera and upload it,
 * on com.mindscape.karotz.Webcam (/com/mindscape/karotz/Webcam).
 *
 * takeSnapshot(id, snapURL, behaviour) opens /dev/camera, switches it to VGA
 * (640x480 RGB565) with a private ioctl, pulses the LED and plays a shutter
 * sound, grabs three frames, JPEG-encodes the last one (quality 80) and
 * either writes it to /tmp/picture.jpg (snapURL "file") or POSTs it as a
 * multipart form to snapURL. On success it emits event(correlationid, 4),
 * where `id` is "interactiveid:correlationid". The camera is closed after
 * every snapshot. `behaviour` is ignored.
 */
#include <fcntl.h>
#include <stdio.h> /* jpeglib.h needs FILE */
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include <curl/curl.h>
#include <jpeglib.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include "karotz-common.h"
#include "karotz-messages.h"
#include "karotz-marshal.h"

#define CAMERA_DEVICE "/dev/camera"
#define FRAME_WIDTH 640
#define FRAME_HEIGHT 480
#define JPEG_QUALITY 80
#define PICTURE_FILE "/tmp/picture.jpg"
#define SHUTTER_SOUND "file:///usr/karotz/sounds/shutter.mp3"

/* The camera driver's private "switch to VGA" ioctl (0xc004f900, the
 * kernel's CAMIF_IOCTL_SET_VGA). It returns 1 if the resolution changed, 0
 * if it was already VGA. */
#define WEBCAM_IOCTL_SET_VGA _IOWR(0xf9, 0, int)

/* RGB565 pixel fields: blue in bits 0-4, green 5-10, red 11-15. */
#define RGB565_BLUE_MASK   0x001f
#define RGB565_GREEN_MASK  0x07e0
#define RGB565_RED_MASK    0xf800

/* The snapshot LED pulse: period and length in ms. */
#define SHOT_PULSE_PERIOD_MS 500
#define SHOT_PULSE_LENGTH_MS 500

/* Frames thrown away after a resolution change while the sensor settles. */
#define SETTLE_FRAMES 30
/* Frames read per snapshot; the last one is kept. */
#define SHOT_FRAMES 3

/* The event signal's code for "snapshot taken". */
#define EVENT_SNAPSHOT_DONE 4

enum {
    SIGNAL_EVENT,
    SIGNAL_REGISTERED,
    N_SIGNALS
};

typedef struct {
    GObject parent;
    DBusGProxy *multimedia;
    DBusGProxy *led;
    DBusGConnection *bus;
} WebcamObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} WebcamObjectClass;

GType webcam_object_get_type(void);
#define WEBCAM_TYPE_OBJECT (webcam_object_get_type())

G_DEFINE_TYPE(WebcamObject, webcam_object, G_TYPE_OBJECT)

static gboolean webcam_object_take_snapshot(WebcamObject *self, char *id, const char *snap_url, gint behaviour,
                                            gboolean *result, GError **error);

#include "webcam-daemon-glue.h"

/* The open camera and its raw frame buffer, between init and close. */
typedef struct {
    int fd;
    unsigned char *buffer; /* one RGB565 frame (allocated with 8 spare lines) */
    int frame_size;        /* bytes per frame */
    int width;
    int height;
    int resolution; /* set but never passed to the driver */
} CameraInput;

/* An encoded picture. */
typedef struct {
    unsigned char *data;
    int size;
} Picture;

static CameraInput *camera;

/* "snapshot_YYYY_mm_dd_HH_MM_SS.jpg" for the current local time, or NULL. */
static char *get_picture_name(void)
{
    char name[100];
    time_t now;
    struct tm *tm;
    char *result;

    memset(name, 0, sizeof(name));
    now = time(NULL);
    tm = localtime(&now);
    if (tm == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "get_picture_name localtime error\n");
        return NULL;
    }
    if (strftime(name, sizeof(name), "snapshot_%Y_%m_%d_%H_%M_%S.jpg", tm) == 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "get_picture_name time error\n");
        return NULL;
    }
    result = g_strdup(name);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "get_picture_name pictureName:%s\n", result);
    return result;
}

/*
 * Open the camera and switch it to VGA. Returns the ioctl's result (0 or 1)
 * or -1 on error; the caller closes the camera either way.
 */
static int input_cam_init(void)
{
    int ret;
    int i;

    if (camera == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: frameIn malloc size: %d\n", (int)sizeof(*camera));
        camera = malloc(sizeof(*camera));
        if (camera == NULL) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: init_input not enough memory for frameIn\n");
            return -1;
        }
        memset(camera, 0, sizeof(*camera));
    }
    camera->width = FRAME_WIDTH;
    camera->height = FRAME_HEIGHT;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: init_input frame_width: %d  height: %d\n", camera->width,
          camera->height);
    camera->frame_size = camera->width * camera->height * 2;
    camera->buffer = calloc(1, camera->width * (camera->height + 8) * 2);

    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: Open device\n");
    camera->fd = open(CAMERA_DEVICE, O_RDWR | O_NONBLOCK);
    if (camera->fd < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: open(V4L2_DEV_NODE) failed\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: open(V4L2_DEV_NODE) ok\n");

    camera->resolution = FRAME_WIDTH;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: WEBCAM_IOCTL_SET_VGA\n");
    /* The argument is NULL, not &camera->resolution: the driver ignores it
     * (the request itself selects VGA, s3c2440camif.c), so the resolution
     * set above only appears in the log messages below. */
    ret = ioctl(camera->fd, WEBCAM_IOCTL_SET_VGA, NULL);
    if (ret == -1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: takeOneShot: error ioctl\n");
        return -1;
    }
    if (ret == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: ioctl. no resolution change: %d\n", camera->resolution);
    } else if (ret == 1) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: ioctl. resolution change to: %d\n", camera->resolution);
        for (i = 0; i < SETTLE_FRAMES; i++) {
            ssize_t n;

            g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: PRE-SHOT !!!!!!!\n");
            n = read(camera->fd, camera->buffer, camera->frame_size);
            if (n < 0) {
                g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: Error grabbing frames\n");
                return -1;
            }
            g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: read frame OK. frameSize wanted:%d, read size :%d\n",
                  camera->frame_size, (int)n);
        }
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: ioctl ok\n");
    return ret;
}

/* Close the camera and forget it. */
static int input_cam_close(void)
{
    close(camera->fd);
    free(camera->buffer);
    camera->buffer = NULL;
    free(camera);
    camera = NULL;
    return 0;
}

/*
 * A libjpeg destination that writes into a caller-supplied malloc'd buffer,
 * doubling it when full. Its name does not clash with the system libjpeg's
 * own jpeg_mem_dest.
 */
typedef struct {
    struct jpeg_destination_mgr pub;
    unsigned char **outbuffer; /* where the final buffer is stored */
    int *outsize;              /* where the encoded size is stored */
    unsigned char *newbuffer;  /* the latest buffer we allocated */
    unsigned char *buffer;     /* the buffer being written */
    int bufsize;
} MemDestination;

static void mem_init_destination(j_compress_ptr cinfo)
{
    (void)cinfo;
}

static boolean mem_empty_output_buffer(j_compress_ptr cinfo)
{
    MemDestination *dest = (MemDestination *)cinfo->dest;
    int size = dest->bufsize;
    unsigned char *bigger = malloc(size * 2);

    if (bigger == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot out of memory");
        return FALSE;
    }
    memcpy(bigger, dest->buffer, dest->bufsize);
    /* The outgrown buffer is freed whoever allocated it (the caller gets
     * the last one back). */
    free(dest->buffer);
    dest->newbuffer = bigger;
    dest->pub.next_output_byte = bigger + dest->bufsize;
    dest->pub.free_in_buffer = dest->bufsize;
    dest->buffer = bigger;
    dest->bufsize = size * 2;
    return TRUE;
}

static void mem_term_destination(j_compress_ptr cinfo)
{
    MemDestination *dest = (MemDestination *)cinfo->dest;

    *dest->outbuffer = dest->buffer;
    *dest->outsize = dest->bufsize - (int)dest->pub.free_in_buffer;
}

static void snapshot_mem_dest(j_compress_ptr cinfo, unsigned char **outbuffer, int *outsize)
{
    MemDestination *dest;

    if (outbuffer == NULL || outsize == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot output buffer or size not given");
        return;
    }
    if (cinfo->dest == NULL)
        cinfo->dest = cinfo->mem->alloc_small((j_common_ptr)cinfo, JPOOL_PERMANENT, sizeof(MemDestination));
    dest = (MemDestination *)cinfo->dest;
    dest->pub.init_destination = mem_init_destination;
    dest->pub.empty_output_buffer = mem_empty_output_buffer;
    dest->pub.term_destination = mem_term_destination;
    dest->outbuffer = outbuffer;
    dest->outsize = outsize;
    dest->newbuffer = NULL;
    if (*outbuffer == NULL || *outsize == 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot outbuffer, outsize not initialized");
        return;
    }
    dest->buffer = *outbuffer;
    dest->pub.next_output_byte = dest->buffer;
    dest->bufsize = *outsize;
    dest->pub.free_in_buffer = dest->bufsize;
}

/* Signal the shot, grab frames and JPEG-encode the last one into `picture`. */
static int input_cam_take_one_shot(Picture *picture, WebcamObject *self)
{
    struct jpeg_compress_struct cinfo;
    struct jpeg_error_mgr jerr;
    JSAMPROW row_pointer[1];
    unsigned char *row;
    const unsigned short *pixel;
    int jpeg_size;
    int i;

    picture->size = camera->frame_size;
    picture->data = malloc(camera->frame_size);
    if (picture->data == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot could not allocate memory for webcam_frame->buf");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_takeOneShot\n");
    ledPulse(self->led, NULL, LED_SEA_GREEN, LED_OFF, SHOT_PULSE_PERIOD_MS, SHOT_PULSE_LENGTH_MS);
    multimediaPlay(self->multimedia, NULL, SHUTTER_SOUND, 1);

    for (i = 0; i < SHOT_FRAMES; i++) {
        ssize_t n = read(camera->fd, camera->buffer, camera->frame_size);

        if (n < 0) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot: Error grabbing frames\n");
            return -1;
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_takeOneShot: read frame OK. frameSize wanted:%d, read size :%d\n",
              camera->frame_size, (int)n);
    }

    cinfo.err = jpeg_std_error(&jerr);
    jpeg_create_compress(&cinfo);
    jpeg_size = picture->size;
    snapshot_mem_dest(&cinfo, &picture->data, &jpeg_size);
    cinfo.image_width = camera->width;
    cinfo.image_height = camera->height;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_RGB;
    jpeg_set_defaults(&cinfo);
    jpeg_set_quality(&cinfo, JPEG_QUALITY, TRUE);
    jpeg_start_compress(&cinfo, TRUE);

    /* Expand RGB565 to 24 bits. Quirk: the low five bits go to the first
     * byte, so the channels are written B,G,R into an image declared RGB.
     * That undoes the kernel's set-up, which swaps Cb and Cr between the
     * sensor (VYUY) and the camera interface (told CbYCrY): the colours
     * come out right (checked with a photo on the rabbit). Change both or
     * neither. The row buffer is freed after the last row. */
    row = calloc(camera->width * 3, 1);
    pixel = (const unsigned short *)camera->buffer;
    while (cinfo.next_scanline < cinfo.image_height) {
        unsigned char *out = row;

        for (i = 0; i < camera->width; i++) {
            unsigned short p = *pixel++;

            out[0] = (p & RGB565_BLUE_MASK) << 3;
            out[1] = (p & RGB565_GREEN_MASK) >> 3;
            out[2] = (p & RGB565_RED_MASK) >> 8;
            out += 3;
        }
        row_pointer[0] = row;
        jpeg_write_scanlines(&cinfo, row_pointer, 1);
    }
    jpeg_finish_compress(&cinfo);
    jpeg_destroy_compress(&cinfo);
    free(row);

    picture->size = jpeg_size;
    if (picture->size < 1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot: bad size: %d\n", picture->size);
        return -1;
    }
    return 0;
}

/* POST the picture to `url` as multipart/form-data, with the request's ids. Returns 0 or -1. */
static int output_httppost_send_file(Picture *picture, const char *interactive_id, const char *correlation_id,
                                     const char *name, const char *url)
{
    struct curl_slist *headers = NULL;
    curl_mime *form;
    curl_mimepart *part;
    CURL *curl;
    int ret = 0;

    curl_global_init(CURL_GLOBAL_ALL);
    curl = curl_easy_init();
    if (curl == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "output_httppost_sendFrame: curl_easy_init error. abort\n");
        return -1;
    }
    /* The picture as a file named `name`, then the request's ids. */
    form = curl_mime_init(curl);
    part = curl_mime_addpart(form);
    curl_mime_name(part, "sendfile");
    curl_mime_filename(part, name);
    curl_mime_data(part, (const char *)picture->data, (size_t)picture->size);
    const char *fields[][2] = { { "filename", name }, { "interactiveid", interactive_id },
                                { "correlationid", correlation_id }, { "uuid", correlation_id },
                                { "submit", "send" } };
    for (size_t i = 0; i < sizeof fields / sizeof fields[0]; i++) {
        part = curl_mime_addpart(form);
        curl_mime_name(part, fields[i][0]);
        curl_mime_data(part, fields[i][1], CURL_ZERO_TERMINATED);
    }
    /* An empty "Expect:" stops curl waiting for 100-continue. */
    headers = curl_slist_append(headers, "Expect:");
    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_MIMEPOST, form);
    curl_easy_setopt(curl, CURLOPT_VERBOSE, 1L);
    if (curl_easy_perform(curl) == CURLE_OK) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "output_httppost_sendFrame curl perform OK\n");
    } else {
        g_log(NULL, G_LOG_LEVEL_WARNING, "output_httppost_sendFrame curl perform ERROR\n");
        ret = -1;
    }
    curl_easy_cleanup(curl);
    curl_mime_free(form);
    curl_slist_free_all(headers);
    return ret;
}

/* Write the picture to /tmp/picture.jpg. Returns 0 or -1. */
static int output_save_in_file(Picture *picture)
{
    int fd;
    ssize_t n;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "output saveInFile: " PICTURE_FILE "\n");
    fd = open(PICTURE_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "output_file could not open the file\n");
        return -1;
    }
    n = write(fd, picture->data, picture->size);
    if (n < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Webcam: output_saveInFile: could not write to file\n");
        close(fd);
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "output_saveInFile data written: %d bytes\n", (int)n);
    close(fd);
    return 0;
}

/* Take, encode and deliver one snapshot. Returns 0 or -1. */
static int lib_take_snapshot(char *id, WebcamObject *self, const char *snap_url)
{
    Picture picture = {NULL, 0}; /* picture.data is freed before returning */
    char *name;
    const char *interactive_id;
    const char *correlation_id;
    int ret;
    WebcamObjectClass *klass;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "lib_takeSnapshot\n");
    /* "interactiveid:correlationid", split in place, once: the upload sends
     * both parts and the event carries the correlation id (NULL when there
     * is no ':'). */
    interactive_id = strtok(id, ":");
    correlation_id = strtok(NULL, ":");
    if (input_cam_init() == -1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "lib_Webcam: takeSnapshot: error init_input. abort\n");
        input_cam_close();
        return -1;
    }
    if (input_cam_take_one_shot(&picture, self) == -1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "lib_Webcam: takeSnapshot: error input_cam_takeOneShot. abort\n");
        input_cam_close();
        free(picture.data);
        return -1;
    }
    name = get_picture_name();
    if (name == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "lib_takeSnapshot: error  get_picture_name. abort\n");
        input_cam_close();
        free(picture.data);
        return -1;
    }

    if (strcmp(snap_url, "file") == 0) {
        ret = output_save_in_file(&picture);
    } else if (output_httppost_send_file(&picture, interactive_id, correlation_id, name, snap_url) == -1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "lib_takeSnapshot: output_httppost_sendFile error\n");
        ret = -1;
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "lib_takeSnapshot: output_httppost_sendFile ok\n");
        ret = 0;
    }

    /* The event is emitted whatever the outcome. */
    klass = (WebcamObjectClass *)G_OBJECT_GET_CLASS(self);
    g_signal_emit(self, klass->signals[SIGNAL_EVENT], 0, correlation_id, EVENT_SNAPSHOT_DONE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "g_signal_emit DONE!!!!\n");
    input_cam_close();
    g_free(name);
    free(picture.data);
    return ret;
}

/* D-Bus method takeSnapshot. `id` is dbus-glib's copy, which is split in place. */
static gboolean webcam_object_take_snapshot(WebcamObject *self, char *id, const char *snap_url, gint behaviour,
                                            gboolean *result, GError **error)
{
    (void)behaviour;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "takeSnapshot: %s\n", snap_url);
    if (lib_take_snapshot(id, self, snap_url) == -1) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "takeSnapshot: error\n");
        *result = FALSE;
    } else {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "takeSnapshot: succeed\n");
        *result = TRUE;
    }
    return TRUE;
}

static void webcam_object_class_init(WebcamObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    klass->signals[SIGNAL_EVENT] = g_signal_new("event", type, G_SIGNAL_RUN_LAST, 0, NULL, NULL,
                                                karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, 2, G_TYPE_STRING,
                                                G_TYPE_INT);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_LAST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    g_return_if_fail(klass != NULL);
}

static void webcam_object_init(WebcamObject *self)
{
    dbus_g_object_type_install_info(WEBCAM_TYPE_OBJECT, &dbus_glib_webcam_object_object_info);
    self->bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, NULL);
    dbus_load_proxy(&self->multimedia, KAROTZ_PROXY_MULTIMEDIA, self->bus);
    dbus_load_proxy(&self->led, KAROTZ_PROXY_LED, self->bus);
    g_return_if_fail(self != NULL);
}

int main(void)
{
    int log_priority;
    GError *error = NULL;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    GMainLoop *loop;
    guint request_result = 0;
    WebcamObject *webcam;
    WebcamObjectClass *klass;

    log_priority = getenv("KAROTZ_DEBUG") != NULL ? LOG_DEBUG : LOG_NOTICE;
    init_karotz_log("Webcam", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");

    /* Failures are only logged here (at WARNING, success too), but either
     * leaves request_result at 0, so the daemon then exits (1) at the name
     * check below. */
    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL)
        g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to open connection to bus");
    else
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Successfully connected to %s bus\n", "SYSTEM");

    webcam = g_object_new(WEBCAM_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Webcam", G_OBJECT(webcam));

    bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, "com.mindscape.karotz.Webcam", 0, &request_result, &error))
        g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to get name");
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "Got result code %u from requesting name", request_result);
        exit(1);
    }

    klass = (WebcamObjectClass *)G_OBJECT_GET_CLASS(webcam);
    g_signal_emit(webcam, klass->signals[SIGNAL_REGISTERED], 0);
    g_main_loop_run(loop);
    return 0;
}
