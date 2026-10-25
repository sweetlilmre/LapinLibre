# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# libexosip2-karotz: libeXosip2 3.5.0 (libeXosip2.so.6), for linphone 3.4.1
# (k2k-daemon). No TLS.
#
################################################################################

LIBEXOSIP2_KAROTZ_VERSION = 3.5.0
LIBEXOSIP2_KAROTZ_SOURCE = libeXosip2-$(LIBEXOSIP2_KAROTZ_VERSION).tar.gz
LIBEXOSIP2_KAROTZ_SITE = https://download-mirror.savannah.gnu.org/releases/exosip
LIBEXOSIP2_KAROTZ_LICENSE = GPL-2.0+
LIBEXOSIP2_KAROTZ_LICENSE_FILES = COPYING
LIBEXOSIP2_KAROTZ_INSTALL_STAGING = YES
LIBEXOSIP2_KAROTZ_DEPENDENCIES = libosip2-karotz
LIBEXOSIP2_KAROTZ_CONF_ENV = CFLAGS="$(KAROTZ_ERA_CFLAGS)"
LIBEXOSIP2_KAROTZ_CONF_OPTS = --disable-static --disable-openssl

$(eval $(autotools-package))
