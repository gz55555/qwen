#!/bin/bash
# Pre-flight checks. Read-only: changes nothing, starts nothing.
set -uo pipefail; . "$(dirname "$0")/common.sh"; FAILED=0
echo "llmmon agent — validate"
cd "$AGENT_DIR" && sha256sum --quiet -c SHA256SUMS && ok "bundle checksums match" || fail "bundle checksum mismatch"
[ -c /dev/nvidiactl ] && ok "NVIDIA device nodes present" || fail "/dev/nvidiactl missing"
ldconfig -p | grep -q libnvidia-ml.so.1 && ok "libnvidia-ml present" || fail "libnvidia-ml.so.1 not found"
command -v supervisorctl >/dev/null && supervisorctl status >/dev/null 2>&1; [ $? -le 3 ] && ok "supervisord reachable" || fail "supervisord not reachable"
for p in 9100 9835; do
  if ss -ltnH "sport = :$p" | grep -q .; then
    # capture first: supervisorctl exits 3 when ANY program (e.g. Vast's jupyter) is not running
    st=$(supervisorctl status $PROGRAMS 2>/dev/null || true)
    grep -q "^llmmon-.*RUNNING" <<<"$st" && ok "port $p in use by llmmon (already running)" || fail "port $p already in use by something else"
  else ok "port $p free"; fi
done
"$AGENT_DIR/bin/node_exporter" --version >/dev/null 2>&1 && ok "node_exporter binary runs" || fail "node_exporter binary"
if [ -x "$AGENT_DIR/venv/bin/python" ]; then
  "$AGENT_DIR/venv/bin/python" -I - <<'PY' && ok "NVML read-only probe OK" || fail "NVML probe failed"
import pynvml; pynvml.nvmlInit()
h = pynvml.nvmlDeviceGetHandleByIndex(0)
n = pynvml.nvmlDeviceGetName(h); n = n.decode() if isinstance(n, bytes) else n
print(f"    GPUs={pynvml.nvmlDeviceGetCount()} gpu0={n} driver={pynvml.nvmlSystemGetDriverVersion()}")
pynvml.nvmlShutdown()
PY
else warn "venv not built yet — run scripts/install.sh"; fi
grep -q "$KEY_TAG" "$AUTH_KEYS" 2>/dev/null && ok "restricted telemetry key authorized" || warn "telemetry key not yet in $AUTH_KEYS (install.sh adds it)"
curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null && ok "Ollama answering on 127.0.0.1:11434 (untouched)" || warn "Ollama not answering (agents do not depend on it)"
[ "$FAILED" -eq 0 ] && echo "VALIDATION PASSED" || { echo "VALIDATION FAILED"; exit 1; }
