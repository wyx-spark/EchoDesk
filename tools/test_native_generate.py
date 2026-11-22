"""
[EN]
End-to-end assertion for the native text runtime: a real generation must succeed.

Why this exists
---------------
v0.2.7 shipped a build in which every completion that used a repeat penalty raised
``TypeError: this function takes at least 5 arguments (4 given)`` from the ctypes sampler call.
No gate item executed a real generation and asserted the result: the WPF probe drove the UI but
never checked the answer text, so the defect passed a green gate and was found by a human looking
at the window.

This check loads the model through ``LLMEngine`` (the same class the backend uses), generates one
short answer with the application's default repeat penalty (1.1) and fails unless the call returns
without raising and the answer text is non-empty. The model is supplied by the release gate from
the development tree; a missing model is a failure here, not a skip.

[ZH]
原生文本运行期的端到端断言：一次真实生成必须成功。

为什么需要
----------
v0.2.7 曾发出过这样一个构建：凡使用重复惩罚的补全都从 ctypes 采样器调用抛出
``TypeError: this function takes at least 5 arguments (4 given)``。当时没有任何门禁项真正执行
生成并断言结果：WPF 探针驱动了界面，却从不检查回答文本，于是缺陷穿过全绿门禁，靠人看窗口才发现。

本检查用 ``LLMEngine``（后端使用的同一个类）加载模型，以应用默认重复惩罚（1.1）生成一个短回答，
断言调用不抛异常且回答文本非空。模型由发布门禁从开发树给出；模型缺失在这里是失败而不是跳过。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
os.environ.setdefault("ECHODESK_LLAMA_RUNTIME", "native")

PROMPT = "用一句话回答：1+1 等于几？"


def find_model(explicit: str) -> str:
    if explicit:
        return explicit
    models = os.path.join(BACKEND, "models")
    if not os.path.isdir(models):
        return ""
    candidates = sorted(
        os.path.join(models, name) for name in os.listdir(models) if name.endswith(".gguf"))
    return candidates[0] if candidates else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="native text generation assertion")
    parser.add_argument("--model", default="", help="GGUF model path (dev tree)")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--repeat-penalty", type=float, default=1.1)
    parser.add_argument("--temperature", type=float, default=0.7)
    args = parser.parse_args()

    model = find_model(args.model)
    if not model or not os.path.isfile(model):
        print(f"找不到模型：{model or '(backend\\models 下没有 .gguf)'}")
        return 2

    from llm_engine import LLMEngine  # noqa: PLC0415 - import after sys.path setup

    started = time.time()
    engine = LLMEngine(model, n_ctx=2048)
    engine.load(model)
    reasoning, answer = engine.chat_complete_split(
        [{"role": "user", "content": PROMPT}],
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        top_p=0.9,
        repeat_penalty=args.repeat_penalty,
    )
    elapsed = time.time() - started
    print(f"模型：{model}")
    print(f"repeat_penalty={args.repeat_penalty} max_tokens={args.max_tokens} 用时 {elapsed:.1f}s")
    print(f"思考区字符数：{len(reasoning)}；回答：{answer[:120]!r}")
    if not answer.strip():
        print("失败：真实生成返回了空回答")
        return 1
    print("通过：真实生成未抛异常且回答非空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
