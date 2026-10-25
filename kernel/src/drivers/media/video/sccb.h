/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef __SOFT_SCCB_H__
#define __SOFT_SCCB_H__

/* RABBITV3: the OV7675 SCCB bus is on GPG7 (clock) and GPG3 (data) */
#define SIO_C	S3C2410_GPG7
#define SIO_D	S3C2410_GPG3

#define State(x)	s3c2410_gpio_getpin(x)
#define High(x)		do{s3c2410_gpio_setpin(x,1); smp_mb();}while(0)
#define Low(x)		do{s3c2410_gpio_setpin(x,0); smp_mb();}while(0)

#define WAIT_STABLE()	do{udelay(10);}while(0)
#define WAIT_CYCLE()	do{udelay(90);}while(0)

#define CFG_READ(x)		do{s3c2410_gpio_cfgpin(x,S3C2410_GPIO_INPUT);smp_mb();}while(0)
#define CFG_WRITE(x)	do{s3c2410_gpio_cfgpin(x,S3C2410_GPIO_OUTPUT);smp_mb();}while(0)

void sccb_write(u8 IdAddr, u8 SubAddr, u8 data);
u8 sccb_read(u8 IdAddr, u8 SubAddr);

#endif
