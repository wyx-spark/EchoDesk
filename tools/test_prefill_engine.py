# -*- coding: utf-8 -*-
"""
[EN]
Engine-side checks for the prefill progress contract (v0.2.7-reader, step 3).

A stub native model drives ``LLMEngine._chat_stream_raw`` / ``_iter_with_heartbeat``:
no real GGUF is needed and nothing is skipped.

What is covered:
  * heartbeats carry a monotonically increasing ``prefill_tokens``;
  * the no-progress watchdog ends the stream normally with ``stalled=true``;
  * a user abort during prefill is swallowed silently and reported as ``stopped=true``;
  * heartbeats during decode behave exactly as before (no prefill fields leak in);
  * ETA is derived from the measured tok/s and is 0 when the rate is not positive.

Why a separate file: the gate list runs one script per concern, so a failure names the
concern instead of hiding inside a 4000-line test module.

[ZH]
引擎侧的预填充进度契约检查（v0.2.7-reader 步 3）。

用桩原生模型驱动 ``LLMEngine._chat_stream_raw`` / ``_iter_with_heartbeat``：不需要真实
GGUF，也没有任何跳过。

覆盖内容：
  * 心跳携带的 ``prefill_tokens`` 单调递增；
  * 无进展看门狗正常结束流且 ``stalled=true``；
  * 预填充期间的用户中止被静默吞掉并上报 ``stopped=true``；
  * decode 阶段的心跳行为与之前完全一致（不混入预填充字段）；
  * ETA 由实测 tok/s 反算，速率非正时给 0。

为什么单独成文件：门禁按关注点逐脚本执行，失败时能直接指出是哪个关注点，而不是在一个
四千行的测试模块里翻找。

Run: ``python tools\\test_prefill_engine.py``. Exit code 0 means all passed.
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from llama_native import NativeLlamaAborted  # noqa: E402
from llm_engine import LLMEngine, StreamStats  # noqa: E402

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


class FakeInference:
    """[EN] Only what the watchdog asks for. [ZH] 只提供看门狗需要的那一项。"""

    def __init__(self, stall_seconds: float):
        self._stall_seconds = float(stall_seconds)

    def resolve_prefill_stall_seconds(self) -> int:
        return int(round(self._stall_seconds))


class FakeLLM:
    """
    [EN]
    Stub native model. It emits prefill progress callbacks (honouring ``should_abort``
    between chunks, exactly like the native loop), then text deltas.

    [ZH]
    桩原生模型。它发出预填充进度回调（像原生循环一样在块间查询 ``should_abort``），然后
    发出文本增量。
    """

    def __init__(self, chunks, total, tps=100.0, chunk_delay=0.08,
                 deltas=("甲", "乙"), delta_delay=0.06, abort_after=None,
                 raise_after=None, pause_after=None, pause_seconds=0.0,
                 stop_event=None):
        self.chunks = list(chunks)
        self.total = int(total)
        self.tps = float(tps)
        self.chunk_delay = float(chunk_delay)
        self.deltas = list(deltas)
        self.delta_delay = float(delta_delay)
        self.abort_after = abort_after
        # [EN] Raise the native abort right after chunk k, the way the prefill loop does
        # [EN] when ``should_abort`` fires at a chunk boundary.
        # [ZH] 在第 k 块之后直接抛原生中止异常，与预填充循环在块边界命中 ``should_abort``
        # [ZH] 时的行为一致。
        self.raise_after = raise_after
        self.pause_after = pause_after
        self.pause_seconds = float(pause_seconds)
        self.stop_event = stop_event
        self.progress_calls = 0
        self.saw_abort = False

    def create_chat_completion(self, **kwargs):
        progress = kwargs["on_prefill_progress"]
        should_abort = kwargs["should_abort"]

        def generator():
            done = 0
            for size in self.chunks:
                if should_abort():
                    self.saw_abort = True
                    raise NativeLlamaAborted("stop requested between chunks")
                time.sleep(self.chunk_delay)
                done = min(self.total, done + int(size))
                self.progress_calls += 1
                progress(done, self.total, done / max(self.tps, 1e-9), self.tps)
                if self.abort_after is not None and self.progress_calls >= self.abort_after:
                    # [EN] Simulate the user pressing stop mid-prefill.
                    # [ZH] 模拟用户在预填充中途按下停止。
                    if self.stop_event is not None:
                        self.stop_event.set()
                if self.raise_after is not None and self.progress_calls >= self.raise_after:
                    raise NativeLlamaAborted("aborted at a chunk boundary")
                if self.pause_after is not None and self.progress_calls >= self.pause_after:
                    # [EN] No further progress: let the watchdog observe the silence.
                    # [ZH] 不再有进展：让看门狗观察到这段静默。
                    deadline = time.monotonic() + self.pause_seconds
                    while time.monotonic() < deadline:
                        if should_abort():
                            self.saw_abort = True
                            raise NativeLlamaAborted("stopped while paused")
                        time.sleep(0.02)
                    self.pause_after = None
            for text in self.deltas:
                time.sleep(self.delta_delay)
                yield {"choices": [{"delta": {"content": text}}]}

        return generator()


def make_engine(llm, stall_seconds=45.0):
    """[EN] Engine without a model. [ZH] 不加载模型的引擎实例。"""
    engine = LLMEngine.__new__(LLMEngine)
    engine.llm = llm
    engine.inference = FakeInference(stall_seconds)
    engine.last_tps = 0.0
    engine.last_elapsed = 0.0
    engine.last_tokens = 0
    return engine


def drain(engine, stats, stop_event, heartbeat=0.05, messages=None, order=None):
    """
    [EN] Consume the raw stream, recording the stats seen at every heartbeat. When
    [EN] ``order`` is supplied, the literal sequence of events is appended to it
    [EN] (``"progress"`` / ``"delta"``) so ordering can be asserted.
    [ZH] 消费原始流，并记录每次心跳时看到的统计快照。传入 ``order`` 时，把事件的真实顺序
    [ZH] 追加进去（``"progress"`` / ``"delta"``），以便对顺序做断言。
    """
    heartbeats = []
    texts = []
    for piece in engine._chat_stream_raw(
            messages or [{"role": "user", "content": "请总结这份合同"}], 0.0, 16,
            stop_event, stats=stats, heartbeat_seconds=heartbeat):
        if piece is None:
            heartbeats.append(stats.to_dict())
            if order is not None:
                order.append("progress")
        else:
            texts.append(piece)
            if order is not None:
                order.append("delta")
    return heartbeats, texts


def main():
    # [EN] 0. The five new fields exist and are serialised.
    # [ZH] 0. 五个新字段存在且进入 to_dict。
    payload = StreamStats().to_dict()
    new_keys = ("prefill_tokens", "prefill_total", "prefill_tps", "eta_seconds", "stalled")
    check("StreamStats.to_dict 含 5 个新字段",
          all(key in payload for key in new_keys),
          [key for key in new_keys if key not in payload])

    # [EN] 1. Prefill progress is visible, monotonic and never exceeds the total.
    # [ZH] 1. 预填充进度可见、单调递增，且不超过总数。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(20, 20, 20, 20, 20), total=100, chunk_delay=0.08,
                  stop_event=stop_event)
    engine = make_engine(llm, stall_seconds=45.0)
    stats = StreamStats()
    heartbeats, _texts = drain(engine, stats, stop_event)
    series = [row["prefill_tokens"] for row in heartbeats]
    check("心跳期间 prefill_tokens 采样不少于 3 次", len(series) >= 3, series)
    check("心跳期间 prefill_tokens 单调不减",
          all(b >= a for a, b in zip(series, series[1:])), series)
    check("心跳期间 prefill_tokens 确实在增长",
          len(series) >= 2 and series[-1] > series[0], series)
    check("prefill_tokens 不超过 prefill_total",
          all(row["prefill_tokens"] <= row["prefill_total"] for row in heartbeats),
          series)
    check("预填充结束后 prefill_total 为 100", stats.prefill_total == 100,
          stats.prefill_total)

    # [EN] 2. Watchdog: the stream ends normally, stalls, and raises nothing.
    # [ZH] 2. 看门狗：流正常结束、标记 stalled，且不抛异常。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(10,), total=100, chunk_delay=0.02, deltas=("不该出现",),
                  pause_after=1, pause_seconds=5.0, stop_event=stop_event)
    engine = make_engine(llm, stall_seconds=0.25)
    stats = StreamStats()
    raised = None
    started = time.monotonic()
    try:
        heartbeats, texts = drain(engine, stats, stop_event, heartbeat=0.05)
    except BaseException as exc:  # noqa: BLE001 - the absence is the assertion
        raised = exc
        heartbeats, texts = [], []
    elapsed = time.monotonic() - started
    check("stall 时不抛异常", raised is None,
          f"{type(raised).__name__}: {raised}" if raised else "")
    check("stall 后 stalled=true", stats.stalled is True, stats.stalled)
    check("stall 时置了停止信号", stop_event.is_set())
    check("stall 在阈值附近结束（< 3s）", elapsed < 3.0, f"{elapsed:.2f}s")
    check("stall 后不再产出内容", texts == [], texts)
    check("stall 路径 finally 写入快照",
          stats.prefill_tokens == 10 and stats.prefill_total == 100,
          f"{stats.prefill_tokens}/{stats.prefill_total}")

    # [EN] 3. User abort during prefill: silent end, stopped=true, nothing raised.
    # [ZH] 3. 预填充期间用户中止：静默结束、stopped=true、不抛异常。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(20, 20, 20, 20), total=100, chunk_delay=0.05,
                  deltas=("不该出现",), abort_after=2, stop_event=stop_event)
    engine = make_engine(llm, stall_seconds=45.0)
    stats = StreamStats()
    raised = None
    try:
        heartbeats, texts = drain(engine, stats, stop_event, heartbeat=0.05)
    except BaseException as exc:  # noqa: BLE001
        raised = exc
        texts = ["<raised>"]
    check("中止时流正常结束，无异常", raised is None,
          f"{type(raised).__name__}: {raised}" if raised else "")
    check("中止时 stopped=true", stats.stopped is True, stats.stopped)
    check("中止时 stalled 保持 false", stats.stalled is False, stats.stalled)
    check("中止后不再产出内容", texts == [], texts)
    check("桩模型确实在块间观测到中止", llm.saw_abort is True)

    # [EN] 4. Decode-phase heartbeats do not regress: they still fire, and the prefill
    # [EN]    fields stay at their neutral values because no prefill ran.
    # [ZH] 4. decode 阶段心跳不回归：照常发出，且因没有预填充而保持中性值。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(), total=0, deltas=("甲", "乙", "丙"), delta_delay=0.15)
    engine = make_engine(llm, stall_seconds=45.0)
    stats = StreamStats()
    heartbeats, texts = drain(engine, stats, stop_event, heartbeat=0.05)
    check("decode 阶段仍有心跳", len(heartbeats) >= 1, len(heartbeats))
    check("decode 阶段心跳不伪造预填充进度",
          all(row["prefill_total"] == 0 and row["prefill_tokens"] == 0
              for row in heartbeats),
          [(row["prefill_tokens"], row["prefill_total"]) for row in heartbeats])
    check("decode 阶段仍产出全部内容", "".join(texts) == "甲乙丙", texts)
    check("decode 阶段 prefill_done 已置真", stats.prefill_done is True)

    # [EN] 5. ETA is derived from the measured rate: 75 remaining at 50 tok/s -> 1.5 s.
    # [ZH] 5. ETA 由实测速率反算：剩余 75、50 tok/s -> 1.5 秒。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(25, 75), total=100, tps=50.0, chunk_delay=0.02,
                  deltas=("甲",), pause_after=1, pause_seconds=0.4,
                  stop_event=stop_event)
    engine = make_engine(llm, stall_seconds=10.0)
    stats = StreamStats()
    heartbeats, _texts = drain(engine, stats, stop_event, heartbeat=0.05)
    etas = [row["eta_seconds"] for row in heartbeats]
    check("心跳可见 ETA=1.5（75 剩余 / 50 tok/s）",
          any(abs(value - 1.5) < 0.06 for value in etas), etas[:6])

    # [EN] 6. A non-positive rate must report 0 instead of a fabricated ETA.
    # [ZH] 6. 速率非正时必须上报 0，而不是编一个 ETA。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(25, 75), total=100, tps=0.0, chunk_delay=0.02,
                  deltas=("甲",), pause_after=1, pause_seconds=0.4,
                  stop_event=stop_event)
    engine = make_engine(llm, stall_seconds=10.0)
    stats = StreamStats()
    heartbeats, _texts = drain(engine, stats, stop_event, heartbeat=0.05)
    etas = [row["eta_seconds"] for row in heartbeats]
    check("tps<=0 时 ETA 为 0", etas and all(value == 0.0 for value in etas), etas[:6])

    # [EN] 7. Close-out: the final snapshot is published at the first token and the
    # [EN]    one-shot notice repaints the metrics row right there.
    # [ZH] 7. 收口：最终快照在首 token 时发布，并由一次性通知在当刻重绘指标行。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(50, 50), total=100, tps=200.0, chunk_delay=0.02,
                  deltas=("甲",), delta_delay=0.02)
    engine = make_engine(llm, stall_seconds=45.0)
    stats = StreamStats()
    # [EN] Periodic heartbeats deliberately disabled (100 s): every ``progress`` seen here
    # [EN] must therefore come from the one-shot notice, not from a timeout.
    # [ZH] 刻意关闭周期性心跳（100 秒）：这里出现的每条 ``progress`` 都只可能来自一次性
    # [ZH] 通知，而不是超时。
    order = []
    heartbeats, texts = drain(engine, stats, stop_event, heartbeat=100.0, order=order)
    check("预填充期间没有周期性心跳（快照来自首 token 分支）",
          len(heartbeats) == 1, len(heartbeats))
    check("首 token 后 prefill_done 为真", stats.prefill_done is True,
          stats.prefill_done)
    check("首 token 后 prefill_tokens 已写满 total",
          stats.prefill_tokens == 100 and stats.prefill_total == 100,
          f"{stats.prefill_tokens}/{stats.prefill_total}")
    check("首个 delta 前恰好一条 progress，顺序为先 progress 后 delta",
          order == ["progress", "delta"], order)
    check("该 progress 快照 prefill_done=true 且 tokens==total",
          heartbeats[0]["prefill_done"] is True
          and heartbeats[0]["prefill_tokens"] == 100
          and heartbeats[0]["prefill_total"] == 100,
          {k: heartbeats[0][k] for k in ("prefill_done", "prefill_tokens",
                                         "prefill_total")})

    # [EN] 9. No prefill (fully cached prompt): there is nothing to announce, so no
    # [EN]    extra ``progress`` may appear.
    # [ZH] 9. 无预填充（提示词整段命中缓存）：没有可播报的内容，因此不得多出 ``progress``。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(), total=0, deltas=("甲", "乙", "丙"), delta_delay=0.02)
    engine = make_engine(llm, stall_seconds=45.0)
    stats = StreamStats()
    order = []
    heartbeats, texts = drain(engine, stats, stop_event, heartbeat=100.0, order=order)
    check("无预填充时没有补充 progress", heartbeats == [], len(heartbeats))
    check("无预填充时事件序列全是 delta", order == ["delta", "delta", "delta"], order)

    # [EN] 10. N deltas must still map to exactly one notice (never per token).
    # [ZH] 10. 连续 N 个 delta 仍只对应一条通知（绝不逐 token 触发）。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(50, 50), total=100, tps=200.0, chunk_delay=0.02,
                  deltas=("一", "二", "三", "四", "五"), delta_delay=0.02)
    engine = make_engine(llm, stall_seconds=45.0)
    stats = StreamStats()
    order = []
    heartbeats, texts = drain(engine, stats, stop_event, heartbeat=100.0, order=order)
    check("5 个 delta 只对应 1 条 progress",
          order.count("progress") == 1 and len(order) == 6,
          f"progress={order.count('progress')} total={len(order)}")

    # [EN] 8. Abort path: the finally publishes the slot snapshot with no heartbeat at all.
    # [ZH] 8. 中止路径：finally 在完全没有心跳的情况下发布进度槽快照。
    stop_event = threading.Event()
    llm = FakeLLM(chunks=(50, 50), total=100, tps=200.0, chunk_delay=0.02,
                  deltas=("不该出现",), raise_after=1)
    engine = make_engine(llm, stall_seconds=45.0)
    stats = StreamStats()
    raised = None
    heartbeats = []
    try:
        heartbeats, texts = drain(engine, stats, stop_event, heartbeat=100.0)
    except BaseException as exc:  # noqa: BLE001
        raised = exc
        texts = ["<raised>"]
    check("中止路径不抛异常且无心跳",
          raised is None and heartbeats == [],
          f"{type(raised).__name__ if raised else 'ok'} / {len(heartbeats)}")
    check("中止路径 finally 写入部分快照",
          stats.prefill_tokens == 50 and stats.prefill_total == 100,
          f"{stats.prefill_tokens}/{stats.prefill_total}")
    check("中止路径 prefill_done 仍为假", stats.prefill_done is False,
          stats.prefill_done)

    total = PASSED + len(FAILED)
    print("-" * 42)
    print(f"PREFLIGHT_ENGINE {PASSED}/{total}")
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print(f"共 {PASSED} 项，全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
