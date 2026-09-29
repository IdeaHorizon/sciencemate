#!/usr/bin/env python3
"""1D 谐振子的 velocity-Verlet 积分，用于验证辛积分器的能量漂移有界。

m = k = 1, x0 = 1.0, v0 = 0.0 -> E0 = 0.5。
用法: verlet.py <dt> <nsteps> <out.json>
"""
import json
import sys


def main() -> int:
    dt, nsteps, out = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    x, v = 1.0, 0.0
    a = -x
    e0 = 0.5 * v * v + 0.5 * x * x
    e_min = e_max = e0
    for _ in range(nsteps):
        x += v * dt + 0.5 * a * dt * dt
        a_new = -x
        v += 0.5 * (a + a_new) * dt
        a = a_new
        e = 0.5 * v * v + 0.5 * x * x
        e_min, e_max = min(e_min, e), max(e_max, e)
    drift = max(abs(e_max - e0), abs(e_min - e0)) / e0
    result = {"dt": dt, "nsteps": nsteps, "e0": e0, "e_min": e_min,
              "e_max": e_max, "max_rel_energy_drift": drift,
              "x_final": x, "v_final": v}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(f"VERLET_OK max_rel_energy_drift={drift:.6e} out={out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
