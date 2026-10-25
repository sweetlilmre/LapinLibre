# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# protobuf-karotz: protobuf 2.3.0 (libprotobuf.so.6), for the Karotz programs
#
################################################################################

PROTOBUF_KAROTZ_VERSION = 2.3.0
PROTOBUF_KAROTZ_SOURCE = protobuf-$(PROTOBUF_KAROTZ_VERSION).tar.bz2
PROTOBUF_KAROTZ_SITE = https://storage.googleapis.com/google-code-archive-downloads/v2/code.google.com/protobuf
PROTOBUF_KAROTZ_LICENSE = BSD-3-Clause
PROTOBUF_KAROTZ_LICENSE_FILES = COPYING.txt
PROTOBUF_KAROTZ_INSTALL_STAGING = YES
PROTOBUF_KAROTZ_DEPENDENCIES = host-protobuf-karotz

# 2.3.0 is C++98 code: GCC 14 defaults to C++17.
PROTOBUF_KAROTZ_CONF_ENV = CXXFLAGS="$(TARGET_CXXFLAGS) -std=gnu++98"
PROTOBUF_KAROTZ_CONF_OPTS = --with-protoc=$(HOST_DIR)/bin/protoc-2.3.0 --disable-static
HOST_PROTOBUF_KAROTZ_CONF_ENV = CXXFLAGS="$(HOST_CXXFLAGS) -std=gnu++98"
HOST_PROTOBUF_KAROTZ_CONF_OPTS = --disable-shared --program-suffix=-2.3.0

$(eval $(autotools-package))
$(eval $(host-autotools-package))
