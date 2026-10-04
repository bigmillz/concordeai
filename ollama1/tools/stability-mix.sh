#!/usr/bin/env bash
# The graphics card at 100% and the processor at 50% for an hour, nothing else:
#   sudo bash stability-mix.sh                 60 minutes
#   sudo bash stability-mix.sh 30              30 minutes
#   sudo bash stability-mix.sh 60 gemma4:26b   a different model on the card
# It is stability-test.sh --phases mix --minutes N --cpu-load 50 --model M, with the
# options fixed so a pasted line can't lose them.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MIN=${1:-60}
MODEL=${2:-gemma4:12b}
exec bash "$HERE/stability-test.sh" --phases mix --minutes "$MIN" --cpu-load 50 --model "$MODEL"
