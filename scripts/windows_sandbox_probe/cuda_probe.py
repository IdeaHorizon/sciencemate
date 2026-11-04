"""CUDA capability probe: driver API only (nvcuda.dll), no torch. Initialises the
driver, JIT-compiles a vector-add PTX kernel, runs it on 1M floats and checks the
result. Prints one JSON line."""

import ctypes
import json
import os
import subprocess
import sys
import time
from array import array

PTX = (
    rb"""
.version 7.0
.target sm_50
.address_size 64
.visible .entry vadd(.param .u64 pa, .param .u64 pb, .param .u64 pc, .param .u32 pn)
{
  .reg .pred p;
  .reg .b32 r<5>;
  .reg .f32 f<4>;
  .reg .b64 rd<11>;
  ld.param.u64 rd1, [pa];
  ld.param.u64 rd2, [pb];
  ld.param.u64 rd3, [pc];
  ld.param.u32 r2, [pn];
  mov.u32 r3, %ctaid.x;
  mov.u32 r4, %ntid.x;
  mov.u32 r1, %tid.x;
  mad.lo.s32 r1, r3, r4, r1;
  setp.ge.u32 p, r1, r2;
  @p bra DONE;
  cvta.to.global.u64 rd4, rd1;
  cvta.to.global.u64 rd5, rd2;
  cvta.to.global.u64 rd6, rd3;
  mul.wide.u32 rd7, r1, 4;
  add.s64 rd8, rd4, rd7;
  add.s64 rd9, rd5, rd7;
  add.s64 rd10, rd6, rd7;
  ld.global.f32 f1, [rd8];
  ld.global.f32 f2, [rd9];
  add.f32 f3, f1, f2;
  st.global.f32 [rd10], f3;
DONE:
  ret;
}
"""
    + b"\0"
)

R = {"steps": []}


def main():
    t0 = time.perf_counter()
    cu = ctypes.WinDLL("nvcuda.dll")
    U64, V = ctypes.c_uint64, ctypes.c_void_p

    def check(name, rc):
        R["steps"].append(name)
        if rc != 0:
            s = ctypes.c_char_p()
            cu.cuGetErrorName(rc, ctypes.byref(s))
            raise RuntimeError(f"{name} -> {rc} {s.value.decode() if s.value else ''}")

    check("cuInit", cu.cuInit(0))
    ver = ctypes.c_int()
    check("cuDriverGetVersion", cu.cuDriverGetVersion(ctypes.byref(ver)))
    n = ctypes.c_int()
    check("cuDeviceGetCount", cu.cuDeviceGetCount(ctypes.byref(n)))
    dev = ctypes.c_int()
    check("cuDeviceGet", cu.cuDeviceGet(ctypes.byref(dev), 0))
    name = ctypes.create_string_buffer(128)
    check("cuDeviceGetName", cu.cuDeviceGetName(name, 128, dev))
    major, minor = ctypes.c_int(), ctypes.c_int()
    cu.cuDeviceGetAttribute(ctypes.byref(major), 75, dev)
    cu.cuDeviceGetAttribute(ctypes.byref(minor), 76, dev)
    R.update(
        driver=ver.value,
        devices=n.value,
        name=name.value.decode(),
        cc=f"{major.value}.{minor.value}",
    )
    ctx = V()
    check("cuDevicePrimaryCtxRetain", cu.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev))
    check("cuCtxSetCurrent", cu.cuCtxSetCurrent(ctx))
    mod, fn = V(), V()
    cu.cuModuleLoadData.argtypes = [ctypes.POINTER(V), ctypes.c_char_p]
    check("cuModuleLoadData(PTX JIT)", cu.cuModuleLoadData(ctypes.byref(mod), PTX))
    cu.cuModuleGetFunction.argtypes = [ctypes.POINTER(V), V, ctypes.c_char_p]
    check("cuModuleGetFunction", cu.cuModuleGetFunction(ctypes.byref(fn), mod, b"vadd"))
    N = 1 << 20
    size = N * 4
    a = array("f", (i * 0.5 for i in range(N)))
    b = array("f", [2.0]) * N
    c = array("f", [0.0]) * N
    da, db, dc = U64(), U64(), U64()
    cu.cuMemAlloc_v2.argtypes = [ctypes.POINTER(U64), ctypes.c_size_t]
    cu.cuMemcpyHtoD_v2.argtypes = [U64, V, ctypes.c_size_t]
    cu.cuMemcpyDtoH_v2.argtypes = [V, U64, ctypes.c_size_t]
    cu.cuMemFree_v2.argtypes = [U64]
    for d in (da, db, dc):
        check("cuMemAlloc", cu.cuMemAlloc_v2(ctypes.byref(d), size))
    check("cuMemcpyHtoD a", cu.cuMemcpyHtoD_v2(da, a.buffer_info()[0], size))
    check("cuMemcpyHtoD b", cu.cuMemcpyHtoD_v2(db, b.buffer_info()[0], size))
    pn = ctypes.c_uint32(N)
    args = (V * 4)(
        ctypes.addressof(da), ctypes.addressof(db), ctypes.addressof(dc), ctypes.addressof(pn)
    )
    cu.cuLaunchKernel.argtypes = [V] + [ctypes.c_uint] * 6 + [ctypes.c_uint, V, V, V]
    check(
        "cuLaunchKernel",
        cu.cuLaunchKernel(fn, (N + 255) // 256, 1, 1, 256, 1, 1, 0, None, args, None),
    )
    check("cuCtxSynchronize", cu.cuCtxSynchronize())
    check("cuMemcpyDtoH", cu.cuMemcpyDtoH_v2(c.buffer_info()[0], dc, size))
    bad = sum(1 for i in range(0, N, 997) if c[i] != a[i] + 2.0)
    R["checked"] = len(range(0, N, 997))
    R["mismatches"] = bad
    R["kernel_result_correct"] = bad == 0
    for d in (da, db, dc):
        cu.cuMemFree_v2(d)
    cu.cuDevicePrimaryCtxRelease_v2(dev)
    R["seconds"] = round(time.perf_counter() - t0, 3)


try:
    main()
    R["ok"] = True
except BaseException as exc:  # noqa: BLE001
    R["ok"] = False
    R["error"] = f"{type(exc).__name__}: {exc}"
try:
    smi = subprocess.run(
        [os.path.join(os.environ["SystemRoot"], "System32", "nvidia-smi.exe"), "-L"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    R["nvidia_smi"] = {"rc": smi.returncode, "out": (smi.stdout or smi.stderr).strip()[:160]}
except BaseException as exc:  # noqa: BLE001
    R["nvidia_smi"] = {"error": f"{type(exc).__name__}: {exc}"}
R["cuda_cache_path"] = os.environ.get("CUDA_CACHE_PATH")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import payload_py

    R["token"] = payload_py.token_facts()
except BaseException as exc:  # noqa: BLE001
    R["token"] = {"error": f"{type(exc).__name__}: {exc}"}
sys.stdout.write(json.dumps(R, ensure_ascii=False) + "\n")
