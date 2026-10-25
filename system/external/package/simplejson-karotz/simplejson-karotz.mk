# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# simplejson-karotz: simplejson 2.0.7 in /usr/lib/python2.4/simplejson: its module files only
# (and _speedups.c, as source).
#
################################################################################

SIMPLEJSON_KAROTZ_VERSION = 2.0.7
SIMPLEJSON_KAROTZ_SOURCE = simplejson-$(SIMPLEJSON_KAROTZ_VERSION).tar.gz
SIMPLEJSON_KAROTZ_SITE = https://files.pythonhosted.org/packages/source/s/simplejson
SIMPLEJSON_KAROTZ_LICENSE = MIT
SIMPLEJSON_KAROTZ_LICENSE_FILES = LICENSE.txt
SIMPLEJSON_KAROTZ_DEPENDENCIES = python24-karotz
SIMPLEJSON_KAROTZ_FILES = __init__.py _speedups.c decoder.py encoder.py scanner.py tool.py

define SIMPLEJSON_KAROTZ_INSTALL_TARGET_CMDS
	for f in $(SIMPLEJSON_KAROTZ_FILES); do \
		install -D -m 644 $(@D)/simplejson/$$f $(TARGET_DIR)/usr/lib/python2.4/simplejson/$$f; \
	done
endef

$(eval $(generic-package))
