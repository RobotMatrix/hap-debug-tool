#!/bin/bash
cd "$(dirname "$0")"
PY=/Users/lasysloth/miniconda3/bin/python3
[ -x "$PY" ] || PY=python3
exec "$PY" hap_gui.py
