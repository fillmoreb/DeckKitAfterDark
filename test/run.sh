#!/bin/bash
# Runs the tests (needs lua5.1 and python3).
cd "$(dirname "$0")/.." && exec python3 -m unittest discover -s test "$@"
