#!/bin/bash
set -uo pipefail; . "$(dirname "$0")/common.sh"
supervisorctl status $PROGRAMS
echo; echo "listeners:"; ss -ltnp "( sport = :9100 or sport = :9835 )"
