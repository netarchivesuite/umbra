#!/usr/bin/env bash
SCRIPT_DIR=$(dirname $(readlink -f $BASH_SOURCE[0]))

ulimit -c 0

AMQP='amqp://guest:guest@localhost:5672/%2f'

$SCRIPT_DIR/../bin/drain-queue --url "$AMQP"

$SCRIPT_DIR/../bin/umbra \
    --max-browsers 1 \
    --executable $SCRIPT_DIR/run-chromium.sh \
    --url "$AMQP" \
    --log_config_file $SCRIPT_DIR/../logging.conf
