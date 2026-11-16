# [EN] Streaming whitespace regression test.
#
# English output used to arrive with the words glued together ("Themodelis")
# whenever a fragment lost its leading space on the way from the tokenizer to
# the interface. This test drives the **real** backend streaming path (the real
# ``Backend.stream`` action plus the real ``ReasoningSplitter``; only the
# ``llama_cpp`` object is a scripted stub) and prints every delta with ``repr()``
# so a lost space is visible at the exact fragment where it happens.
#
# The contract under test:
#   * every inter-word space produced by the tokenizer is forwarded verbatim;
#   * only the line breaks that immediately follow a reasoning tag may be
#     dropped, because they would otherwise render as a leading blank line;
#   * the streamed answer and the final ``done`` payload agree byte for byte.
#
# [ZH] 流式空白回归测试。
#
# 英文输出曾经出现单词粘连（"Themodelis"）：片段在从分词器到界面的链路上丢失了
# 行首空格。本测试驱动**真实**的后端流式链路（真实的 ``Backend.stream`` 动作与真实的
# ``ReasoningSplitter``，只有 ``llama_cpp`` 对象是脚本化桩），并用 ``repr()`` 打印每个
# delta，使空格丢失能精确定位到具体片段。
#
# 被测约定：
#   * 分词器产出的词间空格全部原样转发；
#   * 只有紧随思考标签之后的换行允许被丢弃（否则会渲染成行首空行）；
#   * 流式回答与最终 ``done`` 载荷逐字节一致。
#
# 用法 / Usage:
#   python tools\test_stream_spaces.py
#
# 退出码 / Exit codes: 0 = all checks passed, 1 = at least one check failed.
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import test_reasoning as TR  # noqa: E402  (scripted llama_cpp stub + real Backend wiring)

FAILED = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)
    return ok


def stream(script):
    """Run one scripted answer through the real backend stream action."""
    backend = TR.make_backend(script)
    lines = TR.stream_lines(backend, "hello")
    deltas = [line for line in lines if line.get("type") == "delta"]
    done = [line for line in lines if line.get("type") == "done"]
    answer = "".join(line.get("content", "") for line in deltas
                     if line.get("channel") in (None, "answer") and not line.get("reset"))
    return deltas, answer, (done[-1].get("content", "") if done else "")


def dump(title, deltas):
    print("\n== %s ==" % title)
    for line in deltas:
        print("  channel=%-9s reset=%-5s %r"
              % (line.get("channel", "-"), bool(line.get("reset")), line.get("content", "")))


def case(title, script, expected):
    deltas, answer, done = stream(script)
    dump(title, deltas)
    check("%s / streamed answer" % title, answer == expected, repr(answer))
    check("%s / done payload" % title, done == expected, repr(done))
    check("%s / no doubled spaces" % title, "  " not in answer, repr(answer))


def main():
    print("流式空白回归 / Streaming whitespace regression")

    # [EN] The canonical symptom: one leading-space token per word.
    # [ZH] 典型症状：每个单词都带一个行首空格 token。
    case("plain english", ["The", " quick", " brown", " fox"], "The quick brown fox")

    # [EN] Space in its own token (some tokenizers split it that way).
    # [ZH] 空格单独成一个 token（部分分词器如此切分）。
    case("standalone space tokens", ["Hello", " ", "world"], "Hello world")

    # [EN] Answer after a reasoning block, where the first answer token carries the space.
    # [ZH] 思考块之后的回答：首个回答 token 自带空格。
    case("answer after reasoning tag",
         ["<think>", "plan", "</think>", " The", " answer"], " The answer")

    # [EN] A tab is token content as well and must not be normalised into nothing.
    # [ZH] Tab 同样属于 token 内容，不能被吞掉。
    deltas, answer, _ = stream(["A", "\tB"])
    dump("tab separator", deltas)
    check("tab separator / preserved", answer == "A\tB", repr(answer))

    # [EN] Punctuation glued on purpose: nothing may be inserted either.
    # [ZH] 标点本就紧贴前词：也不允许被插入空格。
    case("punctuation stays glued", ["Order", " #", "42", "."], "Order #42.")

    print("")
    if FAILED:
        print("FAILED: %d 项 / %d check(s)" % (len(FAILED), len(FAILED)))
        for name in FAILED:
            print("  - " + name)
        return 1
    print("OK: 所有空白检查通过 / all whitespace checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
