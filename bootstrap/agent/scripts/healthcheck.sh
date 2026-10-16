#!/bin/bash
# Local end-to-end metrics test on the GPU instance.
set -uo pipefail; . "$(dirname "$0")/common.sh"; FAILED=0
echo "llmmon agent — healthcheck"
for p in $PROGRAMS; do supervisorctl status "$p" | grep -q RUNNING && ok "$p RUNNING" || fail "$p not running"; done
G=$(curl -s -m 5 http://127.0.0.1:9835/metrics) || G=""
N=$(curl -s -m 5 http://127.0.0.1:9100/metrics) || N=""
[ -n "$G" ] && ok "gpu exporter answers on 127.0.0.1:9835" || fail "gpu exporter not answering"
[ -n "$N" ] && ok "node exporter answers on 127.0.0.1:9100" || fail "node exporter not answering"
echo "$G" | grep -q '^llmmon_nvml_up 1' && ok "NVML up" || fail "NVML not up"
echo "$G" | grep -q '^llmmon_exporter_up 1' && ok "GPU poll succeeding" || fail "GPU poll failing"
echo "$G" | grep -E '^llmmon_gpu_(info|utilization_ratio|memory_used_bytes|temperature_celsius|power_watts)\b' | sed 's/^/    /'
echo "$N" | grep -q 'node_filesystem_size_bytes{.*mountpoint="/"' && ok "container root filesystem metric present" || fail "filesystem metric missing"
ss -ltnH | awk '{print $4}' | grep -E ':(9100|9835)$' | grep -vqE '^127\.0\.0\.1:' && fail "exporter bound to a non-loopback address!" || ok "exporters bound to 127.0.0.1 only"
[ "$FAILED" -eq 0 ] && echo "HEALTHY" || { echo "UNHEALTHY"; exit 1; }
