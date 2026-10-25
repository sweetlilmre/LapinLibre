# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# python24-karotz: Python 2.4.5, python2.4 and libpython2.4.so.1.0, which the rabbit's scripts
# (/usr/scripts, /karotz/scripts) are written for. Built with Buildroot 2010.11's
# python.mk recipe and its patches (0001-0004), and LapinLibre's (0005-0006): a host python and
# pgen first, then the target, with MACHDEP linux3. The modules: pyexpat, the CJK codecs and
# unicodedata; not dbm, zipfile, readline, curses, gdbm, bsddb, tkinter or nis. Only .py files
# (BR2_PACKAGE_PYTHON_PY_ONLY). Headers, the Makefile and libpython go to staging, for
# dbus-python.
#
################################################################################

PYTHON24_KAROTZ_VERSION = 2.4.5
PYTHON24_KAROTZ_SOURCE = Python-$(PYTHON24_KAROTZ_VERSION).tar.bz2
PYTHON24_KAROTZ_SITE = https://www.python.org/ftp/python/$(PYTHON24_KAROTZ_VERSION)
PYTHON24_KAROTZ_LICENSE = Python-2.0
PYTHON24_KAROTZ_LICENSE_FILES = LICENSE
PYTHON24_KAROTZ_INSTALL_STAGING = YES
PYTHON24_KAROTZ_DEPENDENCIES = expat zlib

PYTHON24_KAROTZ_DISABLED_MODULES = dbm zipfile readline _curses _curses_panel gdbm bsddb _tkinter nis
# Python 2.4 relies on signed overflow wrapping and on aliasing current GCC no longer forgives.
PYTHON24_KAROTZ_OPT = -fwrapv -fno-strict-aliasing
PYTHON24_KAROTZ_MAKE_OPTS = CC="$(TARGET_CC)" \
	PYTHON_MODULES_INCLUDE=$(STAGING_DIR)/usr/include \
	PYTHON_MODULES_LIB="$(STAGING_DIR)/lib $(STAGING_DIR)/usr/lib" \
	PYTHON_DISABLE_MODULES="$(PYTHON24_KAROTZ_DISABLED_MODULES)" PYTHON_DISABLE_SSL=1 \
	HOSTPYTHON=./hostpython HOSTPGEN=./Parser/hostpgen

define PYTHON24_KAROTZ_CONFIGURE_CMDS
	(cd $(@D) && rm -f config.cache && \
		CC="$(HOSTCC)" OPT="-O2 $(PYTHON24_KAROTZ_OPT)" ./configure --with-cxx=no && \
		$(MAKE) python Parser/pgen && \
		mv python hostpython && mv Parser/pgen Parser/hostpgen && \
		$(MAKE) distclean)
	(cd $(@D) && rm -f config.cache && \
		$(TARGET_CONFIGURE_OPTS) $(TARGET_CONFIGURE_ARGS) \
		OPT="$(TARGET_CFLAGS) $(PYTHON24_KAROTZ_OPT) $(KAROTZ_GCC14_PORTING)" \
		MACHDEP=linux3 ac_sys_system=Linux ac_sys_release=3 \
		./configure --target=$(GNU_TARGET_NAME) --host=$(GNU_TARGET_NAME) \
		--build=$(GNU_HOST_NAME) --prefix=/usr --sysconfdir=/etc --with-cxx=no \
		--enable-shared --disable-ipv6)
endef

define PYTHON24_KAROTZ_BUILD_CMDS
	$(MAKE1) -C $(@D) DESTDIR=$(TARGET_DIR) $(PYTHON24_KAROTZ_MAKE_OPTS)
endef

define PYTHON24_KAROTZ_INSTALL_STAGING_CMDS
	mkdir -p $(STAGING_DIR)/usr/include $(STAGING_DIR)/usr/lib/python2.4/config
	cp -dpr $(@D)/Include $(STAGING_DIR)/usr/include/python2.4
	cp $(@D)/pyconfig.h $(STAGING_DIR)/usr/include/python2.4/
	cp $(@D)/Makefile $(STAGING_DIR)/usr/lib/python2.4/config/
	cp -dp $(@D)/libpython2.4.so.1.0 $(STAGING_DIR)/usr/lib/
	ln -sf libpython2.4.so.1.0 $(STAGING_DIR)/usr/lib/libpython2.4.so
endef

define PYTHON24_KAROTZ_INSTALL_TARGET_CMDS
	rm -rf $(@D)/Lib/test
	$(MAKE1) -C $(@D) install DESTDIR=$(TARGET_DIR) CROSS_COMPILE=yes $(PYTHON24_KAROTZ_MAKE_OPTS)
	rm -f $(TARGET_DIR)/usr/bin/python $(TARGET_DIR)/usr/bin/idle $(TARGET_DIR)/usr/bin/pydoc
	ln -s python2.4 $(TARGET_DIR)/usr/bin/python
	find $(TARGET_DIR)/usr/lib/python2.4 -name '*.pyo' -delete
	find $(TARGET_DIR)/usr/lib/python2.4 -name '*.pyc' -delete
	rm -rf $(TARGET_DIR)/usr/lib/python2.4/test $(TARGET_DIR)/usr/include/python2.4 \
		$(TARGET_DIR)/usr/lib/python2.4/config $(TARGET_DIR)/usr/lib/python2.4/bsddb \
		$(TARGET_DIR)/usr/lib/python2.4/curses $(TARGET_DIR)/usr/lib/python2.4/lib-tk
endef

$(eval $(generic-package))
