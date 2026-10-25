# SPDX-License-Identifier: MIT
# The Karotz userland: 18 programs and two shared libraries.
#
#   make OUT=<dir>                 build with the host toolchain (for tests)
#   make OUT=<dir> CROSS=... ...   build for the rabbit (system/userland.sh
#                                  passes the toolchain and sysroot settings)
#
# Sources are only read; objects, generated code and binaries go to OUT.
# The rabbit is an ARM920T (ARMv4T, soft float) running uClibc-ng;
# binaries for it link against the system build's libraries (LDFLAGS from
# system/userland.sh) with headers from its sysroot (PKG_CONFIG settings).

S        := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
CROSS    ?=
CC       := $(CROSS)gcc
CXX      := $(CROSS)g++
PKG_CONFIG ?= pkg-config
# Code generators run on the build machine. For the rabbit, PROTOC must be
# protoc 2.3.0 (the rabbit's libprotobuf.so.6 is 2.3.0).
DBUS_BINDING_TOOL ?= dbus-binding-tool
GLIB_GENMARSHAL   ?= glib-genmarshal
PROTOC            ?= protoc
LDFLAGS  ?=
CFLAGS   ?= -O2 -g
override CFLAGS += -std=gnu99 -Wall -Wextra -Wno-deprecated-declarations
CXXFLAGS ?= -O2 -g
override CXXFLAGS += -Wall -Wextra -Wno-deprecated-declarations
OUT      ?= build/$(if $(CROSS),$(CROSS:%-=%),host)
GEN      := $(OUT)/gen

DBUS_CFLAGS := $(shell $(PKG_CONFIG) --cflags dbus-glib-1 dbus-1 gobject-2.0 glib-2.0)
DBUS_LIBS   := $(shell $(PKG_CONFIG) --libs dbus-glib-1 dbus-1 gobject-2.0 glib-2.0)
INCLUDES    := -I$(S)/src/usr/libkarotz-common -I$(S)/src/usr/libkarotz-messages -I$(GEN)

all: tools libs daemons

.SECONDEXPANSION:

# --- standalone tools (no D-Bus) -----------------------------------------
TOOLS := rootfs/power rootfs/ears rootfs/led usr/immortaldog \
         usr/scripts/k2k/playback usr/scripts/k2k/rec
tools: $(addprefix $(OUT)/,$(TOOLS))
$(addprefix $(OUT)/,$(TOOLS)): $(OUT)/%: $$(wildcard $(S)/src/%/*.c)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

# --- shared libraries -----------------------------------------------------
LIBDIR := $(OUT)/usr/lib
libs: $(LIBDIR)/libkarotz-messages.so $(LIBDIR)/libkarotz-common.so

$(LIBDIR)/libkarotz-messages.so: $(S)/src/usr/libkarotz-messages/karotz-messages.c $(S)/src/usr/libkarotz-messages/karotz-messages.h
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -fPIC -shared $(DBUS_CFLAGS) $(INCLUDES) -o $@ $< $(LDFLAGS) $(DBUS_LIBS)

COMMON_SRC := $(wildcard $(S)/src/usr/libkarotz-common/*.c)
$(LIBDIR)/libkarotz-common.so: $(COMMON_SRC) $(S)/src/usr/libkarotz-common/karotz-common.h
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -fPIC -shared $(DBUS_CFLAGS) $(INCLUDES) -o $@ $(COMMON_SRC) $(LDFLAGS) $(DBUS_LIBS) -luuid

# --- daemons ----------------------------------------------------------------
# Each daemon's server glue is generated from its D-Bus interface, with the
# GObject prefix the daemon's methods use.
DAEMONS := button-daemon ears-daemon led-daemon rfid-daemon scheduler-daemon webcam-daemon \
           serial-daemon controller multimedia-daemon voice-daemon k2k-daemon karotzVM

# These need libmpd 0.17, protobuf 2.3, linphone 3.4 or V8 2.5, which only
# the system build has: they are built for the rabbit, not for the host.
ARM_ONLY := multimedia-daemon voice-daemon k2k-daemon karotzVM

# Per daemon: <name>_PREFIX (dbus-binding-tool --prefix, from the GObject
# type name), <name>_CFLAGS and <name>_LIBS (extra pkg-config flags/libs; 2010-era
# libraries such as expat and libjpeg have no .pc file, hence the fallbacks).
button-daemon_PREFIX     := button_object
ears-daemon_PREFIX       := ears_object
led-daemon_PREFIX        := led_object
rfid-daemon_PREFIX       := rfid_object
scheduler-daemon_PREFIX  := scheduler_object
webcam-daemon_PREFIX     := webcam_object
serial-daemon_PREFIX     := serial_object
multimedia-daemon_PREFIX := multimedia_object
voice-daemon_PREFIX      := voice_object
controller_PREFIX        := controller_object
k2k-daemon_PREFIX        := k2k_object

ears-daemon_LIBS       :=
led-daemon_LIBS        :=
rfid-daemon_LIBS       := $(shell $(PKG_CONFIG) --libs expat 2>/dev/null || echo -lexpat)
scheduler-daemon_LIBS  :=
webcam-daemon_LIBS     := $(shell $(PKG_CONFIG) --libs libcurl) $(shell $(PKG_CONFIG) --libs libjpeg 2>/dev/null || echo -ljpeg)
serial-daemon_LIBS     :=
multimedia-daemon_LIBS := $(shell $(PKG_CONFIG) --libs libmpd gthread-2.0)
multimedia-daemon_CFLAGS := $(shell $(PKG_CONFIG) --cflags libmpd)
voice-daemon_LIBS      := $(shell $(PKG_CONFIG) --cflags --libs libcurl libxml-2.0 protobuf uuid)
voice-daemon_EXTRA     := $(GEN)/asr-message.pb.cc
# k2k-daemon's libraries in this order, so they load and initialise in this
# order (where libpthread comes matters with LinuxThreads).
k2k-daemon_PRELIBS     := -ldbus-glib-1 -ldbus-1 -lpthread -lgobject-2.0 -lgthread-2.0 -lglib-2.0 -lintl
k2k-daemon_LIBS        := $(shell $(PKG_CONFIG) --cflags linphone 2>/dev/null) -llinphone -lmediastreamer -lortp \
                          -losip2 -leXosip2 -losipparser2 -lpthread -lgthread-2.0
karotzVM_LIBS          := -lv8 $(shell $(PKG_CONFIG) --cflags --libs protobuf libcurl libxml-2.0 uuid zlib) -liconv
karotzVM_EXTRA         := $(GEN)/platform-message.pb.cc
controller_LIBS        := $(shell $(PKG_CONFIG) --cflags --libs protobuf gio-2.0 libcurl libxml-2.0 uuid) \
                          $(shell $(PKG_CONFIG) --libs libjpeg 2>/dev/null || echo -ljpeg) \
                          $(shell $(PKG_CONFIG) --cflags --libs libcrypto 2>/dev/null) \
                          -lintl
# -lintl: the controller calls bindtextdomain() itself; GLib 2.26 brought libintl in with it,
# a current GLib doesn't.
controller_EXTRA       := $(GEN)/platform-message.pb.cc

BUILT_DAEMONS := $(if $(CROSS),$(DAEMONS),$(filter-out $(ARM_ONLY),$(DAEMONS)))
daemons: $(addprefix $(OUT)/usr/,$(BUILT_DAEMONS))

# protobuf code; a daemon lists the .pb.cc it needs in <name>_EXTRA.
$(GEN)/%.pb.cc $(GEN)/%.pb.h: $(S)/proto/%.proto
	@mkdir -p $(GEN)
	$(PROTOC) --proto_path=$(S)/proto --cpp_out=$(GEN) $<

# A program that exports no dbus-glib object (karotzVM) gets an empty glue header.
$(GEN)/%-glue.h: $$(wildcard $(S)/dbus/%.xml)
	@mkdir -p $(dir $@)
	$(if $<,$(DBUS_BINDING_TOOL) --mode=glib-server --prefix=$($*_PREFIX) $< > $@,echo "/* $* exports no dbus-glib object */" > $@)

# Signal marshallers (karotz_smarshal_*), shared by the daemons.
$(GEN)/karotz-marshal.h: $(S)/src/usr/karotz-marshal.list
	@mkdir -p $(dir $@)
	$(GLIB_GENMARSHAL) --prefix=karotz_smarshal --header $< > $@
$(GEN)/karotz-marshal.o: $(S)/src/usr/karotz-marshal.list $(GEN)/karotz-marshal.h
	$(GLIB_GENMARSHAL) --prefix=karotz_smarshal --body --include-header=karotz-marshal.h $< > $(GEN)/karotz-marshal.c
	$(CC) $(CFLAGS) -Wno-unused-parameter $(DBUS_CFLAGS) -I$(GEN) -c -o $@ $(GEN)/karotz-marshal.c

# C++ sources (.cc) are compiled with CXX and protobuf headers from $(GEN).
$(addprefix $(OUT)/usr/,$(DAEMONS)): $(OUT)/usr/%: $$(wildcard $(S)/src/usr/%/*.c $(S)/src/usr/%/*.cc) $$($$*_EXTRA) \
		$(GEN)/%-glue.h $(GEN)/karotz-marshal.o $(LIBDIR)/libkarotz-messages.so $(LIBDIR)/libkarotz-common.so
	@mkdir -p $(dir $@)
	$(if $(filter %.cc,$^),$(CXX) $(CXXFLAGS),$(CC) $(CFLAGS)) $(DBUS_CFLAGS) $(INCLUDES) $($*_CFLAGS) -o $@ \
		$(filter %.c %.cc %.o,$^) $(LDFLAGS) -L$(LIBDIR) -lkarotz-common -lkarotz-messages $($*_PRELIBS) $($*_LIBS) $(DBUS_LIBS)

# --- karotz-player, the rabbit's player on FFmpeg (the system build only: the host has none of its
# libraries at the versions it uses, so it isn't part of all) -------------------------------
AV_CFLAGS := $(shell $(PKG_CONFIG) --cflags libavformat libavcodec libswresample libavutil vorbisidec ogg 2>/dev/null)
AV_LIBS   := $(shell $(PKG_CONFIG) --libs libavformat libavcodec libswresample libavutil vorbisidec ogg 2>/dev/null)
player: $(OUT)/usr/bin/karotz-player
$(OUT)/usr/bin/karotz-player: $(S)/src/usr/karotz-player/karotz-player.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $(AV_CFLAGS) -o $@ $< $(LDFLAGS) $(AV_LIBS)

clean:
	rm -rf $(OUT)

.PHONY: all tools libs daemons player clean
