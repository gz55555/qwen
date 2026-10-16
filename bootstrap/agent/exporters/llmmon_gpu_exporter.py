#!/usr/bin/env python3
"""llmmon GPU + container exporter (Layer 1 agent).

Exposes, on 127.0.0.1 only:
  * NVIDIA GPU telemetry via NVML (no subprocesses; nvidia-smi is never executed)
  * Container resource usage from cgroup v2 (scope = this Vast container)
  * Allow-listed process RSS/CPU from /proc (default: ollama)
  * Physical-host context metrics, explicitly prefixed llmmon_physical_host_*

Design rules (Layer 1 mission):
  * A background thread polls every POLL_INTERVAL seconds; scrapes only read the
    cached snapshot, so scrape rate can never increase GPU/NVML load.
  * Every source is isolated: one failing read never blanks the others.
  * "Unsupported" ≠ 0: unsupported NVML fields are omitted and reported through
    llmmon_gpu_metric_supported{metric}=0.
  * Health: llmmon_exporter_up, llmmon_nvml_up, last-success timestamp, error counters.
"""
import logging
import os
import signal
import sys
import threading
import time

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

try:
    import pynvml  # provided by the nvidia-ml-py package
except Exception:  # pragma: no cover - reported as nvml_up=0
    pynvml = None

VERSION = "1.0.3"
LISTEN_ADDR = os.environ.get("LLMMON_LISTEN_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LLMMON_LISTEN_PORT", "9835"))
POLL_INTERVAL = max(5.0, float(os.environ.get("LLMMON_POLL_INTERVAL", "15")))
INSTANCE_ID = os.environ.get("LLMMON_INSTANCE_ID", "unknown")
PROCESS_ALLOWLIST = [p for p in os.environ.get("LLMMON_PROCESS_ALLOWLIST", "ollama").split(",") if p]
CGROUP = os.environ.get("LLMMON_CGROUP_ROOT", "/sys/fs/cgroup")
PROC = "/proc"
CLK_TCK = os.sysconf("SC_CLK_TCK")

log = logging.getLogger("llmmon-gpu-exporter")

# NVML clock-event / throttle reason bits (nvml.h nvmlClocksEventReason*)
CLOCK_EVENT_REASONS = {
    "gpu_idle": 0x1,
    "applications_clocks_setting": 0x2,
    "sw_power_cap": 0x4,
    "hw_slowdown": 0x8,
    "sync_boost": 0x10,
    "sw_thermal_slowdown": 0x20,
    "hw_thermal_slowdown": 0x40,
    "hw_power_brake_slowdown": 0x80,
    "display_clock_setting": 0x100,
}
TEMP_THRESHOLDS = {"shutdown": 0, "slowdown": 1, "mem_max": 2, "gpu_max": 3}


def read_text(path):
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        return fh.read()


def read_kv(path):
    out = {}
    for line in read_text(path).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


class Snapshot:
    def __init__(self):
        self.families = []
        self.ok = False
        self.nvml_ok = False
        self.last_success = 0.0
        self.duration = 0.0


class Exporter:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = Snapshot()
        self.errors = {}            # source -> count
        self.xid_counts = {}        # gpu index -> count
        self.xid_last = {}          # gpu index -> last xid code
        self.xid_supported = {}     # gpu index -> bool
        self.nvml_initialized = False
        self.stop = threading.Event()

    # ── error bookkeeping ──
    def err(self, source, exc):
        self.errors[source] = self.errors.get(source, 0) + 1
        log.warning("collect error source=%s: %s", source, exc)

    # ── NVML lifecycle ──
    def nvml_init(self):
        if self.nvml_initialized:
            return True
        if pynvml is None:
            raise RuntimeError("pynvml (nvidia-ml-py) not importable")
        pynvml.nvmlInit()
        self.nvml_initialized = True
        log.info("NVML initialised: driver %s", self._s(pynvml.nvmlSystemGetDriverVersion()))
        threading.Thread(target=self.xid_watcher, name="xid-watcher", daemon=True).start()
        return True

    @staticmethod
    def _s(v):
        return v.decode() if isinstance(v, bytes) else str(v)

    def xid_watcher(self):
        """Count critical XID events (best effort; unsupported in some containers)."""
        try:
            evset = pynvml.nvmlEventSetCreate()
            for i in range(pynvml.nvmlDeviceGetCount()):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                try:
                    pynvml.nvmlDeviceRegisterEvents(h, pynvml.nvmlEventTypeXidCriticalError, evset)
                    self.xid_supported[str(i)] = True
                    self.xid_counts.setdefault(str(i), 0)
                except pynvml.NVMLError as e:
                    self.xid_supported[str(i)] = False
                    log.info("XID events unsupported on GPU %d: %s", i, e)
            if not any(self.xid_supported.values()):
                return
            index_by_handle = {}
            while not self.stop.is_set():
                try:
                    data = pynvml.nvmlEventSetWait_v2(evset, 5000)
                except pynvml.NVMLError_Timeout:
                    continue
                except pynvml.NVMLError as e:
                    self.err("nvml_xid", e)
                    time.sleep(5)
                    continue
                key = id(data.device)
                if key not in index_by_handle:
                    try:
                        index_by_handle[key] = str(pynvml.nvmlDeviceGetIndex(data.device))
                    except pynvml.NVMLError:
                        index_by_handle[key] = "unknown"
                gpu = index_by_handle[key]
                self.xid_counts[gpu] = self.xid_counts.get(gpu, 0) + 1
                self.xid_last[gpu] = int(data.eventData)
                log.error("XID critical error on GPU %s: xid=%s", gpu, data.eventData)
        except Exception as e:  # never let this thread kill the exporter
            self.err("nvml_xid", e)

    # ── GPU collection ──
    def collect_gpu(self, fams):
        info = GaugeMetricFamily("llmmon_gpu_info", "Static GPU identity (value is always 1).",
                                 labels=["gpu", "uuid", "name", "pci_bus_id", "driver_version",
                                         "compute_capability", "vbios"])
        g = {}

        def gauge(name, doc, labels=("gpu",)):
            if name not in g:
                g[name] = GaugeMetricFamily(name, doc, labels=list(labels))
            return g[name]

        c = {}

        def counter(name, doc, labels=("gpu",)):
            if name not in c:
                c[name] = CounterMetricFamily(name, doc, labels=list(labels))
            return c[name]

        supported = GaugeMetricFamily("llmmon_gpu_metric_supported",
                                      "1 if NVML returned this field on this GPU, 0 if not supported.",
                                      labels=["gpu", "metric"])

        driver = self._s(pynvml.nvmlSystemGetDriverVersion())
        count = pynvml.nvmlDeviceGetCount()
        gauge("llmmon_gpu_count", "Number of GPUs visible to this container.", labels=()).add_metric([], count)

        for i in range(count):
            gi = str(i)
            h = pynvml.nvmlDeviceGetHandleByIndex(i)

            def field(metric, fn, emit):
                """Read one NVML field; record support; never raise."""
                try:
                    val = fn()
                except pynvml.NVMLError_NotSupported:
                    supported.add_metric([gi, metric], 0)
                    return
                except pynvml.NVMLError as e:
                    self.err("nvml_field", f"gpu{gi} {metric}: {e}")
                    return
                supported.add_metric([gi, metric], 1)
                emit(val)

            name = self._s(pynvml.nvmlDeviceGetName(h))
            uuid = self._s(pynvml.nvmlDeviceGetUUID(h))
            try:
                pci = self._s(pynvml.nvmlDeviceGetPciInfo(h).busId)
            except pynvml.NVMLError:
                pci = ""
            try:
                major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(h)
                cc = f"{major}.{minor}"
            except pynvml.NVMLError:
                cc = ""
            try:
                vbios = self._s(pynvml.nvmlDeviceGetVbiosVersion(h))
            except pynvml.NVMLError:
                vbios = ""
            info.add_metric([gi, uuid, name, pci, driver, cc, vbios], 1)

            field("utilization", lambda: pynvml.nvmlDeviceGetUtilizationRates(h), lambda u: (
                gauge("llmmon_gpu_utilization_ratio", "GPU (SM) busy ratio 0-1.").add_metric([gi], u.gpu / 100.0),
                gauge("llmmon_gpu_memory_utilization_ratio", "Memory controller busy ratio 0-1.").add_metric([gi], u.memory / 100.0)))
            def mem_info():
                # v2 separates driver-reserved memory from "used" (matches nvidia-smi);
                # v1 folds reserved into used, overstating usage by ~450 MiB on R5xx.
                try:
                    return pynvml.nvmlDeviceGetMemoryInfo(h, version=pynvml.nvmlMemory_v2)
                except (TypeError, AttributeError, pynvml.NVMLError_FunctionNotFound,
                        pynvml.NVMLError_NotSupported):
                    return pynvml.nvmlDeviceGetMemoryInfo(h)

            def emit_mem(m):
                gauge("llmmon_gpu_memory_total_bytes", "Framebuffer total bytes.").add_metric([gi], m.total)
                gauge("llmmon_gpu_memory_used_bytes", "Framebuffer used by allocations (excl. driver reserve).").add_metric([gi], m.used)
                gauge("llmmon_gpu_memory_free_bytes", "Framebuffer free bytes.").add_metric([gi], m.free)
                if hasattr(m, "reserved"):
                    gauge("llmmon_gpu_memory_reserved_bytes", "Framebuffer reserved by the driver/firmware.").add_metric([gi], m.reserved)
            field("memory", mem_info, emit_mem)
            field("temperature", lambda: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
                  lambda t: gauge("llmmon_gpu_temperature_celsius", "GPU core temperature.").add_metric([gi], t))
            for tname, tcode in TEMP_THRESHOLDS.items():
                field(f"temperature_threshold_{tname}",
                      lambda tcode=tcode: pynvml.nvmlDeviceGetTemperatureThreshold(h, tcode),
                      lambda v, tname=tname: gauge("llmmon_gpu_temperature_threshold_celsius",
                                                   "Model-specific NVML temperature thresholds.",
                                                   labels=("gpu", "type")).add_metric([gi, tname], v))
            field("power_usage", lambda: pynvml.nvmlDeviceGetPowerUsage(h),
                  lambda p: gauge("llmmon_gpu_power_watts", "Power draw in watts.").add_metric([gi], p / 1000.0))
            field("power_limit", lambda: pynvml.nvmlDeviceGetEnforcedPowerLimit(h),
                  lambda p: gauge("llmmon_gpu_power_limit_watts", "Enforced power limit in watts.").add_metric([gi], p / 1000.0))
            field("energy", lambda: pynvml.nvmlDeviceGetTotalEnergyConsumption(h),
                  lambda e: counter("llmmon_gpu_energy_joules", "Energy since driver load (J).").add_metric([gi], e / 1000.0))
            for cname, ccode in (("graphics", pynvml.NVML_CLOCK_GRAPHICS), ("sm", pynvml.NVML_CLOCK_SM),
                                 ("memory", pynvml.NVML_CLOCK_MEM)):
                field(f"clock_{cname}", lambda ccode=ccode: pynvml.nvmlDeviceGetClockInfo(h, ccode),
                      lambda v, cname=cname: gauge("llmmon_gpu_clock_hz", "Current clock.",
                                                   labels=("gpu", "clock")).add_metric([gi, cname], v * 1e6))
                field(f"clock_max_{cname}", lambda ccode=ccode: pynvml.nvmlDeviceGetMaxClockInfo(h, ccode),
                      lambda v, cname=cname: gauge("llmmon_gpu_clock_max_hz", "Max clock.",
                                                   labels=("gpu", "clock")).add_metric([gi, cname], v * 1e6))
            field("pstate", lambda: pynvml.nvmlDeviceGetPerformanceState(h),
                  lambda v: gauge("llmmon_gpu_pstate", "Performance state (0=max … 15=min).").add_metric([gi], v))
            field("fan_speed", lambda: pynvml.nvmlDeviceGetFanSpeed(h),
                  lambda v: gauge("llmmon_gpu_fan_speed_ratio", "Fan speed ratio 0-1 (often 0/meaningless on rented hosts).").add_metric([gi], v / 100.0))

            def emit_reasons(mask):
                gauge("llmmon_gpu_clock_event_reasons_bitmask", "Raw NVML clock event reason bitmask.").add_metric([gi], mask)
                fam = gauge("llmmon_gpu_clock_event_active", "1 if the clock event (throttle) reason is asserted.",
                            labels=("gpu", "reason"))
                for rname, bit in CLOCK_EVENT_REASONS.items():
                    fam.add_metric([gi, rname], 1 if mask & bit else 0)
            field("clock_event_reasons", lambda: pynvml.nvmlDeviceGetCurrentClocksEventReasons(h)
                  if hasattr(pynvml, "nvmlDeviceGetCurrentClocksEventReasons")
                  else pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(h), emit_reasons)

            field("pcie_replay", lambda: pynvml.nvmlDeviceGetPcieReplayCounter(h),
                  lambda v: counter("llmmon_gpu_pcie_replay_errors", "PCIe replay counter.").add_metric([gi], v))
            field("pcie_link_gen", lambda: pynvml.nvmlDeviceGetCurrPcieLinkGeneration(h),
                  lambda v: gauge("llmmon_gpu_pcie_link_generation", "Current PCIe generation.").add_metric([gi], v))
            field("pcie_link_width", lambda: pynvml.nvmlDeviceGetCurrPcieLinkWidth(h),
                  lambda v: gauge("llmmon_gpu_pcie_link_width", "Current PCIe lane width.").add_metric([gi], v))

            def ecc(kind, code):
                return lambda: pynvml.nvmlDeviceGetTotalEccErrors(h, code, pynvml.NVML_AGGREGATE_ECC)
            field("ecc_corrected", ecc("corrected", pynvml.NVML_MEMORY_ERROR_TYPE_CORRECTED),
                  lambda v: counter("llmmon_gpu_ecc_errors", "Aggregate ECC errors.",
                                    labels=("gpu", "type")).add_metric([gi, "corrected"], v))
            field("ecc_uncorrected", ecc("uncorrected", pynvml.NVML_MEMORY_ERROR_TYPE_UNCORRECTED),
                  lambda v: counter("llmmon_gpu_ecc_errors", "Aggregate ECC errors.",
                                    labels=("gpu", "type")).add_metric([gi, "uncorrected"], v))
            field("compute_processes", lambda: pynvml.nvmlDeviceGetComputeRunningProcesses(h),
                  lambda v: gauge("llmmon_gpu_compute_processes", "Processes holding a CUDA context.").add_metric([gi], len(v)))

            if self.xid_supported.get(gi):
                supported.add_metric([gi, "xid_events"], 1)
                counter("llmmon_gpu_xid_errors", "Critical XID events seen since exporter start.").add_metric(
                    [gi], self.xid_counts.get(gi, 0))
                if gi in self.xid_last:
                    gauge("llmmon_gpu_last_xid", "Code of the most recent critical XID.").add_metric([gi], self.xid_last[gi])
            elif gi in self.xid_supported:
                supported.add_metric([gi, "xid_events"], 0)

        fams.append(info)
        fams.append(supported)
        fams.extend(g.values())
        fams.extend(c.values())

    # ── cgroup (this container): v2 unified or v1 per-controller hierarchies ──
    def collect_cgroup(self, fams):
        # Vast hosts differ: some mount the unified v2 hierarchy, others v1.
        v2 = os.path.exists(f"{CGROUP}/cgroup.controllers")
        fams.append(self._gauge("llmmon_container_cgroup_version", "cgroup version the container metrics come from.",
                                2 if v2 else 1))
        (self.collect_cgroup_v2 if v2 else self.collect_cgroup_v1)(fams)

    def collect_cgroup_v1(self, fams):
        cpu_dir = next((f"{CGROUP}/{d}" for d in ("cpu,cpuacct", "cpuacct", "cpu")
                        if os.path.exists(f"{CGROUP}/{d}/cpuacct.usage")), None)
        if cpu_dir is None:
            raise FileNotFoundError("no cgroup v1 cpuacct hierarchy")
        fams.append(self._counter("llmmon_container_cpu_usage_seconds", "Container CPU time (cgroup).",
                                  int(read_text(f"{cpu_dir}/cpuacct.usage").strip()) / 1e9))
        acct = read_kv(f"{cpu_dir}/cpuacct.stat")          # USER_HZ ticks
        fams.append(self._counter("llmmon_container_cpu_user_seconds", "Container user CPU time.",
                                  acct.get("user", 0) / CLK_TCK))
        fams.append(self._counter("llmmon_container_cpu_system_seconds", "Container system CPU time.",
                                  acct.get("system", 0) / CLK_TCK))
        cpu = read_kv(f"{cpu_dir}/cpu.stat")
        fams.append(self._counter("llmmon_container_cpu_periods", "CFS enforcement periods.", cpu.get("nr_periods", 0)))
        fams.append(self._counter("llmmon_container_cpu_throttled_periods", "CFS periods throttled.",
                                  cpu.get("nr_throttled", 0)))
        fams.append(self._counter("llmmon_container_cpu_throttled_seconds", "Time throttled.",
                                  cpu.get("throttled_time", 0) / 1e9))
        quota = int(read_text(f"{cpu_dir}/cpu.cfs_quota_us").strip())
        period = int(read_text(f"{cpu_dir}/cpu.cfs_period_us").strip())
        cores = quota / period if quota > 0 else float(len(os.sched_getaffinity(0)))
        fams.append(self._gauge("llmmon_container_cpu_quota_cores", "CPU cores the container may use.", cores))

        mem = f"{CGROUP}/memory"
        current = int(read_text(f"{mem}/memory.usage_in_bytes").strip())
        limit = int(read_text(f"{mem}/memory.limit_in_bytes").strip())
        host_total = read_kv(f"{PROC}/meminfo").get("MemTotal:", 0) * 1024
        if limit >= host_total or limit >= 2 ** 62:     # v1 "unlimited" is a huge sentinel
            limit = host_total
        stat = read_kv(f"{mem}/memory.stat")
        inactive_file = stat.get("total_inactive_file", stat.get("inactive_file", 0))
        fams.append(self._gauge("llmmon_container_memory_usage_bytes", "cgroup memory usage.", current))
        fams.append(self._gauge("llmmon_container_memory_limit_bytes", "cgroup memory limit (host total if unlimited).", limit))
        fams.append(self._gauge("llmmon_container_memory_working_set_bytes", "usage minus inactive file cache.",
                                max(0, current - inactive_file)))
        kinds = GaugeMetricFamily("llmmon_container_memory_bytes", "memory.stat breakdown.", labels=["kind"])
        for kind, key in (("anon", "total_rss"), ("file", "total_cache"), ("shmem", "total_shmem")):
            if key in stat:
                kinds.add_metric([kind], stat[key])
        try:
            kinds.add_metric(["kernel"], int(read_text(f"{mem}/memory.kmem.usage_in_bytes").strip()))
        except (FileNotFoundError, ValueError, OSError):
            pass
        fams.append(kinds)
        if "total_swap" in stat:
            fams.append(self._gauge("llmmon_container_swap_usage_bytes", "cgroup swap usage.", stat["total_swap"]))
        try:
            oom = read_kv(f"{mem}/memory.oom_control").get("oom_kill", 0)
        except FileNotFoundError:
            oom = 0
        fams.append(self._counter("llmmon_container_memory_oom_kills", "OOM kills in this cgroup.", oom))

        for name in ("blkio.throttle.io_service_bytes_recursive", "blkio.throttle.io_service_bytes"):
            try:
                text = read_text(f"{CGROUP}/blkio/{name}")
            except FileNotFoundError:
                continue
            io = CounterMetricFamily("llmmon_container_io_bytes", "cgroup blkio bytes.", labels=["device", "direction"])
            for line in text.splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[1] in ("Read", "Write"):
                    io.add_metric([parts[0], parts[1].lower()], int(parts[2]))
            fams.append(io)
            break
        # PSI is a cgroup v2 feature — intentionally absent on v1 hosts (panel shows No data).
        try:
            fams.append(self._gauge("llmmon_container_pids", "Tasks in the cgroup.",
                                    int(read_text(f"{CGROUP}/pids/pids.current").strip())))
        except FileNotFoundError:
            pass

    def collect_cgroup_v2(self, fams):
        cpu = read_kv(f"{CGROUP}/cpu.stat")
        fams.append(self._counter("llmmon_container_cpu_usage_seconds", "Container CPU time (cgroup).",
                                  cpu.get("usage_usec", 0) / 1e6))
        fams.append(self._counter("llmmon_container_cpu_user_seconds", "Container user CPU time.",
                                  cpu.get("user_usec", 0) / 1e6))
        fams.append(self._counter("llmmon_container_cpu_system_seconds", "Container system CPU time.",
                                  cpu.get("system_usec", 0) / 1e6))
        fams.append(self._counter("llmmon_container_cpu_periods", "CFS enforcement periods.", cpu.get("nr_periods", 0)))
        fams.append(self._counter("llmmon_container_cpu_throttled_periods", "CFS periods throttled.",
                                  cpu.get("nr_throttled", 0)))
        fams.append(self._counter("llmmon_container_cpu_throttled_seconds", "Time throttled.",
                                  cpu.get("throttled_usec", 0) / 1e6))
        try:
            quota, period = (read_text(f"{CGROUP}/cpu.max").split() + ["100000"])[:2]
        except FileNotFoundError:   # root cgroup has no cpu.max → unlimited
            quota, period = "max", "100000"
        if quota == "max":
            cores = float(len(os.sched_getaffinity(0)))
        else:
            cores = int(quota) / int(period)
        fams.append(self._gauge("llmmon_container_cpu_quota_cores", "CPU cores the container may use.", cores))

        current = int(read_text(f"{CGROUP}/memory.current").strip())
        try:
            mmax = read_text(f"{CGROUP}/memory.max").strip()
        except FileNotFoundError:   # root cgroup → unlimited
            mmax = "max"
        limit = int(mmax) if mmax != "max" else read_kv(f"{PROC}/meminfo").get("MemTotal:", 0) * 1024
        stat = read_kv(f"{CGROUP}/memory.stat")
        working_set = max(0, current - stat.get("inactive_file", 0))
        fams.append(self._gauge("llmmon_container_memory_usage_bytes", "cgroup memory.current.", current))
        fams.append(self._gauge("llmmon_container_memory_limit_bytes", "cgroup memory.max (host total if unlimited).", limit))
        fams.append(self._gauge("llmmon_container_memory_working_set_bytes", "usage minus inactive file cache.", working_set))
        kinds = GaugeMetricFamily("llmmon_container_memory_bytes", "memory.stat breakdown.", labels=["kind"])
        for kind in ("anon", "file", "kernel", "shmem", "sock"):
            if kind in stat:
                kinds.add_metric([kind], stat[kind])
        fams.append(kinds)
        try:
            fams.append(self._gauge("llmmon_container_swap_usage_bytes", "cgroup memory.swap.current.",
                                    int(read_text(f"{CGROUP}/memory.swap.current").strip())))
        except FileNotFoundError:
            pass
        try:
            events = read_kv(f"{CGROUP}/memory.events")
        except FileNotFoundError:
            events = {}
        fams.append(self._counter("llmmon_container_memory_oom_kills", "OOM kills in this cgroup.", events.get("oom_kill", 0)))

        try:
            io = CounterMetricFamily("llmmon_container_io_bytes", "cgroup io.stat bytes.", labels=["device", "direction"])
            for line in read_text(f"{CGROUP}/io.stat").splitlines():
                parts = line.split()
                kv = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
                io.add_metric([parts[0], "read"], int(kv.get("rbytes", 0)))
                io.add_metric([parts[0], "write"], int(kv.get("wbytes", 0)))
            fams.append(io)
        except FileNotFoundError:
            pass

        psi = GaugeMetricFamily("llmmon_container_pressure_some_ratio", "cgroup PSI 'some' averages.",
                                labels=["resource", "window"])
        for res in ("cpu", "memory", "io"):
            try:
                for line in read_text(f"{CGROUP}/{res}.pressure").splitlines():
                    if line.startswith("some"):
                        kv = dict(p.split("=", 1) for p in line.split()[1:])
                        for w in ("avg10", "avg60", "avg300"):
                            psi.add_metric([res, w], float(kv[w]) / 100.0)
            except (FileNotFoundError, KeyError):
                pass
        fams.append(psi)
        try:
            fams.append(self._gauge("llmmon_container_pids", "Tasks in the cgroup.",
                                    int(read_text(f"{CGROUP}/pids.current").strip())))
        except FileNotFoundError:
            pass

    # ── /proc: container start, watched processes, shared-host context ──
    def collect_proc(self, fams):
        btime = read_kv(f"{PROC}/stat").get("btime", 0)
        start_ticks = int(read_text(f"{PROC}/1/stat").rsplit(")", 1)[1].split()[19])
        fams.append(self._gauge("llmmon_container_start_time_seconds", "Container PID 1 start (unix time).",
                                btime + start_ticks / CLK_TCK))

        rss = {p: 0 for p in PROCESS_ALLOWLIST}
        cpu = {p: 0.0 for p in PROCESS_ALLOWLIST}
        cnt = {p: 0 for p in PROCESS_ALLOWLIST}
        page = os.sysconf("SC_PAGE_SIZE")
        for pid in os.listdir(PROC):
            if not pid.isdigit():
                continue
            try:
                comm = read_text(f"{PROC}/{pid}/comm").strip()
                if comm not in rss:
                    continue
                fields = read_text(f"{PROC}/{pid}/stat").rsplit(")", 1)[1].split()
                cpu[comm] += (int(fields[11]) + int(fields[12])) / CLK_TCK
                rss[comm] += int(read_text(f"{PROC}/{pid}/statm").split()[1]) * page
                cnt[comm] += 1
            except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
                continue  # process exited mid-read
        g_rss = GaugeMetricFamily("llmmon_process_resident_memory_bytes", "RSS of allow-listed processes.", labels=["process"])
        g_cnt = GaugeMetricFamily("llmmon_process_count", "Running allow-listed processes.", labels=["process"])
        c_cpu = CounterMetricFamily("llmmon_process_cpu_seconds", "CPU of allow-listed processes (resets when PIDs exit).",
                                    labels=["process"])
        for p in PROCESS_ALLOWLIST:
            g_rss.add_metric([p], rss[p])
            g_cnt.add_metric([p], cnt[p])
            c_cpu.add_metric([p], cpu[p])
        fams.extend([g_rss, g_cnt, c_cpu])

        # Physical host — shared with other tenants. Context only.
        l1, l5, l15 = read_text(f"{PROC}/loadavg").split()[:3]
        load = GaugeMetricFamily("llmmon_physical_host_load", "SHARED physical host load average (all tenants).",
                                 labels=["window"])
        for w, v in (("1m", l1), ("5m", l5), ("15m", l15)):
            load.add_metric([w], float(v))
        fams.append(load)
        fams.append(self._gauge("llmmon_physical_host_cpu_count", "SHARED physical host CPU count.", os.cpu_count() or 0))
        mi = read_kv(f"{PROC}/meminfo")
        fams.append(self._gauge("llmmon_physical_host_memory_total_bytes", "SHARED physical host RAM.", mi.get("MemTotal:", 0) * 1024))
        fams.append(self._gauge("llmmon_physical_host_memory_available_bytes", "SHARED physical host available RAM.",
                                mi.get("MemAvailable:", 0) * 1024))
        fams.append(self._gauge("llmmon_physical_host_boot_time_seconds", "SHARED physical host boot time.", btime))

    @staticmethod
    def _gauge(name, doc, value):
        f = GaugeMetricFamily(name, doc)
        f.add_metric([], value)
        return f

    @staticmethod
    def _counter(name, doc, value):
        f = CounterMetricFamily(name, doc)
        f.add_metric([], value)
        return f

    # ── poll loop ──
    def poll_once(self):
        t0 = time.monotonic()
        fams = []
        nvml_ok = False
        try:
            self.nvml_init()
            self.collect_gpu(fams)
            nvml_ok = True
        except Exception as e:
            self.err("nvml", e)
            if self.nvml_initialized and pynvml is not None:
                try:  # force a clean re-init on the next cycle
                    pynvml.nvmlShutdown()
                except Exception:
                    pass
                self.nvml_initialized = False
        for source, fn in (("cgroup", self.collect_cgroup), ("procfs", self.collect_proc)):
            try:
                fams_src = []
                fn(fams_src)
                fams.extend(fams_src)
            except Exception as e:
                self.err(source, e)
        # Stamp COUNTERS with the time they were actually read. Polling is
        # decoupled from scraping, so without this a scrape can return a value
        # up to one poll interval old and rate() over short windows aliases
        # (e.g. container CPU at 124 % of quota). Gauges stay unstamped so
        # Prometheus staleness handling still marks them stale on outage.
        read_ms = int(time.time() * 1000)
        for fam in fams:
            if fam.type == "counter":
                fam.samples = [s._replace(timestamp=read_ms / 1000.0) for s in fam.samples]
        snap = Snapshot()
        snap.families = fams
        snap.nvml_ok = nvml_ok
        snap.ok = nvml_ok
        snap.duration = time.monotonic() - t0
        snap.last_success = time.time() if nvml_ok else self.snapshot.last_success
        with self.lock:
            self.snapshot = snap

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as e:  # belt and braces — the loop must survive
                self.err("poll", e)
            self.stop.wait(max(1.0, POLL_INTERVAL - (time.monotonic() - started)))

    # ── prometheus_client custom collector ──
    def collect(self):
        with self.lock:
            snap = self.snapshot
        yield from snap.families
        yield self._gauge("llmmon_exporter_up", "1 if the last poll read the GPU successfully.", 1 if snap.ok else 0)
        yield self._gauge("llmmon_nvml_up", "1 if NVML is initialised and answering.", 1 if snap.nvml_ok else 0)
        yield self._gauge("llmmon_exporter_last_success_timestamp_seconds", "Unix time of the last successful GPU poll.",
                          snap.last_success)
        yield self._gauge("llmmon_exporter_collect_duration_seconds", "Duration of the last poll.", snap.duration)
        yield self._gauge("llmmon_exporter_poll_interval_seconds", "Configured poll interval.", POLL_INTERVAL)
        errs = CounterMetricFamily("llmmon_exporter_collect_errors", "Collection errors by source.", labels=["source"])
        for src in ("nvml", "nvml_field", "nvml_xid", "cgroup", "procfs", "poll"):
            errs.add_metric([src], self.errors.get(src, 0))
        yield errs
        info = GaugeMetricFamily("llmmon_agent_info", "Agent build info.", labels=["version", "instance_id"])
        info.add_metric([VERSION, INSTANCE_ID], 1)
        yield info


def main():
    logging.basicConfig(level=os.environ.get("LLMMON_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    exp = Exporter()
    exp.poll_once()                     # populate before serving
    REGISTRY.register(exp)
    start_http_server(LISTEN_PORT, addr=LISTEN_ADDR)
    log.info("llmmon-gpu-exporter %s listening on %s:%d (poll %.0fs, instance_id=%s)",
             VERSION, LISTEN_ADDR, LISTEN_PORT, POLL_INTERVAL, INSTANCE_ID)

    def shutdown(*_):
        exp.stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    exp.run()
    if exp.nvml_initialized:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
    log.info("stopped")


if __name__ == "__main__":
    main()
