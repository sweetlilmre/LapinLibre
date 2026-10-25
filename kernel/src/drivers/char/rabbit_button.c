/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Rabbit Button Driver: the Karotz's head button as /dev/rabbit_button
 * (character major 52).
 *
 * The button is on GPF0. Every press and release interrupts; the handler
 * queues '1' (pressed: the pin reads low) or '0' (released) in a 16-byte
 * buffer, dropping the oldest byte when it is full, and wakes readers.
 * read() hands out (and removes) up to the requested number of queued
 * bytes without blocking; poll() reports readable while any are queued.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/fs.h>
#include <linux/interrupt.h>
#include <linux/poll.h>
#include <linux/sched.h>
#include <linux/wait.h>
#include <asm/uaccess.h>
#include <mach/hardware.h>
#include <mach/regs-gpio.h>

#define RABBIT_BUTTON_MAJOR	52
#define RABBIT_BUTTON_NAME	"rabbit_button"
#define BUFFER_SIZE		16

/* The byte queued for each state; the pin reads low while pressed. */
#define BUTTON_PRESSED		'1'
#define BUTTON_RELEASED		'0'

static int count;
static char buffer[BUFFER_SIZE];
static DECLARE_WAIT_QUEUE_HEAD(button_wait);

int rabbit_button_open(struct inode *inode, struct file *file)
{
	return 0;
}

int rabbit_button_release(struct inode *inode, struct file *file)
{
	return 0;
}

unsigned int rabbit_button_poll(struct file *file, poll_table *wait)
{
	poll_wait(file, &button_wait, wait);
	if (count)
		return POLLIN | POLLRDNORM;
	return 0;
}

ssize_t rabbit_button_read(struct file *file, char __user *buf, size_t len, loff_t *ppos)
{
	int n = count;
	int i;

	if (n < 1)
		return 0;
	if (len < n)
		n = len;
	if (copy_to_user(buf, buffer, n)) {
		printk("Rabbit Button Driver - copy_to_useri() error\n");
		return -1;
	}
	for (i = n; i < count; i++)
		buffer[i - n] = buffer[i];
	count -= n;
	return n;
}

static struct file_operations rabbit_button_fops = {
	.owner		= THIS_MODULE,
	.read		= rabbit_button_read,
	.poll		= rabbit_button_poll,
	.open		= rabbit_button_open,
	.release	= rabbit_button_release,
};

static irqreturn_t rabbit_button_Interrupt(int irq, void *dev_id)
{
	char state = s3c2410_gpio_getpin(S3C2410_GPF0) ? BUTTON_RELEASED : BUTTON_PRESSED;
	int i;

	if (count == BUFFER_SIZE) {
		for (i = 1; i < BUFFER_SIZE; i++)
			buffer[i - 1] = buffer[i];
		count = BUFFER_SIZE - 1;
	}
	buffer[count++] = state;
	wake_up_interruptible(&button_wait);
	return IRQ_HANDLED;
}

int rabbit_button_init(void)
{
	int irq;

	if (register_chrdev(RABBIT_BUTTON_MAJOR, RABBIT_BUTTON_NAME, &rabbit_button_fops)) {
		printk("Rabbit Button Driver: unable to get major %d\n", RABBIT_BUTTON_MAJOR);
		return -EIO;
	}
	irq = s3c2410_gpio_getirq(S3C2410_GPF0);
	if (irq < 0)
		printk("Rabbit Button Driver: gpioToIrq fail");
	if (request_irq(irq, rabbit_button_Interrupt, IRQF_SAMPLE_RANDOM | IRQF_TRIGGER_RISING | IRQF_TRIGGER_FALLING,
			RABBIT_BUTTON_NAME, NULL))
		printk("Rabbit Button Driver: irq  fail");
	printk("Rabbit Button Driver V1.3 - successfully loaded\n");
	return 0;
}

void rabbit_button_exit(void)
{
	free_irq(s3c2410_gpio_getirq(S3C2410_GPF0), NULL);
	unregister_chrdev(RABBIT_BUTTON_MAJOR, RABBIT_BUTTON_NAME);
}

static irqreturn_t rabbit_button_Interrupt(int irq, void *dev_id);

module_init(rabbit_button_init);
module_exit(rabbit_button_exit);
