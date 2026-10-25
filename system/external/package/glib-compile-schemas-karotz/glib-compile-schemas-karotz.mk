# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# glib-compile-schemas-karotz: GLib's glib-compile-schemas for the target, from libglib2's own
# build (Buildroot's libglib2 removes it from the target)
#
################################################################################

GLIB_COMPILE_SCHEMAS_KAROTZ_VERSION = $(LIBGLIB2_VERSION)
GLIB_COMPILE_SCHEMAS_KAROTZ_SITE = $(BR2_EXTERNAL_LAPINLIBRE_PATH)/package/glib-compile-schemas-karotz
GLIB_COMPILE_SCHEMAS_KAROTZ_SITE_METHOD = local
GLIB_COMPILE_SCHEMAS_KAROTZ_LICENSE = LGPL-2.1+
GLIB_COMPILE_SCHEMAS_KAROTZ_DEPENDENCIES = libglib2

define GLIB_COMPILE_SCHEMAS_KAROTZ_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(LIBGLIB2_BUILDDIR)buildroot-build/gio/glib-compile-schemas \
		$(TARGET_DIR)/usr/bin/glib-compile-schemas
endef

$(eval $(generic-package))
