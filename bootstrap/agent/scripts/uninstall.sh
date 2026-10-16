#!/bin/bash
# Removes the llmmon agents and the key guard. Ollama, the GRC tunnel, and Vast services are not touched.
#   uninstall.sh          stop + deregister programs + remove telemetry key
#   uninstall.sh --purge  additionally delete /opt/llmmon-agent, logs and the llmmon user
set -euo pipefail; . "$(dirname "$0")/common.sh"
supervisorctl stop $PROGRAMS 2>/dev/null || true
for p in $PROGRAMS; do rm -f "$CONF_DIR/$p.conf"; done
supervisorctl reread >/dev/null; supervisorctl update $PROGRAMS 2>/dev/null || true
ok "programs stopped and deregistered"
rm -f /etc/cron.d/llmmon-keys
if [ -f "$AUTH_KEYS" ]; then sed -i.llmmon.bak "/llmmon-telemetry@\|qwenagent-ollama@/d" "$AUTH_KEYS"; ok "guard + agent keys removed (admin key kept; backup: $AUTH_KEYS.llmmon.bak)"; fi
if [ "${1:-}" = "--purge" ]; then
  rm -rf /var/log/llmmon "$AGENT_DIR"; userdel llmmon 2>/dev/null || true; ok "purged $AGENT_DIR"
fi
