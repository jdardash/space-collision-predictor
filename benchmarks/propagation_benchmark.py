"""Benchmark: loop vs vectorized SGP4 propagation."""

import time
from datetime import datetime, timezone

from sda.models import TLERecord
from sda.propagator import propagate_window, propagate_window_numpy


ISS_TLE = TLERecord(
    norad_id=25544,
    name="ISS (ZARYA)",
    line1="1 25544U 98067A   24045.51782528  .00012516  00000+0  22596-3 0  9997",
    line2="2 25544  51.6412 210.9280 0004885 231.2372 247.0342 15.49584387440014",
    epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
)

CSS_TLE = TLERecord(
    norad_id=48274,
    name="CSS (TIANHE)",
    line1="1 48274U 21035A   24045.52083333  .00020000  00000+0  27000-3 0  9991",
    line2="2 48274  41.4700 100.0000 0007000 280.0000  80.0000 15.60000000100001",
    epoch=datetime(2024, 2, 14, tzinfo=timezone.utc),
)

DEMO_TLES = [ISS_TLE, CSS_TLE]

START = datetime(2024, 2, 15, 0, 0, 0, tzinfo=timezone.utc)


def bench(label: str, fn, *args, runs: int = 5):
    """Run a function multiple times and report timing."""
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        result = fn(*args)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    best = min(times)
    avg = sum(times) / len(times)
    return best, avg, result


def main():
    print("=" * 60)
    print("  Space-Domain Awareness — Propagation Benchmark")
    print("=" * 60)
    print()

    # 1. Single satellite, 24h window
    hours = 24.0
    step = 60.0
    n_steps = int(hours * 3600 / step) + 1

    print(f"ISS orbit propagation: {n_steps} steps (24h @ 60s)")
    print("-" * 50)

    best_loop, avg_loop, sv_list = bench(
        "Loop", propagate_window, ISS_TLE, START, hours, step
    )
    print(f"  Loop mode:       {avg_loop*1000:7.1f} ms avg  ({best_loop*1000:.1f} ms best)")

    best_vec, avg_vec, (pos, vel, times) = bench(
        "Vectorized", propagate_window_numpy, ISS_TLE, START, hours, step
    )
    print(f"  Vectorized mode: {avg_vec*1000:7.1f} ms avg  ({best_vec*1000:.1f} ms best)")

    speedup = avg_loop / avg_vec if avg_vec > 0 else float("inf")
    print(f"  Speedup:         {speedup:.1f}x")
    print(f"  Steps returned:  {len(sv_list)} (loop), {len(times)} (vec)")
    print()

    # 2. Fine-step propagation (1s steps, 10 min window)
    fine_hours = 10 / 60.0
    fine_step = 1.0
    fine_n = int(fine_hours * 3600 / fine_step) + 1

    print(f"Fine propagation: {fine_n} steps (10 min @ 1s)")
    print("-" * 50)

    best_fl, avg_fl, _ = bench("Loop", propagate_window, ISS_TLE, START, fine_hours, fine_step)
    best_fv, avg_fv, _ = bench("Vec", propagate_window_numpy, ISS_TLE, START, fine_hours, fine_step)
    print(f"  Loop mode:       {avg_fl*1000:7.1f} ms avg")
    print(f"  Vectorized mode: {avg_fv*1000:7.1f} ms avg")
    print(f"  Speedup:         {avg_fl/avg_fv:.1f}x")
    print()

    # 3. Multi-satellite screening
    n_sats = len(DEMO_TLES)
    n_pairs = n_sats * (n_sats - 1) // 2
    print(f"Screening {n_sats} satellites ({n_pairs} pairs) over 24h:")
    print("-" * 50)

    t0 = time.perf_counter()
    for tle in DEMO_TLES:
        propagate_window_numpy(tle, START, 24.0, 60.0)
    t1 = time.perf_counter()
    total = t1 - t0
    print(f"  Total propagation: {total*1000:.1f} ms")
    print(f"  Per satellite:     {total/n_sats*1000:.1f} ms")
    print()

    print("=" * 60)
    print("  Benchmark complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
