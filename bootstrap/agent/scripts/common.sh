# Shared helpers for llmmon agent scripts (sourced, not executed).
AGENT_DIR=/opt/llmmon-agent
PROGRAMS="llmmon-node-exporter llmmon-gpu-exporter llmmon-keys-guard"
CONF_DIR=/etc/supervisor/conf.d
AUTH_KEYS=/root/.ssh/authorized_keys
KEY_TAG=llmmon-telemetry
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAILED=1; }
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
