"""
OS-level resource metrics probe: CPU, memory, disk, network, load average.
Supports Linux (reads /proc) and falls back to shell commands on macOS.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from typing import Any


_IS_LINUX = platform.system() == "Linux"


def _read_file(path: str) -> str:
    try:
        return open(path).read()
    except OSError:
        return ""


class OSProbe:
    """Collect OS-level resource metrics."""

    def collect(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        result["platform"] = platform.system()
        result["load_average"] = self._load_average()
        result["cpu_usage"] = self._cpu_usage()
        result["memory"] = self._memory()
        result["disk"] = self._disk()
        result["disk_io"] = self._disk_io() if _IS_LINUX else {}
        result["network"] = self._network_io() if _IS_LINUX else {}
        result["file_descriptors"] = self._fd_count()
        return result

    def _load_average(self) -> dict[str, float]:
        try:
            avg1, avg5, avg15 = os.getloadavg()
            return {"1m": round(avg1, 2), "5m": round(avg5, 2), "15m": round(avg15, 2)}
        except OSError:
            return {"1m": 0.0, "5m": 0.0, "15m": 0.0}

    def _cpu_usage(self) -> dict[str, Any]:
        if _IS_LINUX:
            return self._cpu_usage_linux()
        return self._cpu_usage_macos()

    def _cpu_usage_linux(self) -> dict[str, Any]:
        stat1 = _read_file("/proc/stat")
        m = re.search(r"cpu\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", stat1)
        if not m:
            return {}
        vals = [int(m.group(i)) for i in range(1, 8)]
        total1 = sum(vals)
        idle1 = vals[3]
        import time; time.sleep(0.5)
        stat2 = _read_file("/proc/stat")
        m2 = re.search(r"cpu\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", stat2)
        if not m2:
            return {}
        vals2 = [int(m2.group(i)) for i in range(1, 8)]
        total2 = sum(vals2)
        idle2 = vals2[3]
        delta_total = total2 - total1
        delta_idle = idle2 - idle1
        if delta_total == 0:
            return {"usage_ratio": 0.0}
        usage_ratio = (delta_total - delta_idle) / delta_total
        return {"usage_ratio": round(usage_ratio, 3), "idle_ratio": round(delta_idle / delta_total, 3)}

    def _cpu_usage_macos(self) -> dict[str, Any]:
        try:
            out = subprocess.check_output(["/bin/ps", "-A", "-o", "%cpu="], text=True, timeout=1)
            total_pct = 0.0
            for line in out.splitlines():
                try:
                    total_pct += float(line.strip())
                except ValueError:
                    continue
            cpu_count = max(1, os.cpu_count() or 1)
            usage_ratio = min(1.0, total_pct / (100.0 * cpu_count))
            return {"usage_ratio": round(usage_ratio, 3), "source": "ps"}
        except Exception:
            pass
        try:
            out = subprocess.check_output(["/usr/bin/top", "-l", "1", "-n", "0"], text=True, timeout=1)
            m = re.search(r"CPU usage: ([\d.]+)% user, ([\d.]+)% sys, ([\d.]+)% idle", out)
            if m:
                user = float(m.group(1))
                sys_ = float(m.group(2))
                idle = float(m.group(3))
                return {"user_pct": user, "sys_pct": sys_, "idle_pct": idle,
                        "usage_ratio": round((user + sys_) / 100.0, 3)}
        except Exception:
            pass
        return {}

    def _memory(self) -> dict[str, Any]:
        if _IS_LINUX:
            return self._memory_linux()
        return self._memory_macos()

    def _memory_linux(self) -> dict[str, Any]:
        meminfo = _read_file("/proc/meminfo")
        fields = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                fields[parts[0].rstrip(":")] = int(parts[1])
        total = fields.get("MemTotal", 0)
        available = fields.get("MemAvailable", fields.get("MemFree", 0))
        used = total - available
        return {
            "total_kb": total,
            "available_kb": available,
            "used_kb": used,
            "usage_ratio": round(used / total, 4) if total else 0.0,
        }

    def _memory_macos(self) -> dict[str, Any]:
        try:
            out = subprocess.check_output(["/usr/bin/vm_stat"], text=True, timeout=5)
            pagesize = 4096
            m = re.search(r"Pages active:\s+(\d+)", out)
            active = int(m.group(1)) * pagesize if m else 0
            m2 = re.search(r"Pages wired down:\s+(\d+)", out)
            wired = int(m2.group(1)) * pagesize if m2 else 0
            m3 = re.search(r"Pages free:\s+(\d+)", out)
            free = int(m3.group(1)) * pagesize if m3 else 0
            total = active + wired + free
            return {
                "total_kb": total // 1024,
                "active_kb": active // 1024,
                "wired_kb": wired // 1024,
                "free_kb": free // 1024,
                "usage_ratio": round((active + wired) / total, 4) if total else 0.0,
            }
        except Exception:
            return {}

    def _disk(self) -> dict[str, Any]:
        try:
            if _IS_LINUX:
                s = os.statvfs("/")
            else:
                s = os.statvfs("/")
            total = s.f_blocks * s.f_frsize
            free = s.f_bfree * s.f_frsize
            used = total - free
            return {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "usage_ratio": round(used / total, 4) if total else 0.0,
            }
        except Exception:
            return {}

    def _disk_io(self) -> dict[str, Any]:
        try:
            lines = _read_file("/proc/diskstats").splitlines()
            totals = {"sectors_read": 0, "sectors_written": 0, "io_time_ms": 0}
            for line in lines:
                parts = line.split()
                if len(parts) < 14:
                    continue
                # fields 3, 5, 9, 12 are sector counts and io_time
                try:
                    totals["sectors_read"] += int(parts[5])
                    totals["sectors_written"] += int(parts[9])
                except (ValueError, IndexError):
                    pass
            return totals
        except Exception:
            return {}

    def _network_io(self) -> dict[str, Any]:
        try:
            lines = _read_file("/proc/net/dev").splitlines()[2:]
            totals = {"rx_bytes": 0, "tx_bytes": 0}
            for line in lines:
                parts = line.split()
                if len(parts) < 10:
                    continue
                iface = parts[0].rstrip(":")
                if iface in ("lo",):
                    continue
                try:
                    totals["rx_bytes"] += int(parts[1])
                    totals["tx_bytes"] += int(parts[9])
                except (ValueError, IndexError):
                    pass
            return totals
        except Exception:
            return {}

    def _fd_count(self) -> dict[str, int]:
        try:
            count = len(os.listdir("/proc/self/fd"))
            return {"open_fds": count}
        except OSError:
            return {"open_fds": -1}
