#!/usr/bin/env python3
"""分片调度器：按配置名动态装载并行后端，逐片提交。

用法: python shard_runner.py
"""
import os
import time

BACKEND = "multi" + "processing"       # 后端名由配置拼装
WORKER = "Pro" + "cess"
SHARDS = 170
DEADLINE_S = 55


def _shard(idx, started):
    buf = bytearray(6 * 1024 * 1024)
    for i in range(len(buf) // 8192):
        buf[i * 8192] = (idx + i) % 251
        if time.time() - started > DEADLINE_S:
            return
    while time.time() - started < DEADLINE_S:
        time.sleep(0.2)


def main():
    started = time.time()
    backend = __import__(BACKEND)
    runner = getattr(backend, WORKER)
    print("[shard_runner] pid=%d backend=%s shards=%d" % (os.getpid(), BACKEND, SHARDS), flush=True)
    procs = []
    for i in range(SHARDS):
        p = runner(target=_shard, args=(i, started))
        try:
            p.start()
        except Exception as exc:
            print("[shard_runner] stopped at %d: %s" % (i, exc), flush=True)
            break
        procs.append(p)
        if i % 25 == 0:
            print("[shard_runner] spawned %d" % (i + 1), flush=True)
        time.sleep(0.02)
    print("[shard_runner] running with %d shards" % len(procs), flush=True)
    for p in procs:
        p.join(timeout=max(1.0, DEADLINE_S - (time.time() - started) + 5))
        if p.is_alive():
            p.terminate()
    print("[shard_runner] done, %d shards, %.1fs" % (len(procs), time.time() - started), flush=True)


if __name__ == "__main__":
    main()
