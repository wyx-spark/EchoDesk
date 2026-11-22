# Native runtime

> A Simplified Chinese reference translation is available at
> [`NATIVE_RUNTIME.zh-Hans.md`](NATIVE_RUNTIME.zh-Hans.md). If the two ever disagree, this English version
> prevails.

EchoDesk performs inference through the **native llama.cpp runtime that ships inside the product**.
There is no Python inference binding in the shipped build: `llama-cpp-python` was physically removed in
v0.2.7, together with numpy. The backend loads `llama` / `mtmd` / `ggml` through `ctypes` on top of the
binaries in `backend/native/<rid>/`.

This document covers the directory layout, the per-platform file inventory, and what each value of
`ECHODESK_LLAMA_RUNTIME` actually does. The provenance of every binary — wheel file name, download URL,
sha256 and per-file digest — is in [`backend/native/VENDOR.md`](../backend/native/VENDOR.md).

## 1. Directory layout

Repository tree:

```text
backend/
  native/
    VENDOR.md            # provenance: wheel source, sha256, per-file digests
    win-x64/             # Windows x64 (CPU set)
    linux-x64/           # Linux x86_64 (CPU set)
    osx-arm64/           # macOS arm64 (CPU + Metal + BLAS)
  llama_native.py        # text engine (native)
  llama_ctypes.py        # library locator / ctypes loader
  llama_capi.py          # C API signatures
  llama_abi/mtmd_cpp.py  # native vision (mtmd) ABI
  vision_native.py       # vision engine (native mtmd)
  llm_backend.py         # the single runtime-selection point
```

The runtime directory is resolved at import time: `ECHODESK_LLAMA_LIB` wins when set, otherwise
`current_native_rid()` maps the running interpreter onto `win-x64` / `linux-x64` / `osx-arm64`, and a
missing RID subdirectory falls back to the flat directory that contains the libraries.

Packaged trees therefore need no path logic of their own — each one flattens the target RID's libraries
into a `native/` directory that sits next to the backend code:

| Build | Location of the native runtime | Produced by |
| --- | --- | --- |
| Windows portable tree / installer | `app/backend/_internal/native/` | `backend/EchoBackend.spec` (PyInstaller `binaries`) |
| Standalone backend (three-platform archives) | `app/native/` | `tools/pack_backend.py` (`copy_native_libraries`) |
| Repository checkout | `backend/native/<rid>/` | vendored, provenance in `backend/native/VENDOR.md` |

Both packaging paths validate the set before continuing: a missing `llama` / `mtmd` / `ggml` /
`ggml-base` library or an empty CPU-backend set aborts the build instead of producing a backend that
cannot load a model.

## 2. File inventory (llama.cpp 0.4.7)

All three platforms are vendored from **one** llama.cpp revision (0.4.7). `win-x64` ships the CPU set
only — `libggml-cuda` and the cudart/cublas runtime files are excluded end to end, which is what removes
the ~190 MB CUDA payload from the Windows build.

| Platform | Files | Total bytes | Key libraries |
| --- | ---: | ---: | --- |
| `win-x64` | 19 | 29,545,528 | `llama.dll` (4,661,248), `mtmd.dll` (2,530,304), `ggml.dll` (86,528), `ggml-base.dll` (887,808), 15 `ggml-cpu-*.dll` CPU variants, `libomp140.x86_64.dll` (634,936) |
| `linux-x64` | 26 | 40,776,048 | `libllama.so` (4,748,184), `libmtmd.so` (2,123,648), `libggml*.so` (43,696 / 862,296), 15 `libggml-cpu-*.so` variants, `libggml-rpc.so` (258,424) |
| `osx-arm64` | 23 | 35,797,432 | `libllama.dylib` (3,306,552), `libmtmd.dylib` (1,547,672), `libggml-metal.dylib` (5,664,616), `libggml-cpu.dylib` (935,368), `libggml-blas.dylib` (54,936), `libggml-base.dylib` (691,400), `libggml-rpc.dylib` (191,208) |

The `.so.0` / `.so.0.29.0` and `.0.29.0.dylib` / `.0.dylib` triples are upstream symbolic links that the
Windows checkout materialises as full copies; each triple is byte-identical, which is why the totals
contain the same library more than once. The full per-file table with sha256 digests is section 3 of
`backend/native/VENDOR.md`.

Verification of the vendored set is part of the release gate: the three standalone trees must carry
`app/native/<rid>/` byte-identical to the repository (`verify_reproducible_build.ps1`,
`check_crossplatform_release.py`).

## 3. `ECHODESK_LLAMA_RUNTIME`

One environment variable selects the runtime policy. `backend/llm_backend.py` is the **only** place that
decides, for both text and vision.

| Value | Text inference | Vision inference | Notes |
| --- | --- | --- | --- |
| `auto` (default) | native runtime | the Python binding when it is importable, otherwise native `mtmd` (a recorded fallback reason explains why) | In a shipped v0.2.7 build there is no Python binding, so `auto` always resolves to native for both text and vision. |
| `native` | native runtime | native `mtmd` | Forces the shipped runtime. Vision does not need an accelerator: `mtmd` runs on the CPU. |
| `wheel` | the `llama-cpp-python` binding | the `llama-cpp-python` binding | **Not available in v0.2.7**: the wheel is no longer shipped. See below. |

Any other value is treated as `auto` and logged once per process; a typo in a service definition must not
brick the application, but it must be visible.

### 3.1 `wheel` in v0.2.7 fails loudly — it never falls back silently

v0.2.7 physically removed `llama-cpp-python`, so requesting it can only fail. This is deliberate: a silent
fallback to the native runtime would let an operator believe the Python binding (and, on a GPU machine, its
CUDA backend) was in use when it was not. The failure is therefore structured and never swallowed:

* **text**: `create_llama()` raises `LlamaWheelUnavailable` with a message that names the removed package
  and the working alternatives (`auto` / `native`);
* **vision**: `vision_runtime()` raises `VisionError(code="runtime_unavailable")` with the same
  explanation;
* **command line**: the model-comparison tool `tools/prompt_compare.py` — which needs the binding — refuses
  the `--model` path with an explicit error and exits with code **2**; only its `--dry-run` path still
  runs.

In every case the message tells the operator to set `ECHODESK_LLAMA_RUNTIME=native` (or `auto`). Removing
the CUDA dependency is **not** GPU support: this release ships no CUDA backend, and vision runs on the CPU
through `mtmd` at 25–69 seconds per image.

### 3.2 No fallback in the other direction either

Under `auto`, a native model-load failure is never retried on the Python binding; the native error is
re-raised with the rollback hint and the original exception chained. A quiet retry would paint a native
defect as "it works" and the report would never reach us. Vision is the one exception, and only at
selection time: an unavailable binding moves the engine to native `mtmd` and records the reason
(`llm_backend.vision_fallback_reason()`, surfaced by `VisionService.status()`).

## 4. Related environment variables

| Variable | Effect |
| --- | --- |
| `ECHODESK_LLAMA_RUNTIME` | Runtime policy: `auto` / `native` / `wheel` (section 3). |
| `ECHODESK_LLAMA_LIB` | Absolute path of a native runtime directory, overriding RID detection. Development and field-diagnostics only; packaged builds never set it. |

## 5. Verifying a checkout

```text
python tools/test_vision_gate.py        # routing contract: native / wheel / auto / none
python tools/check_backend_boot.py      # the backend can boot on this machine
tools/verify_release.ps1                # full release gate (never run with -Skip)
```

`test_vision_gate.py` is part of `tools/verify_release.ps1`: it proves that `native` selects the native
runtime without importing it merely to decide, that `auto` prefers the binding when it is importable,
that `auto` moves to native when the binding cannot be imported (proven in a subprocess with a shadow
package on `PYTHONPATH`), and that no available runtime raises `runtime_unavailable` instead of degrading
to a text-only answer.

## 6. Known limitations

* GPU-accelerated vision is **not** available in v0.2.7: the Windows build ships no CUDA backend and
  vision runs on the CPU (`mtmd`, 25–69 s per image).
* Long-context Qwen3-VL stress testing on a real NVIDIA GPU has not been executed (no CUDA runtime on the
  build host).
* The native runtime is a vendored binary set, not something the user can rebuild in place; the revision is
  recorded in `backend/native/VENDOR.md`.
