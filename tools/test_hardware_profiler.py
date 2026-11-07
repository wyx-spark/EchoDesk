# -*- coding: utf-8 -*-
"""
[EN]
Unit tests for ``backend/hardware_profiler.py``.

The development host carries an integrated GPU without dedicated video memory.
A test suite that only exercises the real probe would therefore leave the GPU
planning branch unverified. This module consequently builds synthetic hardware
profiles (8 GiB and 12 GiB of free VRAM among them) and asserts the resulting
``n_gpu_layers`` values directly, together with the silent CPU degradation path
and the dynamic context planning rules.

[ZH]
``backend/hardware_profiler.py`` 的单元测试。

开发机配备无独立显存的集成显卡。若测试仅覆盖真实探测，GPU 规划分支将完全
得不到验证。因此本模块构造合成的硬件档案（其中包含 8 GiB 与 12 GiB 空闲
显存），直接断言推导得到的 ``n_gpu_layers`` 取值，并同时覆盖静默降级为纯 CPU
的路径与动态上下文规划规则。

Run: ``python tools\\test_hardware_profiler.py``. Exit code 0 means all passed.
"""
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import hardware_profiler as hp  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    """[EN] Record one assertion / [ZH] 记录一条断言。"""
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def profile(gpu, physical=8, logical=16, ram_total=16 * hp._GIB, ram_available=8 * hp._GIB):
    """[EN] Build a synthetic profile / [ZH] 构造合成档案。"""
    return hp.HardwareProfile(
        cpu=hp.CpuProfile(physical_cores=physical, logical_cores=logical,
                          avx2=True, avx512=False),
        gpu=gpu,
        ram_total_bytes=ram_total,
        ram_available_bytes=ram_available,
    )


def gpu(backend, free_gib, total_gib=None, available=True):
    """[EN] Build a synthetic GPU / [ZH] 构造合成 GPU。"""
    total = int((total_gib if total_gib is not None else free_gib) * hp._GIB)
    return hp.GpuProfile(
        detected_backend=backend,
        available=available,
        name=f"{backend} synthetic",
        total_vram_bytes=total,
        free_vram_bytes=int(free_gib * hp._GIB),
    )


GIB = hp._GIB


def test_cpu_only_degrades_silently():
    print("=== 1. 无独显时必须强制纯 CPU，且不抛出异常 ===")
    cpu_only = profile(hp.GpuProfile("none", False, "", 0, 0, "no_supported_gpu"))
    layers = hp.compute_gpu_layers(cpu_only, model_layers=32, model_bytes=4 * GIB,
                                   kv_cache_bytes=GIB)
    check("无 GPU 时 n_gpu_layers=0", layers == 0, str(layers))
    plan = hp.plan_inference(profile=cpu_only, train_context=32768,
                             requested_context=32768, max_context=32768)
    check("无 GPU 时规划后端为 CPU", plan.backend == "CPU", plan.backend)
    check("无 GPU 时规划层数为 0", plan.n_gpu_layers == 0, str(plan.n_gpu_layers))
    check("CPU 路径仍给出初始上下文 2048", plan.n_ctx_initial == 2048,
          str(plan.n_ctx_initial))
    check("线程数取物理核心数", plan.n_threads == 8, str(plan.n_threads))


def test_eight_gib_vram():
    print("=== 2. 8 GiB 显存：7B Q4 模型应当全量卸载 ===")
    eight = profile(gpu("cuda", 8))
    # [EN] 32 layers, 4.2 GiB of weights, a 1 GiB KV cache at the reserved
    # [EN] ceiling and a 256 MiB compute buffer: 5.45 GiB < 6.4 GiB usable.
    # [ZH] 32 层、4.2 GiB 权重、保留上限下的 1 GiB KV Cache 与 256 MiB 计算缓冲：
    # [ZH] 合计 5.45 GiB，小于 6.4 GiB 可用显存。
    layers = hp.compute_gpu_layers(eight, model_layers=32,
                                   model_bytes=int(4.2 * GIB),
                                   kv_cache_bytes=GIB)
    check("8 GiB 显存 → n_gpu_layers=-1（全量卸载）", layers == -1, str(layers))
    decision, reason = hp._gpu_layer_decision(eight, 32, int(4.2 * GIB), GIB)
    check("全量卸载的判定理由正确", reason == "full_offload_fits_safety_watermark", reason)


def test_twelve_gib_vram():
    print("=== 3. 12 GiB 显存：13B Q4 模型应当全量卸载 ===")
    twelve = profile(gpu("cuda", 12))
    # [EN] 40 layers, 7.5 GiB of weights, a 1.2 GiB KV cache and the buffer fit
    # [EN] inside the 9.6 GiB usable watermark.
    # [ZH] 40 层、7.5 GiB 权重、1.2 GiB KV Cache 与计算缓冲均可容纳在 9.6 GiB
    # [ZH] 的可用水位线之内。
    layers = hp.compute_gpu_layers(twelve, model_layers=40,
                                   model_bytes=int(7.5 * GIB),
                                   kv_cache_bytes=int(1.2 * GIB))
    check("12 GiB 显存 → n_gpu_layers=-1（全量卸载）", layers == -1, str(layers))


def test_vram_watermark_prevents_full_offload():
    print("=== 4. 安全水位线：模型刚好超过 80% 可用显存时不得全量卸载 ===")
    eight = profile(gpu("cuda", 8))
    # [EN] 7 GiB weights + 0.5 GiB KV + 0.25 GiB buffer exceed the 6.4 GiB
    # [EN] usable watermark but fit inside the raw 8 GiB of free VRAM.
    # [ZH] 7 GiB 权重 + 0.5 GiB KV + 0.25 GiB 缓冲超过 6.4 GiB 可用水位线，
    # [ZH] 但可容纳在原始 8 GiB 空闲显存之内。
    layers = hp.compute_gpu_layers(eight, model_layers=32,
                                   model_bytes=7 * GIB,
                                   kv_cache_bytes=int(0.5 * GIB))
    check("超过水位线时不全量卸载", layers != -1, str(layers))
    check("超过水位线时给出部分卸载层数", 0 < layers < 32, str(layers))


def test_partial_offload_layer_count():
    print("=== 5. 4 GiB 显存：部分卸载层数必须为正整数且不超过总层数 ===")
    four = profile(gpu("cuda", 4))
    layers = hp.compute_gpu_layers(four, model_layers=32,
                                   model_bytes=int(4.2 * GIB),
                                   kv_cache_bytes=int(0.5 * GIB))
    # [EN] The exact value is asserted so a regression in the arithmetic cannot
    # [EN] hide behind a loose range check.
    # [ZH] 断言精确取值，使算术回归无法藏在宽松的区间判断之后。
    check("4 GiB 显存 → 部分卸载 18 层", layers == 18, str(layers))


def test_insufficient_vram_returns_zero():
    print("=== 6. 显存不足以容纳 KV 与缓冲时必须回落纯 CPU ===")
    two = profile(gpu("cuda", 2))
    layers = hp.compute_gpu_layers(two, model_layers=32,
                                   model_bytes=int(4.2 * GIB),
                                   kv_cache_bytes=int(2 * GIB))
    check("显存不足时 n_gpu_layers=0", layers == 0, str(layers))


def test_context_planner_and_reserve():
    print("=== 7. 动态上下文：初始 2048、按内存预算收敛、上限不超过训练窗口 ===")
    cpu_only = profile(hp.GpuProfile("none", False, "", 0, 0),
                       ram_total=16 * GIB, ram_available=4 * GIB)
    with mock.patch.object(hp, "_kv_bytes_per_token", return_value=131072.0), \
            mock.patch.object(hp, "_model_shape", return_value=(32, 0)):
        # [EN] Available 4 GiB -> 3.2 GiB after the 20% reserve, minus the
        # [EN] 256 MiB buffer -> 24166 tokens at 128 KiB per token, rounded down
        # [EN] to the 256-token granularity -> 24064.
        # [ZH] 可用 4 GiB 扣除 20% 水位线后为 3.2 GiB，再减去 256 MiB 缓冲，
        # [ZH] 按每 token 128 KiB 计算可得 24166 token，按 256 token 粒度向下
        # [ZH] 取整为 24064。
        limit = hp.safe_context_limit(cpu_only, model_path="synthetic.gguf",
                                      train_context=32768, requested_max=32768,
                                      ceiling=32768)
    check("内存预算收敛到 24064 tokens", limit == 24064, str(limit))

    check("初始上下文固定为 2048", hp.initial_context_for(32768) == 2048)
    check("上限小于 2048 时初始值随之收敛",
          hp.initial_context_for(1024) == 1024, str(hp.initial_context_for(1024)))
    check("训练窗口小于请求值时取训练窗口",
          hp.plan_inference(profile=cpu_only, train_context=4096,
                            requested_context=32768, max_context=32768).n_ctx_max == 4096)


def test_thread_count_is_dynamic():
    print("=== 8. 线程数按物理核心数动态计算，且尊重显式覆盖 ===")
    cpu_only = profile(hp.GpuProfile("none", False, "", 0, 0), physical=6, logical=12)
    auto = hp.plan_inference(profile=cpu_only, max_context=8192, requested_context=8192)
    check("自动线程数 = 物理核心数 6", auto.n_threads == 6, str(auto.n_threads))
    manual = hp.plan_inference(profile=cpu_only, max_context=8192, requested_context=8192,
                               n_threads_override=4)
    check("显式覆盖线程数 4 被采纳", manual.n_threads == 4, str(manual.n_threads))
    clamped = hp.plan_inference(profile=cpu_only, max_context=8192, requested_context=8192,
                                n_threads_override=99)
    check("线程数不超过逻辑核心数", clamped.n_threads == 12, str(clamped.n_threads))


def test_probe_failure_is_silent():
    print("=== 9. 探测内部异常必须静默降级为保守纯 CPU 配置 ===")
    real_impl = hp._probe_impl
    try:
        hp.reset_cache()
        # [EN] Suppress the intentional warning traceback: the assertion focuses
        # [EN] on the degraded return value, not on log output.
        # [ZH] 抑制刻意触发的告警回溯：断言关注降级后的返回值，而非日志输出。
        with mock.patch.object(hp.log, "warning"), \
                mock.patch.object(hp, "_probe_impl",
                                  side_effect=RuntimeError("synthetic failure")):
            captured = hp.probe(force=True)
        check("探测失败不抛出异常", isinstance(captured, hp.HardwareProfile))
        check("探测失败标记为 degraded", captured.degraded is True)
        check("探测失败时 GPU 不可用", captured.gpu.available is False)
        check("探测失败时仍给出物理核心数", captured.cpu.physical_cores >= 1,
              str(captured.cpu.physical_cores))
        check("探测失败时上下文规划仍可执行",
              hp.plan_inference(profile=captured, max_context=2048).n_ctx_initial == 2048)
    finally:
        hp._probe_impl = real_impl
        hp.reset_cache()


def test_real_probe_never_raises():
    print("=== 10. 当前开发机真实探测：识别集显环境并完美执行纯 CPU 路径 ===")
    hp.reset_cache()
    real = hp.probe(force=True)
    check("真实探测不抛出异常", isinstance(real, hp.HardwareProfile))
    check("物理核心数不超过逻辑核心数",
          real.cpu.physical_cores <= real.cpu.logical_cores,
          f"{real.cpu.physical_cores}/{real.cpu.logical_cores}")
    check("物理核心数为正", real.cpu.physical_cores >= 1, str(real.cpu.physical_cores))
    plan = hp.plan_inference(profile=real, train_context=32768,
                             requested_context=32768, max_context=32768)
    check("真实环境给出的初始上下文为 2048", plan.n_ctx_initial == 2048,
          str(plan.n_ctx_initial))
    if not real.gpu.available:
        check("无独显环境强制 n_gpu_layers=0", plan.n_gpu_layers == 0,
              str(plan.n_gpu_layers))
        check("无独显环境后端标记为 CPU", plan.backend == "CPU", plan.backend)
    else:
        check("检测到独立显卡时给出合法卸载层数", plan.n_gpu_layers == -1 or plan.n_gpu_layers >= 0,
              str(plan.n_gpu_layers))
    print(f"  实际档案：{real.summary()}")
    print(f"  实际规划：{plan.to_dict()}")

    # [EN] When a real GGUF model is present, assert the complete plan against
    # [EN] its actual metadata rather than against synthetic dimensions.
    # [ZH] 存在真实 GGUF 模型时，依据其实际元数据断言完整规划，而非合成维度。
    models = sorted(
        path for path in (
            os.path.join(BACKEND, "models", name)
            for name in os.listdir(os.path.join(BACKEND, "models"))
        ) if path.lower().endswith(".gguf")
    ) if os.path.isdir(os.path.join(BACKEND, "models")) else []
    if models:
        real_plan = hp.plan_inference(profile=real, model_path=models[0],
                                      train_context=0, requested_context=32768,
                                      max_context=32768)
        check("真实模型解析出网络层数", real_plan.model_layers > 0,
              str(real_plan.model_layers))
        check("真实模型初始上下文为 2048", real_plan.n_ctx_initial == 2048,
              str(real_plan.n_ctx_initial))
        check("真实模型上下文上限不超过 32768", real_plan.n_ctx_max <= 32768,
              str(real_plan.n_ctx_max))
        if not real.gpu.available:
            check("真实模型在无独显环境强制 n_gpu_layers=0",
                  real_plan.n_gpu_layers == 0, str(real_plan.n_gpu_layers))
        print(f"  真实模型规划：{real_plan.to_dict()}")


def main():
    tests = [
        test_cpu_only_degrades_silently,
        test_eight_gib_vram,
        test_twelve_gib_vram,
        test_vram_watermark_prevents_full_offload,
        test_partial_offload_layer_count,
        test_insufficient_vram_returns_zero,
        test_context_planner_and_reserve,
        test_thread_count_is_dynamic,
        test_probe_failure_is_silent,
        test_real_probe_never_raises,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            FAILED.append(test.__name__)
            print(f"  [FAIL] {test.__name__} 执行异常：{type(exc).__name__}: {exc}")

    print()
    print("=" * 60)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
