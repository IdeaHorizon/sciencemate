#!/usr/bin/env python3
"""长时数据摄取：主进程调度，工作进程持续写入分片日志。

用法: python long_ingest.py <输出目录>
"""
import multiprocessing as mp
import os
import sys
import time

WORKERS = 6
RUN_S = 900          # 设计上跑很久；调用方通常会更早收工


def _worker(idx, outdir):
    path = os.path.join(outdir, "ingest_%d.log" % idx)
    started = time.time()
    with open(path, "a", buffering=1) as fh:
        while time.time() - started < RUN_S:
            fh.write("worker=%d t=%.1f\n" % (idx, time.time() - started))
            time.sleep(0.3)


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(outdir, exist_ok=True)
    print("[ingest] pid=%d workers=%d outdir=%s" % (os.getpid(), WORKERS, outdir), flush=True)
    procs = [mp.Process(target=_worker, args=(i, outdir)) for i in range(WORKERS)]
    for p in procs:
        p.start()
    print("[ingest] all workers up", flush=True)
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
