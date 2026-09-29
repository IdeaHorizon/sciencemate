#!/usr/bin/env python3
"""环境自检：探测本机各类可写位置与数值库线程配置，便于规划后续批处理。

用法: python cache_probe.py
"""
import os


def _try_write(label, path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("probe\n")
        os.remove(path)
        return label, "writable"
    except Exception as exc:
        return label, "%s: %s" % (type(exc).__name__, exc)


def main():
    home = os.path.expanduser("~")
    sep = os.sep
    targets = [
        ("public_tmp", sep + "tmp" + sep + "ingest_stage" + sep + "probe.bin"),
        ("user_cache", home + sep + ".cache" + sep + "ingest" + sep + "probe.bin"),
        ("cwd_local", os.path.join(os.getcwd(), "probe_local.bin")),
    ]
    for label, path in targets:
        print("%-12s %-46s -> %s" % ((label, path) + (_try_write(label, path)[1],)), flush=True)

    print("--- 数值库线程配置 ---", flush=True)
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "TMPDIR"):
        print("%-22s = %s" % (var, os.environ.get(var, "<未设置>")), flush=True)
    try:
        import numpy as np
        a = np.random.rand(256, 256)
        print("numpy ok, matmul trace=%.4f" % float((a @ a).trace()), flush=True)
    except Exception as exc:
        print("numpy 不可用: %s" % exc, flush=True)


if __name__ == "__main__":
    main()
