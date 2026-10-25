/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Rabbit poweron Driver: the Karotz's power-on state as /dev/rabbit_poweron
 * (character major 53).
 *
 * read() returns one byte, '1' when GPC2 reads low and '0' otherwise; the
 * rootfs "power" tool polls it.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/fs.h>
#include <asm/uaccess.h>
#include <mach/hardware.h>
#include <mach/regs-gpio.h>

#define RABBIT_POWERON_MAJOR	53
#define RABBIT_POWERON_NAME	"rabbit_poweron"

/* The byte read() returns for each state; GPC2 reads low when on. */
#define POWERON_ON		'1'
#define POWERON_OFF		'0'

int rabbit_poweron_open(struct inode *inode, struct file *file)
{
	return 0;
}

int rabbit_poweron_release(struct inode *inode, struct file *file)
{
	return 0;
}

ssize_t rabbit_poweron_read(struct file *file, char __user *buf, size_t len, loff_t *ppos)
{
	char state = s3c2410_gpio_getpin(S3C2410_GPC2) ? POWERON_OFF : POWERON_ON;

	if (copy_to_user(buf, &state, sizeof(state))) {
		printk("Rabbit poweron Driver - copy_to_useri() error\n");
		return -1;
	}
	return sizeof(state);
}

static struct file_operations rabbit_poweron_fops = {
	.owner		= THIS_MODULE,
	.read		= rabbit_poweron_read,
	.open		= rabbit_poweron_open,
	.release	= rabbit_poweron_release,
};

int rabbit_poweron_init(void)
{
	if (register_chrdev(RABBIT_POWERON_MAJOR, RABBIT_POWERON_NAME, &rabbit_poweron_fops)) {
		printk("Rabbit Button Driver: unable to get major %d\n", RABBIT_POWERON_MAJOR);
		return -EIO;
	}
	printk("Rabbit poweron Driver V1.0 - successfully loaded\n");
	return 0;
}

void rabbit_poweron_exit(void)
{
	unregister_chrdev(RABBIT_POWERON_MAJOR, RABBIT_POWERON_NAME);
}

module_init(rabbit_poweron_init);
module_exit(rabbit_poweron_exit);
