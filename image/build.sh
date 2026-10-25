#!/bin/bash
# SPDX-License-Identifier: MIT
# Image stage: the USB-key install image in $LL_OUT/usbkey (see
# image/tools/image.py). Needs the system, v8, kernel and userland stages.
. "$(dirname "$0")/../scripts/common.sh"
"$LL_PY" -I -B "$LL_ROOT/image/tools/image.py" "$@"
