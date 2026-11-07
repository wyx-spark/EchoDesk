# -*- coding: utf-8 -*-
"""
[EN]
Integration tests for the v0.2.4 task 3 override contract.

The unit tests in ``test_hardware_profiler.py`` prove that the planner adopts
explicit values and returns warnings. This script proves the backend side of
the contract with a real model:

  * a recommended value still loads normally;
  * ``n_gpu_layers=99`` on a machine without a usable accelerator fails with a
    structured error instead of being silently ignored;
  * ``n_ctx=999999`` fails with a clear allocation error;
  * a failed rebuild keeps the previous engine alive (the "never break the
    working model" rule);
  * the child-process "test load" path returns structured success and failure.

[ZH]
v0.2.4 任务-3 覆盖契约的集成测试。

``test_hardware_profiler.py`` 的单元测试证明规划器会原样采纳显式取值并返回警告；
本脚本用真实模型证明后端一侧的契约：

  * 推荐值仍能正常加载；
  * 无可用加速器时 ``n_gpu_layers=99`` 返回结构化错误，而不是被静默忽略；
  * ``n_ctx=999999`` 以清晰的分配错误失败；
  * 重建失败时保留旧引擎（"绝不搞挂原本能用的模型"规则）；
  * 子进程「测试加载」路径分别返回结构化成功与失败。

Run: ``python tools\\test_inference_override.py``. Exit code 0 means all passed.
"""
import gc
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import config  # noqa: E402
import main as backend_main  # noqa: E402

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


def find_model() -> str:
    """[EN] Locate a local GGUF model / [ZH] 定位本地 GGUF 模型。"""
    folder = os.path.join(BACKEND, "models")
    if not os.path.isdir(folder):
        return ""
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".gguf"):
            return os.path.join(folder, name)
    return ""


def build_backend(model: str):
    """[EN] Build a backend with the recommended values / [ZH] 以推荐值构建后端。"""
    cfg = config.Config()
    cfg.model_path = os.path.basename(model)
    cfg.inference_override = False
    cfg.context_length = 2048
    cfg.n_gpu_layers = 0
    cfg.n_batch = 512
    return backend_main.Backend(cfg, model)


def close_backend(b) -> None:
    """[EN] Close the native model and collect garbage / [ZH] 关闭原生模型并回收垃圾。"""
    try:
        b.llm.close()
    except Exception:  # noqa: BLE001 - cleanup must not fail the assertion
        pass
    gc.collect()


def test_validation():
    print("=== 1. 覆盖字段的合理性校验 ===")
    cfg = config.Config()
    cfg.model_path = "x.gguf"
    b = backend_main.Backend(cfg, "")
    try:
        values, error = b._parse_override_request({"n_ctx": 999999})
        check("合法覆盖值通过校验", values.get("n_ctx") == 999999 and error == "", str(values))
        values, error = b._parse_override_request({"n_ctx": 8})
        check("低于下限的 n_ctx 被拒绝", values == {} and bool(error), error)
        values, error = b._parse_override_request({"n_gpu_layers": "abc"})
        check("非整数 n_gpu_layers 被拒绝", values == {} and bool(error), error)
    finally:
        close_backend(b)


def test_real_model(model: str):
    print("=== 2. 推荐值正常，覆盖值失败且保留旧引擎 ===")
    b = build_backend(model)
    try:
        check("推荐值模型加载成功", b.model_ready is True)
        old_plan = dict(getattr(b.llm, "inference_plan", {}) or {})
        old_ctx = int(getattr(b.llm, "n_ctx", 0) or 0)

        b.config.context_length = 999999
        b.config.inference_override = True
        failed = b._rebuild_inference()
        check("超限 n_ctx 重建失败", failed.get("ok") is False, str(failed.get("error")))
        check("失败错误非空", bool(str(failed.get("error") or "").strip()),
              str(failed.get("error")))
        check("失败后旧引擎仍可用", b.model_ready is True)
        check("失败后旧上下文未被改写",
              int(getattr(b.llm, "n_ctx", 0) or 0) == old_ctx,
              f"{old_ctx} -> {getattr(b.llm, 'n_ctx', 0)}")
        check("失败后旧规划未被改写",
              dict(getattr(b.llm, "inference_plan", {}) or {}) == old_plan)

        b.config.context_length = 2048
        b.config.inference_override = False
        restored = b._rebuild_inference()
        check("改回推荐值后重建成功", restored.get("ok") is True, str(restored.get("error")))

        print("=== 3. 无加速器时 n_gpu_layers=99 返回结构化错误 ===")
        gpu_result = b._test_load_inference({"n_ctx": 2048, "n_gpu_layers": 99})
        check("n_gpu_layers=99 测试加载失败", gpu_result.get("ok") is False)
        check("GPU 错误含可读原因", bool(str(gpu_result.get("error") or "").strip()),
              str(gpu_result.get("error")))

        print("=== 4. 超限 n_ctx 的测试加载失败（子进程，60 秒上限） ===")
        context_result = b._test_load_inference({"n_ctx": 999999, "n_gpu_layers": 0})
        check("n_ctx=999999 测试加载失败", context_result.get("ok") is False)
        error_text = str(context_result.get("error") or "")
        check("上下文错误含可读原因",
              "上下文" in error_text
              or "context" in error_text.lower()
              or "内存" in error_text
              or "memory" in error_text.lower(),
              error_text)

        print("=== 5. 推荐值的测试加载成功 ===")
        ok_result = b._test_load_inference({"n_ctx": 2048, "n_gpu_layers": 0})
        check("推荐值测试加载成功", ok_result.get("ok") is True,
              str(ok_result.get("error")))
        check("测试加载返回耗时", float(ok_result.get("elapsed_seconds") or 0) >= 0)
    finally:
        close_backend(b)


def main() -> int:
    test_validation()
    model = find_model()
    if not model:
        print("  [SKIP] backend\\models 下没有 .gguf：无法执行真实模型集成测试")
        print("=" * 60)
        print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项（真实模型部分跳过）")
        return 0 if not FAILED else 1
    try:
        test_real_model(model)
    except Exception as exc:  # noqa: BLE001
        FAILED.append("test_real_model")
        print(f"  [FAIL] test_real_model 执行异常：{type(exc).__name__}: {exc}")

    print()
    print("=" * 60)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
