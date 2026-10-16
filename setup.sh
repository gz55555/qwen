#!/bin/bash
# setup.sh — bring a freshly rented GPU box up to speed, in one command.
#
#   git clone https://github.com/<you>/<model-repo>.git && cd <model-repo> && ./setup.sh
#
# Run ON THE GPU BOX as root. Idempotent: re-run any time; every step checks before acting.
#
#   1. Preflight      GPU, disk, supervisor, python venv
#   2. SSH access     authorize the central server's keys (public keys only, restricted)
#                     + a guard that re-adds them if authorized_keys gets rewritten
#   3. Ollama         install pinned version + run under supervisor (a running Ollama is never restarted)
#   4. Model          pull if missing (skipped when you copied ~/.ollama from the old box), load into VRAM
#   5. Monitoring     install + start the llmmon agent (GPU + container exporters, loopback only)
#   6. Register       tell the central server this box's IP/port/host key; central then repoints
#                     its SSH tunnels (monitoring + Qwen agent), Prometheus targets and hooks
#
# Options:
#   --token-file F   registration token file (or env LLMMON_REGISTER_TOKEN); without one, step 6 prints the central command
#   --no-register    skip step 6; prints the command to run on the central server instead
#   --host IP --port PORT   override the public SSH endpoint (default: Vast's PUBLIC_IPADDR / VAST_TCP_PORT_22)
#   --no-warm        do not load the model into VRAM
#   --model NAME     override MODEL from model.env
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
B="$HERE/bootstrap"
. "$HERE/model.env"

TOKEN_FILE=""; REGISTER=1; WARM=1; PUB_HOST=""; PUB_PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token-file) TOKEN_FILE="$2"; shift ;;
    --no-register) REGISTER=0 ;;
    --host) PUB_HOST="$2"; shift ;;
    --port) PUB_PORT="$2"; shift ;;
    --no-warm) WARM=0 ;;
    --model) MODEL="$2"; shift ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 64 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

install -d -m 0755 /var/log/llmmon
LOG="/var/log/llmmon/setup-$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*"; echo "log: $LOG"; exit 1; }
# Vast puts PUBLIC_IPADDR / VAST_TCP_PORT_22 in the container (PID 1) environment
envof() { local v="${!1:-}"; [ -n "$v" ] || v=$(tr '\0' '\n' < /proc/1/environ 2>/dev/null | sed -n "s/^$1=//p" | head -1); echo "$v"; }
api() { curl -s -m "${2:-10}" "http://127.0.0.1:11434$1" "${@:3}"; }

echo "setup: model=$MODEL instance_id=$INSTANCE_ID central=$CENTRAL_URL  (log: $LOG)"

# ── 1. preflight ─────────────────────────────────────────────────────────
step "1/6 preflight"
command -v nvidia-smi >/dev/null || die "nvidia-smi not found — not a GPU box?"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | sed 's/^/  gpu: /'
[ -e /sys/fs/cgroup/cgroup.controllers ] && echo "  cgroup: v2" || echo "  cgroup: v1"
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
echo "  root fs: ${FREE_GB} GB free"
need_pkgs=()
command -v curl >/dev/null || need_pkgs+=(curl)
command -v supervisorctl >/dev/null || need_pkgs+=(supervisor)
command -v sshd >/dev/null || [ -x /usr/sbin/sshd ] || need_pkgs+=(openssh-server)
python3 -c 'import ensurepip' 2>/dev/null || command -v uv >/dev/null || need_pkgs+=(python3-venv)
command -v zstd >/dev/null || need_pkgs+=(zstd)          # the Ollama installer needs it
if [ ${#need_pkgs[@]} -gt 0 ]; then
  echo "  installing: ${need_pkgs[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need_pkgs[@]}" >/dev/null
fi
sv_up() { supervisorctl pid 2>/dev/null | grep -qE '^[0-9]+$'; }
if ! sv_up; then echo "  starting supervisord"; supervisord -c /etc/supervisor/supervisord.conf; sleep 3; fi
sv_up || die "supervisord not reachable"
ok "supervisor reachable"
if sshd -T 2>/dev/null | grep -q '^allowtcpforwarding yes'; then ok "sshd allows TCP forwarding (needed for the tunnels)"
else warn "sshd does not report AllowTcpForwarding yes — the central tunnels will fail"; fi

# ── 2. SSH access for the central server ────────────────────────────────
step "2/6 central server SSH access"
# installs the bundle (keys file + guard live in it); programs are started in step 5
A=/opt/llmmon-agent
if [ -e "$A" ] && [ ! -f "$A/SHA256SUMS" ]; then die "$A exists and is not an llmmon bundle — refusing to overwrite"; fi
install -d -m 0750 "$A"
cp -r "$B/agent/." "$A/"
NE="node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64"
if ! (cd "$A" && grep ' ./bin/node_exporter$' SHA256SUMS | sha256sum --status -c - 2>/dev/null); then
  echo "  downloading node_exporter $NODE_EXPORTER_VERSION (checksum-pinned)"
  T=$(mktemp -d)
  curl -fsSL -o "$T/ne.tgz" "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/$NE.tar.gz"
  echo "$NODE_EXPORTER_SHA256  $T/ne.tgz" | sha256sum --quiet -c - || die "node_exporter download checksum mismatch"
  tar -xzf "$T/ne.tgz" -C "$T"; install -d "$A/bin"; install -m 0755 "$T/$NE/node_exporter" "$A/bin/node_exporter"; rm -rf "$T"
fi
sed "s/^LLMMON_INSTANCE_ID=.*/LLMMON_INSTANCE_ID=$INSTANCE_ID/" "$A/agent.env.example" > "$A/agent.env"
chown -R root:root "$A"
(cd "$A" && sha256sum --quiet -c SHA256SUMS) || die "agent bundle checksum mismatch"
ok "monitoring bundle staged in $A (checksums verified)"
"$A/scripts/install.sh" | sed 's/^/  /'
for tag in $(awk '{print $NF}' "$A/authorized_keys.llmmon"); do
  grep -qF "$tag" /root/.ssh/authorized_keys && ok "authorized: $tag" || die "key $tag not in authorized_keys"
done

# ── 3. Ollama ────────────────────────────────────────────────────────────
step "3/6 Ollama"
if ! command -v ollama >/dev/null; then
  echo "  installing Ollama $OLLAMA_VERSION (official installer, version pinned)…"
  curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION="$OLLAMA_VERSION" sh 2>&1 | tail -3 | sed 's/^/  /'
  command -v ollama >/dev/null || die "Ollama install failed"
fi
ok "Ollama binary: $(ollama --version 2>&1 | tail -1 | awk '{print $NF}')"
OLLAMA_BIN=$(command -v ollama)
sed -e "s#@OLLAMA_BIN@#$OLLAMA_BIN#" -e "s#@OLLAMA_ENV@#$OLLAMA_ENV#" "$B/ollama.conf" > /etc/supervisor/conf.d/ollama.conf
if api /api/version 3 >/dev/null; then
  if supervisorctl status ollama 2>/dev/null | grep -q RUNNING; then ok "Ollama running under supervisor"
  else
    warn "Ollama is already running but NOT under supervisor (started by hand) — left untouched."
    warn "It won't come back after a container restart. In a quiet window: pkill -x ollama; supervisorctl update ollama"
  fi
else
  supervisorctl reread >/dev/null; supervisorctl update ollama >/dev/null
  supervisorctl start ollama >/dev/null 2>&1 || true
  for i in $(seq 1 30); do api /api/version 2 >/dev/null && break; sleep 2; done
  api /api/version 2 >/dev/null || die "Ollama did not come up — tail -50 /var/log/ollama.log"
  ok "Ollama started under supervisor on 127.0.0.1:11434"
fi

# ── 4. model ─────────────────────────────────────────────────────────────
step "4/6 model $MODEL"
if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$MODEL"; then
  ok "$MODEL already present (copied from the old box, or pulled earlier)"
else
  echo "  pulling $MODEL ($MODEL_SIZE_HINT; resumes if interrupted)…"
  ollama pull "$MODEL" 2>&1 | tr '\r' '\n' | grep -E 'success|error|pulling manifest|verifying' | tail -5 | sed 's/^/  /' || true
  ollama list | awk 'NR>1{print $1}' | grep -qx "$MODEL" || die "model pull failed — re-run ./setup.sh to resume"
  ok "$MODEL pulled"
fi
CAPS=$(api /api/show 10 -d "{\"model\":\"$MODEL\"}" | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin).get("capabilities",[])))')
ok "capabilities: $CAPS"
if [ "$WARM" -eq 1 ]; then
  if api /api/ps 5 | grep -q "\"$MODEL\""; then ok "model already loaded in VRAM"
  else
    echo "  loading into VRAM (no generation)…"
    api /api/generate 600 -d "{\"model\":\"$MODEL\",\"prompt\":\"\",\"keep_alive\":\"$KEEP_ALIVE\",\"options\":{\"num_ctx\":$NUM_CTX}}" >/dev/null
    api /api/ps 5 | python3 -c 'import json,sys;[print("  \033[32m✓\033[0m loaded %s: %.1f GiB VRAM" % (m["name"], m["size_vram"]/2**30)) for m in json.load(sys.stdin)["models"]]'
  fi
fi

# ── 5. monitoring agent ──────────────────────────────────────────────────
step "5/6 monitoring agent"
"$A/scripts/validate.sh" | sed 's/^/  /' || die "agent validation failed"
"$A/scripts/start.sh" | sed 's/^/  /' || die "agent did not become healthy"

# ── 6. register with the central server ─────────────────────────────────
step "6/6 register with central ($CENTRAL_URL)"
PUB_HOST=${PUB_HOST:-$(envof PUBLIC_IPADDR)}
PUB_PORT=${PUB_PORT:-$(envof VAST_TCP_PORT_22)}
FPR=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')
[ -n "$PUB_HOST" ] && [ -n "$PUB_PORT" ] || die "public SSH endpoint unknown — re-run with --host IP --port PORT (Vast console → instance → IP/ports)"
echo "  public SSH: $PUB_HOST:$PUB_PORT   host key: ED25519 $FPR"
CENTRAL_CMD="/opt/ai-monitoring/scripts/gpu-up.sh --host $PUB_HOST --port $PUB_PORT --fingerprint $FPR --instance-id $INSTANCE_ID --model $MODEL --yes"
manual() { echo; echo "  Run this on the central server instead:"; echo "    $CENTRAL_CMD"; }
[ "$REGISTER" -eq 1 ] || { manual; exit 0; }

TOKEN="${LLMMON_REGISTER_TOKEN:-}"
[ -z "$TOKEN" ] && [ -n "$TOKEN_FILE" ] && TOKEN=$(tr -d ' \n\r' < "$TOKEN_FILE")
# auto-registration is opt-in (central endpoint must be enabled); without a token just print the command
[ -n "$TOKEN" ] || { ok "box is ready"; manual; exit 0; }
AUTH=(-H "Authorization: Bearer $TOKEN")
BODY=$(printf '{"instance_id":"%s","host":"%s","port":%s,"fingerprint":"%s","model":"%s"}' "$INSTANCE_ID" "$PUB_HOST" "$PUB_PORT" "$FPR" "$MODEL")
R=$(curl -s -m 20 -w '\n%{http_code}' "${AUTH[@]}" -H 'Content-Type: application/json' -d "$BODY" "$CENTRAL_URL/gpu/register") || R=$'\n000'
CODE=$(echo "$R" | tail -1); MSG=$(echo "$R" | sed '$d')
case "$CODE" in
  202) ok "central accepted: $MSG" ;;
  401) die "central rejected the token (401)" ;;
  409) warn "central is already running a setup — following it" ;;
  *)   warn "central answered HTTP $CODE: $MSG"; manual; exit 1 ;;
esac
echo "  central is repointing tunnels / Prometheus / hooks (usually 1–3 min)…"
seen=0; RT=$(mktemp)
for i in $(seq 1 120); do
  sleep 5
  S=$(curl -s -m 15 "${AUTH[@]}" "$CENTRAL_URL/gpu/status") || continue
  echo "$S" | python3 -c '
import json,sys
s=json.load(sys.stdin); seen=int(sys.argv[1]); lines=s.get("log",[])
for l in lines[seen:]: print("  │ "+l)
open(sys.argv[2],"w").write("%d %s %s" % (len(lines), s.get("state"), s.get("exit_code")))' "$seen" "$RT" 2>/dev/null || continue
  read -r seen STATE EXIT < "$RT"
  [ "$STATE" = running ] || break
done
rm -f "$RT"
[ "${STATE:-}" = done ] && [ "${EXIT:-1}" = 0 ] || { warn "central setup did not finish cleanly (state=${STATE:-?} exit=${EXIT:-?})"; manual; exit 1; }
ok "central side done: tunnels up, live telemetry flowing"

step "ready"
echo "  Ollama 127.0.0.1:11434 · model $MODEL · monitoring agent running · central tunnels connected"
echo "  log: $LOG"
