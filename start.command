#!/bin/sh
# macOS: double-click in the Finder to start the server in a Terminal window (close the window or press Ctrl+C to stop).
cd "$(dirname "$0")" && exec ./start.sh "$@"
