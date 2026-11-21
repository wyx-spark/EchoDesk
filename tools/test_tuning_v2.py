# -*- coding: utf-8 -*-
"""
[EN]
Unit checks for the v2 calibrator (v0.2.7-reader step 1): three separated
measurements, the ordered acceptance criteria, the drift check, the two-level
search and the v2 configuration layer.

What is covered, one assertion group per requirement:

* **median, not maximum** -- repeats are aggregated with the median;
* **p95 veto** -- a candidate whose prefill wake deviation exceeds the gate loses;
* **decode veto** -- a prefill win that costs more than the decode budget loses;
* **drift void** -- a round whose baseline moved more than 3 % adopts nothing and
  writes nothing;
* **time budget** -- an overrun ends with ``reason=time_budget`` and writes nothing;
* **legacy configuration** -- a pre-v2 file with only ``prompt_tokens`` still works;
* **fingerprint** -- a stored tuning result is annotated ``stale`` when the machine
  or model no longer matches, and nothing re-runs automatically;
* **persist=False** -- the configuration file is byte-identical afterwards.

The model is a stub (no real weights, no native runtime): every test drives the
production code path with a controllable ``NativeLlama`` so a failure names the
criterion instead of hiding inside a real benchmark. There is no SKIP: every check
must run and pass.

[ZH]
v2 校准器的单元检查（v0.2.7-reader 步 1）：三项分离测量、有顺序的采纳判据、漂移自检、
两级寻优与 v2 配置层。

覆盖内容（每条要求一组断言）：

* **中位数而非最大值** —— 重复以中位数聚合；
* **p95 否决** —— 预填充唤醒偏差超过闸门的候选落败；
* **decode 否决** —— 预填充收益却让 decode 付出超额代价的候选落败；
* **漂移作废** —— 基线漂移超过 3% 的一轮不采纳、不写回；
* **时间预算** —— 超时以 ``reason=time_budget`` 收尾且不写回；
* **旧配置兼容** —— 只有 ``prompt_tokens`` 的 v2 之前文件仍可工作；
* **指纹** —— 机器或模型不再相符时已保存的调优结果被标注 ``stale``，且不自动重跑；
* **persist=False** —— 运行结束后配置文件逐字节不变。

模型使用桩（不加载真实权重、不进入原生运行时）：每个测试都用可控的 ``NativeLlama``
驱动生产代码路径，因此失败会指出是哪条判据而不是藏在一次真实基准里。没有 SKIP：每条
检查都必须执行并通过。

Run: ``python tools\\test_tuning_v2.py``. Exit code 0 means all passed.
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import inference_config  # noqa: E402
import inference_tuner  # noqa: E402
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


# [EN] ---------- Stub model ----------
# [ZH] ---------- 桩模型 ----------


class StubLlama:
    """
    [EN]
    Controllable stand-in for ``NativeLlama``. ``plan(index, kwargs)`` returns the
    numbers the next instance should report, so a test can give one configuration a
    specific prefill rate, decode delay or generated-token count. ``plan_prompt``, when
    set, is consulted per call with the prompt length as well -- that is how a test
    makes the long-text guard behave differently from the main rung.

    [ZH]
    ``NativeLlama`` 的可控替身。``plan(index, kwargs)`` 返回下一个实例应上报的数值，
    测试因此可以给某组配置指定的预填充速率、解码延迟或生成 token 数。设置 ``plan_prompt``
    后，每次调用还会带着 prompt 长度再问一次 —— 测试借此让长文本守卫与主判据档表现不同。
    """

    plan = staticmethod(lambda index, kwargs: {"prefill_tps": 100.0})
    plan_prompt = None
    index = 0

    def __init__(self, model_path=None, n_ctx=0, verbose=False, **kwargs):
        cls = type(self)
        self._index = cls.index
        cls.index += 1
        self._kwargs = dict(kwargs)
        self.n_threads = int(kwargs.get("n_threads") or 0)
        self.n_threads_batch = int(kwargs.get("n_threads_batch") or 0)
        self._apply(cls.plan(self._index, kwargs))

    def _apply(self, data):
        """[EN] Install one plan record. [ZH] 装载一条计划记录。"""
        self.tokens = int(data.get("tokens", 8))
        self.decode_delay = float(data.get("decode_delay", 0.0))
        seconds = float(data.get("prefill_seconds", 0.5))
        tokens = int(data.get("prefill_tokens", 4096))
        # [EN] ``last_prefill`` is T1's contract: the calibrator must read the prefill
        # [EN] rate from here rather than deriving it from the total runtime.
        # [ZH] ``last_prefill`` 是 T1 的契约：校准器必须从这里读取预填充速率，而不是由
        # [ZH] 总耗时反推。
        self.last_prefill = {"tokens": tokens, "seconds": max(0.001, seconds),
                             "tps": float(data["prefill_tps"])}

    def create_completion(self, prompt, max_tokens=128, temperature=0.0, stream=True,
                          echo=False, on_prefill_progress=None, **kwargs):
        resolver = type(self).plan_prompt
        if resolver is not None:
            self._apply(resolver(self._index, self._kwargs, len(prompt)))
        if callable(on_prefill_progress):
            on_prefill_progress(self.last_prefill["tokens"], self.last_prefill["tokens"],
                                self.last_prefill["seconds"], self.last_prefill["tps"])
        count = max(1, min(int(max_tokens), self.tokens))

        def generator():
            for position in range(count):
                if self.decode_delay > 0.0 and position > 0:
                    time.sleep(self.decode_delay)
                yield {"choices": [{"text": "x", "index": 0}]}

        return generator()

    def close(self):
        pass


def _install_stub():
    """[EN] Put the stub where ``_measure_once`` imports it from. [ZH] 把桩放到 ``_measure_once`` 的导入位置。"""
    module = types.ModuleType("llama_native")
    module.NativeLlama = StubLlama
    sys.modules["llama_native"] = module


class FakeWatcher:
    """[EN] Watcher stub with a fixed deviation list. [ZH] 偏差列表固定的监视线程桩。"""

    deviations = [3.0, 5.0, 7.0]

    def __init__(self, interval_s=None):
        pass

    def start(self):
        pass

    def stop(self):
        return list(type(self).deviations)

    @property
    def p95_ms(self):
        return inference_tuner._p95(self.deviations)


def _make_config(directory, tuning=None, **overrides):
    """
    [EN] Write a configuration file into ``directory`` and load it. [ZH] 在 ``directory`` 写一个配置文件并加载。
    """
    payload = {
        "n_threads": 4,
        "n_threads_batch": 0,
        "n_batch": 512,
        "n_ctx_default": 4096,
        "tuning": {
            "repeats": 3,
            "gen_tokens": 4,
            "max_trials": 8,
            "max_seconds": 900,
            "prompt_tokens_short": 64,
            "prompt_tokens_long": 96,
            "prompt_tokens_xlong": 128,
        },
    }
    if tuning:
        payload["tuning"].update(tuning)
    payload.update(overrides)
    path = os.path.join(directory, "inference_config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return InferenceConfig.load(path)


def _make_model(directory):
    """[EN] Empty ``.gguf`` placeholder: the tuner only requires an existing file. [ZH] 空的 ``.gguf`` 占位文件：调优器只要求路径存在。"""
    path = os.path.join(directory, "stub-model.gguf")
    with open(path, "wb") as handle:
        handle.write(b"GGUF")
    return path


def _run_tune(config, model_path, persist=True, watcher=None, deadline=None,
              guard_deadline=None, confirm_budget=None, forbid_save=False):
    """
    [EN] Drive ``iter_tune`` with the stub model and collect every event. [ZH] 用桩模型驱动 ``iter_tune`` 并收集全部事件。
    """
    events = []
    guard_patch = mock.patch.object(inference_tuner, "_inference_guard",
                                    lambda: threading.Lock())
    with guard_patch:
        patches = []
        if forbid_save:
            # [EN] Any write becomes a test failure: "nothing was written" is asserted
            # [EN] by making the write impossible, not by trusting the flag.
            # [ZH] 任何写入都变成测试失败："没有写回"是通过让写入不可能发生来断言的，
            # [ZH] 而不是信任一个标志位。
            patches.append(mock.patch.object(
                InferenceConfig, "save",
                side_effect=AssertionError("配置被写回了")))
        if watcher is not None:
            patches.append(mock.patch.object(inference_tuner, "_WakeWatcher", watcher))
        if deadline is not None:
            patches.append(mock.patch.object(inference_tuner, "_deadline_reached", deadline))
        if guard_deadline is not None:
            patches.append(mock.patch.object(inference_tuner, "_guard_deadline_reached",
                                             guard_deadline))
        if confirm_budget is not None:
            patches.append(mock.patch.object(inference_tuner, "_confirm_budget_available",
                                             confirm_budget))
        for patch in patches:
            patch.start()
        try:
            for event in inference_tuner.iter_tune(model_path, config=config,
                                                   persist=persist):
                events.append(event)
        finally:
            for patch in reversed(patches):
                patch.stop()
    report = None
    for event in events:
        inner = event.get("_report")
        if isinstance(inner, inference_tuner.TuneReport):
            report = inner
    return report, events


def _sha256(path):
    """[EN] File digest. [ZH] 文件摘要。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def test_median_not_maximum(directory):
    """
    [EN]
    Repeats are aggregated with the median. The stub reports 10 / 10 / 100 prefill
    tokens per second for the three baseline repeats: the result must be 10, not the
    maximum 100. It must also come from ``last_prefill`` (the stub's own number), not
    from the total runtime.

    [ZH]
    重复以中位数聚合。桩给三次基线重复上报 10 / 10 / 100 的预填充速率：结果必须是 10，
    而不是最大值 100；而且必须来自 ``last_prefill``（桩自己的数字），不由总耗时反推。
    """
    cfg = _make_config(directory, tuning={"repeats": 3})
    model = _make_model(directory)
    values = [10.0, 10.0, 100.0]

    def plan(index, kwargs):
        value = values[index % len(values)]
        return {"prefill_tps": value, "prefill_seconds": 1000.0 / max(value, 0.001),
                "prefill_tokens": 4096, "tokens": 6}

    StubLlama.index = 0
    StubLlama.plan = staticmethod(plan)
    report, _ = _run_tune(cfg, model, persist=False)
    baseline = report.baseline
    check("重复取中位数（10/10/100 -> 10，不是最大值 100）",
          baseline is not None and abs(baseline.prefill_tps - 10.0) < 1e-6,
          "" if baseline is None else baseline.prefill_tps)
    check("prefill_tps 取自 last_prefill，而非总耗时反推",
          baseline is not None and abs(baseline.prefill_tps - baseline.tps) > 1e-6,
          "" if baseline is None else f"prefill={baseline.prefill_tps} total={baseline.tps}")
    check("三次重复都记录在案",
          baseline is not None and baseline.repeats == 3,
          "" if baseline is None else baseline.repeats)


def test_p95_veto(directory):
    """
    [EN]
    A candidate whose prefill wake deviation p95 exceeds the gate is rejected with
    ``reason=p95``, even though its prefill rate is fine. The 50 ms default comes from
    the configuration, so the test also proves the policy is read from ``tuning``.

    [ZH]
    预填充唤醒偏差 p95 超过闸门的候选以 ``reason=p95`` 被否决，即使它的预填充速率没问题。
    50 ms 默认值来自配置，因此本测试同时证明策略确实从 ``tuning`` 读取。
    """
    cfg = _make_config(directory, tuning={"repeats": 2})
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 100.0,
                                                         "tokens": 6})
    FakeWatcher.deviations = [120.0, 130.0]
    report, _ = _run_tune(cfg, model, persist=False, watcher=FakeWatcher)
    rejected = [trial for trial in report.trials if not trial.ok
                or report.judgements.get(trial.label, {}).get("accepted") is False]
    check("p95 超限的候选全部落败",
          bool(report.trials) and len(rejected) == len(report.trials),
          f"trials={len(report.trials)} rejected={len(rejected)}")
    check("落败原因记为 p95",
          report.reason == "p95",
          report.reason)
    check("未采纳、未写回（persist=False）",
          report.adopted is False and report.persisted is False,
          f"adopted={report.adopted} persisted={report.persisted}")

    # [EN] Unit form of the same rule: the gate is inclusive at exactly the limit.
    # [ZH] 同一条规则的单元形式：恰好等于上限时视为通过。
    baseline = inference_tuner.TrialResult(n_threads=4, n_batch=512,
                                           prefill_tps=100.0, decode_tps=20.0)
    at_limit = inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=110.0,
                                           decode_tps=20.0, p95_ms=50.0, ok=True)
    over = inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=110.0,
                                       decode_tps=20.0, p95_ms=50.1, ok=True)
    check("p95 恰好等于 50ms 时通过",
          inference_tuner.evaluate_candidate(baseline, at_limit).accepted is True)
    check("p95 超过 50ms 时否决",
          inference_tuner.evaluate_candidate(baseline, over).reason == "p95")


def test_decode_veto(directory):
    """
    [EN]
    Response speed is a hard constraint: a candidate that improves prefill but makes
    decode more than 2 % slower is rejected with ``reason=decode_regression``.

    [ZH]
    响应速度是硬约束：预填充更快却让 decode 慢超过 2% 的候选以
    ``reason=decode_regression`` 被否决。
    """
    baseline = inference_tuner.TrialResult(n_threads=4, n_batch=512,
                                           prefill_tps=100.0, decode_tps=20.0)
    borderline = inference_tuner.TrialResult(n_threads=5, n_batch=512, prefill_tps=120.0,
                                             decode_tps=19.7, p95_ms=10.0, ok=True)
    too_slow = inference_tuner.TrialResult(n_threads=5, n_batch=512, prefill_tps=120.0,
                                           decode_tps=19.0, p95_ms=10.0, ok=True)
    verdict = inference_tuner.evaluate_candidate(baseline, too_slow)
    check("decode 退化 >2% 时否决", verdict.reason == "decode_regression",
          verdict.reason)
    check("decode 退化否决时 prefill 收益仍如实记录",
          abs(verdict.prefill_gain - 0.2) < 1e-9, verdict.prefill_gain)
    check("decode 退化 <=2% 时不否决（1.5%）",
          inference_tuner.evaluate_candidate(baseline, borderline).accepted is True,
          inference_tuner.evaluate_candidate(baseline, borderline).reason)
    check("prefill 提升不足 3% 时以 no_improvement 否决",
          inference_tuner.evaluate_candidate(
              baseline,
              inference_tuner.TrialResult(n_threads=5, n_batch=512, prefill_tps=102.0,
                                          decode_tps=20.0, p95_ms=1.0, ok=True)
          ).reason == "no_improvement")


def test_drift_void(directory):
    """
    [EN]
    The baseline is re-measured after the candidates; when it moved more than 3 % the
    whole round is void (``reason=drift``) and nothing is written, because a number
    the machine cannot reproduce must not become the shipped configuration.

    [ZH]
    候选测完后复测基线；漂移超过 3% 则整轮作废（``reason=drift``）且不写回，因为这台
    机器复现不出的数字绝不能用作出厂配置。
    """
    cfg = _make_config(directory, tuning={"repeats": 1, "max_trials": 4})
    model = _make_model(directory)
    before = _sha256(os.path.join(directory, "inference_config.json"))

    # [EN] A machine whose throughput decays with every model load: the retest is
    # [EN] necessarily slower than the baseline, so the drift gate must fire. The first
    # [EN] two loads (the noise probes) are identical so the noise gate stays quiet.
    # [ZH] 一台吞吐随每次模型加载而衰减的机器：复测必然慢于基线，因此漂移闸门必须触发。
    # [ZH] 前两次加载（噪声探测）刻意相同，使噪声闸门保持沉默。
    def plan(index, kwargs):
        return {"prefill_tps": 100.0 / (1.0 + max(0, index - 1)), "tokens": 4}

    StubLlama.index = 0
    StubLlama.plan = staticmethod(plan)
    report, events = _run_tune(cfg, model, persist=True, forbid_save=True)
    check("基线漂移 >3% 时整轮作废 reason=drift",
          report.reason == "drift", report.reason)
    check("漂移自检记录前后两次基线与比例",
          bool(report.drift)
          and report.drift.get("before_prefill_tps", 0) > 0
          and report.drift.get("ratio", 0) > inference_tuner.DRIFT_RATIO
          and report.drift.get("invalid") is True,
          report.drift)
    check("作废的一轮未采纳",
          report.adopted is False and report.persisted is False,
          f"adopted={report.adopted} persisted={report.persisted}")
    check("作废的一轮写回路径一次都没走（save 被禁用）",
          any(event.get("stage") == "aborted" and event.get("reason") == "drift"
              for event in events))
    check("作废的一轮配置文件字节不变",
          _sha256(os.path.join(directory, "inference_config.json")) == before)

    # [EN] The pure helper: a stable machine is not invalid.
    # [ZH] 纯函数形式：稳定的机器不算作废。
    stable = inference_tuner.baseline_drift(
        inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=100.0),
        inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=101.0))
    check("漂移 1% 时不算作废", stable["invalid"] is False, stable)


def test_time_budget(directory):
    """
    [EN]
    When the budget is spent the run closes with ``reason=time_budget`` on what was
    measured and writes nothing -- "the best so far" is not a verified optimum.

    [ZH]
    预算用尽时按已测数据以 ``reason=time_budget`` 收尾且不写回 —— "目前最好"不是被验证的
    最优。
    """
    cfg = _make_config(directory, tuning={"repeats": 1, "max_trials": 8})
    model = _make_model(directory)
    before = _sha256(os.path.join(directory, "inference_config.json"))
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 100.0,
                                                         "tokens": 4})
    report, events = _run_tune(cfg, model, persist=True, forbid_save=True,
                               deadline=lambda deadline: True)
    check("时间预算触发时 reason=time_budget",
          report.reason == "time_budget", report.reason)
    check("时间预算触发时不采纳未验证的最优",
          report.adopted is False and report.persisted is False,
          f"adopted={report.adopted} persisted={report.persisted}")
    check("时间预算触发时试探数为零（首轮即收尾）",
          len(report.trials) == 0, len(report.trials))
    check("时间预算触发时配置文件字节不变",
          _sha256(os.path.join(directory, "inference_config.json")) == before)
    check("时间预算事件带 aborted 阶段",
          any(event.get("stage") == "aborted"
              and event.get("reason") == "time_budget" for event in events))


def test_legacy_config_compatibility(directory=None):
    """
    [EN]
    A pre-v2 file only knows ``prompt_tokens``: it must behave as the short tier, the
    new keys must default, and illegal values must be clamped instead of raising.

    [ZH]
    v2 之前的文件只知道 ``prompt_tokens``：它必须作为短档生效，新键取默认，非法值被钳制
    而不是抛异常。
    """
    legacy = InferenceConfig.from_dict({"tuning": {"prompt_tokens": 1024}})
    policy = legacy.resolve_tuning()
    check("旧键 prompt_tokens 兼容为短档", policy["prompt_tokens_short"] == 1024,
          policy["prompt_tokens_short"])
    check("旧配置缺新键时取文档默认值",
          legacy.tuning_repeats() == 5
          and abs(legacy.tuning_ratio("min_improvement_ratio", 0.0) - 0.03) < 1e-9
          and abs(legacy.tuning_ratio("decode_regression_ratio", 0.0) - 0.02) < 1e-9
          and abs(legacy.tuning_p95_ms() - 50.0) < 1e-9
          and legacy.tuning_max_seconds() == 900
          and legacy.tuning_prompt_tokens("long") == 4096
          and legacy.tuning_prompt_tokens("xlong") == 16384,
          policy)
    check("旧键不覆盖显式的短档键",
          InferenceConfig.from_dict(
              {"tuning": {"prompt_tokens": 256, "prompt_tokens_short": 512}}
          ).tuning_prompt_tokens("short") == 512)
    illegal = InferenceConfig.from_dict({"tuning": {
        "repeats": "abc", "min_improvement_ratio": "x", "responsiveness_p95_ms": None,
        "decode_regression_ratio": -1, "max_seconds": "nope",
        "prompt_tokens_long": [], "prompt_tokens_xlong": 10 ** 9,
    }})
    check("非法值钳制且不抛异常",
          illegal.tuning_repeats() == 5
          and abs(illegal.tuning_ratio("decode_regression_ratio", 0.02) - 0.0) < 1e-9
          and illegal.tuning_max_seconds() == 900
          and illegal.tuning_prompt_tokens("long") == 4096
          and illegal.tuning_prompt_tokens("xlong") == inference_config.TUNING_PROMPT_MAX,
          illegal.resolve_tuning())
    check("describe 暴露归一化后的 v2 策略",
          "tuning_resolved" in InferenceConfig.from_dict({}).describe()
          and "tuned_stale" in InferenceConfig.from_dict({}).describe())


def test_fingerprint_stale(directory):
    """
    [EN]
    A tuning result carries the fingerprint it was measured on. When the machine or the
    model no longer matches, ``describe()['tuned_stale']`` is true and the differing
    keys are named -- and nothing re-runs the tuner (the stored numbers stay put).

    [ZH]
    调优结果携带它被测得时的指纹。机器或模型不再相符时 ``describe()['tuned_stale']``
    为真并列出差异键 —— 而且绝不重跑调优（已存数字保持原样）。
    """
    cfg = _make_config(directory, tuning={"repeats": 1, "max_trials": 2})
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 100.0,
                                                         "tokens": 4})
    report, _ = _run_tune(cfg, model, persist=False)
    fingerprint = cfg.tuned_fingerprint
    tuned_at = cfg.tuned_at
    threads = cfg.n_threads
    check("调优结果写入完整指纹",
          isinstance(fingerprint, dict)
          and set(inference_config.FINGERPRINT_KEYS).issubset(set(fingerprint))
          and fingerprint.get("model") == os.path.basename(model),
          fingerprint)
    check("同一台机器上不标 stale",
          cfg.describe(model)["tuned_stale"] is False,
          cfg.describe(model).get("tuned_fingerprint_diff"))

    current = inference_config.hardware_fingerprint(model)
    changed = dict(current)
    changed["physical_cores"] = int(current.get("physical_cores") or 0) + 8
    changed["cpu_brand"] = "a-different-machine"
    with mock.patch.object(inference_config, "hardware_fingerprint",
                           lambda *args, **kwargs: changed):
        snapshot = cfg.describe(model)
    check("指纹不符时标 stale", snapshot["tuned_stale"] is True, snapshot["tuned_stale"])
    check("差异项被点名",
          "physical_cores" in snapshot["tuned_fingerprint_diff"]
          and "cpu_brand" in snapshot["tuned_fingerprint_diff"],
          snapshot["tuned_fingerprint_diff"])
    check("指纹不符不自动重调（线程数与调优时间都未变）",
          cfg.n_threads == threads and cfg.tuned_at == tuned_at
          and cfg.tuned_fingerprint.get("physical_cores")
          == fingerprint.get("physical_cores"),
          f"n_threads={cfg.n_threads} tuned_at={cfg.tuned_at}")
    check("stored 指纹为空时不误报 stale",
          InferenceConfig.from_dict({"tuned_fingerprint": {}}).tuned_is_stale() is False)


def test_persist_false_zero_write(directory):
    """
    [EN]
    ``persist=False`` is the rehearsal contract: the file is byte-identical before and
    after a full calibration run, and the report says nothing was persisted.

    [ZH]
    ``persist=False`` 是演练的契约：完整校准前后文件逐字节相同，报告也如实说明未写回。
    """
    cfg = _make_config(directory, tuning={"repeats": 2, "max_trials": 4})
    model = _make_model(directory)
    path = os.path.join(directory, "inference_config.json")
    before = _sha256(path)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 100.0 + index,
                                                         "tokens": 4})
    report, events = _run_tune(cfg, model, persist=False, forbid_save=True)
    check("persist=False 时文件零改动",
          _sha256(path) == before, _sha256(path))
    check("persist=False 时报告如实标注",
          report.persisted is False, report.persisted)
    check("persist=False 时不产生 persisted 事件",
          not any(event.get("stage") == "persisted" for event in events))
    check("校准确实执行了基线、候选与漂移自检",
          report.baseline is not None
          and len(report.trials) > 0
          and bool(report.drift) and bool(report.baseline_retest),
          f"trials={len(report.trials)} drift={report.drift}")


def test_two_level_search(directory):
    """
    [EN]
    The search changes one factor at a time: level 1 varies only the decode thread
    count (prefill fixed at its automatic value), level 2 varies only the prefill
    thread count around the level-1 winner. The 16384-token workload is measured for
    the finalists only.

    [ZH]
    寻优一次只变一个因子：一级只变 decode 线程（prefill 固定自动值），二级围绕一级胜者
    只变 prefill 线程。16384 token 负载只对最终候选测量。
    """
    cfg = _make_config(directory, tuning={"repeats": 1, "max_trials": 8,
                                          "min_improvement_ratio": 0.0})
    model = _make_model(directory)
    # [EN] Captured before the run: a winning candidate rewrites ``cfg.n_threads``.
    # [ZH] 在运行前取好：胜出的候选会改写 ``cfg.n_threads``。
    auto_batch = cfg.resolve_n_threads_batch()
    # [EN] Decode threads 5 is fastest, so it wins level 1 and level 2 keeps it fixed.
    # [ZH] decode 线程 5 最快，因此拿下第一级，第二级固定它。
    def plan(index, kwargs):
        threads = int(kwargs.get("n_threads") or 0)
        return {"prefill_tps": 100.0 + (10.0 if threads == 5 else 0.0), "tokens": 4}

    StubLlama.index = 0
    StubLlama.plan = staticmethod(plan)
    report, events = _run_tune(cfg, model, persist=False)
    levels = [event.get("level") for event in events if event.get("stage") == "trial"]
    check("两级寻优都有试探", "decode" in levels and "prefill" in levels, levels)
    level1 = [event["trial"] for event in events
              if event.get("stage") == "trial" and event.get("level") == "decode"]
    check("一级只变 decode 线程（prefill 固定自动值）",
          bool(level1) and all(trial["n_threads_batch"] == auto_batch for trial in level1),
          [(trial["n_threads"], trial["n_threads_batch"]) for trial in level1])
    level2 = [event["trial"] for event in events
              if event.get("stage") == "trial" and event.get("level") == "prefill"]
    check("二级固定一级胜者的 decode 线程",
          bool(level2) and all(trial["n_threads"] == 5 for trial in level2),
          [(t["n_threads"], t["n_threads_batch"]) for t in level2])
    check("二级只变 prefill 线程",
          len({trial["n_threads_batch"] for trial in level2}) > 1,
          [trial["n_threads_batch"] for trial in level2])
    check("本轮漂移自检通过（未被作废）",
          report.drift.get("invalid") is False, report.drift)
    xlong_events = [event for event in events if event.get("stage") == "xlong"]
    check("16384 只对最终 2 个候选复测",
          len(xlong_events) <= 2, len(xlong_events))
    check("trial 数受 max_trials 约束",
          len(report.trials) <= cfg.tuning_max_trials(), len(report.trials))


def test_rung_selection(directory=None):
    """
    [EN]
    Algorithm 1-2: the main rung is the largest of ``{512, 4096, 16384}`` whose
    estimated round fits into ``2 * primary_target_seconds``. Fast / medium / slow
    probes are checked one by one, plus the "even 512 does not fit" fallback.

    [ZH]
    算法 1–2：主档取 ``{512, 4096, 16384}`` 中"估算单轮时长不超过
    ``2 * primary_target_seconds``"的最大档。快 / 中 / 慢三种探测量各验一例，另加
    "连 512 都放不下"的回退。
    """
    rungs = [512, 4096, 16384]
    fast = inference_tuner.select_rung(840.0, 84.7, 128, rungs, 10.0)
    check("快机（840 tps）选 4096 档：16384 估算 21.0s > 20s 上限",
          fast["selected_rung"] == 4096
          and fast["limit_seconds"] == 20.0
          and fast["estimates_seconds"]["16384"] > 20.0
          and fast["estimates_seconds"]["4096"] <= 20.0,
          fast)
    check("选档报告含探测量、各档估算与理由",
          fast["probe_tps"] == 840.0
          and fast["reason"] == "estimate_within_2x_target"
          and set(fast["estimates_seconds"]) == {"512", "4096", "16384"},
          fast)
    medium = inference_tuner.select_rung(230.0, 52.3, 128, rungs, 10.0)
    check("中速机（230 tps）选 512 档：4096 估算 20.3s > 20s 上限",
          medium["selected_rung"] == 512
          and medium["estimates_seconds"]["4096"] > 20.0,
          medium)
    slow = inference_tuner.select_rung(85.0, 28.0, 128, rungs, 10.0)
    check("慢机（85 tps）选 512 档",
          slow["selected_rung"] == 512, slow)
    check("估算 = 档位/probe_tps + gen_tokens/probe_decode_tps",
          abs(slow["estimates_seconds"]["512"] - (512 / 85.0 + 128 / 28.0)) < 1e-3,
          slow["estimates_seconds"]["512"])
    tiny = inference_tuner.select_rung(20.0, 5.0, 128, rungs, 10.0)
    check("连 512 都超限时仍用 512，并标 fallback",
          tiny["selected_rung"] == 512
          and tiny["reason"] == "fallback_smallest_rung",
          tiny)


def test_repeats_policy(directory):
    """
    [EN]
    Algorithm 3: ``repeats = clamp(floor(remaining / (candidates * round)), 3, 5)``.
    The floor of three is never negotiated -- a tiny budget yields three repeats and a
    ``time_budget`` ending, never two repeats and a fake pass.

    [ZH]
    算法 3：``repeats = clamp(floor(剩余预算 / (候选数 × 单轮耗时)), 3, 5)``。
    下限 3 绝不可谈 —— 预算极小只会得到三次重复与 ``time_budget`` 收尾，绝不是两次重复
    加一个假的通过。
    """
    starved = inference_tuner.derive_repeats(0.0, 8, 100.0)
    check("预算为零时仍 >=3", starved["value"] == 3, starved)
    leftover = inference_tuner.derive_repeats(30.0, 8, 100.0)
    check("预算远远不够时仍 >=3", leftover["value"] == 3, leftover)
    rich = inference_tuner.derive_repeats(895.0, 7, 6.4)
    check("预算充足时取到上限 5", rich["value"] == 5, rich)
    capped = inference_tuner.derive_repeats(895.0, 7, 6.4, maximum=4)
    check("配置上限 4 时取 4", capped["value"] == 4, capped)
    floored = inference_tuner.derive_repeats(895.0, 7, 6.4, maximum=1)
    check("配置上限低于 3 时下限仍为 3", floored["value"] == 3, floored)

    cfg = _make_config(directory, tuning={"repeats": 4})
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 500.0,
                                                         "tokens": 4})
    report, events = _run_tune(cfg, model, persist=False)
    check("报告含 repeats 推导依据",
          report.repeats_policy.get("value") == report.baseline.repeats
          and report.repeats_policy.get("candidates", 0) > 0
          and report.repeats_policy.get("round_seconds", 0) > 0,
          report.repeats_policy)
    check("实际 repeats 受配置上限约束",
          report.baseline.repeats == 4, report.baseline.repeats)
    check("repeats 事件带上报",
          any(event.get("stage") == "repeats" for event in events))


def _guard_config(directory, **overrides):
    """[EN] Config with the real three tiers, so 4096 / 8192 can actually be selected. [ZH] 用真实三档的配置，才可能选中 4096 / 8192。"""
    tuning = {
        "repeats": 5,
        "gen_tokens": 128,
        "max_trials": 8,
        "prompt_tokens_short": 512,
        "prompt_tokens_long": 4096,
        "prompt_tokens_xlong": 16384,
        "min_improvement_ratio": 0.0,
        "long_guard_ratio": 0.02,
        "long_guard_seconds": 300,
    }
    tuning.update(overrides)
    return _make_config(directory, tuning=tuning)


def _fast_profile(index, kwargs):
    """
    [EN]
    840 tps, plus 5 % for decode threads 5. The baseline (4 threads) decodes slower on
    purpose: a candidate that is clearly faster at decode keeps the decode veto from
    firing on stub timing noise, so these tests exercise the guard, not the clock.

    [ZH]
    840 tps，decode 线程 5 时 +5%。基线（4 线程）刻意解码更慢：明显更快的候选可以避免
    decode 否决因桩的计时噪声误触发，使这些测试检验的是守卫而不是时钟。
    """
    threads = int(kwargs.get("n_threads") or 0)
    return {"prefill_tps": 882.0 if threads == 5 else 840.0, "tokens": 4,
            "decode_delay": 0.01 if threads == 5 else 0.03}


def test_long_guard_veto(directory):
    """
    [EN]
    Algorithm 4-5: the winner is measured once at ``min(2 * main rung, 16384)``. A
    prefill regression beyond ``long_guard_ratio`` there vetoes it -- the "fast on
    short prompts, slow on long ones" case the guard exists for. The veto writes
    nothing, even though a winning candidate was found on the main rung.

    [ZH]
    算法 4–5：胜者在 ``min(2 × 主档, 16384)`` 上测一次。该档预填充退化超过
    ``long_guard_ratio`` 即否决 —— 正是守卫存在的"短负载快、长负载慢"情形。即使主判据档
    已选出胜者，否决也什么都不写。
    """
    cfg = _guard_config(directory)
    model = _make_model(directory)
    path = os.path.join(directory, "inference_config.json")
    before = _sha256(path)
    events = []
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)

    def guard_profile(index, kwargs, prompt_chars):
        threads = int(kwargs.get("n_threads") or 0)
        if prompt_chars > 20000 and threads == 5:
            # [EN] The winner is 4.8 % slower at the guard rung.
            # [ZH] 胜者在守卫档慢 4.8%。
            return {"prefill_tps": 800.0, "tokens": 4, "decode_delay": 0.02}
        return _fast_profile(index, kwargs)

    StubLlama.plan_prompt = staticmethod(guard_profile)
    report, events = _run_tune(cfg, model, persist=True, forbid_save=True)
    guard = report.long_guard
    check("探测选中 4096 主档", report.rung.get("selected_rung") == 4096,
          report.rung)
    check("守卫档 = min(2×主档, 16384) = 8192", guard.get("rung") == 8192, guard.get("rung"))
    check("守卫退化 >2% 时否决该候选",
          guard.get("status") == "vetoed" and guard.get("regression") > 0.02,
          guard)
    check("守卫否决后整轮不采纳（reason=long_guard）",
          report.reason == "long_guard" and report.adopted is False,
          f"{report.reason}/{report.adopted}")
    check("守卫否决后不写回（save 被禁用）且文件字节不变",
          _sha256(path) == before)
    check("守卫否决事件可见",
          any(event.get("stage") == "aborted"
              and event.get("reason") == "long_guard" for event in events))
    check("守卫测量了两端（基线 840 / 候选 800）",
          guard.get("baseline", {}).get("prefill_tps") == 840.0
          and guard.get("candidate", {}).get("prefill_tps") == 800.0,
          {key: guard.get(key) for key in ("baseline", "candidate")})


def test_long_guard_skipped(directory):
    """
    [EN]
    Algorithm 4: when the guard's own sub-budget cannot host the candidate run, the
    status is ``skipped_budget`` and the adoption carries ``long_guard_skipped``.
    "Not tested" is never reported as "passed".

    [ZH]
    算法 4：守卫自己的子预算装不下候选那次运行时，状态为 ``skipped_budget``，采纳结论带
    ``long_guard_skipped``。"没测"绝不会被报成"通过"。
    """
    cfg = _guard_config(directory)
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)
    StubLlama.plan_prompt = staticmethod(
        lambda index, kwargs, prompt_chars: _fast_profile(index, kwargs))
    report, events = _run_tune(cfg, model, persist=False,
                               guard_deadline=lambda deadline: True)
    guard = report.long_guard
    check("子预算不足时标记 skipped_budget", guard.get("status") == "skipped_budget",
          guard)
    check("跳过时采纳结论带 long_guard_skipped=true",
          report.long_guard_skipped is True
          and report.adopted is True
          and report.reason == "improved",
          f"skipped={report.long_guard_skipped} adopted={report.adopted} "
          f"reason={report.reason}")
    check("跳过时不把未测写成通过（守卫两端均为空）",
          guard.get("baseline") == {} and guard.get("candidate") == {
          } and "vetoed" not in str(guard.get("status")),
          guard)
    check("persisted 事件携带 long_guard_skipped 与状态",
          any(event.get("stage") == "persisted"
              and event.get("long_guard_skipped") is True
              and event.get("long_guard_status") == "skipped_budget"
              for event in events),
          [event.get("stage") for event in events])


def test_all_candidates_rejected(directory):
    """
    [EN]
    When every candidate loses its criterion the round reports ``no_improvement``, the
    long-text guard is ``not_applicable`` (there is no candidate to guard), and nothing
    is written.

    [ZH]
    当所有候选都落败时，整轮报告 ``no_improvement``，长文本守卫为 ``not_applicable``
    （没有候选可守卫），并且什么都不写。
    """
    cfg = _make_config(directory, tuning={"repeats": 3, "max_trials": 4,
                                          "min_improvement_ratio": 0.5})
    model = _make_model(directory)
    path = os.path.join(directory, "inference_config.json")
    before = _sha256(path)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 100.0,
                                                         "tokens": 4})
    report, events = _run_tune(cfg, model, persist=False, forbid_save=True)
    check("全部候选被否时 reason=no_improvement",
          report.reason == "no_improvement", report.reason)
    check("全部被否时不采纳、不写回",
          report.adopted is False and report.persisted is False
          and _sha256(path) == before,
          f"adopted={report.adopted} persisted={report.persisted}")
    check("无候选可守卫时 long_guard=not_applicable",
          report.long_guard.get("status") == "not_applicable",
          report.long_guard)
    check("全部被否时没有 persisted 事件",
          not any(event.get("stage") == "persisted" for event in events))


def test_guard_second_candidate(directory):
    """
    [EN]
    The guard measures both finalists (one run each). When the first-ranked candidate
    is vetoed at the guard rung but the second passes, the second is adopted and the
    report carries ``guard_vetoed_first=true`` -- a guard veto on the winner must not
    throw away a runner-up that is fine on long text.

    [ZH]
    守卫对最终两个候选各测一次。若第一名在守卫档被否决而第二名通过，则采纳第二名，并在
    报告中记录 ``guard_vetoed_first=true`` —— 胜者被守卫否决，不应该连带丢掉一个长文本
    表现正常的次优候选。
    """
    cfg = _guard_config(directory)
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)

    def guard_profile(index, kwargs, prompt_chars):
        threads = int(kwargs.get("n_threads") or 0)
        batch_threads = int(kwargs.get("n_threads_batch") or 0)
        if prompt_chars > 20000 and threads == 5:
            if batch_threads == 2:
                # [EN] First finalist: 4.8 % slower at the guard rung -> vetoed.
                # [ZH] 第一名：守卫档慢 4.8% → 否决。
                return {"prefill_tps": 800.0, "tokens": 4, "decode_delay": 0.02}
            # [EN] Second finalist: identical to the baseline -> passes.
            # [ZH] 第二名：与基线持平 → 通过。
            return {"prefill_tps": 840.0, "tokens": 4, "decode_delay": 0.02}
        return _fast_profile(index, kwargs)

    StubLlama.plan_prompt = staticmethod(guard_profile)
    report, events = _run_tune(cfg, model, persist=False)
    guard = report.long_guard
    labels = [item["label"] for item in guard.get("candidates", [])]
    check("守卫对最终两个候选各测一次",
          len(labels) == 2, labels)
    check("第一名被守卫否决、第二名通过 -> 采纳第二名",
          guard.get("status") == "passed"
          and guard.get("selected_label") == labels[1]
          and report.best.label == labels[1],
          f"status={guard.get('status')} selected={guard.get('selected_label')} "
          f"best={report.best.label if report.best else None}")
    check("记录 guard_vetoed_first=true",
          guard.get("guard_vetoed_first") is True,
          guard.get("guard_vetoed_first"))
    check("第一名 guard_delta 为负、第二名非负",
          guard["candidates"][0]["guard_delta"] < 0.0
          and guard["candidates"][1]["guard_delta"] >= 0.0,
          [item["guard_delta"] for item in guard["candidates"]])
    check("采纳第二名且报告 applied/scope 一致",
          report.adopted is True and report.applied is True
          and report.tuned_scope == "long" and cfg.n_threads == 5
          and cfg.n_threads_batch == 1,
          f"adopted={report.adopted} applied={report.applied} "
          f"scope={report.tuned_scope} threads={cfg.n_threads}/{cfg.n_threads_batch}")
    check("persisted 事件带 guard_vetoed_first",
          any(event.get("stage") == "persisted"
              and event.get("guard_vetoed_first") is True for event in events))


def test_guard_both_vetoed(directory):
    """
    [EN]
    When both finalists fail the guard the round is over: ``reason=long_guard``, no
    adoption, nothing written.
    [ZH]
    两个最终候选都在守卫档落败时整轮结束：``reason=long_guard``，不采纳，不写回。
    """
    cfg = _guard_config(directory)
    model = _make_model(directory)
    path = os.path.join(directory, "inference_config.json")
    before = _sha256(path)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)
    StubLlama.plan_prompt = staticmethod(
        lambda index, kwargs, prompt_chars: (
            {"prefill_tps": 780.0, "tokens": 4, "decode_delay": 0.02}
            if prompt_chars > 20000 and int(kwargs.get("n_threads") or 0) == 5
            else _fast_profile(index, kwargs)))
    report, events = _run_tune(cfg, model, persist=True, forbid_save=True)
    guard = report.long_guard
    check("两个候选都被守卫否决时 status=vetoed",
          guard.get("status") == "vetoed"
          and len(guard.get("candidates", [])) == 2
          and all(item["vetoed"] for item in guard["candidates"]),
          guard.get("candidates"))
    check("两个都失败 -> reason=long_guard 且不采纳",
          report.reason == "long_guard" and report.adopted is False
          and report.persisted is False,
          f"{report.reason}/{report.adopted}/{report.persisted}")
    check("两候选失败时不写回（save 被禁用）且文件不变",
          _sha256(path) == before)
    check("guard_delta 为负、regression 为正（符号含义相反且显式）",
          guard.get("guard_delta") is not None and guard["guard_delta"] < 0.0
          and guard.get("regression") is not None and guard["regression"] > 0.0
          and abs(guard["guard_delta"] / 100.0 + guard["regression"]) < 1e-6,
          f"guard_delta={guard.get('guard_delta')} regression={guard.get('regression')}")


def test_tuned_scope(directory):
    """
    [EN]
    ``tuned_scope`` is ``long`` exactly when the guard rung reaches
    ``tuning.long_scope_min_rung`` (default 8192). A short-scope result may be recorded
    but must not rewrite the thread counts; a long-scope one may.

    [ZH]
    守卫档达到 ``tuning.long_scope_min_rung``（默认 8192）时 ``tuned_scope`` 才是
    ``long``。短文本范围的结果可以记录，但不得改写线程数；长文本范围的可以。
    """
    check("8192 守卫档 -> long",
          inference_config.scope_for_rung(8192, 8192) == "long")
    check("4096 守卫档 -> short",
          inference_config.scope_for_rung(4096, 8192) == "short")
    check("1024 守卫档 -> short",
          inference_config.scope_for_rung(1024, 8192) == "short")
    check("未知 tuned_scope 归一化为空",
          InferenceConfig.from_dict({"tuned_scope": "gigantic"}).tuned_scope == "")
    check("describe() 暴露 tuned_scope",
          "tuned_scope" in InferenceConfig.from_dict({"tuned_scope": "long"}).describe())

    # [EN] Short scope (tiny tiers -> guard rung 256): recorded, not applied.
    # [ZH] 短文本范围（小档位 -> 守卫档 256）：记录但不生效。
    short_dir = os.path.join(directory, "short")
    os.makedirs(short_dir, exist_ok=True)
    short_cfg = _make_config(short_dir, tuning={"repeats": 3})
    short_model = _make_model(short_dir)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)
    report, events = _run_tune(short_cfg, short_model, persist=False)
    check("短文本范围：tuned_scope=short 且 applied=false",
          report.tuned_scope == "short" and report.applied is False,
          f"{report.tuned_scope}/{report.applied}")
    check("短文本范围：线程数未被改写（保持 4/2）",
          short_cfg.n_threads == 4,
          short_cfg.n_threads)
    check("短文本范围：tuned_scope 写入配置快照",
          short_cfg.tuned_scope == "short", short_cfg.tuned_scope)

    long_dir = os.path.join(directory, "long")
    os.makedirs(long_dir, exist_ok=True)
    long_cfg = _guard_config(long_dir)
    long_model = _make_model(long_dir)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)
    StubLlama.plan_prompt = staticmethod(
        lambda index, kwargs, prompt_chars: _fast_profile(index, kwargs))
    report, _ = _run_tune(long_cfg, long_model, persist=False)
    check("长文本范围：tuned_scope=long 且 applied=true",
          report.tuned_scope == "long" and report.applied is True,
          f"{report.tuned_scope}/{report.applied}")
    check("长文本范围：线程数被改写为胜者 (5/2)",
          long_cfg.n_threads == 5 and long_cfg.n_threads_batch == 2,
          f"{long_cfg.n_threads}/{long_cfg.n_threads_batch}")


def test_decode_drift_unreliable(directory):
    """
    [EN]
    Decode is the noisy quantity: when the baseline's own ``decode_tps`` moves more than
    3 % between the start and the end of the round, the per-candidate decode deltas are
    artefacts, so the round reports ``decode_deltas_unreliable=true`` and stops using
    them as a veto. The control case (no drift) keeps the veto.

    [ZH]
    decode 是噪声大户：基线自己的 ``decode_tps`` 在轮初与轮末之间漂移超过 3% 时，各候选的
    decode 差值就是假象，因此本轮报告 ``decode_deltas_unreliable=true`` 并停止用它否决。
    对照情形（无漂移）维持否决。
    """
    cfg = _make_config(directory, tuning={"repeats": 3, "gen_tokens": 8,
                                          "max_trials": 4,
                                          "min_improvement_ratio": 0.0})
    model = _make_model(directory)

    def plan(index, kwargs):
        threads = int(kwargs.get("n_threads") or 0)
        if threads == 5:
            return {"prefill_tps": 880.0, "tokens": 4, "decode_delay": 0.04}
        return {"prefill_tps": 800.0, "tokens": 4, "decode_delay": 0.01}

    StubLlama.index = 0
    StubLlama.plan = staticmethod(plan)
    StubLlama.plan_prompt = None

    real_measure = inference_tuner._measure_once

    def wrapped(*args, **kwargs):
        result = real_measure(*args, **kwargs)
        label = kwargs.get("label")
        if label is None and len(args) > 7:
            label = args[7]
        if label == "baseline_decode_retest":
            # [EN] Force a decode drift far beyond 3 %.
            # [ZH] 强制制造远超 3% 的 decode 漂移。
            result.decode_tps = float(result.decode_tps) * 0.5
        return result

    with mock.patch.object(inference_tuner, "_measure_once", wrapped):
        report, events = _run_tune(cfg, model, persist=False)
    check("decode 漂移超限时报告 decode_deltas_unreliable=true",
          report.decode_deltas_unreliable is True
          and report.decode_drift.get("ratio", 0) > 0.03,
          report.decode_drift)
    check("decode 漂移超限时不再用 decode 退化否决",
          any(judgement.get("accepted") is True
              and judgement.get("decode_veto_applied") is False
              and judgement.get("decode_delta") < -0.02
              for judgement in report.judgements.values()),
          report.judgements)
    check("漂移超限时仍可采纳（guard 通过）",
          report.adopted is True, report.adopted)
    # [EN] Control: identical numbers, no drift -> the veto still fires.
    # [ZH] 对照：同样的数值、无漂移 -> 否决仍然生效。
    baseline = inference_tuner.TrialResult(n_threads=4, n_batch=512,
                                           prefill_tps=800.0, decode_tps=100.0)
    candidate = inference_tuner.TrialResult(n_threads=5, n_batch=512,
                                            prefill_tps=880.0, decode_tps=25.0,
                                            p95_ms=5.0, ok=True)
    check("对照：无漂移时 decode 退化仍被否决",
          inference_tuner.evaluate_candidate(baseline, candidate).reason
          == "decode_regression")
    check("对照：显式关闭否决时同一候选通过",
          inference_tuner.evaluate_candidate(
              baseline, candidate, apply_decode_veto=False).accepted is True)


def _spy_labels():
    """
    [EN]
    Patch ``_measure_once`` with a recorder that returns the real result, so a test can
    prove how many runs of which kind happened (the "no extra baseline" requirement).

    [ZH]
    用记录器包住 ``_measure_once`` 并返回真实结果，测试因此能证明跑了几次、分别是什么
    （"没有额外基线开销"这条要求）。
    """
    real = inference_tuner._measure_once
    labels = []

    def spy(*args, **kwargs):
        label = kwargs.get("label")
        if label is None and len(args) > 7:
            label = args[7]
        labels.append(label)
        return real(*args, **kwargs)

    return spy, labels


def test_noise_probe_stable(directory):
    """
    [EN]
    Two identical probes -> the run continues, and the first probe is reused for rung
    selection: exactly two short runs, one baseline, and no third short baseline.

    [ZH]
    两次一致的探测 -> 继续运行，且第一次探测被复用于选档：恰好两次短跑、一次基线，没有
    第三次短基线。
    """
    cfg = _make_config(directory, tuning={"repeats": 3, "max_trials": 4})
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 500.0,
                                                         "tokens": 4})
    spy, labels = _spy_labels()
    with mock.patch.object(inference_tuner, "_measure_once", spy):
        report, events = _run_tune(cfg, model, persist=False)
    noise = report.noise_probe
    check("两次探测一致 -> noise_probe.unstable=false",
          noise.get("unstable") is False, noise)
    check("prefill 相对差为 0", noise.get("prefill_ratio") == 0.0, noise)
    check("恰好两次短探测（a/b 各一次）",
          labels.count("noise_probe_a") == 1 and labels.count("noise_probe_b") == 1,
          labels)
    check("探测结果被复用为短档基线（无第三次短跑）",
          report.probe is not None and report.probe.label == "noise_probe_a"
          and "baseline_short" not in labels,
          labels)
    check("只跑一次主判据基线",
          labels.count("baseline") == 1, labels)
    check("探测通过后正常进入选档",
          report.rung.get("selected_rung") in (64, 96, 128) and len(report.trials) > 0,
          report.rung)
    check("noise_probe 事件可见",
          any(event.get("stage") == "noise_probe" for event in events))


def test_noise_probe_prefill_unstable(directory):
    """
    [EN]
    Two probes differing by more than ``noise_probe_ratio`` on **prefill** abort the run
    immediately: ``reason=noisy_environment``, zero candidates, no guard, and not a
    single cfg field touched (the reason is neither ``no_improvement`` nor ``drift``).

    [ZH]
    两次探测在**预填充**上相差超过 ``noise_probe_ratio`` 时立即收尾：
    ``reason=noisy_environment``、零候选、不跑守卫，且一个 cfg 字段都没碰
    （该 reason 既不是 ``no_improvement`` 也不是 ``drift``）。
    """
    cfg = _make_config(directory, tuning={"repeats": 3})
    model = _make_model(directory)
    path = os.path.join(directory, "inference_config.json")
    before = _sha256(path)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(
        lambda index, kwargs: {"prefill_tps": 100.0 if index == 0 else 95.0,
                               "tokens": 4})
    report, events = _run_tune(cfg, model, persist=True, forbid_save=True)
    noise = report.noise_probe
    check("prefill 差 5% -> 判定环境不稳",
          noise.get("unstable") is True
          and abs(noise["prefill_ratio"] - 0.05) < 0.002,
          noise)
    check("早退 reason=noisy_environment（既非 no_improvement 也非 drift）",
          report.reason == "noisy_environment", report.reason)
    check("早退时候选扫描次数为 0",
          len(report.trials) == 0
          and not any(event.get("stage") == "trial" for event in events),
          len(report.trials))
    check("早退时不跑守卫", report.long_guard.get("status") == "not_run",
          report.long_guard)
    check("报告含可操作建议", "关闭其它占用 CPU 的程序" in noise.get("advice", ""),
          noise.get("advice"))
    check("早退时不产生任何 cfg 写入",
          _sha256(path) == before
          and cfg.tuned_at == "" and cfg.tuned_baseline == {} and cfg.tuned_best == {}
          and cfg.tuned_fingerprint == {} and cfg.n_threads == 4,
          f"tuned_at={cfg.tuned_at!r}")
    check("早退时不产生 persisted/keep 事件",
          not any(event.get("stage") in ("persisted", "keep") for event in events))


def test_noise_probe_decode_unstable(directory):
    """
    [EN]
    A probe pair that agrees on prefill but not on **decode** also aborts: either metric
    alone is enough, because either one can be the noisy half of a host.
    [ZH]
    预填充一致但 **decode** 不一致的探测对同样收尾：任一项单独越限即可，因为噪声可能
    只出现在其中一半。
    """
    cfg = _make_config(directory, tuning={"repeats": 3, "gen_tokens": 8})
    model = _make_model(directory)

    def plan(index, kwargs):
        # [EN] 8 generated tokens: 7 decode intervals of 50 ms vs 30 ms -> ~40 % apart.
        # [ZH] 生成 8 个 token：7 个解码间隔 50 ms 与 30 ms -> 相差约 40%。
        return {"prefill_tps": 100.0, "tokens": 8,
                "decode_delay": 0.05 if index == 0 else 0.03}

    StubLlama.index = 0
    StubLlama.plan = staticmethod(plan)
    report, events = _run_tune(cfg, model, persist=False)
    noise = report.noise_probe
    check("仅 decode 差超限 -> 判定环境不稳",
          noise.get("decode_compared") is True
          and noise["prefill_ratio"] == 0.0
          and noise["decode_ratio"] > 0.03
          and noise.get("unstable") is True,
          noise)
    check("仅 decode 超限时同样 reason=noisy_environment",
          report.reason == "noisy_environment", report.reason)
    check("仅 decode 超限时候选扫描仍为 0",
          len(report.trials) == 0, len(report.trials))


def test_noise_probe_boundary(directory=None):
    """
    [EN]
    The boundary is explicit and stable: the rule is "**exceeds** the limit", so exactly
    3.0 % continues and 3.1 % aborts. An unmeasurably short decode segment is not
    compared (``decode_compared=false``) rather than silently failing the probe.

    [ZH]
    边界明确且稳定：规则是"**超过**阈值"，因此恰好 3.0% 继续、3.1% 收尾。短到测不出来的
    解码段记录 ``decode_compared=false`` 而不比较，而不是悄悄判失败。
    """
    first = inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=100.0,
                                        decode_tps=50.0, tokens=8)
    at_limit = inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=97.0,
                                           decode_tps=50.0, tokens=8)
    over = inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=96.9,
                                       decode_tps=50.0, tokens=8)
    boundary = inference_tuner.evaluate_noise_probe(first, at_limit, 0.03)
    check("恰好 3.0% 不判不稳（规则是严格大于）",
          abs(boundary["prefill_ratio"] - 0.03) < 1e-9
          and boundary["unstable"] is False,
          boundary)
    check("3.1% 判不稳", inference_tuner.evaluate_noise_probe(
        first, over, 0.03)["unstable"] is True)
    tiny_a = inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=100.0,
                                         decode_tps=50.0, tokens=2)
    tiny_b = inference_tuner.TrialResult(n_threads=4, n_batch=512, prefill_tps=100.0,
                                         decode_tps=10.0, tokens=2)
    tiny = inference_tuner.evaluate_noise_probe(tiny_a, tiny_b, 0.03)
    check("解码段过短时不比较 decode（decode_compared=false）",
          tiny["decode_compared"] is False and tiny["decode_ratio"] == 0.0
          and tiny["unstable"] is False,
          tiny)


def test_confirm_reproducible(directory):
    """
    [EN]
    The winner is re-measured once after the scan and must still beat the end-of-round
    baseline by the adoption threshold. When it does, the run is adopted; the
    comparison baseline is the drift check's retest (no third baseline run).

    [ZH]
    胜者在扫描结束后再测一次，必须仍比轮末基线高出采纳阈值才算数。达标则采纳；对照基线
    取漂移自检的复测（不跑第三次基线）。
    """
    cfg = _guard_config(directory, min_improvement_ratio=0.03)
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)
    StubLlama.plan_prompt = None
    spy, labels = _spy_labels()
    with mock.patch.object(inference_tuner, "_measure_once", spy):
        report, events = _run_tune(cfg, model, persist=False)
    confirm = report.confirm
    check("复测达标（>=3%）时采纳",
          report.adopted is True and report.reason == "improved"
          and confirm.get("status") == "measured" and confirm.get("passed") is True,
          f"{report.adopted}/{report.reason}/{confirm}")
    check("复测明细为两档实测值",
          abs(float(confirm["winner_confirm_tps"]) - 882.0) < 1e-6
          and abs(float(confirm["baseline_after_tps"]) - 840.0) < 1e-6
          and abs(float(confirm["delta"]) - 0.05) < 1e-3,
          confirm)
    check("复测只调用一次胜者、不追加基线复测",
          labels.count("winner_confirm") == 1
          and labels.count("baseline") == 1
          and labels.count("baseline_retest") == 1,
          labels)
    check("confirm 事件可见",
          any(event.get("stage") == "confirm" for event in events))


def test_confirm_unconfirmed(directory):
    """
    [EN]
    When the fresh re-measurement only reaches 1 %, the win did not reproduce:
    ``reason=unconfirmed_improvement`` (its own reason -- not ``no_improvement``, not
    ``drift``), nothing adopted, nothing written.

    [ZH]
    新一次复测只到 1% 时，这次胜利无法复现：``reason=unconfirmed_improvement``（独立
    reason —— 不是 ``no_improvement``，不是 ``drift``），不采纳、不写回。
    """
    cfg = _guard_config(directory, min_improvement_ratio=0.03)
    model = _make_model(directory)
    path = os.path.join(directory, "inference_config.json")
    before = _sha256(path)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)
    StubLlama.plan_prompt = None
    real = inference_tuner._measure_once

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        label = kwargs.get("label")
        if label is None and len(args) > 7:
            label = args[7]
        if label == "winner_confirm":
            # [EN] The win collapses to +1 % on a fresh run.
            # [ZH] 新一次运行里，胜利缩到 +1%。
            result.prefill_tps = 848.4
        return result

    with mock.patch.object(inference_tuner, "_measure_once", wrapped):
        report, events = _run_tune(cfg, model, persist=True, forbid_save=True)
    confirm = report.confirm
    check("复测掉到 1% -> reason=unconfirmed_improvement",
          report.reason == "unconfirmed_improvement", report.reason)
    check("复测失败时不采纳",
          report.adopted is False and report.applied is False
          and report.best.label == "baseline",
          f"{report.adopted}/{report.applied}")
    check("confirm 明细 delta 约 +1%、passed=false",
          abs(float(confirm["delta"]) - 0.01) < 2e-3
          and confirm.get("passed") is False,
          confirm)
    check("复测失败时零写入（save 被禁用且文件不变）",
          _sha256(path) == before and cfg.n_threads == 4,
          f"n_threads={cfg.n_threads}")
    check("unconfirmed 事件可见",
          any(event.get("stage") == "aborted"
              and event.get("reason") == "unconfirmed_improvement" for event in events))


def test_confirm_no_winner(directory):
    """
    [EN]
    With no accepted candidate there is nothing to confirm: the confirm stage is
    ``not_applicable`` and no extra measurement is spent.
    [ZH]
    没有通过判据的候选时没有可复测对象：复测阶段为 ``not_applicable``，不额外花任何测量。
    """
    cfg = _make_config(directory, tuning={"repeats": 3, "max_trials": 4,
                                          "min_improvement_ratio": 0.5})
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(lambda index, kwargs: {"prefill_tps": 100.0,
                                                         "tokens": 4})
    spy, labels = _spy_labels()
    with mock.patch.object(inference_tuner, "_measure_once", spy):
        report, _ = _run_tune(cfg, model, persist=False)
    check("无胜者时 confirm=not_applicable",
          report.confirm.get("status") == "not_applicable", report.confirm)
    check("无胜者时不跑复测", labels.count("winner_confirm") == 0, labels)
    check("无胜者时 reason 仍是 no_improvement",
          report.reason == "no_improvement", report.reason)


def test_confirm_skipped_budget(directory):
    """
    [EN]
    Budget is checked *before* the confirmation run. Without budget for evidence the
    run closes with ``confirm=skipped_budget`` and adopts nothing -- an unverified
    number must not change the configuration.
    [ZH]
    预算在复测**之前**检查。没有预算取证时以 ``confirm=skipped_budget`` 收尾且不采纳 ——
    未验证的数字不得改变配置。
    """
    cfg = _guard_config(directory, min_improvement_ratio=0.03)
    model = _make_model(directory)
    StubLlama.index = 0
    StubLlama.plan = staticmethod(_fast_profile)
    StubLlama.plan_prompt = None
    spy, labels = _spy_labels()
    with mock.patch.object(inference_tuner, "_measure_once", spy):
        report, events = _run_tune(cfg, model, persist=False,
                                   confirm_budget=lambda deadline, estimate: False)
    confirm = report.confirm
    check("预算不足 -> confirm=skipped_budget",
          confirm.get("status") == "skipped_budget", confirm)
    check("预算不足时不采纳",
          report.adopted is False and report.applied is False
          and report.best.label == "baseline",
          f"{report.adopted}/{report.applied}/{report.best.label}")
    check("预算不足时 reason=unconfirmed_improvement",
          report.reason == "unconfirmed_improvement", report.reason)
    check("预算不足时不跑复测测量", labels.count("winner_confirm") == 0, labels)


def main():
    _install_stub()
    # [EN] The tuner logs its verdicts at WARNING level; the tests assert the report
    # [EN] objects, so the noise is suppressed instead of being read as results.
    # [ZH] 调优器以 WARNING 级别记录裁决；测试断言的是报告对象，因此压掉这些噪声，
    # [ZH] 避免被误读为测试结果。
    import logging

    inference_tuner.log.setLevel(logging.CRITICAL)
    print("=" * 60)
    with tempfile.TemporaryDirectory() as root:
        for name, fn in (
                ("median", test_median_not_maximum),
                ("p95_veto", test_p95_veto),
                ("decode_veto", test_decode_veto),
                ("drift", test_drift_void),
                ("time_budget", test_time_budget),
                ("legacy_config", test_legacy_config_compatibility),
                ("fingerprint", test_fingerprint_stale),
                ("persist_false", test_persist_false_zero_write),
                ("two_level", test_two_level_search),
                ("rung_selection", test_rung_selection),
                ("repeats_policy", test_repeats_policy),
                ("long_guard_veto", test_long_guard_veto),
                ("long_guard_skipped", test_long_guard_skipped),
                ("all_rejected", test_all_candidates_rejected),
                ("guard_second", test_guard_second_candidate),
                ("guard_both_vetoed", test_guard_both_vetoed),
                ("tuned_scope", test_tuned_scope),
                ("decode_drift", test_decode_drift_unreliable),
                ("noise_stable", test_noise_probe_stable),
                ("noise_prefill", test_noise_probe_prefill_unstable),
                ("noise_decode", test_noise_probe_decode_unstable),
                ("noise_boundary", test_noise_probe_boundary),
                ("confirm_ok", test_confirm_reproducible),
                ("confirm_unconfirmed", test_confirm_unconfirmed),
                ("confirm_no_winner", test_confirm_no_winner),
                ("confirm_budget", test_confirm_skipped_budget),
        ):
            directory = os.path.join(root, name)
            os.makedirs(directory, exist_ok=True)
            # [EN] No test may inherit another test's prompt-length hook.
            # [ZH] 任何测试都不得继承上一个测试的 prompt 长度钩子。
            StubLlama.plan_prompt = None
            print(f"--- {name} ---")
            fn(directory)
    print("-" * 42)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print(f"共 {PASSED} 项，全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
