#!/bin/bash
# Container-scoped node_exporter. CPU/memory/load collectors are DISABLED because
# inside a Vast container they report the shared physical host (see discovery doc).
# Only the container root filesystem "/" is kept (bind mounts like /etc/hosts and
# /usr/bin/nvidia-smi point at HOST disks and are excluded).
set -euo pipefail
set -a; . /opt/llmmon-agent/agent.env; set +a
exec nice -n 10 /opt/llmmon-agent/bin/node_exporter \
  --web.listen-address="${LLMMON_NODE_EXPORTER_ADDR}" \
  --collector.disable-defaults \
  --collector.filesystem \
  --collector.filesystem.mount-points-exclude='^/.+' \
  --collector.filesystem.fs-types-exclude='^(tmpfs|devtmpfs|proc|sysfs|cgroup2?|mqueue|devpts|nsfs|fuse.*)$' \
  --collector.netdev \
  --collector.netdev.device-exclude='^lo$' \
  --collector.netstat \
  --collector.sockstat \
  --collector.uname
