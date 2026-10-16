#!/bin/bash
# Idempotent install. Does NOT start the agents (use start.sh).
# Safe to re-run after a Vast recycle/replacement (container fs is not persistent).
set -euo pipefail; . "$(dirname "$0")/common.sh"
echo "llmmon agent — install"
cd "$AGENT_DIR"; sha256sum --quiet -c SHA256SUMS; ok "checksums verified"
id llmmon >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin llmmon
ok "service user llmmon"
[ -f agent.env ] || cp agent.env.example agent.env
chmod 0644 agent.env; chmod 0755 bin/node_exporter scripts/*.sh
if [ ! -x venv/bin/python ]; then
  python3 -m venv venv 2>/dev/null || { rm -rf venv; uv venv --quiet --python /usr/bin/python3 venv; }
fi
if [ -x venv/bin/pip ]; then venv/bin/pip install --quiet --no-index --find-links wheels nvidia-ml-py==13.580.82 prometheus-client==0.22.1
else uv pip install --quiet --python venv/bin/python --no-index --find-links wheels nvidia-ml-py==13.580.82 prometheus-client==0.22.1; fi
ok "python venv with pinned offline wheels"
install -d -m 0755 /var/log/llmmon
for p in $PROGRAMS; do install -m 0644 "supervisor/$p.conf" "$CONF_DIR/$p.conf"; done
ok "supervisor programs installed ($PROGRAMS)"
# managed key lines (telemetry, agent, admin): apply now; the llmmon-keys-guard supervisor
# program re-applies them every 30 s because Vast/other deployments may rewrite authorized_keys
/opt/llmmon-agent/scripts/keys-guard.sh
rm -f /etc/cron.d/llmmon-keys    # older bundles used cron
ok "managed keys applied (guard: llmmon-keys-guard); telemetry key forward-only to 9100/9835"
# exporters run as the unprivileged llmmon user: group read/traverse, no write
chown -R root:llmmon "$AGENT_DIR"; chmod -R g+rX,g-w,o-rwx "$AGENT_DIR"
ok "ownership root:llmmon, mode 0750 (agents cannot modify their own code)"
supervisorctl reread >/dev/null
echo "Installed. Next: $AGENT_DIR/scripts/start.sh"
