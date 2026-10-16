#!/bin/bash
# Foreground launcher used by supervisor. Low CPU priority so inference always wins.
set -euo pipefail
set -a; . /opt/llmmon-agent/agent.env; set +a
exec nice -n 10 /opt/llmmon-agent/venv/bin/python -I -u /opt/llmmon-agent/exporters/llmmon_gpu_exporter.py
