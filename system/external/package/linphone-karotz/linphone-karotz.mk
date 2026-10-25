# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# linphone-karotz: linphone 3.4.1's libraries (liblinphone.so.4, libmediastreamer.so.1,
# libortp.so.8; oRTP and mediastreamer2 come in its tarball), for k2k-daemon.
# Configured for the rabbit: no video, no user interface, OSS audio only, no IPv6, no TLS.
#
################################################################################

LINPHONE_KAROTZ_VERSION = 3.4.1
LINPHONE_KAROTZ_SOURCE = linphone-$(LINPHONE_KAROTZ_VERSION).tar.gz
LINPHONE_KAROTZ_SITE = https://download-mirror.savannah.gnu.org/releases/linphone/3.4.x/sources
LINPHONE_KAROTZ_LICENSE = GPL-2.0+
LINPHONE_KAROTZ_LICENSE_FILES = COPYING
LINPHONE_KAROTZ_INSTALL_STAGING = YES
LINPHONE_KAROTZ_DEPENDENCIES = libexosip2-karotz speex host-pkgconf host-intltool
LINPHONE_KAROTZ_CONF_ENV = CFLAGS="$(KAROTZ_ERA_CFLAGS)"
LINPHONE_KAROTZ_CONF_OPTS = --disable-static --disable-video --enable-gtk_ui=no \
	--enable-console_ui=no --disable-x11 --disable-nls --disable-manual --disable-strict \
	--enable-alsa=no --enable-oss=yes --enable-portaudio=no --enable-pulseaudio=no \
	--enable-ipv6=no --disable-tests --with-osip=$(STAGING_DIR)/usr --enable-ssl=no

$(eval $(autotools-package))
