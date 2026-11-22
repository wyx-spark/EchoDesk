"""Native text cache-reuse gate (T3-3 step 2).

Why this exists
---------------
A fully cached prompt used to skip prefill *entirely*, so no ``llama_decode`` ran for
that request and ``llama_sampler_sample(ctx, -1)`` read the logits left over from the
previous request's last decode. A repeated prompt therefore produced an empty answer
(when the previous request ended on EOG) or continued the previous sentence (silent
wrong content). Both the streaming and the non-streaming entry points were affected
because they share ``NativeLlama._generate``.

Assertions
---------
1. greedy, same prompt twice, streaming:  both non-empty and byte-identical.
2. greedy, same prompt twice, non-stream: both non-empty and byte-identical.
3. full cache hit still evaluates exactly one token (the fix must not become a full
   recompute), reported from ``NativeLlama.last_prefill``.
4. the reverse direction: a growing conversation still reuses the prefix, i.e. the
   suffix evaluated is smaller than the whole rendered prompt. This is what stops
   anyone from "fixing" item 1 by disabling prefix reuse altogether.
5. the product surface (``LLMEngine.chat_stream_split``) shows the same guarantee.

Run bare: python tools/test_native_cache_reuse.py
Exit code 0 = pass or skip, 1 = failure.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))
os.environ.setdefault("ECHODESK_LLAMA_RUNTIME", "native")

MODEL_NAME = "Qwen3-0.6B-Q8_0.gguf"
MODEL_CANDIDATES = [
    os.environ.get("ECHODESK_TEST_MODEL"),
    os.path.join(os.path.dirname(REPO), "models", MODEL_NAME),
    # [EN] The master checkout and the reader checkout are siblings; the model cache
    # [EN] lives under the reader tree's sibling directory on the dev box.
    # [ZH] master 工作树与 reader 工作树互为兄弟目录；开发机上的模型缓存放在
    # [ZH] reader 树同级的目录下。
    os.path.join(os.path.dirname(REPO), "EchoDesk", "models", MODEL_NAME),
    os.path.join(REPO, "models", MODEL_NAME),
]

PROMPT = [{"role": "user", "content": "In one short sentence, what colour is a clear midday sky?"}]
NEXT_TURN = [{"role": "user", "content": "And in one short sentence, what colour is the sea?"}]

CHECKS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok)))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       (" -- " + detail) if detail else ""))


def find_model() -> str:
    for path in MODEL_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return ""


def stream_text(llm, messages, max_tokens=24) -> str:
    parts = []
    for chunk in llm.create_chat_completion(
            messages=messages, stream=True, temperature=0.0, max_tokens=max_tokens):
        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
        parts.append(delta)
    return "".join(parts)


def complete_text(llm, messages, max_tokens=24) -> str:
    result = llm.create_chat_completion(
        messages=messages, stream=False, temperature=0.0, max_tokens=max_tokens)
    return (result.get("choices", [{}])[0].get("message", {}).get("content") or "")


def prefill_tokens(llm) -> int:
    return int((llm.last_prefill or {}).get("tokens", -1))


def main() -> int:
    model = find_model()
    if not model:
        print("SKIP native cache-reuse gate: %s not found (set ECHODESK_TEST_MODEL)"
              % MODEL_NAME)
        print("TEST native_cache_reuse skipped=1")
        return 0

    from llama_native import NativeLlama  # noqa: E402

    llm = NativeLlama(model, n_ctx=1024, n_threads=4, n_gpu_layers=0)
    try:
        # ---- 1. streaming, same prompt twice -------------------------------------
        first = stream_text(llm, PROMPT)
        check("stream#1 non-empty", bool(first.strip()), repr(first.strip()[:60]))
        second = stream_text(llm, PROMPT)
        check("stream#2 non-empty", bool(second.strip()), repr(second.strip()[:60]))
        check("stream#2 identical to #1", first == second and bool(first.strip()))
        hit_tokens = prefill_tokens(llm)
        check("full cache hit evaluates exactly 1 token", hit_tokens == 1,
              "last_prefill.tokens=%d" % hit_tokens)

        # ---- 2. non-stream, same prompt twice ------------------------------------
        third = complete_text(llm, PROMPT)
        check("nonstream#1 non-empty", bool(third.strip()), repr(third.strip()[:60]))
        fourth = complete_text(llm, PROMPT)
        check("nonstream#2 non-empty", bool(fourth.strip()), repr(fourth.strip()[:60]))
        check("nonstream#2 identical to #1", third == fourth and bool(third.strip()))
        check("nonstream full cache hit evaluates exactly 1 token",
              prefill_tokens(llm) == 1, "last_prefill.tokens=%d" % prefill_tokens(llm))

        # ---- 3. prefix reuse must stay active on a growing conversation ----------
        grown = PROMPT + [{"role": "assistant", "content": third.strip()}] + NEXT_TURN
        rendered = llm.apply_chat_template(grown, add_assistant=True)
        total = len(llm.tokenize(rendered, add_bos=True, special=True))
        answer = stream_text(llm, grown)
        evaluated = prefill_tokens(llm)
        check("growing conversation answered", bool(answer.strip()),
              repr(answer.strip()[:60]))
        check("growing conversation reuses the prefix", 0 <= evaluated < total,
              "evaluated=%d of total=%d" % (evaluated, total))

        # ---- 4. product surface: LLMEngine.chat_stream_split twice ---------------
        from llm_engine import LLMEngine  # noqa: E402
        from reasoning import ANSWER, REASONING  # noqa: E402
        engine = LLMEngine(model, n_ctx=1024, n_threads=4, n_gpu_layers=0)
        try:
            # [EN] The whole stream is compared, not just the answer channel: with a
            # [EN] thinking model a small ``max_tokens`` can be spent entirely on
            # [EN] ``<think>``, which legitimately leaves the answer channel empty. The
            # [EN] defect being guarded here is "the repeat yields nothing at all".
            # [ZH] 比较的是整条流，而不只是回答通道：思考模型在小 ``max_tokens`` 下可能把预算
            # [ZH] 全用在 ``<think>`` 上，此时回答通道为空是正常行为。此处要守住的缺陷是
            # [ZH] "重复请求什么都产出不了"。
            def engine_reply():
                buf = []
                events = 0
                for channel, piece in engine.chat_stream_split(
                        PROMPT, temperature=0.0, max_tokens=128):
                    events += 1
                    if channel in (ANSWER, REASONING) and piece:
                        buf.append(piece)
                return events, "".join(buf)

            events_first, engine_first = engine_reply()
            events_second, engine_second = engine_reply()
            check("chat_stream_split #1 produces events", events_first > 0,
                  "events=%d" % events_first)
            check("chat_stream_split #2 produces events", events_second > 0,
                  "events=%d" % events_second)
            check("chat_stream_split #1 non-empty", bool(engine_first.strip()))
            check("chat_stream_split #2 non-empty", bool(engine_second.strip()))
            check("chat_stream_split repeat identical",
                  engine_first == engine_second and bool(engine_first.strip()))
        finally:
            engine.close()
    finally:
        llm.close()

    failed = [name for name, ok in CHECKS if not ok]
    print("TEST native_cache_reuse passed=%d failed=%d"
          % (len(CHECKS) - len(failed), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
