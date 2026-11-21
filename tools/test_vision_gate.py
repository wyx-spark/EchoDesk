# -*- coding: utf-8 -*-
"""
[EN]
Vision runtime-selection test (T3-2, task book §12.3.1 and §14.3.2).

The contract:
* ``native``  -> ``vision_runtime()`` returns "native" and does not import the
  native runtime merely to decide;
* ``wheel``   -> "wheel" when the wheel is importable;
* ``auto``    -> "wheel" when the wheel works, and "native" when it does not (the
  T3-3 safety net), recorded with a fallback reason;
* no runtime -> ``VisionError(code="runtime_unavailable")``.

The "wheel missing" case is proven in a subprocess with a shadow package on
``PYTHONPATH`` that raises on import -- the real environment is never modified.

Run: ``python tools/test_vision_gate.py``  (exit 0 = contract holds)

[ZH]
视觉运行期选择测试（T3-2，任务书 §12.3.1 与 §14.3.2）。

契约：
* ``native``  -> ``vision_runtime()`` 返回 "native"，且不为了判定而导入原生运行期；
* ``wheel``   -> 轮子可导入时返回 "wheel"；
* ``auto``    -> 轮子可用时 "wheel"；不可用时 "native"（T3-3 安全垫），并记录回落原因；
* 无任何运行期 -> ``VisionError(code="runtime_unavailable")``。

「轮子缺失」用例通过 ``PYTHONPATH`` 上的影子包在子进程中证明（导入即抛错），
真实环境从不被修改。

运行：``python tools/test_vision_gate.py``（退出 0 = 契约成立）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

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


def _reset_wheel_cache():
    """[EN] Forget the cached import probe so a monkeypatch takes effect. / [ZH] 清除导入探测缓存，使 monkeypatch 生效。"""
    import llm_backend

    llm_backend._WHEEL_STATE = None


def test_native_selects_native_without_loading_it():
    """[EN] ``native`` must return "native" and not import the runtime to decide. / [ZH] ``native`` 必须返回 "native"，且不为了判定而导入运行期。"""
    import llm_backend

    with _with_mode("native"):
        runtime = llm_backend.vision_runtime()
    check("native: vision_runtime == native", runtime == "native", str(runtime))
    check("native: no runtime DLL module was imported",
          "llama_native" not in sys.modules and "llama_abi" not in sys.modules,
          sorted(m for m in sys.modules if m.startswith("llama")))


def test_wheel_mode_selects_wheel():
    """[EN] ``wheel`` must select the wheel. / [ZH] ``wheel`` 必须选择轮子。"""
    import llm_backend

    with _with_mode("wheel"):
        runtime = llm_backend.vision_runtime()
    check("wheel: vision_runtime == wheel", runtime == "wheel", str(runtime))


def test_auto_prefers_the_wheel():
    """[EN] ``auto`` must use the wheel whenever it works (no speed regression). / [ZH] 轮子可用时 ``auto`` 必须用轮子（不得降速）。"""
    import llm_backend

    _reset_wheel_cache()
    with _with_mode("auto"):
        runtime = llm_backend.vision_runtime()
    check("auto: vision_runtime == wheel", runtime == "wheel", str(runtime))
    check("auto: no fallback recorded", not llm_backend.vision_fallback_reason(),
          llm_backend.vision_fallback_reason())


def test_auto_falls_back_to_native():
    """
    [EN]
    T3-3 safety net: with the wheel unimportable, ``auto`` must select the native
    engine instead of failing. Proven in a subprocess whose PYTHONPATH carries a
    shadow ``llama_cpp`` that raises on import.

    [ZH]
    T3-3 安全垫：轮子不可导入时，``auto`` 必须选择原生引擎而不是失败。用子进程证明，
    其 ``PYTHONPATH`` 带一个导入即抛错的影子 ``llama_cpp``。
    """
    with tempfile.TemporaryDirectory() as shadow:
        with open(os.path.join(shadow, "llama_cpp.py"), "w", encoding="utf-8") as handle:
            handle.write("raise ImportError('shadow: llama_cpp is intentionally "
                         "unimportable for this test')\n")
        env = dict(os.environ)
        env["PYTHONPATH"] = shadow
        env["ECHODESK_LLAMA_RUNTIME"] = "auto"
        code = (
            "import sys;"
            "sys.path.insert(0, %r);"
            "import llm_backend;"
            "print('RUNTIME=' + llm_backend.vision_runtime());"
            "print('FALLBACK=' + llm_backend.vision_fallback_reason())"
            % os.path.join(REPO, "backend")
        )
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, encoding="utf-8")
        out = proc.stdout or ""
        check("auto+missing wheel: subprocess exit 0", proc.returncode == 0,
              (proc.stderr or "")[-300:])
        check("auto+missing wheel: selects native", "RUNTIME=native" in out, out)
        fallback = ""
        for line in out.splitlines():
            if line.startswith("FALLBACK="):
                fallback = line[len("FALLBACK="):]
        check("auto+missing wheel: fallback reason visible", bool(fallback.strip()),
              out)


def test_no_runtime_raises():
    """[EN] Neither runtime -> ``runtime_unavailable``. / [ZH] 两种运行期都不可用 -> ``runtime_unavailable``。"""
    import llm_backend
    from vision_errors import CODE_RUNTIME_UNAVAILABLE, VisionError

    real_wheel, real_native = llm_backend._wheel_importable, llm_backend._native_available
    llm_backend._wheel_importable = lambda: False
    llm_backend._native_available = lambda: False
    try:
        with _with_mode("auto"):
            try:
                llm_backend.vision_runtime()
                raised = None
            except VisionError as exc:
                raised = exc
    finally:
        llm_backend._wheel_importable = real_wheel
        llm_backend._native_available = real_native
    check("none: raises VisionError(runtime_unavailable)",
          isinstance(raised, VisionError)
          and raised.code == CODE_RUNTIME_UNAVAILABLE,
          repr(raised))


def main() -> int:
    test_native_selects_native_without_loading_it()
    test_wheel_mode_selects_wheel()
    test_auto_prefers_the_wheel()
    test_auto_falls_back_to_native()
    test_no_runtime_raises()
    print("TEST vision_gate failures=%d" % len(FAILURES))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
