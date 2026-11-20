"""
[EN]
Unit checks for the context-page configuration layer (v0.2.6-reader).

What is covered: the ``rope_*`` fields survive a configuration round trip, the effective
scaling handed to the native runtime is computed from the extension factor, and a disabled
mode never leaks a stale factor into the engine.

Why a separate file: the gate list runs one script per concern, so a failure names the
concern instead of a 4000-line test module.

[ZH]
上下文页配置层的单元检查（v0.2.6-reader）。

覆盖内容：``rope_*`` 字段经配置读写一轮后保持不变；交给原生运行时的生效缩放由扩展倍数
推导；关闭状态下绝不把残留系数带进引擎。

为什么单独成文件：门禁按关注点逐脚本执行，失败时能直接指出是哪个关注点，而不是在一个
四千行的测试模块里翻找。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from inference_config import InferenceConfig  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    """[EN] Record one assertion. [ZH] 记录一条断言。"""
    global PASSED
    if condition:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name} {detail}")


def main():
    # [EN] 1. Defaults: the shipped file must behave exactly as before the feature.
    # [ZH] 1. 默认值：随包默认行为必须与功能加入前完全一致。
    default = InferenceConfig.from_dict({})
    rope = default.resolve_rope()
    check("默认 rope_type 为 none", default.rope_type == "none", default.rope_type)
    check("默认生效缩放的 freq_scale 为 0（沿用模型自带）",
          rope["freq_scale"] == 0.0, repr(rope))

    # [EN] 2. Round trip: what the page writes is what the next start reads.
    # [ZH] 2. 往返：页面写入的值，下次启动读到的就是它。
    payload = {
        "n_ctx_max": 262144,
        "rope_type": "yarn",
        "rope_scale": 4.0,
        "rope_orig_ctx": 32768,
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "inference_config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        loaded = InferenceConfig.load(path)
    check("n_ctx_max 可持久化", loaded.n_ctx_max == 262144, loaded.n_ctx_max)
    check("rope_type 可持久化", loaded.rope_type == "yarn", loaded.rope_type)
    check("rope_orig_ctx 可持久化", loaded.rope_orig_ctx == 32768, loaded.rope_orig_ctx)

    # [EN] 3. Derivation: factor 4 means frequency scale 1/4, and the reciprocal follows
    # [EN]        the factor even if the stored frequency disagrees.
    # [ZH] 3. 推导：4 倍扩展对应 1/4 频率缩放；即使存的频率不一致，也以倍数为准。
    scaled = InferenceConfig.from_dict({"rope_type": "yarn", "rope_scale": 4.0,
                                        "rope_freq_scale": 1.0, "rope_orig_ctx": 32768})
    effective = scaled.resolve_rope()
    check("4 倍扩展得到 freq_scale=0.25", abs(effective["freq_scale"] - 0.25) < 1e-9,
          repr(effective))
    check("类型与原文一致", effective["type"] == "yarn", effective["type"])

    # [EN] 4. Disabled mode: a leftover factor must not reach the engine.
    # [ZH] 4. 关闭状态：残留系数不得进入引擎。
    disabled = InferenceConfig.from_dict({"rope_type": "none", "rope_scale": 8.0,
                                          "rope_orig_ctx": 32768})
    off = disabled.resolve_rope()
    check("none 模式清空倍数与原文",
          off["scale"] == 1.0 and off["freq_scale"] == 0.0 and off["orig_ctx"] == 0,
          repr(off))

    # [EN] 5. Unknown mode falls back to none instead of guessing an extension.
    # [ZH] 5. 未知模式回退到 none，而不是猜一个扩展方式。
    unknown = InferenceConfig.from_dict({"rope_type": "yarn2", "rope_scale": 2.0})
    check("未知模式回退 none", unknown.rope_type == "none", unknown.rope_type)

    # [EN] 6. The engine kwargs carry the rope fields (they are what NativeLlama receives).
    # [ZH] 6. 引擎 kwargs 带上 rope 字段（NativeLlama 收到的就是这些）。
    kwargs = scaled.llama_kwargs()
    check("llama_kwargs 带 rope_type/scale/orig_ctx",
          kwargs.get("rope_type") == "yarn"
          and kwargs.get("rope_orig_ctx") == 32768
          and abs(float(kwargs.get("rope_freq_scale", 0)) - 0.25) < 1e-9,
          repr({k: v for k, v in kwargs.items() if k.startswith("rope")}))

    print("-" * 42)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print(f"共 {PASSED} 项，全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
