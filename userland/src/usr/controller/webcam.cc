// SPDX-License-Identifier: MIT
/*
 * Snapshots taken by the controller itself (the "direct" and "video"
 * webcam requests): one 320x240 RGB565 frame read from /dev/camera,
 * compressed to JPEG in memory.
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <jpeglib.h>

#include "controller.h"

/* The camera driver's "switch to QVGA" ioctl (0xc004f901, the kernel's
 * CAMIF_IOCTL_SET_QVGA). */
#define WEBCAM_IOCTL_SET_QVGA _IOWR(0xf9, 1, int)

/* RGB565 pixel fields: blue in bits 0-4, green 5-10, red 11-15. */
#define RGB565_BLUE_MASK   0x001f
#define RGB565_GREEN_MASK  0x07e0
#define RGB565_RED_MASK    0xf800
#define JPEG_QUALITY 20

/* The capture: device, frame buffer and geometry. */
typedef struct {
    int fd;
    unsigned char *buffer;
    int frame_size;
    int width;
    int height;
    int resolution;
} Capture;

static Capture *capture;

/* ---- in-memory JPEG destination, growing as needed ----------------------- */

typedef struct {
    struct jpeg_destination_mgr pub;
    unsigned char **output;
    int *output_size;
    unsigned char *allocated;
    unsigned char *buffer;
    int size;
} MemoryDestination;

static void destination_init(j_compress_ptr cinfo)
{
    (void)cinfo;
}

static boolean destination_grow(j_compress_ptr cinfo)
{
    MemoryDestination *dest = (MemoryDestination *)cinfo->dest;
    int size = dest->size;
    unsigned char *buffer = (unsigned char *)malloc(size * 2);

    if (buffer == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot out of memory");
        return FALSE;
    }
    memcpy(buffer, dest->buffer, dest->size);
    if (dest->allocated != NULL)
        free(dest->allocated);
    dest->allocated = buffer;
    dest->pub.next_output_byte = buffer + dest->size;
    dest->pub.free_in_buffer = dest->size;
    dest->buffer = buffer;
    dest->size = size * 2;
    return TRUE;
}

static void destination_term(j_compress_ptr cinfo)
{
    MemoryDestination *dest = (MemoryDestination *)cinfo->dest;

    *dest->output = dest->buffer;
    *dest->output_size = dest->size - dest->pub.free_in_buffer;
}

/* Compress into *output (of *output_size bytes), reallocating if it is too small. */
static void memory_destination(j_compress_ptr cinfo, unsigned char **output, int *output_size)
{
    if (output == NULL || output_size == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot output buffer or size not given");
        return;
    }
    if (cinfo->dest == NULL)
        cinfo->dest = (struct jpeg_destination_mgr *)(*cinfo->mem->alloc_small)((j_common_ptr)cinfo, JPOOL_PERMANENT,
                                                                                sizeof(MemoryDestination));
    MemoryDestination *dest = (MemoryDestination *)cinfo->dest;
    dest->pub.init_destination = destination_init;
    dest->pub.empty_output_buffer = destination_grow;
    dest->pub.term_destination = destination_term;
    dest->output = output;
    dest->output_size = output_size;
    dest->allocated = NULL;
    if (*output == NULL || *output_size == 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot outbuffer, outsize not initialized");
        return;
    }
    dest->buffer = *output;
    dest->pub.next_output_byte = dest->buffer;
    dest->size = *output_size;
    dest->pub.free_in_buffer = dest->size;
}

/* ---- capture -------------------------------------------------------------------- */

/* Open the camera and switch it to QVGA; after a resolution change 30 frames
 * are discarded. Returns -1 on error, otherwise the ioctl's result. */
static int capture_init(void)
{
    int result;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: init_input\n");
    if (capture == NULL) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: frameIn malloc size: %d\n", (int)sizeof(Capture));
        capture = (Capture *)malloc(sizeof(Capture));
        if (capture == NULL) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: init_input not enough memory for frameIn\n");
            return -1;
        }
        memset(capture, 0, sizeof(Capture));
    }
    capture->width = 320;
    capture->height = 240;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: init_input width: %d  height: %d\n", capture->width,
          capture->height);
    capture->frame_size = capture->width * capture->height * 2;
    capture->buffer = (unsigned char *)calloc(1, capture->width * (capture->height + 8) * 2);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: Open device\n");
    capture->fd = open("/dev/camera", O_RDWR | O_NONBLOCK);
    if (capture->fd < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: open(V4L2_DEV_NODE) failed\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: open(V4L2_DEV_NODE) ok\n");
    capture->resolution = 320;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: WEBCAM_IOCTL_SET_QVGA !!!!\n");
    result = ioctl(capture->fd, WEBCAM_IOCTL_SET_QVGA, 0);
    if (result == -1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: takeOneShot: error ioctl\n");
        return -1;
    }
    if (result == 0) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: ioctl. no resolution change: %d\n", capture->resolution);
    } else if (result == 1) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: ioctl. resolution change to: %d\n", capture->resolution);
        for (int i = 0; i < 30; i++) {
            g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: PRE-SHOT\n");
            ssize_t n = read(capture->fd, capture->buffer, capture->frame_size);
            if (n < 0) {
                g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_init: Error grabbing frames\n");
                return -1;
            }
            g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: read frame OK. frameSize wanted:%d, read size :%d\n",
                  capture->frame_size, (int)n);
        }
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_init: ioctl ok\n");
    return result;
}

/* One frame as JPEG into a malloc'ed buffer. The 16-bit pixels' low five
 * bits go first (as blue-first RGB565), the same order as webcam-daemon's:
 * with the sensor and camera interface set up as the kernel does, that
 * order gives the right colours. */
static int capture_shot(unsigned char **jpeg, unsigned long *size)
{
    struct jpeg_compress_struct cinfo;
    struct jpeg_error_mgr error;

    *jpeg = (unsigned char *)malloc(capture->frame_size);
    if (*jpeg == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING,
              "Webcam: takeOneShot could not allocate memory for global_webcam_data->buf\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_takeOneShot\n");
    ssize_t n = read(capture->fd, capture->buffer, capture->frame_size);
    if (n < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "input_cam_takeOneShot: Error grabbing frames\n");
        return -1;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "input_cam_takeOneShot: read frame OK. frameSize wanted:%d, read size :%d\n",
          capture->frame_size, (int)n);
    cinfo.err = jpeg_std_error(&error);
    jpeg_create_compress(&cinfo);
    int jpeg_size = capture->frame_size;
    memory_destination(&cinfo, jpeg, &jpeg_size);
    cinfo.image_width = capture->width;
    cinfo.image_height = capture->height;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_RGB;
    jpeg_set_defaults(&cinfo);
    jpeg_set_quality(&cinfo, JPEG_QUALITY, TRUE);
    jpeg_start_compress(&cinfo, TRUE);
    unsigned char *row = (unsigned char *)calloc(capture->width * 3, 1);
    const unsigned short *pixel = (const unsigned short *)capture->buffer;
    while (cinfo.next_scanline < cinfo.image_height) {
        unsigned char *out = row;
        for (int x = 0; x < capture->width; x++) {
            unsigned short value = *pixel++;
            out[0] = (value & RGB565_BLUE_MASK) << 3;
            out[1] = (value & RGB565_GREEN_MASK) >> 3;
            out[2] = (value & RGB565_RED_MASK) >> 8;
            out += 3;
        }
        JSAMPROW rows[1] = { row };
        jpeg_write_scanlines(&cinfo, rows, 1);
    }
    jpeg_finish_compress(&cinfo);
    jpeg_destroy_compress(&cinfo);
    *size = jpeg_size;
    if (row != NULL)
        free(row);
    return 0;
}

static void capture_close(void)
{
    close(capture->fd);
    free(capture->buffer);
    capture->buffer = NULL;
    if (capture != NULL)
        free(capture);
    capture = NULL;
}

/* 1 on success, -1 on failure. */
int webcam_take_snapshot(void **jpeg, unsigned long *size)
{
    int result = 1;

    if (capture_init() == -1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "lib_Webcam: takeSnapshot: error init_input. abort\n");
        result = -1;
    } else if (capture_shot((unsigned char **)jpeg, size) == -1) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "lib_Webcam: takeSnapshot: error input_cam_takeOneShot. abort\n");
        result = -1;
    }
    capture_close();
    return result;
}
