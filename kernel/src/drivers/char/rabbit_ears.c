/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Rabbit Ears Driver: the Karotz's two ear motors as /dev/rabbit_ears
 * (character major 60).
 *
 * Each ear has a motor driven by a PWM timer (left: PWM0 on TOUT0, right:
 * PWM1 on TOUT1) with a direction line (left: GPF4, right: GPF5), and an
 * encoder that interrupts once per step (left: EINT1, right: EINT2).
 *
 * ioctl (type 'E'):
 *   1 _IOW  move the right ear: struct { int pwm; int pos; } - run towards
 *           pos at speed |pwm|, forwards or backwards as needed
 *   2 _IOW  the same for the left ear
 *   3 _IOR  read the step counters { right, left };
 *     _IOW  zero each counter whose value is given as 0
 *   4 _IOR  read the status bits; _IOW  clear status bits (bits 0-1, the
 *           "moving" flags, are always kept)
 * Status bits: 1 left moving, 2 right moving, 4 left moved without being
 * driven or stepped faster than 7 x 20 ms (blocked/pushed), 8 the same
 * for the right ear. Both are set at start-up.
 *
 * Every encoder step queues one byte for read(): the time since the
 * previous step in 20 ms units (at most 127), with bit 7 set for the left
 * ear. read() hands out up to the requested number of queued bytes.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/fs.h>
#include <linux/interrupt.h>
#include <linux/time.h>
#include <linux/pwm.h>
#include <linux/err.h>
#include <asm/io.h>
#include <asm/uaccess.h>
#include <mach/hardware.h>
#include <mach/regs-gpio.h>
#include <mach/irqs.h>

#define RABBIT_EARS_NAME	"rabbit_ears"
#define RABBIT_EARS_MAJOR	60
#define RABBIT_EARS_MAGIC	'E'

/* ioctl command numbers (_IOC_NR); the header comment gives their data. */
#define RABBIT_EARS_MOVE_RIGHT	1
#define RABBIT_EARS_MOVE_LEFT	2
#define RABBIT_EARS_POSITION	3
#define RABBIT_EARS_STATUS	4
#define RABBIT_EARS_MAXNR	RABBIT_EARS_STATUS

#define EAR_LEFT_MOVING		1
#define EAR_RIGHT_MOVING	2
#define EAR_LEFT_BLOCKED	4
#define EAR_RIGHT_BLOCKED	8

#define PWM_PERIOD_NS		16320
/* The duty cycle in ns is the speed (|pwm|) times 64; a stopped motor is
 * left at speed 1. */
#define PWM_DUTY_SHIFT		6
#define PWM_SPEED_STOPPED	1
#define BUFFER_SIZE		32

/* The direction line's level for each way a motor turns. */
#define EAR_DIR_FORWARD		0
#define EAR_DIR_BACKWARD	1

/* The PWM channels, and the order of the counters in RABBIT_EARS_POSITION's data. */
#define EAR_LEFT_PWM		0
#define EAR_RIGHT_PWM		1
#define EAR_RIGHT		0
#define EAR_LEFT		1

/* Step timing: times are measured in 20 ms units, and a step less than 7
 * units after the previous one marks the ear as blocked/pushed. */
#define EAR_STEP_UNIT_US	20000
#define EAR_STEP_FAST		7

/* A queued step byte: bit 7 set for the left ear, bits 0-6 the time since
 * the previous step in 20 ms units. */
#define EAR_STEP_LEFT		0x80
#define EAR_STEP_TIME_MASK	0x7f
#define EAR_STEP_TIME_MAX	127

/* The motor pins: TOUT0/TOUT1 (GPB0/1) and the direction lines GPF4/5. */
#define EAR_GPBCON_MASK		((3 << 0) | (3 << 2))
#define EAR_GPBCON		(S3C2410_GPB0_TOUT0 | S3C2410_GPB1_TOUT1)
#define EAR_GPFCON_MASK		((3 << 8) | (3 << 10))
#define EAR_GPFCON		(S3C2410_GPF4_OUTP | S3C2410_GPF5_OUTP)

struct ear_move {
	int pwm;
	int pos;
};

int rabbit_ears_major = RABBIT_EARS_MAJOR;
int right_direction = 1;
int left_direction = 1;
int ears_status = EAR_LEFT_BLOCKED | EAR_RIGHT_BLOCKED;

static unsigned char count;
static int right_target, left_target;
static int right_position, left_position;
static struct pwm_device *pwm0, *pwm1;
static struct timeval left_start, right_start;
static struct timeval left_now, right_now;
static char buffer[BUFFER_SIZE];

static irqreturn_t rabbit_ears_LeftInterrupt(int irq, void *dev_id);
static irqreturn_t rabbit_ears_RightInterrupt(int irq, void *dev_id);
int rabbit_ears_ioctl(struct inode *inode, struct file *file, unsigned int cmd, unsigned long arg);
ssize_t rabbit_ears_read(struct file *file, char __user *buf, size_t len, loff_t *ppos);
void move_left(int speed);
void move_right(int speed);
static struct file_operations rabbit_ears_fops;

int utimediff(struct timeval from, struct timeval to)
{
	return (to.tv_sec - from.tv_sec) * USEC_PER_SEC + (to.tv_usec - from.tv_usec);
}

int rabbit_ears_open(struct inode *inode, struct file *file)
{
	return 0;
}

int rabbit_ears_release(struct inode *inode, struct file *file)
{
	return 0;
}

int val_abs(int value)
{
	if (value < 0)
		return -value;
	return value;
}

ssize_t rabbit_ears_read(struct file *file, char __user *buf, size_t len, loff_t *ppos)
{
	int size = 0;
	int i;

	if (count != 0) {
		if (count < len)
			size = count;
		else
			size = len;
		if (copy_to_user(buf, buffer, size)) {
			printk("Rabbit Ear Driver - copy_to_useri() error\n");
			size = -1;
		} else {
			for (i = size; i < count; i++)
				buffer[i - size] = buffer[i];
			count -= size;
		}
	}
	return size;
}

static struct file_operations rabbit_ears_fops = {
	.owner		= THIS_MODULE,
	.read		= rabbit_ears_read,
	.ioctl		= rabbit_ears_ioctl,
	.open		= rabbit_ears_open,
	.release	= rabbit_ears_release,
};

int rabbit_ears_init(void)
{
	int result;

	result = register_chrdev(rabbit_ears_major, RABBIT_EARS_NAME, &rabbit_ears_fops);
	if (result < 0) {
		printk("Rabbit Ears Driver - cannot obtain major number %d\n", rabbit_ears_major);
		return result;
	}
	/* TOUT0 and TOUT1 drive the motors, GPF4 and GPF5 are their direction lines. */
	__raw_writel((__raw_readl(S3C2410_GPBCON) & ~EAR_GPBCON_MASK) | EAR_GPBCON, S3C2410_GPBCON);
	__raw_writel((__raw_readl(S3C2410_GPFCON) & ~EAR_GPFCON_MASK) | EAR_GPFCON, S3C2410_GPFCON);

	pwm0 = pwm_request(EAR_LEFT_PWM, "pwm0");
	if (pwm0 == ERR_PTR(-EBUSY) || pwm0 == ERR_PTR(-ENOENT)) {
		printk("Rabbit Ears Driver - Can't get pwm0\n");
		return -1;
	}
	printk("Rabbit Ears Driver - pwm0 request OK\n");
	pwm1 = pwm_request(EAR_RIGHT_PWM, "pwm1");
	if (pwm1 == ERR_PTR(-EBUSY) || pwm1 == ERR_PTR(-ENOENT)) {
		printk("Rabbit Ears Driver - Can't get pwm1\n");
		return -1;
	}
	printk("Rabbit Ears Driver - pwm1 request OK\n");
	pwm_config(pwm0, PWM_SPEED_STOPPED << PWM_DUTY_SHIFT, PWM_PERIOD_NS);
	pwm_config(pwm1, PWM_SPEED_STOPPED << PWM_DUTY_SHIFT, PWM_PERIOD_NS);
	pwm_enable(pwm0);
	pwm_enable(pwm1);
	if (request_irq(IRQ_EINT2, rabbit_ears_RightInterrupt, 0, "Rabbit Right Ear IT", NULL)) {
		printk("Rabbit Ear Driver - Can't get IRQ1\n");
		return -1;
	}
	if (request_irq(IRQ_EINT1, rabbit_ears_LeftInterrupt, 0, "Rabbit Left ear IT", NULL)) {
		printk("Rabbit Ear Driver - Can't get IRQ2\n");
		return -1;
	}
	printk("Rabbit Ears Driver V2.3 - successfully loaded\n");
	return 0;
}

void move_left(int speed)
{
	ears_status |= EAR_LEFT_MOVING;
	if (speed > 0) {
		do_gettimeofday(&left_start);
		s3c2410_gpio_setpin(S3C2410_GPF4, EAR_DIR_FORWARD);
		left_direction = 1;
	} else if (speed == 0) {
		ears_status &= ~EAR_LEFT_MOVING;
		speed = PWM_SPEED_STOPPED;
	} else {
		do_gettimeofday(&left_start);
		s3c2410_gpio_setpin(S3C2410_GPF4, EAR_DIR_BACKWARD);
		left_direction = -1;
		speed = -speed;
	}
	pwm_enable(pwm0);
	pwm_config(pwm0, speed << PWM_DUTY_SHIFT, PWM_PERIOD_NS);
}

static irqreturn_t rabbit_ears_LeftInterrupt(int irq, void *dev_id)
{
	int speed;
	int i;

	if (!(ears_status & EAR_LEFT_MOVING))
		ears_status |= EAR_LEFT_BLOCKED;
	if (left_direction > 0) {
		if (++left_position >= left_target) {
			s3c2410_gpio_setpin(S3C2410_GPF4, EAR_DIR_BACKWARD);
			move_left(0);
		}
	} else {
		if (--left_position <= left_target) {
			s3c2410_gpio_setpin(S3C2410_GPF4, EAR_DIR_FORWARD);
			move_left(0);
		}
	}
	do_gettimeofday(&left_now);
	speed = utimediff(left_start, left_now) / EAR_STEP_UNIT_US;
	if (speed < EAR_STEP_FAST)
		ears_status |= EAR_LEFT_BLOCKED;
	left_start.tv_sec = left_now.tv_sec;
	left_start.tv_usec = left_now.tv_usec;
	if (count == BUFFER_SIZE) {
		for (i = 1; i < BUFFER_SIZE; i++)
			buffer[i - 1] = buffer[i];
		count = BUFFER_SIZE - 1;
	}
	if (speed > EAR_STEP_TIME_MAX)
		speed = EAR_STEP_TIME_MAX;
	buffer[count++] = speed | EAR_STEP_LEFT;
	return IRQ_HANDLED;
}

int rabbit_ears_ioctl(struct inode *inode, struct file *file, unsigned int cmd, unsigned long arg)
{
	struct ear_move *move = (struct ear_move *)arg;
	int *values = (int *)arg;
	int ret;

	if (_IOC_TYPE(cmd) != RABBIT_EARS_MAGIC) {
		printk("Rabbit Ears Driver - Wrong MAGIC\n");
		ret = -ENOTTY;
	} else if (_IOC_NR(cmd) > RABBIT_EARS_MAXNR) {
		printk("Rabbit Ears Driver - Wrong NR\n");
		ret = -ENOTTY;
	} else {
		ret = -EFAULT;
		switch (_IOC_NR(cmd)) {
		case RABBIT_EARS_MOVE_RIGHT:
			if ((_IOC_DIR(cmd) & _IOC_WRITE) &&
			    access_ok(VERIFY_READ, (void __user *)arg, _IOC_SIZE(cmd))) {
				right_target = move->pos;
				if (right_position < right_target)
					move_right(val_abs(move->pwm));
				else if (right_position > right_target)
					move_right(-val_abs(move->pwm));
				else
					move_right(0);
				ret = 0;
			}
			break;
		case RABBIT_EARS_MOVE_LEFT:
			if ((_IOC_DIR(cmd) & _IOC_WRITE) &&
			    access_ok(VERIFY_READ, (void __user *)arg, _IOC_SIZE(cmd))) {
				left_target = move->pos;
				if (left_position < left_target)
					move_left(val_abs(move->pwm));
				else if (left_position > left_target)
					move_left(-val_abs(move->pwm));
				else
					move_left(0);
				ret = 0;
			}
			break;
		case RABBIT_EARS_POSITION:
			if (_IOC_DIR(cmd) & _IOC_READ) {
				if (access_ok(VERIFY_WRITE, (void __user *)arg, _IOC_SIZE(cmd))) {
					values[EAR_RIGHT] = right_position;
					values[EAR_LEFT] = left_position;
					ret = 0;
				}
			} else if (_IOC_DIR(cmd) & _IOC_WRITE) {
				if (access_ok(VERIFY_READ, (void __user *)arg, _IOC_SIZE(cmd))) {
					if (values[EAR_RIGHT] == 0)
						right_position = 0;
					if (values[EAR_LEFT] == 0)
						left_position = 0;
					ret = 0;
				}
			}
			break;
		case RABBIT_EARS_STATUS:
			if (_IOC_DIR(cmd) & _IOC_READ) {
				if (access_ok(VERIFY_WRITE, (void __user *)arg, _IOC_SIZE(cmd))) {
					values[0] = ears_status;
					ret = 0;
				}
			} else if (_IOC_DIR(cmd) & _IOC_WRITE) {
				if (access_ok(VERIFY_READ, (void __user *)arg, _IOC_SIZE(cmd))) {
					ears_status &= values[0] | EAR_LEFT_MOVING | EAR_RIGHT_MOVING;
					ret = 0;
				}
			}
			break;
		default:
			ret = 0;
		}
	}
	return ret;
}

void move_right(int speed)
{
	ears_status |= EAR_RIGHT_MOVING;
	if (speed > 0) {
		do_gettimeofday(&right_start);
		s3c2410_gpio_setpin(S3C2410_GPF5, EAR_DIR_FORWARD);
		right_direction = 1;
	} else if (speed == 0) {
		ears_status &= ~EAR_RIGHT_MOVING;
		speed = PWM_SPEED_STOPPED;
	} else {
		do_gettimeofday(&right_start);
		s3c2410_gpio_setpin(S3C2410_GPF5, EAR_DIR_BACKWARD);
		right_direction = -1;
		speed = -speed;
	}
	pwm_enable(pwm1);
	pwm_config(pwm1, speed << PWM_DUTY_SHIFT, PWM_PERIOD_NS);
}

static irqreturn_t rabbit_ears_RightInterrupt(int irq, void *dev_id)
{
	int speed;
	int i;

	if (!(ears_status & EAR_RIGHT_MOVING))
		ears_status |= EAR_RIGHT_BLOCKED;
	if (right_direction > 0) {
		if (++right_position >= right_target) {
			s3c2410_gpio_setpin(S3C2410_GPF5, EAR_DIR_BACKWARD);
			move_right(0);
		}
	} else {
		if (--right_position <= right_target) {
			s3c2410_gpio_setpin(S3C2410_GPF5, EAR_DIR_FORWARD);
			move_right(0);
		}
	}
	do_gettimeofday(&right_now);
	speed = utimediff(right_start, right_now) / EAR_STEP_UNIT_US;
	if (speed < EAR_STEP_FAST)
		ears_status |= EAR_RIGHT_BLOCKED;
	right_start.tv_sec = right_now.tv_sec;
	right_start.tv_usec = right_now.tv_usec;
	if (count == BUFFER_SIZE) {
		for (i = 1; i < BUFFER_SIZE; i++)
			buffer[i - 1] = buffer[i];
		count = BUFFER_SIZE - 1;
	}
	if (speed > EAR_STEP_TIME_MAX)
		speed = EAR_STEP_TIME_MAX;
	buffer[count++] = speed & EAR_STEP_TIME_MASK;
	return IRQ_HANDLED;
}

void rabbit_ears_exit(void)
{
	pwm_free(pwm0);
	pwm_free(pwm1);
	free_irq(IRQ_EINT1, NULL);
	free_irq(IRQ_EINT2, NULL);
	unregister_chrdev(rabbit_ears_major, RABBIT_EARS_NAME);
	printk("Rabbit Ears Driver - successfully unloaded\n");
}

module_init(rabbit_ears_init);
module_exit(rabbit_ears_exit);
