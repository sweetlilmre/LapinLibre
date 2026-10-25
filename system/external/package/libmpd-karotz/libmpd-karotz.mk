# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# libmpd-karotz: libmpd 0.17.0 with the Karotz changes in 0001-karotz.patch (libmpd.so.0, as
# multimedia-daemon uses it)
#
################################################################################

LIBMPD_KAROTZ_VERSION = 0.17.0
LIBMPD_KAROTZ_SOURCE = libmpd-$(LIBMPD_KAROTZ_VERSION).tar.gz
LIBMPD_KAROTZ_SITE = https://www.musicpd.org/download/libmpd/$(LIBMPD_KAROTZ_VERSION)
LIBMPD_KAROTZ_LICENSE = GPL-2.0+
LIBMPD_KAROTZ_LICENSE_FILES = COPYING
LIBMPD_KAROTZ_INSTALL_STAGING = YES
LIBMPD_KAROTZ_DEPENDENCIES = libglib2 host-pkgconf
LIBMPD_KAROTZ_CONF_OPTS = --disable-static

$(eval $(autotools-package))
