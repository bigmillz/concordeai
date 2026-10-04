#!/usr/bin/env bash
# The graphics card at 100%, the processor left alone, nothing else:
#   sudo bash stability-gpu.sh                 60 minutes
#   sudo bash stability-gpu.sh 15              15 minutes
#   sudo bash stability-gpu.sh 60 gemma4:26b   a different model on the card
# It is stability-test.sh --phases gpu --minutes N --model M. Every 30 s it prints the
# temperatures, the card's busy percent and how many answers have come back, and it stops
# with the reason if nothing has been answered after 150 s.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MIN=${1:-60}
MODEL=${2:-gemma4:12b}
exec bash "$HERE/stability-test.sh" --phases gpu --minutes "$MIN" --model "$MODEL"
