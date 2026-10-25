/* SPDX-License-Identifier: GPL-2.0-only */
/* linux/arch/arm/mach-s3c2410/include/mach/ts.h
 *
 * Copyright (c) 2005 Arnaud Patard <arnaud.patard@rtp-net.org>
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License version 2 as
 * published by the Free Software Foundation.
 *
 * RABBITV3: the touchscreen platform data from the openmoko/FriendlyARM
 * s3c2410 touchscreen patch (three words: delay, prescaler, oversampling).
 */

#ifndef __ASM_ARM_TS_H
#define __ASM_ARM_TS_H

struct s3c2410_ts_mach_info {
	int delay;
	int presc;
	int oversampling_shift;
};

void s3c24xx_ts_set_platdata(struct s3c2410_ts_mach_info *);

#endif /* __ASM_ARM_TS_H */
