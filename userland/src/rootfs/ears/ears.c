/* SPDX-License-Identifier: MIT */
/*
 * ears: move or recalibrate the ear motors through /dev/rabbit_ears.
 *
 *   ears                          reset: drive both ears backwards to the encoder gap
 *   ears <pos> <pwm> <pos> <pwm>  left ear, then right ear
 *
 * The ears_* functions are the motor library the program is built on.
 */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <unistd.h>

/* A motor command: speed (1..255) and a signed step count. */
struct ear_move {
    int pwm;
    int pos;
};

/* ioctls of the rabbit_ears driver ('E', 0x45). */
#define EARS_IOC_MOVE_RIGHT  _IOW('E', 1, struct ear_move)   /* 0x40084501 */
#define EARS_IOC_MOVE_LEFT   _IOW('E', 2, struct ear_move)   /* 0x40084502 */
#define EARS_IOC_GET_POS     _IOR('E', 3, struct ear_move)   /* 0x80084503 */
#define EARS_IOC_SET_POS     _IOW('E', 3, struct ear_move)   /* 0x40084503 */
#define EARS_IOC_GET_STATUS  _IOR('E', 4, int)               /* 0x80044504 */
#define EARS_IOC_SET_STATUS  _IOW('E', 4, int)               /* 0x40044504 */

/* ears_update() flags: which ear the driver reported on. */
#define EAR_LEFT  1
#define EAR_RIGHT 2

/* An event byte from the driver: bit 7 set for the left ear, the low seven
 * bits the time since that ear's previous step, in 20 ms units. */
#define EAR_EVENT_LEFT       0x80
#define EAR_EVENT_TIME_MASK  0x7f

#define EAR_PWM_MAX          255   /* full speed */
#define EAR_RESET_STEPS      100  /* driven backwards this far: about six turns, enough to pass the gap */
/* During a reset, steps further apart than this (24 x 20 ms = 480 ms) mean
 * the encoder's gap has gone by: the ears turn freely, and the reference
 * position is a missing step in each ear's encoder. */
#define RESET_VALUE          24

static const char *device = "/dev/rabbit_ears";
static int gFd;

int ears_file_descriptor(void)
{
    return gFd;
}

int stop_motorL(void)
{
    struct ear_move m = { 0, 0 };
    return ioctl(gFd, EARS_IOC_MOVE_LEFT, &m);
}

int stop_motorR(void)
{
    struct ear_move m = { 0, 0 };
    return ioctl(gFd, EARS_IOC_MOVE_RIGHT, &m);
}

int stop_motors(void)
{
    struct ear_move m = { 0, 0 };
    return ioctl(gFd, EARS_IOC_MOVE_LEFT, &m) | ioctl(gFd, EARS_IOC_MOVE_RIGHT, &m);
}

int rotate_motor_private(int pwmL, int posL, int pwmR, int posR)
{
    struct ear_move right = { pwmR, posR };
    struct ear_move left = { pwmL, posL };
    int r = ioctl(gFd, EARS_IOC_MOVE_RIGHT, &right);
    return r | ioctl(gFd, EARS_IOC_MOVE_LEFT, &left);
}

int rotate_motor_privateL(int pwm, int pos)
{
    struct ear_move m = { pwm, pos };
    return ioctl(gFd, EARS_IOC_MOVE_LEFT, &m);
}

int rotate_motor_privateR(int pwm, int pos)
{
    struct ear_move m = { pwm, pos };
    return ioctl(gFd, EARS_IOC_MOVE_RIGHT, &m);
}

int ears_get_position(struct ear_move *pos)
{
    return ioctl(gFd, EARS_IOC_GET_POS, pos);
}

int ears_set_position(struct ear_move *pos)
{
    return ioctl(gFd, EARS_IOC_SET_POS, pos);
}

int ears_get_status(void)
{
    int status;
    ioctl(gFd, EARS_IOC_GET_STATUS, &status);
    return status;
}

int ears_set_status(int status)
{
    return ioctl(gFd, EARS_IOC_SET_STATUS, &status);
}

/*
 * Read one event from the driver: bit 7 set means the left ear, the low
 * seven bits are the value. Returns EAR_LEFT, EAR_RIGHT or 0 (no event).
 */
int ears_update(unsigned char *value)
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

int ears_init(void)
{
    gFd = open(device, O_RDWR);
    if (gFd < 0) {
        puts("can't open motor device");
        abort();
    }
    return 0;
}

void ears_flush(void)
{
    char buf[32];
    while (read(gFd, buf, sizeof buf) != 0)
        ;
}

/*
 * Drive both ears backwards at full speed until each reports a step time
 * above RESET_VALUE (its first event is ignored), stopping each ear as it gets there, then
 * zero the position counters.
 */
int ears_reset(void)
{
    struct ear_move zero = { 0, 0 };
    char buf[32];
    unsigned char value;
    int doneL = 0, doneR = 0;
    int firstL = 1, firstR = 1;
    int flags;

    ears_set_position(&zero);
    while (read(gFd, buf, sizeof buf) != 0)
        ;
    stop_motors();
    rotate_motor_private(EAR_PWM_MAX, -EAR_RESET_STEPS, EAR_PWM_MAX, -EAR_RESET_STEPS);

    while (!doneL || !doneR) {
        do {
            flags = ears_update(&value);
        } while (flags == 0);

        if ((flags & EAR_LEFT) == EAR_LEFT && !doneL) {
            if (firstL) {
                firstL = 0;
            } else if (value > RESET_VALUE) {
                doneL = 1;
                stop_motorL();
            }
        } else if ((flags & EAR_RIGHT) && !doneR) {
            if (firstR) {
                firstR = 0;
            } else if (value > RESET_VALUE) {
                doneR = 1;
                stop_motorR();
            }
        }
    }

    ears_set_position(&zero);
    ears_get_status();
    ears_set_status(0);
    ears_get_status();
    return 0;
}

int main(int argc, char **argv)
{
    ears_init();
    if (argc == 1) {
        puts("reset");
        ears_reset();
    } else if (argc == 5) {
        rotate_motor_private(atoi(argv[2]), atoi(argv[1]), atoi(argv[4]), atoi(argv[3]));
    } else {
        puts("Usage : ./test_ears <pos> <pwm> <pos> <pwm>\n"
             "- pwm0 and pwm1 are values between 1 and 255\n"
             "- dir0 and dir1 values are 0 for clockwise, 1 for counterclockwise");
    }
    return 0;
}
