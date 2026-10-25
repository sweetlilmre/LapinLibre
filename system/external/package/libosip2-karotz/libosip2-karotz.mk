# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# libosip2-karotz: libosip2 3.5.0 (libosip2.so.6 and libosipparser2.so.6), for
# linphone 3.4.1 (k2k-daemon). Buildroot 2026.02's libosip2 is 5.x, an API
# linphone 3.4 can't use.
#
################################################################################

LIBOSIP2_KAROTZ_VERSION = 3.5.0
LIBOSIP2_KAROTZ_SOURCE = libosip2-$(LIBOSIP2_KAROTZ_VERSION).tar.gz
LIBOSIP2_KAROTZ_SITE = https://ftp.gnu.org/gnu/osip
LIBOSIP2_KAROTZ_LICENSE = LGPL-2.1+
LIBOSIP2_KAROTZ_LICENSE_FILES = COPYING
LIBOSIP2_KAROTZ_INSTALL_STAGING = YES
LIBOSIP2_KAROTZ_CONF_ENV = CFLAGS="$(KAROTZ_ERA_CFLAGS)"
LIBOSIP2_KAROTZ_CONF_OPTS = --disable-static

$(eval $(autotools-package))
