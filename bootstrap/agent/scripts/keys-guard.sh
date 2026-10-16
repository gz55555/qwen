#!/bin/bash
# Re-applies llmmon-managed SSH key lines if something rewrote /root/.ssh/authorized_keys
# (Vast key sync, another deployment). Only ADDS missing managed lines; never removes or edits
# anyone else's keys. Runs under supervisor (llmmon-keys-guard) with --loop: every 30 s.
# (Vast containers usually have no cron daemon, supervisor is always there.)
set -euo pipefail
if [ "${1:-}" = "--loop" ]; then
  while true; do "$0" || true; sleep 30; done
fi
AK=/root/.ssh/authorized_keys
SRC=/opt/llmmon-agent/authorized_keys.llmmon
[ -s "$SRC" ] || exit 0
install -d -m 0700 /root/.ssh; touch "$AK"; chmod 0600 "$AK"
changed=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  tag=${line##* }                                  # key comment = last field (unique per key)
  grep -qF -- "$tag" "$AK" && continue
  [ -z "$(tail -c1 "$AK")" ] || echo >> "$AK"      # never glue onto a line without newline
  printf '%s\n' "$line" >> "$AK"; changed=1
done < "$SRC"
[ "$changed" = 1 ] && echo "$(date -u +%FT%TZ) restored managed authorized_keys lines" || true
