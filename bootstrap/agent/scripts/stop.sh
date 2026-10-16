#!/bin/bash
# Stops the monitoring agents only. Ollama and Vast services are not touched.
set -euo pipefail; . "$(dirname "$0")/common.sh"
supervisorctl stop $PROGRAMS
