# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# dbus-python-karotz: dbus-python 0.83.0 (_dbus_bindings.so, _dbus_glib_bindings.so and the dbus
# package in Python 2.4's site-packages), for the rabbit's scripts. configure is
# told where Python 2.4 is, since it can't run the target's.
#
################################################################################

DBUS_PYTHON_KAROTZ_VERSION = 0.83.0
DBUS_PYTHON_KAROTZ_SOURCE = dbus-python-$(DBUS_PYTHON_KAROTZ_VERSION).tar.gz
DBUS_PYTHON_KAROTZ_SITE = https://dbus.freedesktop.org/releases/dbus-python
DBUS_PYTHON_KAROTZ_LICENSE = MIT
DBUS_PYTHON_KAROTZ_LICENSE_FILES = COPYING
DBUS_PYTHON_KAROTZ_DEPENDENCIES = dbus-glib python24-karotz
DBUS_PYTHON_KAROTZ_CONF_ENV = CFLAGS="$(TARGET_CFLAGS) $(KAROTZ_GCC14_PORTING)" \
	am_cv_pathless_PYTHON=python \
	ac_cv_path_PYTHON=$(PYTHON24_KAROTZ_DIR)/hostpython \
	am_cv_python_version=2.4 \
	am_cv_python_platform=linux2 \
	am_cv_python_pythondir=/usr/lib/python2.4/site-packages \
	am_cv_python_pyexecdir=/usr/lib/python2.4/site-packages \
	am_cv_python_includes=-I$(STAGING_DIR)/usr/include/python2.4
DBUS_PYTHON_KAROTZ_CONF_OPTS = --disable-html-docs --disable-api-docs --disable-static

# The bindings are installed without their libtool archives.
define DBUS_PYTHON_KAROTZ_REMOVE_LA
	rm -f $(TARGET_DIR)/usr/lib/python2.4/site-packages/_dbus_bindings.la \
		$(TARGET_DIR)/usr/lib/python2.4/site-packages/_dbus_glib_bindings.la
endef
DBUS_PYTHON_KAROTZ_POST_INSTALL_TARGET_HOOKS += DBUS_PYTHON_KAROTZ_REMOVE_LA

$(eval $(autotools-package))
