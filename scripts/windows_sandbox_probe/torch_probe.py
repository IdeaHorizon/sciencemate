"""torch-level CUDA probe: import torch from PROBE_TORCHLIB, check cuda availability, run a GPU
matmul and compare with the CPU result. Prints one JSON line."""

import json
import os
import sys
import time

R = {}
t0 = time.perf_counter()
try:
    sys.path.insert(0, os.environ["PROBE_TORCHLIB"])
    import torch

    R.update(
        torch=torch.__version__, cuda_build=torch.version.cuda, available=torch.cuda.is_available()
    )
    if R["available"]:
        R.update(
            device=torch.cuda.get_device_name(0),
            capability=".".join(map(str, torch.cuda.get_device_capability(0))),
        )
        g = torch.Generator().manual_seed(0)
        a = torch.randn(2048, 2048, generator=g)
        b = torch.randn(2048, 2048, generator=g)
        ref = a @ b
        gpu = a.cuda() @ b.cuda()
        torch.cuda.synchronize()
        R["matmul_max_abs_diff"] = float((gpu.cpu() - ref).abs().max())
        R["matmul_close"] = bool(torch.allclose(gpu.cpu(), ref, rtol=1e-3, atol=1e-2))
        x = torch.arange(1 << 20, device="cuda", dtype=torch.float32)
        R["elementwise_ok"] = bool(
            ((x * 2 + 1).cpu() == (torch.arange(1 << 20, dtype=torch.float32) * 2 + 1)).all()
        )
        R["mem_allocated_mb"] = round(torch.cuda.max_memory_allocated() / 2**20, 1)
    R["ok"] = True
except BaseException as exc:  # noqa: BLE001
    R["ok"] = False
    R["error"] = f"{type(exc).__name__}: {exc}"[:600]
R["seconds"] = round(time.perf_counter() - t0, 2)
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import payload_py

    R["token"] = payload_py.token_facts()
except BaseException as exc:  # noqa: BLE001
    R["token"] = {"error": f"{type(exc).__name__}: {exc}"}
sys.stdout.write(json.dumps(R) + "\n")
