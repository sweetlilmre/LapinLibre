# SPDX-License-Identifier: GPL-2.0-or-later
################################################################################
#
# mpd-karotz: mpd 0.15.12 with the Karotz count_externalClient status field
# (0001), and its FFmpeg plug-in ported to FFmpeg 6.1 (0002).
# Its features: mad, vorbis and FFmpeg decoders; file, http, lastfm and mms input;
# null, fifo and OSS outputs.
#
################################################################################

MPD_KAROTZ_VERSION = 0.15.12
MPD_KAROTZ_SOURCE = mpd-$(MPD_KAROTZ_VERSION).tar.bz2
MPD_KAROTZ_SITE = https://www.musicpd.org/download/mpd/0.15
MPD_KAROTZ_LICENSE = GPL-2.0+
MPD_KAROTZ_LICENSE_FILES = COPYING
# Tremor, not libvorbis: fixed-point Ogg Vorbis on the FPU-less ARM920T.
MPD_KAROTZ_DEPENDENCIES = host-pkgconf libglib2 libmad libid3tag tremor ffmpeg libcurl libmms zlib

MPD_KAROTZ_CONF_ENV = CFLAGS="$(TARGET_CFLAGS) -std=gnu99 $(KAROTZ_GCC14_PORTING)"
MPD_KAROTZ_CONF_OPTS = \
	--disable-werror --disable-documentation --disable-test \
	--enable-mad --enable-vorbis --with-tremor --enable-ffmpeg --enable-id3 \
	--enable-curl --enable-lastfm --enable-mms \
	--enable-oss --enable-fifo \
	--disable-alsa --disable-ao --disable-jack --disable-mvp --disable-pulse --disable-pipe-output \
	--disable-httpd-output --disable-shout --disable-lame-encoder --disable-vorbis-encoder \
	--disable-audiofile --disable-flac --disable-oggflac --disable-mikmod --disable-modplug \
	--disable-mpc --disable-sidplay --disable-fluidsynth --disable-wildmidi --disable-wavpack \
	--disable-sqlite --disable-lsr --disable-cue --disable-bzip2 --disable-zip --disable-iso9660

$(eval $(autotools-package))
