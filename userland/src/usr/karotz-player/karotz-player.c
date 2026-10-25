/* SPDX-License-Identifier: MIT */
/*
 * karotz-player: the rabbit's player for sounds, URLs and web radio, which
 * multimedia-daemon and the update scripts run:
 *
 *   karotz-player [-include CONF] [-playlist] [-loop N] [-idle] ... PATH_OR_URL
 *
 * CONF holds key=value lines (/etc/conf/karotz-player.conf): input=file=FIFO names the
 * command FIFO, idle=yes keeps the player running after the last item, ao=oss:DEVICE the output
 * device (/dev/dsp by default), loop=N as -loop. Other keys and options (the -afm, -ao, -slave,
 * -quiet of older scripts) are accepted and ignored.
 *
 * Commands, one per line on the FIFO: loadfile ARG [0|1] and loadlist ARG [0|1] (0
 * replaces what is playing, 1 appends; ARG may be in single or double quotes), pause (toggles),
 * stop, quit [CODE]. A playlist (-playlist or loadlist) is an .m3u, .pls or .asx file or URL.
 *
 * Audio is decoded with FFmpeg and written to the OSS device as 16-bit samples at the stream's own
 * rate and channels (mono or stereo), as the device accepts them. The rabbit's CPU has no FPU, so
 * nothing is converted when the decoder's samples already match (the rabbit's own sounds, 22 kHz
 * mono), and anything else is converted by swresample in 16-bit integers. Vorbis is decoded by
 * Tremor, in fixed point, rather than by FFmpeg's floating-point decoder. While paused nothing is written; a network stream goes on being read (and dropped) so the
 * server keeps the connection, a file just waits. Without idle the player exits when the last
 * item has played, and SIGTERM ends it at once.
 */
#define _GNU_SOURCE  /* strcasestr, strndup */
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/ioctl.h>
#include <sys/soundcard.h>
#include <sys/stat.h>
#include <unistd.h>

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/opt.h>
#include <libswresample/swresample.h>
#include <tremor/ivorbiscodec.h>

#define MAX_ITEMS 256
#define PLAYLIST_MAX (64 * 1024)

static const char *fifo_path;
static const char *audio_device = "/dev/dsp";
static int idle_mode;
static int loops = 1;  /* -loop: 1 plays once, 0 forever */

static char *items[MAX_ITEMS];
static int n_items;

static int fifo_fd = -1;
static int dsp_fd = -1;
static int dsp_rate, dsp_channels;    /* what the device accepted */
static int asked_rate, asked_channels;  /* what it was opened for */
static int paused;
static int abort_item;  /* a command replaced what is playing (or stopped it) */
static int quit_code = -1;

/* ---- the item list ------------------------------------------------------- */

static void clear_items(void)
{
    for (int i = 0; i < n_items; i++)
        free(items[i]);
    n_items = 0;
}

static void add_item(const char *s)
{
    while (isspace((unsigned char)*s))
        s++;
    size_t n = strlen(s);
    while (n > 0 && isspace((unsigned char)s[n - 1]))
        n--;
    if (n == 0 || n_items == MAX_ITEMS)
        return;
    items[n_items++] = strndup(s, n);
}

/* An entry relative to the playlist's own location becomes absolute (or a full URL). */
static void add_relative(const char *base, const char *entry)
{
    if (strstr(entry, "://") != NULL || entry[0] == '/') {
        add_item(entry);
        return;
    }
    const char *slash = strrchr(base, '/');
    if (slash == NULL) {
        add_item(entry);
        return;
    }
    size_t dir = (size_t)(slash - base) + 1;
    char *full = malloc(dir + strlen(entry) + 1);
    if (full == NULL)
        return;
    memcpy(full, base, dir);
    strcpy(full + dir, entry);
    add_item(full);
    free(full);
}

/* The entries of an .m3u (lines not starting with #), .pls (FileN=) or .asx (href="...") playlist. */
static void add_playlist(const char *where)
{
    AVIOContext *io = NULL;
    char *text = malloc(PLAYLIST_MAX + 1);
    int len = 0;

    if (text == NULL)
        return;
    if (avio_open(&io, where, AVIO_FLAG_READ) < 0) {
        fprintf(stderr, "Failed to open playlist %s\n", where);
        free(text);
        return;
    }
    while (len < PLAYLIST_MAX) {
        int r = avio_read(io, (unsigned char *)text + len, PLAYLIST_MAX - len);
        if (r <= 0)
            break;
        len += r;
    }
    avio_closep(&io);
    text[len] = '\0';

    int before = n_items;
    if (strcasestr(text, "<asx") != NULL) {
        for (char *p = text; (p = strcasestr(p, "href")) != NULL; ) {
            p += 4;
            while (*p == ' ' || *p == '=')
                p++;
            char q = *p;
            if (q != '"' && q != '\'')
                continue;
            char *end = strchr(++p, q);
            if (end == NULL)
                break;
            *end = '\0';
            add_relative(where, p);
            p = end + 1;
        }
    } else {
        int pls = strcasestr(text, "[playlist]") != NULL;
        for (char *line = strtok(text, "\r\n"); line != NULL; line = strtok(NULL, "\r\n")) {
            while (isspace((unsigned char)*line))
                line++;
            if (pls) {
                if (strncasecmp(line, "file", 4) == 0 && strchr(line, '=') != NULL)
                    add_relative(where, strchr(line, '=') + 1);
            } else if (*line != '\0' && *line != '#') {
                add_relative(where, line);
            }
        }
    }
    free(text);
    printf("Playlist %s: %d entries\n", where, n_items - before);
}

/* ---- commands ------------------------------------------------------------ */

/* The first argument, without quotes; rest points after it. */
static char *argument(char *s, char **rest)
{
    while (isspace((unsigned char)*s))
        s++;
    char *start = s, *end;
    if (*s == '\'' || *s == '"') {
        char q = *s;
        start = s + 1;
        end = strchr(start, q);
        if (end == NULL)
            end = start + strlen(start);
    } else {
        end = start;
        while (*end != '\0' && !isspace((unsigned char)*end))
            end++;
    }
    *rest = *end != '\0' ? end + 1 : end;
    *end = '\0';
    return start;
}

static void command(char *line)
{
    char *rest, *word = argument(line, &rest);

    if (strcmp(word, "pause") == 0) {
        paused = !paused;
        printf(paused ? "  =====  PAUSE  =====\n" : "  =====  RESUME  =====\n");
    } else if (strcmp(word, "loadfile") == 0 || strcmp(word, "loadlist") == 0) {
        int list = word[4] == 'l';
        char *what = argument(rest, &rest);
        char *flag = argument(rest, &rest);
        if (*what == '\0')
            return;
        char *copy = strdup(what);
        if (copy == NULL)
            return;
        if (atoi(flag) == 0) {  /* replace */
            clear_items();
            abort_item = 1;
            paused = 0;
        }
        if (list)
            add_playlist(copy);
        else
            add_item(copy);
        free(copy);
    } else if (strcmp(word, "stop") == 0) {
        clear_items();
        abort_item = 1;
        paused = 0;
    } else if (strcmp(word, "quit") == 0) {
        quit_code = atoi(argument(rest, &rest));
        abort_item = 1;
    }
}

/* Read and act on whatever commands are waiting; wait up to timeout_ms for one. */
static void poll_commands(int timeout_ms)
{
    static char buf[4096];
    static size_t used;

    if (fifo_fd < 0) {
        if (timeout_ms > 0)
            usleep(timeout_ms * 1000);
        return;
    }
    struct pollfd p = { .fd = fifo_fd, .events = POLLIN };
    if (poll(&p, 1, timeout_ms) <= 0)
        return;
    ssize_t r = read(fifo_fd, buf + used, sizeof buf - 1 - used);
    if (r <= 0)
        return;
    used += (size_t)r;
    buf[used] = '\0';
    char *start = buf, *nl;
    while ((nl = strchr(start, '\n')) != NULL) {
        *nl = '\0';
        command(start);
        start = nl + 1;
    }
    used = strlen(start);
    memmove(buf, start, used);
    if (used == sizeof buf - 1)  /* a line too long to be a command */
        used = 0;
}

/* FFmpeg's blocking I/O checks this: a command can abandon a stalled connection. */
static int interrupted(void *opaque)
{
    (void)opaque;
    poll_commands(0);
    return abort_item || quit_code >= 0;
}

/* ---- output --------------------------------------------------------------- */

/* The device, set to 16-bit samples at rate and channels; kept open while the next item has the
 * same format, reopened for a different one (OSS sets the format on a fresh open). */
static int open_dsp(int rate, int channels)
{
    int v;

    if (dsp_fd >= 0) {
        if (rate == asked_rate && channels == asked_channels)
            return 0;
        close(dsp_fd);
    }
    asked_rate = rate;
    asked_channels = channels;
    dsp_fd = open(audio_device, O_WRONLY);
    if (dsp_fd < 0) {
        fprintf(stderr, "Can't open audio device %s: %s\n", audio_device, strerror(errno));
        return -1;
    }
    v = AFMT_S16_LE;
    ioctl(dsp_fd, SNDCTL_DSP_SETFMT, &v);
    v = channels;
    if (ioctl(dsp_fd, SNDCTL_DSP_CHANNELS, &v) < 0 || (v != 1 && v != 2))
        v = 2;
    dsp_channels = v;
    v = rate;
    if (ioctl(dsp_fd, SNDCTL_DSP_SPEED, &v) < 0 || v <= 0)
        v = 44100;
    dsp_rate = v;
    if (dsp_channels != channels || dsp_rate != rate)
        fprintf(stderr, "The audio device plays %d Hz, %d channel(s) for %d Hz, %d.\n",
                dsp_rate, dsp_channels, rate, channels);
    return 0;
}

static void write_dsp(const uint8_t *data, size_t len)
{
    while (len > 0 && !abort_item) {
        ssize_t w = write(dsp_fd, data, len);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            return;
        }
        data += w;
        len -= (size_t)w;
    }
}

/* ---- samples to the device ---------------------------------------------------- */

/* One item's output: the device is set from the first samples; they are written as they are when
 * they already match it, or converted by swresample in 16-bit integers. */
struct output {
    int direct;  /* -1 until the first samples */
    SwrContext *swr;
    uint8_t *buf;
    int cap;
};

static int output(struct output *o, const uint8_t **data, enum AVSampleFormat format,
                  const AVChannelLayout *layout, int rate, int samples)
{
    int channels = layout->nb_channels;

    if (o->direct < 0) {
        if (open_dsp(rate, channels > 1 ? 2 : 1) < 0)
            return -1;
        /* 16-bit packed samples, or planar mono (the same bytes), at the device's format. */
        o->direct = channels == dsp_channels && rate == dsp_rate &&
                    (format == AV_SAMPLE_FMT_S16 || (format == AV_SAMPLE_FMT_S16P && channels == 1));
        if (!o->direct) {
            AVChannelLayout to;
            av_channel_layout_default(&to, dsp_channels);
            if (swr_alloc_set_opts2(&o->swr, &to, AV_SAMPLE_FMT_S16, dsp_rate, layout, format, rate, 0, NULL) < 0 ||
                av_opt_set_sample_fmt(o->swr, "internal_sample_fmt", AV_SAMPLE_FMT_S16P, 0) < 0 ||
                swr_init(o->swr) < 0)
                return -1;
        }
    }
    if (o->direct) {
        write_dsp(data[0], (size_t)samples * channels * 2);
        return 0;
    }
    int frame_bytes = dsp_channels * 2;
    int n = swr_get_out_samples(o->swr, samples);
    if (n * frame_bytes > o->cap) {
        o->cap = n * frame_bytes;
        av_free(o->buf);
        if ((o->buf = av_malloc(o->cap)) == NULL)
            return -1;
    }
    uint8_t *out[1] = { o->buf };
    int got = swr_convert(o->swr, out, n, data, samples);
    if (got > 0)
        write_dsp(o->buf, (size_t)got * frame_bytes);
    return 0;
}

static void output_free(struct output *o)
{
    swr_free(&o->swr);
    av_free(o->buf);
}

/* ---- Vorbis, with Tremor --------------------------------------------------------- */

/* FFmpeg's Vorbis decoder works in floating point, which the rabbit's CPU emulates far too slowly
 * to keep up; Tremor decodes in fixed point, from the packets FFmpeg demuxes. */
struct tremor {
    vorbis_info vi;
    vorbis_comment vc;
    vorbis_dsp_state vd;
    vorbis_block vb;
    int started;  /* vd and vb are set up */
    long packetno;
    int16_t *pcm;
    int cap;
};

/* The three header packets in a Vorbis stream's extradata: Xiph lacing (a count of 2, then the
 * first two sizes in 255-steps) or, as some containers store them, each after a 16-bit size. */
static int vorbis_headers(const uint8_t *x, int size, const uint8_t *hdr[3], int len[3])
{
    int p, i;

    if (size >= 3 && x[0] == 2) {
        p = 1;
        for (i = 0; i < 2; i++) {
            len[i] = 0;
            while (p < size && x[p] == 255)
                len[i] += x[p++];
            if (p >= size)
                return -1;
            len[i] += x[p++];
        }
        len[2] = size - p - len[0] - len[1];
        if (len[2] <= 0)
            return -1;
        hdr[0] = x + p;
        hdr[1] = hdr[0] + len[0];
        hdr[2] = hdr[1] + len[1];
        return 0;
    }
    for (p = 0, i = 0; i < 3; i++) {
        if (p + 2 > size)
            return -1;
        len[i] = (x[p] << 8) | x[p + 1];
        hdr[i] = x + p + 2;
        p += 2 + len[i];
        if (p > size)
            return -1;
    }
    return 0;
}

static int tremor_open(struct tremor *t, const AVCodecParameters *par)
{
    const uint8_t *hdr[3];
    int len[3], i;

    vorbis_info_init(&t->vi);
    vorbis_comment_init(&t->vc);
    if (par->extradata == NULL || vorbis_headers(par->extradata, par->extradata_size, hdr, len) < 0)
        return -1;
    for (i = 0; i < 3; i++) {
        ogg_packet op = { 0 };
        op.packet = (unsigned char *)hdr[i];
        op.bytes = len[i];
        op.b_o_s = i == 0;
        op.packetno = i;
        if (vorbis_synthesis_headerin(&t->vi, &t->vc, &op) < 0)
            return -1;
    }
    if (vorbis_synthesis_init(&t->vd, &t->vi) != 0)
        return -1;
    vorbis_block_init(&t->vd, &t->vb);
    t->started = 1;
    t->packetno = 3;
    return 0;
}

/* One packet: decode it, then write what it completes (the first two channels of more than two). */
static int tremor_packet(struct tremor *t, const AVPacket *pkt, struct output *o)
{
    ogg_packet op = { 0 };
    ogg_int32_t **pcm;
    int n, c, j;
    int channels = t->vi.channels > 2 ? 2 : t->vi.channels;
    AVChannelLayout layout;

    op.packet = pkt->data;
    op.bytes = pkt->size;
    op.packetno = t->packetno++;
    op.granulepos = -1;
    if (vorbis_synthesis(&t->vb, &op) == 0)
        vorbis_synthesis_blockin(&t->vd, &t->vb);
    av_channel_layout_default(&layout, channels);
    while ((n = vorbis_synthesis_pcmout(&t->vd, &pcm)) > 0) {
        if (n * channels > t->cap) {
            t->cap = n * channels;
            av_free(t->pcm);
            if ((t->pcm = av_malloc((size_t)t->cap * sizeof *t->pcm)) == NULL)
                return -1;
        }
        /* Tremor's samples have 9 bits below 16-bit range, as its own ov_read takes them. */
        for (c = 0; c < channels; c++)
            for (j = 0; j < n; j++) {
                ogg_int32_t v = pcm[c][j] >> 9;
                t->pcm[j * channels + c] = v > 32767 ? 32767 : v < -32768 ? -32768 : (int16_t)v;
            }
        const uint8_t *data[1] = { (const uint8_t *)t->pcm };
        if (output(o, data, AV_SAMPLE_FMT_S16, &layout, (int)t->vi.rate, n) < 0)
            return -1;
        vorbis_synthesis_read(&t->vd, n);
    }
    return 0;
}

static void tremor_close(struct tremor *t)
{
    if (t->started) {
        vorbis_block_clear(&t->vb);
        vorbis_dsp_clear(&t->vd);
    }
    vorbis_comment_clear(&t->vc);
    vorbis_info_clear(&t->vi);
    av_free(t->pcm);
}

/* ---- one item --------------------------------------------------------------- */

static void play(const char *url)
{
    AVFormatContext *fmt = avformat_alloc_context();
    AVCodecContext *dec = NULL;
    AVPacket *pkt = av_packet_alloc();
    AVFrame *frame = av_frame_alloc();
    AVDictionary *opts = NULL;
    struct output out = { -1, NULL, NULL, 0 };
    struct tremor tremor;
    int vorbis = 0, stream;

    memset(&tremor, 0, sizeof tremor);

    if (fmt == NULL || pkt == NULL || frame == NULL)
        goto done;
    fmt->interrupt_callback.callback = interrupted;
    av_dict_set(&opts, "reconnect", "1", 0);
    av_dict_set(&opts, "rw_timeout", "15000000", 0);
    printf("Playing %s.\n", url);
    fflush(stdout);
    if (avformat_open_input(&fmt, url, NULL, &opts) < 0) {
        fprintf(stderr, "Failed to open %s.\n", url);
        goto done;
    }
    if (avformat_find_stream_info(fmt, NULL) < 0)
        goto done;
    stream = av_find_best_stream(fmt, AVMEDIA_TYPE_AUDIO, -1, -1, NULL, 0);
    if (stream < 0) {
        fprintf(stderr, "No audio in %s.\n", url);
        goto done;
    }
    const AVCodecParameters *par = fmt->streams[stream]->codecpar;
    if (par->codec_id == AV_CODEC_ID_VORBIS) {
        vorbis = 1;
        if (tremor_open(&tremor, par) < 0) {
            fprintf(stderr, "Can't read the Vorbis headers of %s.\n", url);
            goto done;
        }
    } else {
        const AVCodec *codec = avcodec_find_decoder(par->codec_id);
        if (codec == NULL || (dec = avcodec_alloc_context3(codec)) == NULL ||
            avcodec_parameters_to_context(dec, par) < 0 || avcodec_open2(dec, codec, NULL) < 0) {
            fprintf(stderr, "No decoder for %s.\n", url);
            goto done;
        }
    }
    int network = fmt->pb == NULL || !(fmt->pb->seekable & AVIO_SEEKABLE_NORMAL);

    while (!abort_item && quit_code < 0) {
        poll_commands(0);
        if (paused && !network) {
            poll_commands(100);
            continue;
        }
        int r = av_read_frame(fmt, pkt);
        if (r < 0)
            break;  /* the end, or the connection is gone */
        if (pkt->stream_index != stream || paused) {
            av_packet_unref(pkt);
            continue;
        }
        if (vorbis) {
            r = tremor_packet(&tremor, pkt, &out);
            av_packet_unref(pkt);
            if (r < 0) {
                fprintf(stderr, "Can't play the audio of %s.\n", url);
                goto done;
            }
            continue;
        }
        if (avcodec_send_packet(dec, pkt) < 0) {
            av_packet_unref(pkt);
            continue;
        }
        av_packet_unref(pkt);
        while (avcodec_receive_frame(dec, frame) == 0) {
            r = output(&out, (const uint8_t **)frame->extended_data, frame->format, &frame->ch_layout,
                       frame->sample_rate, frame->nb_samples);
            av_frame_unref(frame);
            if (r < 0) {
                fprintf(stderr, "Can't play the audio of %s.\n", url);
                goto done;
            }
        }
    }
done:
    av_dict_free(&opts);
    output_free(&out);
    if (vorbis)
        tremor_close(&tremor);
    avcodec_free_context(&dec);
    avformat_close_input(&fmt);
    av_packet_free(&pkt);
    av_frame_free(&frame);
}

/* Play the list (loops times, or forever with 0). A command that replaces the list starts the
 * new one from its first item; stop (an empty list) or quit ends it. */
static void run_list(void)
{
    int pass = 0;

    while (quit_code < 0 && n_items > 0 && (loops == 0 || pass < loops)) {
        int replaced = 0;
        for (int i = 0; i < n_items && quit_code < 0; i++) {
            char *url = strdup(items[i]);
            if (url == NULL)
                return;
            abort_item = 0;
            play(url);
            free(url);
            if (abort_item) {
                abort_item = 0;
                replaced = 1;
                break;
            }
        }
        if (replaced)
            pass = 0;
        else
            pass++;
    }
}

/* ---- setup -------------------------------------------------------------- */

static void config_line(char *line)
{
    char *eq = strchr(line, '=');
    if (line[0] == '#' || eq == NULL)
        return;
    *eq = '\0';
    char *key = line, *value = eq + 1;
    value[strcspn(value, "\r\n")] = '\0';
    if (strcmp(key, "input") == 0 && strncmp(value, "file=", 5) == 0)
        fifo_path = strdup(value + 5);
    else if (strcmp(key, "idle") == 0)
        idle_mode = strcmp(value, "yes") == 0 || strcmp(value, "1") == 0;
    else if (strcmp(key, "ao") == 0 && strncmp(value, "oss:", 4) == 0)
        audio_device = strdup(value + 4);
    else if (strcmp(key, "loop") == 0)
        loops = atoi(value);
}

static void read_config(const char *path)
{
    char line[512];
    FILE *f = fopen(path, "r");
    if (f == NULL)
        return;
    while (fgets(line, sizeof line, f) != NULL)
        config_line(line);
    fclose(f);
}

/* Options that take a value, so their value isn't taken for the item. */
static int takes_value(const char *opt)
{
    static const char *const with[] = { "afm", "ao", "ac", "af", "volume", "cache", "user-agent", "softvol-max", NULL };
    for (int i = 0; with[i] != NULL; i++)
        if (strcmp(opt, with[i]) == 0)
            return 1;
    return 0;
}

int main(int argc, char **argv)
{
    int playlist = 0;
    const char *target = NULL;

    signal(SIGPIPE, SIG_IGN);
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (a[0] != '-' || a[1] == '\0') {
            target = a;
            continue;
        }
        const char *opt = a + (a[1] == '-' ? 2 : 1);
        if (strcmp(opt, "include") == 0 && i + 1 < argc)
            read_config(argv[++i]);
        else if (strcmp(opt, "playlist") == 0)
            playlist = 1;
        else if (strcmp(opt, "loop") == 0 && i + 1 < argc)
            loops = atoi(argv[++i]);
        else if (strcmp(opt, "idle") == 0)
            idle_mode = 1;
        else if (strcmp(opt, "input") == 0 && i + 1 < argc && strncmp(argv[i + 1], "file=", 5) == 0)
            fifo_path = argv[++i] + 5;
        else if (takes_value(opt) && i + 1 < argc)
            i++;
        /* anything else (-slave, -quiet, -noconsolecontrols...) is accepted and ignored */
    }
    avformat_network_init();
    if (fifo_path != NULL) {
        struct stat st;
        if (stat(fifo_path, &st) != 0)
            mkfifo(fifo_path, 0666);
        /* Read and write: a writer closing its end never gives us EOF, and the daemon's
         * open() for writing never waits for us. */
        fifo_fd = open(fifo_path, O_RDWR | O_NONBLOCK);
    }
    if (target != NULL) {
        if (playlist)
            add_playlist(target);
        else
            add_item(target);
    }
    run_list();
    while (idle_mode && quit_code < 0) {
        poll_commands(200);
        if (n_items > 0) {
            run_list();
            clear_items();
        }
    }
    if (dsp_fd >= 0)
        close(dsp_fd);
    avformat_network_deinit();
    return quit_code >= 0 ? quit_code : 0;
}
