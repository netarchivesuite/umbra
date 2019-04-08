#!/usr/bin/env bash

#Script that shows you how to add in extra flags for the chrome(ium) browser.

SCRIPT_DIR=$(dirname $(readlink -f $BASH_SOURCE[0]))

mkdir -p ${SCRIPT_DIR}/chrome_logs

LOGFILE="$SCRIPT_DIR/chrome_logs/chrome.$$.log"

#Notice that we use $@ to include the script arguments
#Notice that we DO NOT start the browser in the background, in fact we use exec to make the process take over this process
# If we did not, the pid that umbra would get, and thus kill, would be the pid of the script, not the pid of the browser, which might allow the browser to live on.
# This way, the browser dies, and it dies well.
# But we still keep a logfile of what the browser experienced, named for the pid of the browser
exec chromium-browser \
    --headless \
    --no-sandbox \
    --disable-3d-apis \
    --disable-accelerated-video	\
    --disable-background-mode \
    --disable-gpu \
    --disable-plugins \
    --disable-plugins-discovery \
    --disable-preconnect \
    --disable-translate \
    --disable-local-storage \
    --full-memory-crash-report \
    --mute-audio \
    --disable-gpu-early-init \
    --enable-logging=stderr \
    --log-level=0 \
    --disable-dev-shm-usage \
    $@ \
    &>>$LOGFILE