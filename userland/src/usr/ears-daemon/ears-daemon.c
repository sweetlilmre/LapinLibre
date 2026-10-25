/* SPDX-License-Identifier: MIT */
/*
 * ears-daemon: drive the two ear motors through /dev/rabbit_ears on behalf of
 * D-Bus clients of com.mindscape.karotz.Ears (/com/mindscape/karotz/Ears).
 *
 * Every 60 ms the daemon polls the driver: it reads one encoder event, runs
 * the pending reset and move requests of its three "motions" (right ear, left
 * ear, both ears) and watches each ear's position to report when it starts,
 * stops or is blocked. Methods only queue a request (tagged with the caller's
 * id); the polling tick carries it out and reports back with the "event",
 * "reset_finished" and move_* signals.
 *
 * The ears_* / *_motor* functions are the same motor library the
 * standalone "ears" tool is built on (src/rootfs/ears/ears.c).
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <syslog.h>
#include <unistd.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include "karotz-common.h"
#include "karotz-messages.h"
#include "karotz-marshal.h"

/* ------------------------------------------------------------------------ */
/* Motor library                                                             */
/* ------------------------------------------------------------------------ */

/* A motor command: speed (1..255) and the target step count. */
struct ear_move {
    int pwm;
    int pos;
};

/*
 * The driver's position counters (EARS_IOC_GET_POS). EARS_IOC_SET_POS takes
 * the same layout; the daemon writes 0 into the counter it wants zeroed and 1
 * into the other.
 */
struct ear_positions {
    int right;
    int left;
};

/* ioctls of the rabbit_ears driver ('E', 0x45). */
#define EARS_IOC_MOVE_RIGHT  _IOW('E', 1, struct ear_move)       /* 0x40084501 */
#define EARS_IOC_MOVE_LEFT   _IOW('E', 2, struct ear_move)       /* 0x40084502 */
#define EARS_IOC_GET_POS     _IOR('E', 3, struct ear_positions)  /* 0x80084503 */
#define EARS_IOC_SET_POS     _IOW('E', 3, struct ear_positions)  /* 0x40084503 */
/* EARS_IOC_SET_POS: a counter given as 0 is zeroed, any other value leaves
 * it as it is (the driver's ioctl 3 write). */
#define COUNTER_ZERO 0
#define COUNTER_KEEP 1
#define EARS_IOC_GET_STATUS  _IOR('E', 4, int)                   /* 0x80044504 */
#define EARS_IOC_SET_STATUS  _IOW('E', 4, int)                   /* 0x40044504 */

/* ears_update() flags: which ear the driver reported on. */
#define EAR_LEFT  1
#define EAR_RIGHT 2

/* An event byte from the driver: bit 7 set for the left ear, the low seven
 * bits the time since that ear's previous step, in 20 ms units. */
#define EAR_EVENT_LEFT       0x80
#define EAR_EVENT_TIME_MASK  0x7f

#define EAR_PWM_MAX          255  /* full speed */
#define EAR_RESET_STEPS      100  /* driven backwards this far: about six turns, enough to pass the gap */
/* During a reset, steps further apart than this (24 x 20 ms = 480 ms) mean
 * the encoder's gap has gone by: the ears turn freely, and the reference
 * position is a missing step in each ear's encoder. The long interval is only
 * reported with the first step after the gap, and the ear coasts on after its
 * motor stops, so it rests up to a step past the zeroed counter (the
 * counters usually read -1 after a reset). Known issue: where counter 0 lies
 * physically therefore varies by up to a step, and the ears have been seen
 * out of line after a reset (not reproduced since). */
#define RESET_VALUE          24

/* Driver status bits: an ear's motor is running, an ear needs a reset. */
#define STATUS_LEFT_BUSY    1
#define STATUS_RIGHT_BUSY   2
#define STATUS_LEFT_LOST    4
#define STATUS_RIGHT_LOST   8

static const char *device = "/dev/rabbit_ears";
static int gFd;

static int stop_motorL(void)
{
    struct ear_move m = { 0, 0 };
    return ioctl(gFd, EARS_IOC_MOVE_LEFT, &m);
}

static int stop_motorR(void)
{
    struct ear_move m = { 0, 0 };
    return ioctl(gFd, EARS_IOC_MOVE_RIGHT, &m);
}

static int stop_motors(void)
{
    struct ear_move m = { 0, 0 };
    int r = ioctl(gFd, EARS_IOC_MOVE_LEFT, &m);
    return r | ioctl(gFd, EARS_IOC_MOVE_RIGHT, &m);
}

static int rotate_motor_private(int pwmL, int posL, int pwmR, int posR)
{
    struct ear_move right = { pwmR, posR };
    struct ear_move left = { pwmL, posL };
    int r = ioctl(gFd, EARS_IOC_MOVE_RIGHT, &right);
    return r | ioctl(gFd, EARS_IOC_MOVE_LEFT, &left);
}

static int rotate_motor_privateL(int pwm, int pos)
{
    struct ear_move m = { pwm, pos };
    return ioctl(gFd, EARS_IOC_MOVE_LEFT, &m);
}

static int rotate_motor_privateR(int pwm, int pos)
{
    struct ear_move m = { pwm, pos };
    return ioctl(gFd, EARS_IOC_MOVE_RIGHT, &m);
}

static int ears_get_position(struct ear_positions *pos)
{
    return ioctl(gFd, EARS_IOC_GET_POS, pos);
}

static int ears_set_position(struct ear_positions *pos)
{
    return ioctl(gFd, EARS_IOC_SET_POS, pos);
}

static unsigned ears_get_status(void)
{
    /* A failed ioctl gives the last status read: an uninitialised value
     * could end a running move or set off the blocked check. */
    static unsigned last = 0;  /* no motor busy, until a status is read */
    unsigned status;

    if (ioctl(gFd, EARS_IOC_GET_STATUS, &status) == 0)
        last = status;
    return last;
}

static int ears_set_status(int status)
{
    return ioctl(gFd, EARS_IOC_SET_STATUS, &status);
}

/*
 * Read one event from the driver: bit 7 set means the left ear, the low
 * seven bits are the value. Returns EAR_LEFT, EAR_RIGHT or 0 (no event).
 */
static int ears_update(unsigned char *value)
{
    unsigned char event;
    int flags = 0;

    if (read(gFd, &event, 1) != 0) {
        if (event & EAR_EVENT_LEFT) {
            *value = event & EAR_EVENT_TIME_MASK;
            flags |= EAR_LEFT;
        } else {
            *value = event;
            flags |= EAR_RIGHT;
        }
    }
    return flags;
}

static int ears_init(void)
{
    gFd = open(device, O_RDWR);
    if (gFd < 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "can't open motor device");
        abort();
    }
    return 0;
}

/* Discard queued events: the driver's read() returns 0 once it is empty. */
static void ears_flush(void)
{
    char buf[32];
    while (read(gFd, buf, sizeof buf) != 0)
        ;
}

/* ------------------------------------------------------------------------ */
/* The D-Bus object                                                          */
/* ------------------------------------------------------------------------ */

#define TICK_MS 60          /* driver polling period */
#define RESET_STATUS_DELAY_MS 300
#define STOPPED_MS 1300     /* still this long (more than) while moving: stopped */
#define BLOCKED_MS 1400     /* still this long (at least) during a move: blocked */
#define STEPS_PER_TURN 17

enum {
    SIGNAL_EVENT,
    SIGNAL_REGISTERED,
    SIGNAL_MOVE_LEFT_START,
    SIGNAL_MOVE_RIGHT_START,
    SIGNAL_MOVE_LEFT_STOP,
    SIGNAL_MOVE_RIGHT_STOP,
    SIGNAL_MOVE_LEFT_BLOCKED,
    SIGNAL_MOVE_RIGHT_BLOCKED,
    SIGNAL_RESET_FINISHED,
    N_SIGNALS
};

/* Codes carried by the "event" signal. */
enum { EVENT_MOVE_DONE = 4, EVENT_INTERRUPTED = 5 };

/* Motion.move_state / Motion.reset_state */
enum { REQUEST_IDLE, REQUEST_PENDING, REQUEST_RUNNING };

/* EarWatch.state */
enum { WATCH_STILL, WATCH_STARTING, WATCH_MOVING, WATCH_STOPPING };

/*
 * One of the three request channels: 'R' (right ear), 'L' (left ear) or 'B'
 * (both). A channel holds at most one pending reset and one pending move.
 */
typedef struct Motion {
    char ear;
    int move_state;
    int absolute; /* move (target position) rather than move_step (step count) */
    int right_position, right_speed;
    int left_position, left_speed;
    unsigned busy_mask; /* driver status bits of the motors this channel uses */
    void (*start_move)(struct Motion *motion);
    int reset_state;
    int right_reset_done, right_skip_first;
    int left_reset_done, left_skip_first;
    int (*stop)(void);
    void (*start_reset)(void);
    GString *id; /* caller's id, echoed in the signals */
} Motion;

/* Watches one ear's position counter to report start, stop and blockage. */
typedef struct {
    char ear;
    int state;
    int still_ms;  /* time the position has not changed */
    int moving_ms; /* time since the ear started moving */
    int steps;     /* position changes since it started moving */
    int last_position;
    unsigned busy_mask;
} EarWatch;

typedef struct {
    GObject parent;
    Motion right, left, both;
    EarWatch right_watch, left_watch;
} EarsObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} EarsObjectClass;

GType ears_object_get_type(void);
#define EARS_TYPE_OBJECT (ears_object_get_type())

G_DEFINE_TYPE(EarsObject, ears_object, G_TYPE_OBJECT)

gboolean ears_object_reset(EarsObject *self, const char *id, const char *ears, gboolean *result, GError **error);
gboolean ears_object_move(EarsObject *self, const char *id, int l_position, int l_speed, int r_position,
                          int r_speed, gboolean *result, GError **error);
gboolean ears_object_move_left(EarsObject *self, const char *id, int l_position, int l_speed, gboolean *result,
                               GError **error);
gboolean ears_object_move_right(EarsObject *self, const char *id, int r_position, int r_speed, gboolean *result,
                                GError **error);
gboolean ears_object_move_step(EarsObject *self, const char *id, int l_position, int l_speed, int r_position,
                               int r_speed, gboolean *result, GError **error);
gboolean ears_object_move_left_step(EarsObject *self, const char *id, int l_position, int l_speed,
                                    gboolean *result, GError **error);
gboolean ears_object_move_right_step(EarsObject *self, const char *id, int r_position, int r_speed,
                                     gboolean *result, GError **error);

#include "ears-daemon-glue.h"

static EarsObject *ears;
static int right_reset_steps; /* position counters read when each ear passed its gap */
static int left_reset_steps;

#define SIGNAL(name) (((EarsObjectClass *)G_OBJECT_GET_CLASS(ears))->signals[name])

/* Step counts are taken modulo one turn, then folded into -8..8. */
static int positive_mod_turn(int steps)
{
    int r = steps % STEPS_PER_TURN;
    if (r < 0)
        r += STEPS_PER_TURN;
    return r;
}

static int shortest_steps(int steps)
{
    if (steps > 8)
        return steps - STEPS_PER_TURN;
    if (steps < -8)
        /* Unreachable today (every caller passes positive_mod_turn()'s
         * 0..16), but correct for any input. */
        return steps + STEPS_PER_TURN;
    return steps;
}

/* --- reset starters (Motion.start_reset): run the ears backwards to the gap --- */

static void start_reset_both(void)
{
    struct ear_positions pos;

    ears->right_watch.still_ms = 0;
    ears->left_watch.still_ms = 0;
    pos.right = COUNTER_ZERO;
    pos.left = COUNTER_ZERO;
    ears_set_position(&pos);
    ears_get_position(&pos);
    rotate_motor_private(EAR_PWM_MAX, pos.left - EAR_RESET_STEPS, EAR_PWM_MAX, pos.right - EAR_RESET_STEPS);
}

static void start_reset_right(void)
{
    struct ear_positions pos;

    ears->right_watch.still_ms = 0;
    pos.right = COUNTER_ZERO;
    pos.left = COUNTER_KEEP;
    ears_set_position(&pos);
    ears_get_position(&pos);
    rotate_motor_privateR(EAR_PWM_MAX, pos.right - EAR_RESET_STEPS);
}

static void start_reset_left(void)
{
    struct ear_positions pos;

    ears->left_watch.still_ms = 0;
    pos.right = COUNTER_KEEP;
    pos.left = COUNTER_ZERO;
    ears_set_position(&pos);
    ears_get_position(&pos);
    rotate_motor_privateL(EAR_PWM_MAX, pos.left - EAR_RESET_STEPS);
}

/* --- move starters (Motion.start_move) --- */

static void start_move_both(Motion *motion)
{
    struct ear_positions pos;
    int right_steps, left_steps;

    ears->right_watch.still_ms = 0;
    ears->left_watch.still_ms = 0;
    ears_get_position(&pos);
    if (!motion->absolute) {
        left_steps = motion->left_position;
        right_steps = motion->right_position;
    } else {
        /*
         * The targets are measured from the positions read above, as for one
         * ear; the counters are not touched.
         */
        left_steps = shortest_steps(positive_mod_turn(motion->left_position - pos.left));
        right_steps = shortest_steps(positive_mod_turn(motion->right_position - pos.right));
    }
    rotate_motor_private(motion->left_speed, pos.left + left_steps, motion->right_speed, pos.right + right_steps);
}

static void start_move_right(Motion *motion)
{
    struct ear_positions pos;
    int steps;

    ears->right_watch.still_ms = 0;
    ears_get_position(&pos);
    if (!motion->absolute)
        steps = motion->right_position;
    else
        steps = shortest_steps(positive_mod_turn(motion->right_position - pos.right));
    rotate_motor_privateR(motion->right_speed, pos.right + steps);
}

static void start_move_left(Motion *motion)
{
    struct ear_positions pos;
    int steps;

    ears->left_watch.still_ms = 0;
    ears_get_position(&pos);
    if (!motion->absolute)
        steps = motion->left_position;
    else
        steps = shortest_steps(positive_mod_turn(motion->left_position - pos.left));
    rotate_motor_privateL(motion->left_speed, pos.left + steps);
}

static void init_motions(EarsObject *self)
{
    self->right.ear = 'R';
    self->left.ear = 'L';
    self->both.ear = 'B';
    self->right_watch.ear = 'R';
    self->left_watch.ear = 'L';
    self->right.move_state = REQUEST_IDLE;
    self->left.move_state = REQUEST_IDLE;
    self->both.move_state = REQUEST_IDLE;
    self->right.reset_state = REQUEST_IDLE;
    self->left.reset_state = REQUEST_IDLE;
    self->both.reset_state = REQUEST_IDLE;
    self->right.stop = stop_motorR;
    self->left.stop = stop_motorL;
    self->both.stop = stop_motors;
    self->right.start_reset = start_reset_right;
    self->left.start_reset = start_reset_left;
    self->both.start_reset = start_reset_both;
    self->right.start_move = start_move_right;
    self->left.start_move = start_move_left;
    self->both.start_move = start_move_both;
    self->right.busy_mask = STATUS_RIGHT_BUSY;
    self->left.busy_mask = STATUS_LEFT_BUSY;
    self->both.busy_mask = STATUS_LEFT_BUSY | STATUS_RIGHT_BUSY;
    self->right_watch.busy_mask = STATUS_RIGHT_BUSY;
    self->left_watch.busy_mask = STATUS_LEFT_BUSY;
    self->right_watch.state = WATCH_STILL;
    self->left_watch.state = WATCH_STILL;
}

/* --- request queueing (called from the D-Bus methods) --- */

static void request_move_right(int position, int speed, int absolute)
{
    ears->right.absolute = absolute;
    ears->right.move_state = REQUEST_PENDING;
    ears->right.right_position = position;
    ears->right.right_speed = speed;
}

static void request_move_left(int position, int speed, int absolute)
{
    ears->left.absolute = absolute;
    ears->left.move_state = REQUEST_PENDING;
    ears->left.left_position = position;
    ears->left.left_speed = speed;
}

static void request_move_both(int r_position, int r_speed, int l_position, int l_speed, int absolute)
{
    ears->both.absolute = absolute;
    ears->both.move_state = REQUEST_PENDING;
    ears->both.right_position = r_position;
    ears->both.right_speed = r_speed;
    ears->both.left_position = l_position;
    ears->both.left_speed = l_speed;
}

/* The *_done flags say which ears still have to pass their gap. */
static void request_reset_right(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s", "createAsyncResetR");
    ears->right.reset_state = REQUEST_PENDING;
    ears->right.right_reset_done = 0;
    ears->right.right_skip_first = 1;
    ears->right.left_reset_done = 1;
    ears->right.left_skip_first = 1;
}

static void request_reset_left(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s", "createAsyncResetL");
    ears->left.reset_state = REQUEST_PENDING;
    ears->left.right_reset_done = 1;
    ears->left.right_skip_first = 1;
    ears->left.left_reset_done = 0;
    ears->left.left_skip_first = 1;
}

static void request_reset_both(void)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s", "createAsyncResetRandL");
    ears->both.reset_state = REQUEST_PENDING;
    ears->both.right_reset_done = 0;
    ears->both.right_skip_first = 1;
    ears->both.left_reset_done = 0;
    ears->both.left_skip_first = 1;
}

/* Abandon whatever the channel is doing, telling its caller. */
static void cancel_motion(Motion *motion)
{
    if (motion->reset_state != REQUEST_IDLE || motion->move_state != REQUEST_IDLE) {
        motion->stop();
        g_signal_emit(ears, SIGNAL(SIGNAL_EVENT), 0, motion->id->str, EVENT_INTERRUPTED);
        motion->reset_state = REQUEST_IDLE;
        motion->move_state = REQUEST_IDLE;
    }
}

static void set_motion_id(char ear, const char *id)
{
    if (ear == 'L')
        g_string_assign(ears->left.id, id);
    else if (ear == 'R')
        g_string_assign(ears->right.id, id);
    else if (ear == 'B')
        g_string_assign(ears->both.id, id);
}

/* A new request for an ear cancels every channel that drives that ear. */
static void cancel_motions(char ear)
{
    if (ear == 'L') {
        cancel_motion(&ears->left);
        cancel_motion(&ears->both);
    } else if (ear == 'R') {
        cancel_motion(&ears->right);
        cancel_motion(&ears->both);
    } else if (ear == 'B') {
        cancel_motion(&ears->right);
        cancel_motion(&ears->left);
        cancel_motion(&ears->both);
    }
    /* The counters are left alone, so they keep the ears' positions since
     * their last reset. */
}

/* --- the reset state machine --- */

/* One-shot timeout armed when an ear passes its gap. */
static gboolean reset_status(gpointer data)
{
    int status = GPOINTER_TO_INT(data);
    unsigned s;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "reset status %d", status);
    ears_set_status(status);
    s = ears_get_status();
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Asyncstatus %d%d%d%d", s & 1, (s & 2) >> 1, (s & 4) >> 2, (s & 8) >> 3);
    return FALSE;
}

/* Feed one driver event into a running reset: stop each ear at its gap. */
static void run_reset(Motion *motion, int flags, unsigned char value)
{
    struct ear_positions pos;
    unsigned status;

    if ((flags & EAR_LEFT) && !motion->left_reset_done) {
        if (motion->left_skip_first) {
            motion->left_skip_first = 0;
        } else if (value > RESET_VALUE) {
            motion->left_reset_done = 1;
            stop_motorL();
            ears_get_position(&pos);
            left_reset_steps = pos.left;
            g_log(NULL, G_LOG_LEVEL_DEBUG, "left ear reset steps %d", pos.left);
            pos.right = COUNTER_KEEP;
            pos.left = COUNTER_ZERO;
            ears_set_position(&pos);
            g_timeout_add(RESET_STATUS_DELAY_MS, reset_status, GINT_TO_POINTER(STATUS_RIGHT_LOST));
        }
    }
    if ((flags & EAR_RIGHT) && !motion->right_reset_done) {
        if (motion->right_skip_first) {
            motion->right_skip_first = 0;
        } else if (value > RESET_VALUE) {
            motion->right_reset_done = 1;
            stop_motorR();
            ears_get_position(&pos);
            right_reset_steps = pos.right;
            g_log(NULL, G_LOG_LEVEL_DEBUG, "right ear reset steps %d", pos.right);
            pos.right = COUNTER_ZERO;
            pos.left = COUNTER_KEEP;
            ears_set_position(&pos);
            g_timeout_add(RESET_STATUS_DELAY_MS, reset_status, GINT_TO_POINTER(STATUS_LEFT_LOST));
        }
    }
    if (motion->left_reset_done && motion->right_reset_done) {
        motion->reset_state = REQUEST_IDLE;
        if (motion->move_state == REQUEST_IDLE) {
            ears_get_position(&pos); /* result unused */
            g_log(NULL, G_LOG_LEVEL_DEBUG, "ears reset steps: right %d, left %d", -right_reset_steps, -left_reset_steps);
            g_signal_emit(ears, SIGNAL(SIGNAL_RESET_FINISHED), 0, motion->id->str, -right_reset_steps,
                          -left_reset_steps);
        }
        status = ears_get_status();
        g_log(NULL, G_LOG_LEVEL_DEBUG, "RLstatus %d%d%d%d", status & 1, (status & 2) >> 1, (status & 4) >> 2,
              (status & 8) >> 3);
    }
}

static void step_reset(Motion *motion, int flags, unsigned char value)
{
    if (motion->reset_state == REQUEST_PENDING) {
        motion->reset_state = REQUEST_RUNNING;
        motion->stop();
        ears_flush();
        motion->start_reset();
    } else if (motion->reset_state == REQUEST_RUNNING) {
        run_reset(motion, flags, value);
    }
}

/* --- the move state machine (waits for any reset of the channel) --- */

static void step_move(Motion *motion, unsigned status)
{
    if (motion->reset_state != REQUEST_IDLE)
        return;
    if (motion->move_state == REQUEST_PENDING) {
        motion->move_state = REQUEST_RUNNING;
        motion->start_move(motion);
    } else if (motion->move_state == REQUEST_RUNNING && (motion->busy_mask & status) == 0) {
        motion->move_state = REQUEST_IDLE;
        g_signal_emit(ears, SIGNAL(SIGNAL_EVENT), 0, motion->id->str, EVENT_MOVE_DONE);
    }
}

/* Abort a running move whose ear has not moved for BLOCKED_MS. */
static void check_blocked(Motion *motion, EarWatch *right, EarWatch *left)
{
    gboolean right_blocked, left_blocked;

    if (motion->move_state != REQUEST_RUNNING)
        return;
    right_blocked = right != NULL && right->still_ms >= BLOCKED_MS;
    left_blocked = left != NULL && left->still_ms >= BLOCKED_MS;
    if (!right_blocked && !left_blocked)
        return;
    /* Stopped once, and one interrupted event for the move, however many of
     * its ears are blocked; each blocked ear still gets its own signal. */
    motion->stop();
    if (right_blocked)
        g_signal_emit(ears, SIGNAL(SIGNAL_MOVE_RIGHT_BLOCKED), 0);
    if (left_blocked)
        g_signal_emit(ears, SIGNAL(SIGNAL_MOVE_LEFT_BLOCKED), 0);
    g_signal_emit(ears, SIGNAL(SIGNAL_EVENT), 0, motion->id->str, EVENT_INTERRUPTED);
    motion->move_state = REQUEST_IDLE;
}

/* --- position watching --- */

static void watch_ear(EarWatch *watch, int position, unsigned status)
{
    if (watch->last_position == position) {
        watch->still_ms += TICK_MS;
    } else {
        watch->still_ms = 0;
        watch->steps++;
    }

    if (watch->state == WATCH_MOVING) {
        if (watch->busy_mask & status)
            watch->state = WATCH_STOPPING;
        watch->moving_ms += TICK_MS;
        if (watch->still_ms > STOPPED_MS)
            watch->state = WATCH_STOPPING;
    } else if (watch->state == WATCH_STOPPING) {
        watch->state = WATCH_STILL;
        if (watch->ear == 'R')
            g_signal_emit(ears, SIGNAL(SIGNAL_MOVE_RIGHT_STOP), 0, watch->steps, watch->moving_ms);
        else
            g_signal_emit(ears, SIGNAL(SIGNAL_MOVE_LEFT_STOP), 0, watch->steps, watch->moving_ms);
    } else if (watch->state == WATCH_STARTING) {
        if ((watch->busy_mask & status) == 0) {
            if (watch->last_position != position) {
                watch->state = WATCH_MOVING;
                if (watch->ear == 'R')
                    g_signal_emit(ears, SIGNAL(SIGNAL_MOVE_RIGHT_START), 0);
                else
                    g_signal_emit(ears, SIGNAL(SIGNAL_MOVE_LEFT_START), 0);
                watch->steps = 1;
                watch->moving_ms = 0;
            }
        } else {
            watch->state = WATCH_STILL;
        }
    } else if ((watch->busy_mask & status) == 0 && watch->last_position != position) {
        watch->state = WATCH_STARTING;
    }
    watch->last_position = position;
}

/* The polling tick. */
static gboolean tick(gpointer data)
{
    EarsObject *self = data;
    struct ear_positions pos;
    unsigned char value = 0; /* only meaningful when flags != 0 */
    unsigned status;
    int flags;

    flags = ears_update(&value);
    step_reset(&self->right, flags, value);
    step_reset(&self->left, flags, value);
    step_reset(&self->both, flags, value);

    status = ears_get_status();
    step_move(&self->right, status);
    step_move(&self->left, status);
    step_move(&self->both, status);

    check_blocked(&self->right, &self->right_watch, NULL);
    check_blocked(&self->left, NULL, &self->left_watch);
    check_blocked(&self->both, &self->right_watch, &self->left_watch);

    ears_get_position(&pos);
    status = ears_get_status();
    watch_ear(&self->right_watch, pos.right, status);
    watch_ear(&self->left_watch, pos.left, status);
    return TRUE;
}

/* --- D-Bus methods --- */

gboolean ears_object_move(EarsObject *self, const char *id, int l_position, int l_speed, int r_position,
                          int r_speed, gboolean *result, GError **error)
{
    unsigned status;

    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %d %d", "move", l_position, r_position);
    status = ears_get_status();
    cancel_motions('B');
    set_motion_id('B', id);
    if (status & (STATUS_LEFT_LOST | STATUS_RIGHT_LOST))
        request_reset_both();
    request_move_both(r_position, r_speed, l_position, l_speed, 1);
    *result = TRUE;
    return TRUE;
}

gboolean ears_object_move_step(EarsObject *self, const char *id, int l_position, int l_speed, int r_position,
                               int r_speed, gboolean *result, GError **error)
{
    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %d %d", "move_step", l_position, r_position);
    cancel_motions('B');
    set_motion_id('B', id);
    request_move_both(r_position, r_speed, l_position, l_speed, 0);
    *result = TRUE;
    return TRUE;
}

gboolean ears_object_move_right(EarsObject *self, const char *id, int r_position, int r_speed, gboolean *result,
                                GError **error)
{
    unsigned status;

    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %d", "move_right", r_position);
    status = ears_get_status();
    cancel_motions('R');
    set_motion_id('R', id);
    if (status & STATUS_RIGHT_LOST)
        request_reset_right();
    request_move_right(r_position, r_speed, 1);
    *result = TRUE;
    return TRUE;
}

gboolean ears_object_move_right_step(EarsObject *self, const char *id, int r_position, int r_speed,
                                     gboolean *result, GError **error)
{
    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %d", "move_right_step", r_position);
    cancel_motions('R');
    /* The move's events carry this id on the right channel. */
    set_motion_id('R', id);
    request_move_right(r_position, r_speed, 0);
    *result = TRUE;
    return TRUE;
}

gboolean ears_object_move_left(EarsObject *self, const char *id, int l_position, int l_speed, gboolean *result,
                               GError **error)
{
    unsigned status;

    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %d", "move_left", l_position);
    status = ears_get_status();
    cancel_motions('L');
    set_motion_id('L', id);
    if (status & STATUS_LEFT_LOST)
        request_reset_left();
    request_move_left(l_position, l_speed, 1);
    *result = TRUE;
    return TRUE;
}

gboolean ears_object_move_left_step(EarsObject *self, const char *id, int l_position, int l_speed,
                                    gboolean *result, GError **error)
{
    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %d", "move_left_step", l_position);
    cancel_motions('L');
    set_motion_id('L', id);
    request_move_left(l_position, l_speed, 0);
    *result = TRUE;
    return TRUE;
}

/* ears: "L", "R", or anything else for both. */
gboolean ears_object_reset(EarsObject *self, const char *id, const char *which, gboolean *result, GError **error)
{
    (void)self;
    (void)error;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "%s %c", "reset", which[0]);
    if (which[0] == 'L') {
        cancel_motions('L');
        set_motion_id('L', id);
        request_reset_left();
    } else if (which[0] == 'R') {
        cancel_motions('R');
        set_motion_id('R', id);
        request_reset_right();
    } else {
        cancel_motions('B');
        set_motion_id('B', id);
        request_reset_both();
    }
    *result = TRUE;
    return TRUE;
}

/* --- GObject plumbing --- */

static void ears_object_class_init(EarsObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    /* Marshallers for the non-void signals come from src/usr/karotz-marshal.list. */
    klass->signals[SIGNAL_EVENT] = g_signal_new("event", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, 2, G_TYPE_STRING,
                                                G_TYPE_INT);
    klass->signals[SIGNAL_REGISTERED] = g_signal_new("registered", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                     g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_MOVE_LEFT_START] = g_signal_new("move_left_start", type, G_SIGNAL_RUN_FIRST, 0, NULL,
                                                          NULL, g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_MOVE_RIGHT_START] = g_signal_new("move_right_start", type, G_SIGNAL_RUN_FIRST, 0, NULL,
                                                           NULL, g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_MOVE_LEFT_STOP] = g_signal_new("move_left_stop", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                         karotz_smarshal_VOID__INT_INT, G_TYPE_NONE, 2, G_TYPE_INT,
                                                         G_TYPE_INT);
    klass->signals[SIGNAL_MOVE_RIGHT_STOP] = g_signal_new("move_right_stop", type, G_SIGNAL_RUN_FIRST, 0, NULL,
                                                          NULL, karotz_smarshal_VOID__INT_INT, G_TYPE_NONE, 2,
                                                          G_TYPE_INT, G_TYPE_INT);
    klass->signals[SIGNAL_MOVE_LEFT_BLOCKED] = g_signal_new("move_left_blocked", type, G_SIGNAL_RUN_FIRST, 0, NULL,
                                                            NULL, g_cclosure_marshal_VOID__VOID, G_TYPE_NONE, 0);
    klass->signals[SIGNAL_MOVE_RIGHT_BLOCKED] = g_signal_new("move_right_blocked", type, G_SIGNAL_RUN_FIRST, 0,
                                                             NULL, NULL, g_cclosure_marshal_VOID__VOID,
                                                             G_TYPE_NONE, 0);
    klass->signals[SIGNAL_RESET_FINISHED] = g_signal_new("reset_finished", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                         karotz_smarshal_VOID__STRING_INT_INT, G_TYPE_NONE, 3,
                                                         G_TYPE_STRING, G_TYPE_INT, G_TYPE_INT);
    g_return_if_fail(klass != NULL);
}

static void ears_object_init(EarsObject *self)
{
    ears = self;
    self->right.id = g_string_sized_new(40);
    self->left.id = g_string_sized_new(40);
    self->both.id = g_string_sized_new(40);
    init_motions(self);
    ears_init();
    dbus_g_object_type_install_info(EARS_TYPE_OBJECT, &dbus_glib_ears_object_object_info);
    /* The ears' positions are unknown at start-up: recalibrate both. */
    request_reset_both();
    g_return_if_fail(self != NULL);
}

int main(void)
{
    int log_priority;
    GError *error = NULL;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;
    GMainLoop *loop;
    EarsObject *object;
    guint request_result;

    log_priority = getenv("KAROTZ_DEBUG") == NULL ? LOG_NOTICE : LOG_DEBUG;
    init_karotz_log("Ears", &log_priority);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");

    bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (bus == NULL)
        die("Failed to open connection to bus", error);
    else
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Successfully connected to %s bus\n", "SYSTEM");

    object = g_object_new(EARS_TYPE_OBJECT, NULL);
    dbus_g_connection_register_g_object(bus, "/com/mindscape/karotz/Ears", G_OBJECT(object));

    bus_proxy = dbus_g_proxy_new_for_name(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, "com.mindscape.karotz.Ears", 0, &request_result, &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }

    g_timeout_add(TICK_MS, tick, object);
    g_signal_emit(object, ((EarsObjectClass *)G_OBJECT_GET_CLASS(object))->signals[SIGNAL_REGISTERED], 0);
    g_main_loop_run(loop);
    return 0;
}
