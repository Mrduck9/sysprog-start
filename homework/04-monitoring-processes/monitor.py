#!/usr/bin/env python3
"""Монитор процессов — задание «04 — Мониторинг процессов».

Раз в 1,5 секунды записывает время, PID, имя/командную строку, CPU и RSS,
отмечает превышения порогов. Завершившийся процесс и его перезапуск
(новый PID с той же командой) учитываются раздельно.

Запуск:
    python monitor.py                 # прогон по telemetry_sample.csv
    python monitor.py --live          # живые процессы run_workers.py
    python monitor.py --live --rss-threshold 30

Результат: строки в консоли и журнал monitor_log.csv.
"""

import argparse
import csv
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
MIB = 1024.0 * 1024.0


def _mb(value):
    """МБ с одним знаком или «—», если значение неизвестно."""
    return "—" if value is None else f"{value:.1f}"


def _pct(value):
    return "—" if value is None else f"{value:.2f}"


# ---------------------------------------------------------------------------
# Статистика процесса: Windows — kernel32/psapi, остальные ОС — ps(1)
# ---------------------------------------------------------------------------
if os.name == "nt":
    from ctypes import wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class _FILETIME(ctypes.Structure):
        _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

    class _PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.GetProcessTimes.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _k32.GetProcessTimes.restype = wt.BOOL
    _k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _k32.GetExitCodeProcess.restype = wt.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.LPWSTR,
        ctypes.POINTER(wt.DWORD),
    ]
    _k32.QueryFullProcessImageNameW.restype = wt.BOOL
    _psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
    _psapi.GetProcessMemoryInfo.restype = wt.BOOL

    def _filetime_s(value):
        return ((value.hi << 32) | value.lo) / 10_000_000.0

    def sample(pid):
        """RSS (МБ), суммарное CPU-время (с) и имя процесса, либо None."""
        handle = _k32.OpenProcess(0x1000 | 0x0400 | 0x0010, False, int(pid))
        if not handle:
            return None
        try:
            # Завершённый процесс ещё может открываться по PID, пока живы
            # чужие хэндлы, поэтому проверяем код выхода (259 = STILL_ACTIVE).
            exit_code = wt.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:
                return None
            created, exited = _FILETIME(), _FILETIME()
            kernel, user = _FILETIME(), _FILETIME()
            if not _k32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(pmc)
            if not _psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return None
            rss = pmc.WorkingSetSize / MIB
            name = ""
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(len(buf))
            if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            return {
                "name": name,
                "rss_mb": rss,
                "cpu_time": _filetime_s(kernel) + _filetime_s(user),
            }
        finally:
            _k32.CloseHandle(handle)

else:

    def _ps_time(value):
        """'MM:SS', 'HH:MM:SS' или 'DD-HH:MM:SS' -> секунды."""
        try:
            days = 0
            if "-" in value:
                day_part, value = value.split("-", 1)
                days = int(day_part)
            chunks = [int(chunk) for chunk in value.split(":")]
        except (TypeError, ValueError):
            return None
        seconds = 0.0
        for chunk in chunks:
            seconds = seconds * 60 + chunk
        return days * 86400.0 + seconds

    def sample(pid):
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=,time=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        parts = out.split(None, 2)
        if len(parts) < 2:
            return None
        return {
            "name": os.path.basename(parts[2]) if len(parts) > 2 else "",
            "rss_mb": int(parts[0]) / 1024.0,
            "cpu_time": _ps_time(parts[1]),
        }


# ---------------------------------------------------------------------------
# Монитор: учёт процессов, перезапусков и порогов
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, rss_threshold, cpu_threshold, log_path):
        self.rss_threshold = rss_threshold
        self.cpu_threshold = cpu_threshold
        self.log = open(log_path, "w", newline="", encoding="utf-8-sig")
        self.writer = csv.writer(self.log, lineterminator="\n")
        self.writer.writerow(
            [
                "time",
                "pid",
                "name",
                "cmdline",
                "cpu_percent",
                "rss_mb",
                "rss_step_mb",
                "rss_exceed",
                "cpu_exceed",
                "note",
            ]
        )
        self.procs = {}  # pid -> состояние процесса
        self.exited = []  # завершённые PID в порядке завершения
        self.restarts = []  # (старый PID, новый PID)

    def update(self, ts, samples):
        """samples: список dict(pid, name, cmdline, rss_mb, cpu_percent|cpu_time)."""
        present = {sample["pid"]: sample for sample in samples}

        # 1. Процессы, которые исчезли: фиксируем отдельно от новых.
        for pid in list(self.procs):
            proc = self.procs[pid]
            if not proc["alive"] or pid in present:
                continue
            proc["alive"] = False
            proc["exit_ts"] = ts
            self.exited.append(pid)
            print(
                f"  * завершён: PID {pid} {proc['cmdline']} "
                f"(жил {ts - proc['first_ts']:.1f} с, пик RSS {proc['max_rss']:.1f} МБ)"
            )

        # 2. Новые и текущие процессы.
        for pid, sample_data in present.items():
            proc = self.procs.get(pid)
            if proc is None:
                proc = {
                    "pid": pid,
                    "cmdline": sample_data["cmdline"],
                    "name": sample_data["name"],
                    "first_ts": ts,
                    "first_rss": sample_data["rss_mb"],
                    "prev_rss": None,
                    "max_rss": sample_data["rss_mb"] or 0.0,
                    "samples": 0,
                    "rss_exceed": 0,
                    "cpu_exceed": 0,
                    "cpu_time": None,
                    "cpu_ts": None,
                    "alive": True,
                    "restart_of": self._match_restart(sample_data["cmdline"]),
                }
                self.procs[pid] = proc
                if proc["restart_of"] is not None:
                    self.restarts.append((proc["restart_of"], pid))
                    print(
                        f"  * перезапуск: PID {proc['restart_of']} -> {pid} "
                        f"({proc['cmdline']}, та же команда)"
                    )
                else:
                    print(f"  * запущен: PID {pid} ({proc['cmdline']})")
            else:
                proc["alive"] = True

            # CPU: из CSV приходит готовый процент, в live считаем по дельте
            # суммарного CPU-времени.
            cpu_percent = sample_data.get("cpu_percent")
            if cpu_percent is None and sample_data.get("cpu_time") is not None:
                if proc["cpu_time"] is not None and proc["cpu_ts"] is not None:
                    delta_t = ts - proc["cpu_ts"]
                    delta_cpu = sample_data["cpu_time"] - proc["cpu_time"]
                    if delta_t > 0 and delta_cpu >= 0:
                        cpu_percent = 100.0 * delta_cpu / delta_t
                proc["cpu_time"] = sample_data["cpu_time"]
                proc["cpu_ts"] = ts

            rss = sample_data["rss_mb"]
            step = None if (rss is None or proc["prev_rss"] is None) else rss - proc["prev_rss"]
            proc["prev_rss"] = rss
            proc["samples"] += 1
            if rss is not None:
                proc["max_rss"] = max(proc["max_rss"], rss)

            rss_bad = rss is not None and rss > self.rss_threshold
            cpu_bad = cpu_percent is not None and cpu_percent > self.cpu_threshold
            if rss_bad:
                proc["rss_exceed"] += 1
            if cpu_bad:
                proc["cpu_exceed"] += 1

            notes = []
            if rss_bad:
                notes.append("ПОРОГ RSS")
            if cpu_bad:
                notes.append("ПОРОГ CPU")
            note = ";".join(notes)

            self.writer.writerow(
                [
                    datetime.fromtimestamp(ts).isoformat(timespec="seconds"),
                    pid,
                    proc["name"],
                    proc["cmdline"],
                    "" if cpu_percent is None else f"{cpu_percent:.1f}",
                    "" if rss is None else f"{rss:.2f}",
                    "" if step is None else f"{step:+.2f}",
                    "1" if rss_bad else "0",
                    "1" if cpu_bad else "0",
                    note,
                ]
            )

            print(
                f"{datetime.fromtimestamp(ts).strftime('%H:%M:%S')}  "
                f"pid={pid:<6} {proc['cmdline']:<38} "
                f"RSS={_mb(rss):>7} МБ  "
                f"CPU={_pct(cpu_percent):>6} %  "
                f"Δ={_mb(step):>6} МБ"
                + (f"  << {note}" if note else "")
            )

    def _match_restart(self, cmdline):
        """Новый PID с той же командной строкой, что у завершившегося."""
        normalized = " ".join(cmdline.split())
        for pid in reversed(self.exited):
            old = self.procs[pid]
            if old.get("restarted_by") is None and " ".join(old["cmdline"].split()) == normalized:
                old["restarted_by"] = True
                return pid
        return None

    def finish(self):
        self.log.close()
        total_rss = sum(proc["rss_exceed"] for proc in self.procs.values())
        total_cpu = sum(proc["cpu_exceed"] for proc in self.procs.values())
        print("=" * 100)
        print(
            f"ИТОГО: процессов {len(self.procs)}, перезапусков {len(self.restarts)}, "
            f"превышений RSS {total_rss}, CPU {total_cpu}"
        )
        for pid, proc in self.procs.items():
            status = "работает" if proc["alive"] else "завершён"
            if proc.get("restarted_by"):
                status += " (был перезапущен)"
            if proc.get("restart_of") is not None:
                status += f" (после PID {proc['restart_of']})"
            print(
                f"  PID {pid:<6} {proc['cmdline']:<38} "
                f"сэмплов {proc['samples']:<3} "
                f"RSS {_mb(proc['first_rss'])}→{_mb(proc['max_rss'])} МБ  "
                f"превыш. {proc['rss_exceed']}  {status}"
            )
        print("=" * 100)
        print(f"Журнал: {self.log.name}")


# ---------------------------------------------------------------------------
# Режим 1: прогон по telemetry_sample.csv
# ---------------------------------------------------------------------------
def run_csv(csv_path, monitor):
    groups = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            ts = datetime.fromisoformat(row["timestamp"].strip()).timestamp()
            groups.setdefault(ts, []).append(
                {
                    "pid": int(row["pid"]),
                    "name": row["name"].strip(),
                    "cmdline": f"worker.py --scenario {row['scenario'].strip()}",
                    "rss_mb": float(row["rss_mb"]),
                    "cpu_percent": float(row["cpu_percent"]),
                }
            )
    for ts in sorted(groups):
        monitor.update(ts, groups[ts])


# ---------------------------------------------------------------------------
# Режим 2: живые процессы run_workers.py
# ---------------------------------------------------------------------------
def run_live(monitor, interval):
    runner = subprocess.Popen(
        [sys.executable, str(BASE / "run_workers.py")],
        cwd=str(BASE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = queue.Queue()

    def read_output():
        for line in runner.stdout:
            lines.put(line)

    threading.Thread(target=read_output, daemon=True).start()

    # run_workers.py печатает "growth: PID=..." / "steady: PID=..."
    launched = {}  # pid -> командная строка
    pattern = re.compile(r"(growth|steady): PID=(\d+)")
    started = time.monotonic()
    tick = 0
    while True:
        while not lines.empty():
            match = pattern.match(lines.get().strip())
            if match:
                mode, pid = match.group(1), int(match.group(2))
                launched[pid] = f"worker.py --mode {mode} --seconds 60"

        ts = time.time()
        samples = []
        for pid in list(launched):
            info = sample(pid)
            if info is None:
                continue
            samples.append({**info, "pid": pid, "cmdline": launched[pid]})
        monitor.update(ts, samples)
        # завершившиеся PID больше не опрашиваем
        for pid in list(launched):
            proc = monitor.procs.get(pid)
            if proc is not None and not proc["alive"]:
                launched.pop(pid, None)

        if runner.poll() is not None:
            # Лабораторный прогон закончился: один финальный опрос, чтобы
            # зафиксировать завершения, и выход.
            time.sleep(0.2)
            final = []
            for pid in list(launched):
                info = sample(pid)
                if info is not None:
                    final.append({**info, "pid": pid, "cmdline": launched[pid]})
            monitor.update(time.time(), final)
            break

        tick += 1
        time.sleep(max(0.0, started + tick * interval - time.monotonic()))
    runner.wait(timeout=10)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="Монитор процессов: время, PID, имя/командная строка, CPU, RSS, пороги."
    )
    parser.add_argument("--live", action="store_true", help="наблюдать живые процессы run_workers.py")
    parser.add_argument("--csv", default=str(BASE / "telemetry_sample.csv"), help="CSV с замерами")
    parser.add_argument("--interval", type=float, default=1.5, help="период опроса, с")
    parser.add_argument("--rss-threshold", type=float, default=100.0, help="порог RSS, МБ")
    parser.add_argument("--cpu-threshold", type=float, default=50.0, help="порог CPU, %% одного ядра")
    parser.add_argument("--log", default=str(BASE / "monitor_log.csv"), help="журнал замеров")
    args = parser.parse_args()

    monitor = Monitor(args.rss_threshold, args.cpu_threshold, args.log)
    print(
        f"Монитор процессов | {'живые процессы' if args.live else 'telemetry_sample.csv'} | "
        f"пороги: RSS > {args.rss_threshold} МБ, CPU > {args.cpu_threshold} %"
    )
    if args.live:
        run_live(monitor, args.interval)
    else:
        run_csv(args.csv, monitor)
    monitor.finish()


if __name__ == "__main__":
    main()
