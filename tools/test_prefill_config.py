# -*- coding: utf-8 -*-
"""
[EN]
Unit checks for the prefill configuration layer (v0.2.7-reader, step 1).

What is covered: the four new keys survive a round trip, a file without them falls back
to the documented defaults, out-of-range values are clamped instead of raising, the
``0`` / negative thread semantics resolve as specified, a chunk larger than ``n_batch``
is pulled back, and ``llama_kwargs()`` never hands a zero ``n_threads_batch`` to the
native layer.

Why a separate file: the gate list runs one script per concern, so a failure names the
concern instead of hiding inside a 4000-line test module.

[ZH]
预填充配置层的单元检查（v0.2.7-reader 步 1）。

覆盖内容：四个新键经读写一轮后保持不变；旧文件缺这些键时回退到文档默认值；越界值被
钳制而不是抛异常；``0`` / 负值的线程语义按规格解析；大于 ``n_batch`` 的分块被压回；
``llama_kwargs()`` 绝不把 0 交给原生层的 ``n_threads_batch``。

为什么单独成文件：门禁按关注点逐脚本执行，失败时能直接指出是哪个关注点，而不是在一个
四千行的测试模块里翻找。

Run: ``python tools\\test_prefill_config.py``. Exit code 0 means all passed.
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
    # [EN] 1. Missing keys: an old file must behave as before the feature existed, and
    # [EN]    the default is "throttling on", which is the point of this round.
    # [ZH] 1. 缺键：旧文件的行为必须与功能加入前一致，且默认就是"开启限流"——这正是本轮目的。
    default = InferenceConfig.from_dict({})
    check("缺键时四个新键取默认值",
          default.n_threads_batch == 0
          and default.prefill_chunk_tokens == 0
          and default.prefill_yield_ms == 8
          and default.prefill_stall_seconds == 45,
          f"{default.n_threads_batch}/{default.prefill_chunk_tokens}/"
          f"{default.prefill_yield_ms}/{default.prefill_stall_seconds}")
    check("缺键时自动线程数 = 核数-2（16 核 -> 14）",
          default.resolve_n_threads_batch(logical_cores=16) == 14,
          default.resolve_n_threads_batch(logical_cores=16))
    check("缺键时分块跟随 n_batch",
          default.resolve_prefill_chunk_tokens() == 512,
          default.resolve_prefill_chunk_tokens())

    # [EN] 2. Negative value = follow n_threads exactly, with no headroom subtracted.
    # [ZH] 2. 负值 = 严格跟随 n_threads，不扣除余量。
    follow = InferenceConfig.from_dict({"n_threads": 6, "n_threads_batch": -1})
    check("-1 跟随 n_threads", follow.resolve_n_threads_batch() == 6,
          follow.resolve_n_threads_batch())
    check("-1 时给定核数直接采用", follow.resolve_n_threads_batch(logical_cores=12) == 12,
          follow.resolve_n_threads_batch(logical_cores=12))

    # [EN] 3. Zero = automatic, but the pool may never collapse to zero.
    # [ZH] 3. 0 = 自动，但线程池绝不能塌缩成 0。
    auto = InferenceConfig.from_dict({"n_threads": 16})
    check("0 = 自动取 核数-2（16 -> 14）", auto.resolve_n_threads_batch() == 14,
          auto.resolve_n_threads_batch())
    two = InferenceConfig.from_dict({"n_threads": 2})
    check("0 = 自动在极小核数下仍 >= 1", two.resolve_n_threads_batch() == 1,
          two.resolve_n_threads_batch())

    # [EN] 4. Upper and lower clamps: a hand-edited file must not raise.
    # [ZH] 4. 上下限钳制：手改文件不得抛异常。
    top = InferenceConfig.from_dict({"n_threads_batch": 99999, "prefill_yield_ms": 9999,
                                     "prefill_stall_seconds": 9999})
    check("线程数上限钳制到 1024", top.n_threads_batch == 1024, top.n_threads_batch)
    check("yield 上限钳制到 50", top.prefill_yield_ms == 50, top.prefill_yield_ms)
    check("stall 上限钳制到 300", top.prefill_stall_seconds == 300,
          top.prefill_stall_seconds)
    bottom = InferenceConfig.from_dict({"n_threads_batch": -99, "prefill_yield_ms": -5,
                                        "prefill_stall_seconds": 1})
    check("线程数下限钳制到 -1", bottom.n_threads_batch == -1, bottom.n_threads_batch)
    check("yield 下限钳制到 0", bottom.prefill_yield_ms == 0, bottom.prefill_yield_ms)
    check("stall 下限钳制到 10", bottom.prefill_stall_seconds == 10,
          bottom.prefill_stall_seconds)

    # [EN] 5. Chunk size: over the batch is pulled back, under 32 is lifted, 0 follows.
    # [ZH] 5. 分块大小：超批被压回、低于 32 被抬高、0 跟随。
    big = InferenceConfig.from_dict({"n_batch": 256, "prefill_chunk_tokens": 4096})
    check("chunk 超 n_batch 被压回",
          big.prefill_chunk_tokens == 256 and big.resolve_prefill_chunk_tokens() == 256,
          f"{big.prefill_chunk_tokens}/{big.resolve_prefill_chunk_tokens()}")
    tiny = InferenceConfig.from_dict({"n_batch": 512, "prefill_chunk_tokens": 4})
    check("chunk 低于 32 被抬到 32", tiny.prefill_chunk_tokens == 32,
          tiny.prefill_chunk_tokens)
    follow_chunk = InferenceConfig.from_dict({"n_batch": 128})
    check("chunk=0 跟随 n_batch", follow_chunk.resolve_prefill_chunk_tokens() == 128,
          follow_chunk.resolve_prefill_chunk_tokens())

    # [EN] 6. Illegal values fall back to the defaults instead of raising.
    # [ZH] 6. 非法值回退默认值，而不是抛异常。
    bad = InferenceConfig.from_dict({"n_threads_batch": "abc", "prefill_chunk_tokens": None,
                                     "prefill_yield_ms": "x",
                                     "prefill_stall_seconds": []})
    check("非法值回退默认且不抛异常",
          bad.n_threads_batch == 0
          and bad.prefill_chunk_tokens == 0
          and bad.prefill_yield_ms == 8
          and bad.prefill_stall_seconds == 45,
          f"{bad.n_threads_batch}/{bad.prefill_chunk_tokens}/"
          f"{bad.prefill_yield_ms}/{bad.prefill_stall_seconds}")

    # [EN] 7. The native layer always receives a concrete, positive thread count.
    # [ZH] 7. 原生层拿到的永远是具体且为正的线程数。
    kwargs = default.llama_kwargs()
    check("llama_kwargs 带 n_threads_batch 且为整数",
          "n_threads_batch" in kwargs and isinstance(kwargs["n_threads_batch"], int),
          repr(kwargs.get("n_threads_batch")))
    check("llama_kwargs 的 n_threads_batch 恒 >= 1",
          int(kwargs.get("n_threads_batch", 0)) >= 1, repr(kwargs.get("n_threads_batch")))
    check("llama_kwargs 用的是解析后的最终值",
          kwargs.get("n_threads_batch") == default.resolve_n_threads_batch(),
          f"{kwargs.get('n_threads_batch')} vs {default.resolve_n_threads_batch()}")
    one = InferenceConfig.from_dict({"n_threads": 1})
    check("自动线程数跌到 1 时也不给原生层 0",
          int(one.llama_kwargs().get("n_threads_batch", 0)) >= 1,
          repr(one.llama_kwargs().get("n_threads_batch")))

    # [EN] 8. Round trip through a real file in a temporary directory: no key is lost.
    # [ZH] 8. 经临时目录里的真实文件往返：不丢任何键。
    payload = {"n_batch": 512, "n_threads_batch": 6, "prefill_chunk_tokens": 128,
               "prefill_yield_ms": 12, "prefill_stall_seconds": 60}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "inference_config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        loaded = InferenceConfig.load(path)
        again = InferenceConfig.from_dict(loaded.to_dict(), path=path)
    check("往返后四个新键不丢",
          again.n_threads_batch == 6
          and again.prefill_chunk_tokens == 128
          and again.prefill_yield_ms == 12
          and again.prefill_stall_seconds == 60,
          f"{again.n_threads_batch}/{again.prefill_chunk_tokens}/"
          f"{again.prefill_yield_ms}/{again.prefill_stall_seconds}")
    check("to_dict 含四个新键",
          all(key in again.to_dict() for key in
              ("n_threads_batch", "prefill_chunk_tokens",
               "prefill_yield_ms", "prefill_stall_seconds")),
          sorted(again.to_dict()))

    # [EN] 9. Snapshot and one-line summary expose the prefill tuning.
    # [ZH] 9. 快照与单行摘要都能看出预填充调优。
    snapshot = again.describe()
    check("describe 快照含 prefill 四项",
          snapshot.get("n_threads_batch") == 6
          and snapshot.get("prefill_chunk_tokens") == 128
          and snapshot.get("prefill_yield_ms") == 12
          and snapshot.get("prefill_stall_seconds") == 60,
          repr({k: v for k, v in snapshot.items() if "prefill" in k or "batch" in k}))
    line = again.log_line()
    check("单行摘要体现 prefill 线程数与分块",
          "n_threads_batch=6" in line and "prefill_chunk=128" in line, line)

    print("-" * 42)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    sample = InferenceConfig.from_dict({"n_threads": 8})
    print(f"PREFILL n_threads={sample.resolve_n_threads()} "
          f"n_threads_batch={sample.resolve_n_threads_batch()} "
          f"chunk={sample.resolve_prefill_chunk_tokens()} "
          f"yield={sample.resolve_prefill_yield_ms()} "
          f"stall={sample.resolve_prefill_stall_seconds()}")
    print(f"共 {PASSED} 项，全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
