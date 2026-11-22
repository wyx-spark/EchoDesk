# -*- coding: utf-8 -*-
"""
[EN]
Native text cache-reuse gate (Reader v0.2.7 bug fix, T1).

Why this exists
---------------
A fully cached prompt used to skip prefill *entirely*, so no ``llama_decode`` ran for
that request and ``llama_sampler_sample(ctx, -1)`` read the logits left over from the
previous request's last decode. A repeated prompt therefore produced an empty answer
(when the previous request ended on EOG) or continued the previous sentence (silent
wrong content). Both the streaming and the non-streaming entry points were affected
because they share ``NativeLlama._generate``.

Assertions
----------
1. greedy (temperature=0), same prompt twice, streaming: both non-empty and
   byte-identical.
2. greedy (temperature=0), same prompt twice, non-streaming: both non-empty and
   byte-identical.
3. a full cache hit still evaluates exactly one token (the fix must not become a full
   recompute), reported from ``NativeLlama.last_prefill``.
4. reverse direction: a growing conversation still reuses the prefix, i.e. the suffix
   evaluated is strictly smaller than the whole rendered prompt. This is what stops
   anyone from "fixing" item 1 by disabling prefix reuse altogether.

Determinism note (reader-specific, do not "fix" by pinning a seed)
------------------------------------------------------------------
Reader's ``_make_sampler`` hard-codes ``api.LLAMA_DEFAULT_SEED`` (the random sentinel)
for the distribution sampler and exposes no seed knob. Determinism is therefore obtained
only through ``temperature=0`` (the greedy sampler); never by trying to pin a seed.

[ZH]
原生文本缓存复用门禁（Reader v0.2.7 缺陷修复，T1）。

为什么需要它
------------
整段命中缓存的提示词此前会完全跳过预填充，本轮不跑任何 ``llama_decode``，
``llama_sampler_sample(ctx, -1)`` 读到的是上一次请求最后一次 decode 残留的 logits。
于是重复的提示词会返回空回答（上次以 EOG 结束时）或续写上一句（静默的错误内容）。
流式与非流式入口共用 ``NativeLlama._generate``，因此两者都受影响。

断言
----
1. 贪心（temperature=0），同一提示词连发两次，流式：两次都非空且逐字节相同；
2. 贪心（temperature=0），同一提示词连发两次，非流式：两次都非空且逐字节相同；
3. 完整命中缓存时仍恰好求值 1 个 token（修复不得退化成全量重算），数据取自
   ``NativeLlama.last_prefill``；
4. 反向断言：增长会话仍复用前缀，即求值的后缀严格小于整段渲染后的提示词。
   这条防止有人为了"修好"第 1 项而把前缀复用整体关掉。

确定性说明（reader 特有，不要靠固定种子"修"它）
------------------------------------------------
reader 的 ``_make_sampler`` 把分布采样器的种子写死为 ``api.LLAMA_DEFAULT_SEED``
（随机哨兵），也没有暴露种子参数。因此确定性只能由 ``temperature=0``（贪心采样器）
提供，绝不通过固定种子获得。

Run bare: ``python tools\\test_native_cache_reuse.py``.
Exit code 0 = pass or skip (no model), 1 = failure.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

# [EN] Reader bundles one small model for development; the environment variable wins so
# [EN] another GGUF can be substituted without editing this file.
# [ZH] reader 自带一个小模型供开发使用；环境变量优先，便于不改本文件就换成别的 GGUF。
MODEL_NAME = "Qwen3-0.6B-Q6_K.gguf"
MODEL_CANDIDATES = [
    os.environ.get("ECHODESK_TEST_MODEL"),
    os.path.join(REPO, "backend", "models", MODEL_NAME),
    os.path.join(REPO, "models", MODEL_NAME),
]

PROMPT = [{"role": "user", "content": "In one short sentence, what colour is a clear midday sky?"}]
NEXT_TURN = [{"role": "user", "content": "And in one short sentence, what colour is the sea?"}]

CHECKS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """[EN] Record one assertion. [ZH] 记录一条断言。"""
    CHECKS.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" -- " + detail) if detail else ""))


def find_model() -> str:
    """[EN] Locate the GGUF model. [ZH] 定位 GGUF 模型。"""
    for path in MODEL_CANDIDATES:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def stream_text(llm, messages, max_tokens=24) -> str:
    """[EN] Drain the streaming entry point into one string.
    [ZH] 把流式入口的增量拼成一个字符串。"""
    parts = []
    for chunk in llm.create_chat_completion(
            messages=messages, stream=True, temperature=0.0, max_tokens=max_tokens):
        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
        parts.append(delta)
    return "".join(parts)


def complete_text(llm, messages, max_tokens=24) -> str:
    """[EN] Call the non-streaming entry point once.
    [ZH] 调用一次非流式入口。"""
    result = llm.create_chat_completion(
        messages=messages, stream=False, temperature=0.0, max_tokens=max_tokens)
    return (result.get("choices", [{}])[0].get("message", {}).get("content") or "")


def prefill_tokens(llm) -> int:
    """[EN] Tokens evaluated by the most recent prefill. [ZH] 最近一次预填充求值的 token 数。"""
    return int((llm.last_prefill or {}).get("tokens", -1))


def main() -> int:
    model = find_model()
    if not model:
        print("SKIP native cache-reuse gate: %s not found (set ECHODESK_TEST_MODEL)"
              % MODEL_NAME)
        print("TEST native_cache_reuse skipped=1")
        return 0

    # [EN] Kept inside main so the skip path never touches the native runtime.
    # [ZH] 放在 main 内部，使跳过路径完全不接触原生运行期。
    from llama_native import NativeLlama  # noqa: E402

    # [EN] q8_0 K/V cache is the Reader baseline and needs FlashAttention, so the test
    # [EN] mirrors the product configuration instead of the bare constructor defaults.
    # [ZH] q8_0 的 K/V Cache 是 Reader 基线且需要 FlashAttention，因此测试沿用真实配置，
    # [ZH] 而不是构造函数的裸默认值。
    llm = NativeLlama(model, n_ctx=1024, n_threads=4, n_threads_batch=2,
                      n_gpu_layers=0, flash_attn=True, prefill_yield_ms=0)
    try:
        # ---- 1. streaming, same prompt twice -------------------------------------
        first = stream_text(llm, PROMPT)
        check("stream#1 non-empty", bool(first.strip()), repr(first.strip()[:60]))
        hit_first = prefill_tokens(llm)
        second = stream_text(llm, PROMPT)
        check("stream#2 non-empty", bool(second.strip()), repr(second.strip()[:60]))
        check("stream#2 identical to #1", first == second and bool(first.strip()))
        hit_tokens = prefill_tokens(llm)
        check("full cache hit evaluates exactly 1 token", hit_tokens == 1,
              "last_prefill.tokens=%d (first call evaluated %d)" % (hit_tokens, hit_first))
        # [EN] The second prompt was fully cached, so the fix re-evaluates one token
        # [EN] while the prompt itself is much longer: that is the "1 vs full" contrast.
        # [ZH] 第二次提示词整段命中，修复后只重算 1 个 token，而提示词本身远长于 1：
        # [ZH] 这正是"1 vs 全量"的对比。
        check("hit re-evaluation is far below the prompt length",
              hit_tokens == 1 and hit_tokens < len(llm.tokenize(
                  llm.apply_chat_template(PROMPT, add_assistant=True),
                  add_bos=True, special=True)),
              "hit=%d" % hit_tokens)

        # ---- 2. non-stream, same prompt twice ------------------------------------
        third = complete_text(llm, PROMPT)
        check("nonstream#1 non-empty", bool(third.strip()), repr(third.strip()[:60]))
        fourth = complete_text(llm, PROMPT)
        check("nonstream#2 non-empty", bool(fourth.strip()), repr(fourth.strip()[:60]))
        check("nonstream#2 identical to #1", third == fourth and bool(third.strip()))
        check("nonstream full cache hit evaluates exactly 1 token",
              prefill_tokens(llm) == 1, "last_prefill.tokens=%d" % prefill_tokens(llm))

        # ---- 3. prefix reuse must stay active on a growing conversation ----------
        # [EN] The assistant turn repeats what the model actually produced, so the
        # [EN] rendered prompt really is an extension of the cached token sequence.
        # [ZH] assistant 轮复用模型真实产出的内容，使渲染后的提示词确实是已缓存 token
        # [ZH] 序列的延伸。
        grown = PROMPT + [{"role": "assistant", "content": third.strip()}] + NEXT_TURN
        rendered = llm.apply_chat_template(grown, add_assistant=True)
        total = len(llm.tokenize(rendered, add_bos=True, special=True))
        answer = stream_text(llm, grown)
        evaluated = prefill_tokens(llm)
        check("growing conversation answered", bool(answer.strip()),
              repr(answer.strip()[:60]))
        check("growing conversation reuses the prefix", 1 <= evaluated < total,
              "evaluated=%d of total=%d" % (evaluated, total))
    finally:
        llm.close()

    failed = [name for name, ok in CHECKS if not ok]
    print("TEST native_cache_reuse passed=%d failed=%d"
          % (len(CHECKS) - len(failed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
