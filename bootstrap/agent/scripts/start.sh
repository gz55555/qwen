#!/bin/bash
set -euo pipefail; . "$(dirname "$0")/common.sh"
supervisorctl reread >/dev/null
supervisorctl update $PROGRAMS        # adds new programs (autostart) / applies changed config
supervisorctl start $PROGRAMS 2>/dev/null || true
sleep 6
"$(dirname "$0")/healthcheck.sh"
