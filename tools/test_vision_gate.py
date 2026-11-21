# -*- coding: utf-8 -*-
"""
[EN]
Vision runtime gate test (T3-2, task book §12.3.1).

The contract under test: a vision request must fail with a typed error whenever no
available llama.cpp runtime can serve it -- that is ``ECHODESK_LLAMA_RUNTIME=native``
today, and ``auto`` with the wheel gone after T3-3. It must never degrade into a
text-only answer, and it must refuse *before* any runtime DLL is loaded.

This test is deliberately model-free: the gate is a policy decision, so it can be
exercised with dummy paths. Loading a 1.4 GB VLM to prove a boolean would make the
test too slow to run on every change.

Run: ``python tools/test_vision_gate.py``  (exit 0 = contract holds)

[ZH]
视觉运行期闸门测试（T3-2，任务书 §12.3.1）。

被测契约：只要没有可用的 llama.cpp 运行期能承载视觉，视觉请求就必须以带类型的错误失败
—— 今天是指 ``ECHODESK_LLAMA_RUNTIME=native``，以及 T3-3 之后轮子不复存在的 ``auto``。
它绝不能退化成纯文本回答，并且必须在加载任何运行期 DLL **之前**拒绝。

本测试刻意不依赖模型：闸门是策略判定，用假路径即可覆盖。为了证明一个布尔值而加载
1.4 GB 的 VLM 会让测试慢到无法在每次改动时运行。

运行：``python tools/test_vision_gate.py``（退出 0 = 契约成立）
"""
from __future__ import annotations

import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """[EN] Record one assertion. / [ZH] 记录一条断言。"""
    print("%s %s%s" % ("PASS" if condition else "FAIL", name,
                       (" -> " + detail) if (detail and not condition) else ""))
    if not condition:
        FAILURES.append(name)


def _with_mode(mode: str):
    """[EN] Context manager switching the runtime policy env var. / [ZH] 切换运行期策略环境变量的上下文管理器。"""
    class _Mode:
        def __enter__(self):
            self.previous = os.environ.get("ECHODESK_LLAMA_RUNTIME")
            os.environ["ECHODESK_LLAMA_RUNTIME"] = mode
            return self

        def __exit__(self, *exc):
            if self.previous is None:
                os.environ.pop("ECHODESK_LLAMA_RUNTIME", None)
            else:
                os.environ["ECHODESK_LLAMA_RUNTIME"] = self.previous
            return False

    return _Mode()


def _raises(fn):
    """[EN] Call ``fn`` and return ``(exception_or_None)``. / [ZH] 调用 ``fn`` 并返回 ``(异常或 None)``。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the exception is the value under test
        return exc
    return None


def test_native_rejects_vision():
    """[EN] ``native`` must refuse with the typed error and the rollback hint. / [ZH] ``native`` 必须以带类型错误 + 回退指引拒绝。"""
    import llm_backend

    with _with_mode("native"):
        exc = _raises(llm_backend.vision_runtime)
    check("native: vision_runtime raises", isinstance(
        exc, llm_backend.NativeVisionUnsupported), repr(exc))
    message = str(exc or "")
    check("native: message names the rollback lever",
          "ECHODESK_LLAMA_RUNTIME=wheel" in message, message)
    check("native: no runtime DLL module was imported",
          "llama_native" not in sys.modules and "llama_abi" not in sys.modules,
          sorted(m for m in sys.modules if m.startswith("llama")))


def test_service_gate_precedes_path_checks():
    """[EN] The gate must fire before the path checks, so the type survives to the dispatcher. / [ZH] 闸门必须早于路径校验，使异常类型能传到分发层。"""
    import vision

    service = vision.VisionService(model_path="", mmproj_path="")
    with _with_mode("native"):
        exc = _raises(lambda: service.chat("missing.png", "hi"))
    check("native: VisionService.chat raises NativeVisionUnsupported",
          isinstance(exc, vision.NativeVisionUnsupported), repr(exc))
    service.close()


def test_auto_still_uses_the_wheel():
    """[EN] ``auto`` must keep vision on the wheel (public promise until T3-3). / [ZH] ``auto`` 必须继续把视觉放在轮子上（T3-3 之前的公开承诺）。"""
    import llm_backend

    with _with_mode("auto"):
        runtime = llm_backend.vision_runtime()
    check("auto: vision_runtime == wheel", runtime == "wheel", str(runtime))


def test_auto_without_wheel_fails_loudly():
    """[EN] T3-3 scenario: ``auto`` with no wheel must raise, not fall back to text. / [ZH] T3-3 情形：``auto`` 且轮子缺失时必须抛出，不得回落纯文本。"""
    import llm_backend

    real_find_spec = importlib.util.find_spec

    def _missing(name, *args, **kwargs):
        if name == "llama_cpp":
            return None
        return real_find_spec(name, *args, **kwargs)

    importlib.util.find_spec = _missing
    try:
        with _with_mode("auto"):
            exc = _raises(llm_backend.vision_runtime)
    finally:
        importlib.util.find_spec = real_find_spec
    check("auto+missing wheel: raises", isinstance(
        exc, llm_backend.NativeVisionUnsupported), repr(exc))
    check("auto+missing wheel: message names the rollback lever",
          "ECHODESK_LLAMA_RUNTIME" in str(exc or ""), str(exc or ""))


def test_wheel_mode_serves_vision():
    """[EN] ``wheel`` must serve vision. / [ZH] ``wheel`` 必须承载视觉。"""
    import llm_backend

    with _with_mode("wheel"):
        runtime = llm_backend.vision_runtime()
    check("wheel: vision_runtime == wheel", runtime == "wheel", str(runtime))


def main() -> int:
    test_native_rejects_vision()
    test_service_gate_precedes_path_checks()
    test_auto_still_uses_the_wheel()
    test_auto_without_wheel_fails_loudly()
    test_wheel_mode_serves_vision()
    print("TEST vision_gate failures=%d" % len(FAILURES))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
