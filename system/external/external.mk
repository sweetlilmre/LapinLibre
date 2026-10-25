# SPDX-License-Identifier: GPL-2.0-or-later
include $(BR2_EXTERNAL_LAPINLIBRE_PATH)/package/karotz-era.mk.inc
include $(sort $(wildcard $(BR2_EXTERNAL_LAPINLIBRE_PATH)/package/*/*.mk))
