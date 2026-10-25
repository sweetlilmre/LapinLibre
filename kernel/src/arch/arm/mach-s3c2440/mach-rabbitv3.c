/* SPDX-License-Identifier: GPL-2.0-only */
/* linux/arch/arm/mach-s3c2440/mach-rabbitv3.c
 *
 * The Karotz (Nabaztag v3): machine RABBITV3, number 1912.
 * A Samsung S3C2440A on a 2440SODIMM module, 64 MB of SDRAM and
 * 256 MB of NAND.
 *
 * Based on mach-smdk2440.c.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License version 2 as
 * published by the Free Software Foundation.
 */

#include <linux/kernel.h>
#include <linux/types.h>
#include <linux/interrupt.h>
#include <linux/list.h>
#include <linux/timer.h>
#include <linux/init.h>
#include <linux/input.h>
#include <linux/gpio_keys.h>
#include <linux/serial_core.h>
#include <linux/platform_device.h>
#include <linux/io.h>
#include <linux/mtd/mtd.h>
#include <linux/mtd/nand.h>
#include <linux/mtd/partitions.h>
#include <linux/spi/spi.h>

#include <asm/sizes.h>

#include <asm/mach/arch.h>
#include <asm/mach/map.h>
#include <asm/mach/irq.h>

#include <mach/hardware.h>
#include <asm/irq.h>
#include <asm/mach-types.h>

#include <plat/regs-serial.h>
#include <mach/regs-gpio.h>
#include <mach/regs-lcd.h>
#include <mach/regs-clock.h>
#include <mach/idle.h>
#include <mach/fb.h>
#include <mach/spi.h>
#include <mach/usb-control.h>
#include <asm/plat-s3c24xx/udc.h>
#include <asm/plat-s3c/nand.h>
#include <asm/plat-s3c/iic.h>
#include <plat/hwmon.h>
#include <sound/s3c24xx_uda134x.h>

#include <plat/s3c2410.h>
#include <plat/s3c2440.h>
#include <plat/clock.h>
#include <plat/devs.h>
#include <plat/cpu.h>

static struct map_desc RabbitV3_iodesc[] __initdata = {
};

/* All three UARTs: PCLK, interrupt mode with RX timeout and RX error
 * interrupts, TX interrupt on level; 8N1; FIFOs on, RX trigger at 8 bytes,
 * TX at 16. (S3C2443_UCON_RXERR_IRQEN is bit 6, which the S3C2440 has too.) */
#define UCON	(S3C2410_UCON_TXILEVEL | S3C2410_UCON_RXFIFO_TOI | \
		 S3C2443_UCON_RXERR_IRQEN | S3C2410_UCON_TXIRQMODE | \
		 S3C2410_UCON_RXIRQMODE)
#define ULCON	(S3C2410_LCON_CS8 | S3C2410_LCON_PNONE)
#define UFCON	(S3C2440_UFCON_TXTRIG16 | S3C2440_UFCON_RXTRIG8 | \
		 S3C2410_UFCON_FIFOMODE)

/* GPH has no S3C2410_GPH_PUPDIS() in this kernel. */
#define RABBITV3_GPH_PUPDIS(x)	(1 << (x))

/* UPLL: 12 MHz crystal x (MDIV + 8) / ((PDIV + 2) x 2^SDIV)
 * = 12 x (56 + 8) / ((2 + 2) x 2^2) = 48 MHz for USB. */
#define RABBITV3_UPLL_MDIV	56
#define RABBITV3_UPLL_PDIV	2
#define RABBITV3_UPLL_SDIV	2
#define RABBITV3_UPLLCON_48MHZ	((RABBITV3_UPLL_MDIV << S3C2410_PLLCON_MDIVSHIFT) | \
				 (RABBITV3_UPLL_PDIV << S3C2410_PLLCON_PDIVSHIFT) | \
				 (RABBITV3_UPLL_SDIV << S3C2410_PLLCON_SDIVSHIFT))

/* NAND layout: each partition follows the previous one, except for an
 * unused 1 MB after "kernel rescue" and another after "ramdisk rescue"; the
 * last one runs to the end of the 256 MB chip. */
#define RABBITV3_NAND_BOOTLOADER_SIZE	SZ_256K
#define RABBITV3_NAND_KERNEL_SIZE	(3 * SZ_1M)
#define RABBITV3_NAND_RAMDISK_SIZE	(5 * SZ_1M)
#define RABBITV3_NAND_YAFFS_RESCUE_SIZE	(3 * SZ_1M)
#define RABBITV3_NAND_YAFFS_SIZE	(234 * SZ_1M)
#define RABBITV3_NAND_RESCUE_GAP	SZ_1M

#define RABBITV3_NAND_KERNEL_OFFSET	RABBITV3_NAND_BOOTLOADER_SIZE
#define RABBITV3_NAND_RAMDISK_OFFSET	(RABBITV3_NAND_KERNEL_OFFSET + RABBITV3_NAND_KERNEL_SIZE)
#define RABBITV3_NAND_KRESCUE_OFFSET	(RABBITV3_NAND_RAMDISK_OFFSET + RABBITV3_NAND_RAMDISK_SIZE)
#define RABBITV3_NAND_RRESCUE_OFFSET	(RABBITV3_NAND_KRESCUE_OFFSET + RABBITV3_NAND_KERNEL_SIZE + \
					 RABBITV3_NAND_RESCUE_GAP)
#define RABBITV3_NAND_YRESCUE_OFFSET	(RABBITV3_NAND_RRESCUE_OFFSET + RABBITV3_NAND_RAMDISK_SIZE + \
					 RABBITV3_NAND_RESCUE_GAP)
#define RABBITV3_NAND_YAFFS_OFFSET	(RABBITV3_NAND_YRESCUE_OFFSET + RABBITV3_NAND_YAFFS_RESCUE_SIZE)
#define RABBITV3_NAND_MYSTERY_OFFSET	(RABBITV3_NAND_YAFFS_OFFSET + RABBITV3_NAND_YAFFS_SIZE)
#define RABBITV3_NAND_MYSTERY_SIZE	(SZ_256M - RABBITV3_NAND_MYSTERY_OFFSET)

/* NAND timings, in ns. */
#define RABBITV3_NAND_TACLS	20
#define RABBITV3_NAND_TWRPH0	50
#define RABBITV3_NAND_TWRPH1	20

/* SPI0 and its one device, the LED driver, on chip select 1 (GPG2). */
#define RABBITV3_SPI_BUS	0
#define RABBITV3_SPI_LED_CS	1
#define RABBITV3_SPI_LED_HZ	10000000	/* 10 MHz */
#define RABBITV3_SPI_NUM_CS	8

/* I2C: the controller's own slave address, and 400 kHz. */
#define RABBITV3_I2C_SLAVE_ADDR	0x10
#define RABBITV3_I2C_FREQ	400000

/* The D+ pull-up's level on GPG11. */
#define RABBITV3_UDC_PULLUP_ON	1
#define RABBITV3_UDC_PULLUP_OFF	0

/* The LCD ports' set-up: every pin in its function 2 (0b10 per pin),
 * pull-ups off on GPC0-15 and GPD0-7, every bit of each register set. */
#define RABBITV3_GPIO_ALL_FUNC2	0xaaaaaaaa
#define RABBITV3_GPCUP_LCD	0x0000ffff
#define RABBITV3_GPDUP_LCD	0x000000ff
#define RABBITV3_GPIO_MASK_ALL	0xffffffff

/* hwmon: the two battery/power ADC inputs, read raw (mV = value). */
static struct s3c_hwmon_chcfg RabbitV3_hwmon_channels[] = {
	{ .name = "AIN0", .mult = 1, .div = 1, },
	{ .name = "AIN1", .mult = 1, .div = 1, },
};

static struct s3c_hwmon_pdata RabbitV3_hwmon_info = {
	.in	= RabbitV3_hwmon_channels,
	.nr_in	= ARRAY_SIZE(RabbitV3_hwmon_channels),
};

struct platform_device s3c_device_hwmon = {
	.name	= "s3c24xx-hwmon",
	.id	= -1,
	.dev	= {
		.parent		= &s3c_device_adc.dev,
		.platform_data	= &RabbitV3_hwmon_info,
	},
};

/* NAND: 256 MB, no ECC (the bootloader writes without it). */
static struct mtd_partition RabbitV3_nand_part[] = {
	[0] = {
		.name		= "bootloader",
		.size		= RABBITV3_NAND_BOOTLOADER_SIZE,
		.offset		= 0,
		.mask_flags	= MTD_POWERUP_LOCK,
	},
	[1] = {
		.name		= "Kernel",
		.size		= RABBITV3_NAND_KERNEL_SIZE,
		.offset		= RABBITV3_NAND_KERNEL_OFFSET,
	},
	[2] = {
		.name		= "ramdisk",
		.size		= RABBITV3_NAND_RAMDISK_SIZE,
		.offset		= RABBITV3_NAND_RAMDISK_OFFSET,
	},
	[3] = {
		.name		= "kernel rescue",
		.size		= RABBITV3_NAND_KERNEL_SIZE,
		.offset		= RABBITV3_NAND_KRESCUE_OFFSET,
		.mask_flags	= MTD_POWERUP_LOCK,
	},
	[4] = {
		.name		= "ramdisk rescue",
		.size		= RABBITV3_NAND_RAMDISK_SIZE,
		.offset		= RABBITV3_NAND_RRESCUE_OFFSET,
		.mask_flags	= MTD_POWERUP_LOCK,
	},
	[5] = {
		.name		= "yaffs rescue",
		.size		= RABBITV3_NAND_YAFFS_RESCUE_SIZE,
		.offset		= RABBITV3_NAND_YRESCUE_OFFSET,
	},
	[6] = {
		.name		= "yaffs",
		.size		= RABBITV3_NAND_YAFFS_SIZE,
		.offset		= RABBITV3_NAND_YAFFS_OFFSET,
	},
	[7] = {
		.name		= "mystery box",
		.size		= RABBITV3_NAND_MYSTERY_SIZE,
		.offset		= RABBITV3_NAND_MYSTERY_OFFSET,
	},
};

static struct s3c2410_nand_set RabbitV3_nand_sets[] = {
	[0] = {
		.disable_ecc	= 1,
		.nr_chips	= 1,
		.nr_partitions	= ARRAY_SIZE(RabbitV3_nand_part),
		.name		= "NAND",
		.partitions	= RabbitV3_nand_part,
	},
};

static struct s3c2410_platform_nand RabbitV3_nand_info = {
	.tacls		= RABBITV3_NAND_TACLS,
	.twrph0		= RABBITV3_NAND_TWRPH0,
	.twrph1		= RABBITV3_NAND_TWRPH1,
	.nr_sets	= ARRAY_SIZE(RabbitV3_nand_sets),
	.sets		= RabbitV3_nand_sets,
};

/* SPI0: the LED driver, through spidev, on chip select 1 (GPG2). */
static struct spi_board_info RabbitV3_spi_board_info[] = {
	{
		.modalias	= "spidev",
		.max_speed_hz	= RABBITV3_SPI_LED_HZ,
		.bus_num	= RABBITV3_SPI_BUS,
		.chip_select	= RABBITV3_SPI_LED_CS,
		.mode		= SPI_MODE_0,
	},
};

static void s3c24xx_spidev_chip_select(struct s3c2410_spi_info *spi, int cs, int pol);

static struct s3c2410_spi_info RabbitV3_spi0_info = {
	.pin_cs		= 0,	/* unused: set_cs drives the chip select */
	.num_cs		= RABBITV3_SPI_NUM_CS,
	.bus_num	= RABBITV3_SPI_BUS,
	.board_size	= ARRAY_SIZE(RabbitV3_spi_board_info),
	.board_info	= RabbitV3_spi_board_info,
	.set_cs		= s3c24xx_spidev_chip_select,
};

static struct s3c2410_platform_i2c RabbitV3_i2c_info = {
	.slave_addr	= RABBITV3_I2C_SLAVE_ADDR,
	.bus_freq	= RABBITV3_I2C_FREQ,
	.max_freq	= RABBITV3_I2C_FREQ,
};

/* hwport is the UART's number. */
static struct s3c2410_uartcfg RabbitV3_uartcfgs[] = {
	[0] = {
		.hwport	     = 0,
		.flags	     = 0,
		.ucon	     = UCON,
		.ulcon	     = ULCON,
		.ufcon	     = UFCON,
	},
	[1] = {
		.hwport	     = 1,
		.flags	     = 0,
		.ucon	     = UCON,
		.ulcon	     = ULCON,
		.ufcon	     = UFCON,
	},
	[2] = {
		.hwport	     = 2,
		.flags	     = 0,
		.ucon	     = UCON,
		.ulcon	     = ULCON,
		.ufcon	     = UFCON,
	},
};

static struct s3c2410_hcd_info RabbitV3_usb_info = {
	.port[0]	= {
		.flags	= S3C_HCDFLG_USED,
	},
	.port[1]	= {
		.flags	= S3C_HCDFLG_USED,
	},
};

/* The head button, also as an input device (it is GPF0, like /dev/rabbit_button). Unused by the
 * rabbit's build: its driver (CONFIG_KEYBOARD_GPIO) is off, and button-daemon reads
 * /dev/rabbit_button. */
static struct gpio_keys_button RabbitV3_buttons[] = {
	{
		.code		= KEY_ENTER,
		.gpio		= S3C2410_GPF0,
		.active_low	= 0,
		.desc		= "key_enter",
	},
};

static struct gpio_keys_platform_data RabbitV3_button_data = {
	.buttons	= RabbitV3_buttons,
	.nbuttons	= ARRAY_SIZE(RabbitV3_buttons),
};

static struct platform_device RabbitV3_button_device = {
	.name		= "gpio-keys",
	.id		= -1,
	.dev		= {
		.platform_data	= &RabbitV3_button_data,
	},
};

/* The UDA1341 codec, its L3 bus on GPB2-4. */
static struct s3c24xx_uda134x_platform_data RabbitV3_uda134x_data = {
	.l3_clk		= S3C2410_GPB4,
	.l3_mode	= S3C2410_GPB2,
	.l3_data	= S3C2410_GPB3,
	.model		= UDA134X_UDA1341,
};

static struct platform_device RabbitV3_uda134x = {
	.name		= "s3c24xx_uda134x",
	.dev		= {
		.platform_data	= &RabbitV3_uda134x_data,
	},
};

/* USB device: the D+ pull-up on GPG11. */
static void RabbitV3_udc_pullup(enum s3c2410_udc_cmd_e cmd);

static struct s3c2410_udc_mach_info RabbitV3_udc_cfg __initdata = {
	.udc_command	= RabbitV3_udc_pullup,
};

/* LCD driver info: three modes, though the Karotz has no screen. The
 * numbers are each mode's size (pixels), pixel clock (ps), depth (bits) and
 * margins and sync lengths (pixel clocks, lines), as the fields name them. */
static struct s3c2410fb_display RabbitV3_lcd_cfg[] __initdata = {
	{
		.type		= S3C2410_LCDCON1_TFT,
		.width		= 320,
		.height		= 240,
		.pixclock	= 80000,
		.xres		= 320,
		.yres		= 240,
		.bpp		= 16,
		.left_margin	= 21,
		.right_margin	= 39,
		.hsync_len	= 28,
		.upper_margin	= 16,
		.lower_margin	= 5,
		.vsync_len	= 2,
		.lcdcon5	= S3C2410_LCDCON5_FRM565 | S3C2410_LCDCON5_INVVLINE |
				  S3C2410_LCDCON5_INVVDEN | S3C2410_LCDCON5_INVPWREN |
				  S3C2410_LCDCON5_PWREN | S3C2410_LCDCON5_HWSWP,
	}, {
		.type		= S3C2410_LCDCON1_TFT,
		.width		= 640,
		.height		= 480,
		.pixclock	= 2000000,
		.xres		= 640,
		.yres		= 480,
		.bpp		= 24,
		.left_margin	= 16,
		.right_margin	= 48,
		.hsync_len	= 96,
		.upper_margin	= 33,
		.lower_margin	= 16,
		.vsync_len	= 1,
		.lcdcon5	= S3C2410_LCDCON5_FRM565 | S3C2410_LCDCON5_INVVLINE |
				  S3C2410_LCDCON5_INVVFRAME | S3C2410_LCDCON5_INVVDEN |
				  S3C2410_LCDCON5_BSWP | S3C2410_LCDCON5_HWSWP,
	}, {
		.type		= S3C2410_LCDCON1_TFT,
		.width		= 640,
		.height		= 480,
		.pixclock	= 20000000,
		.xres		= 640,
		.yres		= 48,
		.bpp		= 24,
		.left_margin	= 16,
		.right_margin	= 48,
		.hsync_len	= 96,
		.upper_margin	= 33,
		.lower_margin	= 16,
		.vsync_len	= 1,
		.lcdcon5	= S3C2410_LCDCON5_FRM565 | S3C2410_LCDCON5_INVVLINE |
				  S3C2410_LCDCON5_INVVFRAME | S3C2410_LCDCON5_INVVDEN |
				  S3C2410_LCDCON5_BSWP | S3C2410_LCDCON5_HWSWP,
	},
};

static struct s3c2410fb_mach_info RabbitV3_fb_info __initdata = {
	.displays	= RabbitV3_lcd_cfg,
	.num_displays	= ARRAY_SIZE(RabbitV3_lcd_cfg),
	.default_display = 0,

	/* GPC and GPD: every pin in its LCD function (0b10 per pin), except
	 * VLINE (GPC2), which stays an input; pull-ups off on GPC0-15 and
	 * GPD0-7. */
	.gpcup		= RABBITV3_GPCUP_LCD,
	.gpcup_mask	= RABBITV3_GPIO_MASK_ALL,
	.gpccon		= RABBITV3_GPIO_ALL_FUNC2 & ~S3C2410_GPC2_VLINE,
	.gpccon_mask	= RABBITV3_GPIO_MASK_ALL,
	.gpdup		= RABBITV3_GPDUP_LCD,
	.gpdup_mask	= RABBITV3_GPIO_MASK_ALL,
	.gpdcon		= RABBITV3_GPIO_ALL_FUNC2,
	.gpdcon_mask	= RABBITV3_GPIO_MASK_ALL,
};

static struct platform_device *RabbitV3_devices[] __initdata = {
	&s3c_device_lcd,
	&s3c_device_wdt,
	&s3c_device_rtc,
	&s3c_device_adc,
	&s3c_device_hwmon,
	&s3c_device_i2c,
	&s3c_device_iis,
	&s3c_device_spi0,
	&s3c_device_usb,
	&s3c_device_camif,
	&s3c_device_nand,
	&RabbitV3_button_device,
	&RabbitV3_uda134x,
	&s3c_device_pwm0,
	&s3c_device_pwm1,
};

static struct platform_device *RabbitV3_usb[] __initdata = {
	&s3c_device_usbgadget,
};

static void __init RabbitV3_init(void)
{
	s3c_device_nand.dev.platform_data = &RabbitV3_nand_info;
	s3c24xx_udc_set_platdata(&RabbitV3_udc_cfg);
	s3c_device_spi0.dev.platform_data = &RabbitV3_spi0_info;
	s3c_device_i2c.dev.platform_data = &RabbitV3_i2c_info;
	RabbitV3_fb_info.default_display = 0;
	s3c24xx_fb_set_platdata(&RabbitV3_fb_info);
	platform_add_devices(RabbitV3_devices, ARRAY_SIZE(RabbitV3_devices));
	spi_register_board_info(RabbitV3_spi_board_info, ARRAY_SIZE(RabbitV3_spi_board_info));
	s3c2410_modify_misccr(S3C2410_MISCCR_USBHOST | S3C2410_MISCCR_USBSUSPND0 | S3C2410_MISCCR_USBSUSPND1, 0);
	platform_add_devices(RabbitV3_usb, ARRAY_SIZE(RabbitV3_usb));
}

static void __init RabbitV3_init_irq(void)
{
	s3c24xx_init_irq();
}

int usb_RabbitV3_init(void);

static void __init RabbitV3_map_io(void)
{
	s3c24xx_init_io(RabbitV3_iodesc, ARRAY_SIZE(RabbitV3_iodesc));

	/* GPIO set-up, port by port (GPF is left as the bootloader set it). */

	/* GPA: the memory bus (address lines, chip selects, NAND control);
	 * GPA0 is an output. */
	__raw_writel(S3C2410_GPA0_OUT |
		     S3C2410_GPA1_ADDR16 | S3C2410_GPA2_ADDR17 |
		     S3C2410_GPA3_ADDR18 | S3C2410_GPA4_ADDR19 |
		     S3C2410_GPA5_ADDR20 | S3C2410_GPA6_ADDR21 |
		     S3C2410_GPA7_ADDR22 | S3C2410_GPA8_ADDR23 |
		     S3C2410_GPA9_ADDR24 | S3C2410_GPA10_ADDR25 |
		     S3C2410_GPA11_ADDR26 |
		     S3C2410_GPA12_nGCS1 | S3C2410_GPA13_nGCS2 |
		     S3C2410_GPA14_nGCS3 | S3C2410_GPA15_nGCS4 |
		     S3C2410_GPA16_nGCS5 |
		     S3C2410_GPA17_CLE | S3C2410_GPA18_ALE |
		     S3C2410_GPA19_nFWE | S3C2410_GPA20_nFRE |
		     S3C2410_GPA21_nRSTOUT | S3C2410_GPA22_nFCE,
		     S3C2410_GPACON);

	/* GPB0-4 outputs, without pull-ups: GPB0/1 the ear motors (the ears
	 * driver switches them to TOUT0/1), GPB2-4 the UDA1341's L3 bus. */
	__raw_writel(S3C2410_GPB0_OUTP | S3C2410_GPB1_OUTP | S3C2410_GPB2_OUTP |
		     S3C2410_GPB3_OUTP | S3C2410_GPB4_OUTP,
		     S3C2410_GPBCON);
	__raw_writel(S3C2410_GPB_PUPDIS(0) | S3C2410_GPB_PUPDIS(1) |
		     S3C2410_GPB_PUPDIS(2) | S3C2410_GPB_PUPDIS(3) |
		     S3C2410_GPB_PUPDIS(4),
		     S3C2410_GPBUP);

	/* GPC, GPD: all inputs with pull-ups (the LCD driver sets them up). */
	__raw_writel(0, S3C2410_GPCCON);
	__raw_writel(0, S3C2410_GPCUP);
	__raw_writel(0, S3C2410_GPDCON);
	__raw_writel(0, S3C2410_GPDUP);

	/* GPE: I2S (GPE0-3, pull-ups off), the I2S data-out pin GPE4 as an
	 * output (the I2S driver switches it while transmitting), SPI0. */
	__raw_writel(S3C2410_GPE0_I2SLRCK | S3C2410_GPE1_I2SSCLK |
		     S3C2410_GPE2_CDCLK | S3C2410_GPE3_I2SSDI |
		     S3C2410_GPE4_OUTP |
		     S3C2410_GPE11_SPIMISO0 | S3C2410_GPE12_SPIMOSI0 |
		     S3C2410_GPE13_SPICLK0,
		     S3C2410_GPECON);
	__raw_writel(S3C2410_GPE_PUPDIS(0) | S3C2410_GPE_PUPDIS(1) |
		     S3C2410_GPE_PUPDIS(2) | S3C2410_GPE_PUPDIS(3),
		     S3C2410_GPEUP);

	/* GPG: the SPI chip selects nSS0/nSS1 (GPG2/3), SPICLK1 (GPG7), GPG9
	 * an output, GPG11 as EINT19. */
	__raw_writel(S3C2410_GPG2_nSS0 | S3C2410_GPG3_nSS1 |
		     S3C2410_GPG7_SPICLK1 | S3C2410_GPG9_OUTP |
		     S3C2410_GPG11_EINT19,
		     S3C2410_GPGCON);
	__raw_writel(S3C2410_GPG_PUPDIS(2) | S3C2410_GPG_PUPDIS(3) |
		     S3C2410_GPG_PUPDIS(7) | S3C2410_GPG_PUPDIS(9) |
		     S3C2410_GPG_PUPDIS(10) | S3C2410_GPG_PUPDIS(11),
		     S3C2410_GPGUP);

	/* GPH: UART0 (nRTS0, TXD0, RXD0) and UART1 (TXD1, RXD1). */
	__raw_writel(S3C2410_GPH1_nRTS0 | S3C2410_GPH2_TXD0 | S3C2410_GPH3_RXD0 |
		     S3C2410_GPH4_TXD1 | S3C2410_GPH5_RXD1,
		     S3C2410_GPHCON);
	__raw_writel(RABBITV3_GPH_PUPDIS(1) | RABBITV3_GPH_PUPDIS(2) |
		     RABBITV3_GPH_PUPDIS(3) | RABBITV3_GPH_PUPDIS(4) |
		     RABBITV3_GPH_PUPDIS(5) | RABBITV3_GPH_PUPDIS(9) |
		     RABBITV3_GPH_PUPDIS(10),
		     S3C2410_GPHUP);

	usb_RabbitV3_init();

	s3c24xx_init_clocks(0);
	s3c24xx_init_uarts(RabbitV3_uartcfgs, ARRAY_SIZE(RabbitV3_uartcfgs));
}

/* USB host: the OHCI platform data, and the UPLL at 48 MHz. (gcc inlines
 * this into RabbitV3_map_io and moves the store up.) */
int usb_RabbitV3_init(void)
{
	s3c_device_usb.dev.platform_data = &RabbitV3_usb_info;
	__raw_writel(RABBITV3_UPLLCON_48MHZ, S3C2410_UPLLCON);
	return 0;
}

static void s3c24xx_spidev_chip_select(struct s3c2410_spi_info *spi, int cs, int pol)
{
	if (cs == RABBITV3_SPI_LED_CS) {
		s3c2410_gpio_cfgpin(S3C2410_GPG2, S3C2410_GPIO_OUTPUT);
		s3c2410_gpio_setpin(S3C2410_GPG2, pol);
	}
}

static void RabbitV3_udc_pullup(enum s3c2410_udc_cmd_e cmd)
{
	switch (cmd) {
	case S3C2410_UDC_P_ENABLE:
		s3c2410_gpio_setpin(S3C2410_GPG11, RABBITV3_UDC_PULLUP_ON);
		break;
	case S3C2410_UDC_P_DISABLE:
		s3c2410_gpio_setpin(S3C2410_GPG11, RABBITV3_UDC_PULLUP_OFF);
		break;
	default:
		break;
	}
}

/* phys_io, io_pg_offst and boot_params as in mach-smdk2440.c. The name is
 * replaced by config.toml's kernel.machine when system/kernel/build.sh prepares
 * the tree. */
MACHINE_START(RABBITV3, "RABBITV3 (KAROTZ)")
	.phys_io	= S3C2410_PA_UART,
	.io_pg_offst	= (((u32)S3C24XX_VA_UART) >> 18) & 0xfffc,
	.boot_params	= S3C2410_SDRAM_PA + 0x100,

	.init_irq	= RabbitV3_init_irq,
	.map_io		= RabbitV3_map_io,
	.init_machine	= RabbitV3_init,
	.timer		= &s3c24xx_timer,
MACHINE_END
