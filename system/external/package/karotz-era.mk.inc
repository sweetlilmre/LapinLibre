# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# Shared by the system build's packages kept at old versions: 2001-2010 code built with
# GCC 14, which makes errors of what GCC 4 only warned about. Third-party code, so porting flags
# rather than source changes.
#
################################################################################

KAROTZ_GCC14_PORTING = -Wno-error=implicit-function-declaration -Wno-error=incompatible-pointer-types \
	-Wno-error=int-conversion -Wno-error=return-mismatch -Wno-error=implicit-int

KAROTZ_ERA_CFLAGS = $(TARGET_CFLAGS) -std=gnu11 $(KAROTZ_GCC14_PORTING)
