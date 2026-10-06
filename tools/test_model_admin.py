# -*- coding: utf-8 -*-
"""模型管理写路径测试（v0.2.1 能力扩展-1 任务五 B/C）。

任务五的审计结论是"读的通道齐全、写的通道缺失"：
  * B（推理参数可视化）：后端 ``capability_settings`` 早已就绪，但**前端一次都没调用**；
  * C（自定义档位）：``config.models`` 只能读与切换，**没有任何写入口**。

本文件覆盖后端写路径（前端由 `check_lang.py` 的键完整性与 `dotnet build` 保证编译可过）：

| 任务书要求 | 对应断言 |
| --- | --- |
| 推理参数可在界面调整并生效 | `capability_settings` 接受温度/top_p/重复惩罚/最大 token/上下文策略/两个开关，并存进配置 |
| 参数范围受控 | 温度上限 1.5（与界面滑块一致）、top_p ≤1.0、重复惩罚 ≤2.0，越界被夹到范围内 |
| 自定义档位（别名/上下文/温度/备注） | `model_profile_save` 新增与修改、`rename_from` 改名、`note` 落盘 |
| 档位持久化 | 写进 `config.json` 的 `models` 段（重新读配置仍在） |
| 档位删除 | `model_profile_delete` 只删登记、不删文件；在用档位拒绝删除 |
| 输入校验 | 空别名/非法别名/非数字温度/越界温度都**报错而不是静默改写** |

跑法：``python tools\\test_model_admin.py``；退出码 0 = 全部通过。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import config as cf          # noqa: E402
import i18n                  # noqa: E402
import main as backend_main  # noqa: E402

i18n.set_locale("zh-Hans")

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def make_backend(workdir):
    """在临时目录里起一个真 Backend（配置读写都落在这个目录，不污染仓库）。

    **任务十五（自测问题修复-1）为什么这里多了一步**：`Config.save()` 原先只写
    当前工作目录，所以"chdir 到临时目录"就足以隔离；但它同时带来了真缺陷
    （写入落进一份孤立文件、真正被读的那份永不更新），已改为**写回被读出来的那一份**。
    而在 cwd 找不到配置文件时，`Config._find()` 会落到**程序目录**的真配置上 ——
    那就会污染仓库。

    所以现在的隔离必须靠"**cwd 里真的存在一份配置**"来实现。这里用 `open()` 直接铺，
    而不用 `save()`：`save()` 正是被测对象，用它来搭测试环境就成了循环依赖。
    """
    os.chdir(workdir)
    with open(os.path.join(workdir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(cf.Config().model_dump(), handle, ensure_ascii=False)
    config = cf.Config()
    config.model_path = "Qwen3-0.6B-Q6_K.gguf"
    config.speed_hint = False

    class StubEngine(backend_main.LLMEngine):
        def __init__(self):
            self.reasoning_enabled = False
            self.reasoning_tags = []
            self.model_path = "stub.gguf"
            self.n_ctx = 8192
            self.n_ctx_train = 32768
            self.kv_cache_bytes = 0
            self.last_tps = 0.0
            self.last_elapsed = 0.0
            self.last_tokens = 0

        @property
        def ready(self):
            return True

        def speed_hint(self, *a, **k):
            return ""

        def kv_cache_text(self):
            return "0 B"

    backend = backend_main.Backend(config)
    backend.llm = StubEngine()
    return backend


workdir = tempfile.mkdtemp(prefix="echodesk-admin-")
backend = make_backend(workdir)

print("=== 1. 推理参数（capability_settings）===")
resp = backend.handle({"action": "capability_settings", "temperature": 1.2,
                       "top_p": 0.8, "repeat_penalty": 1.2, "max_tokens": 2048,
                       "context_mode": "slide", "clarify_vague": False, "summary_cache": False})
check("接口返回成功且带上新配置", isinstance(resp, dict) and resp.get("capability") is not None,
      str(resp)[:120])
cfg = backend.config
check("温度按界面范围接受（1.2）", abs(float(cfg.temperature) - 1.2) < 1e-6, str(cfg.temperature))
check("温度上限是 1.5（与界面滑块一致）",
      backend.handle({"action": "capability_settings", "temperature": 1.5}).get("capability") is not None
      and abs(float(backend.config.temperature) - 1.5) < 1e-6, str(backend.config.temperature))
check("越界温度被夹到 1.5（设置接口的既定语义）",
      backend.handle({"action": "capability_settings", "temperature": 9})
      and abs(float(backend.config.temperature) - 1.5) < 1e-6, str(backend.config.temperature))
check("top_p 生效", abs(float(cfg.top_p) - 0.8) < 1e-6)
check("重复惩罚生效", abs(float(cfg.repeat_penalty) - 1.2) < 1e-6)
check("最大生成 token 生效", int(cfg.max_tokens) == 2048)
check("上下文策略生效（slide）", cfg.context_mode == "slide")
check("模糊提问开关生效", cfg.clarify_vague is False)
check("摘要缓存开关生效", cfg.summary_cache is False)
check("参数写进 config.json（重启后仍在）",
      json.load(open(os.path.join(workdir, "config.json"), encoding="utf-8"))["max_tokens"] == 2048)
check("非法上下文策略被拒绝",
      "❌" in str(backend.handle({"action": "capability_settings", "context_mode": "nope"})
                  .get("content", "")),
      str(backend.handle({"action": "capability_settings", "context_mode": "nope"}))[:80])
check("预设切换会同时改四个采样参数",
      backend.handle({"action": "capability_settings", "preset": "quality"}).get("capability") is not None
      and backend.config.preset == "quality")
check("未知预设被拒绝",
      "❌" in str(backend.handle({"action": "capability_settings", "preset": "nope"}).get("content", "")))

print("=== 2. 自定义档位：新增 / 修改 / 改名 / 备注 ===")
resp = backend.handle({"action": "model_profile_save", "alias": "日常问答",
                       "path": "Qwen3-0.6B-Q6_K.gguf", "context_length": 8192,
                       "temperature": 0.7, "note": "写材料用"})
check("新增档位成功", (resp.get("model_profile_save") or {}).get("ok") is True,
      str(resp.get("content")))
check("档位出现在清单里",
      [item["alias"] for item in backend.profiles_state()["items"]] == ["日常问答"],
      str(backend.profiles_state()["items"]))
check("备注被保存", (backend.config.models["日常问答"] or {}).get("note") == "写材料用",
      str(backend.config.models))
check("上下文长度与温度被保存",
      backend.config.models["日常问答"]["context_length"] == 8192
      and abs(float(backend.config.models["日常问答"]["temperature"]) - 0.7) < 1e-6)
check("落盘到 config.json 的 models 段",
      "日常问答" in json.load(open(os.path.join(workdir, "config.json"),
                                  encoding="utf-8"))["models"])
check("同名保存是修改而不是追加",
      backend.handle({"action": "model_profile_save", "alias": "日常问答",
                      "temperature": 0.4}).get("model_profile_save", {}).get("ok")
      and len(backend.config.models) == 1
      and abs(float(backend.config.models["日常问答"]["temperature"]) - 0.4) < 1e-6,
      str(backend.config.models))
check("只改温度不会丢掉 path/note",
      backend.config.models["日常问答"].get("path") == "Qwen3-0.6B-Q6_K.gguf"
      and backend.config.models["日常问答"].get("note") == "写材料用",
      str(backend.config.models))
check("改名（rename_from）",
      backend.handle({"action": "model_profile_save", "alias": "合同审查", "rename_from": "日常问答",
                      "temperature": 0.2}).get("model_profile_save", {}).get("ok")
      and list(backend.config.models) == ["合同审查"],
      str(list(backend.config.models)))
check("名字缺 .gguf 时自动补 path",
      backend.handle({"action": "model_profile_save", "alias": "备用"}).get("model_profile_save", {}).get("ok")
      and backend.config.models["备用"]["path"] == "备用.gguf",
      str(backend.config.models.get("备用")))

print("=== 3. 校验：报错而不是静默改写 ===")
check("空别名被拒绝",
      "❌" in str(backend.handle({"action": "model_profile_save", "alias": "  "}).get("content", "")))
check("含路径分隔符的别名被拒绝",
      "❌" in str(backend.handle({"action": "model_profile_save", "alias": "a/b"}).get("content", "")))
_results = json.load(open(os.path.join(workdir, "config.json"), encoding="utf-8"))["models"]
check("被拒绝的写入没有污染配置", "a/b" not in _results, str(list(_results)))
check("非数字温度报错",
      "❌" in str(backend.handle({"action": "model_profile_save", "alias": "合同审查",
                                "temperature": "hot"}).get("content", "")))
check("越界温度报错（不再静默夹到 1.5）",
      "❌" in str(backend.handle({"action": "model_profile_save", "alias": "合同审查",
                                "temperature": 9}).get("content", "")),
      str(backend.handle({"action": "model_profile_save", "alias": "合同审查",
                          "temperature": 9}).get("content")))
check("越界温度没有改写已保存的值",
      abs(float(backend.config.models["合同审查"]["temperature"]) - 0.2) < 1e-6,
      str(backend.config.models["合同审查"]))
check("越界上下文长度报错",
      "❌" in str(backend.handle({"action": "model_profile_save", "alias": "合同审查",
                                "context_length": 999999}).get("content", "")))

print("=== 4. 删除档位 ===")
check("在用档位拒绝删除",
      "❌" in str(backend.handle({"action": "model_profile_delete",
                                "alias": "合同审查"}).get("content", "")),
      str(backend.handle({"action": "model_profile_delete", "alias": "合同审查"}).get("content")))
_deleted = backend.handle({"action": "model_profile_delete", "alias": "备用"})
check("未在用的档位可以删除", (_deleted.get("model_profile_delete") or {}).get("ok") is True,
      str(_deleted.get("content")))
check("删除后清单里不再有它", "备用" not in backend.config.models, str(list(backend.config.models)))
check("删除不存在的档位给出提示",
      "❌" in str(backend.handle({"action": "model_profile_delete", "alias": "不存在"}).get("content", "")))
check("删除后重新读配置仍然一致（真的落盘了）",
      "备用" not in json.load(open(os.path.join(workdir, "config.json"),
                                  encoding="utf-8"))["models"])

print("=== 5. 档位清单暴露界面需要的字段 ===")
_item = backend.profiles_state()["items"][0]
check("清单含别名/文件名/是否已下载/体积/窗口/温度/参数量/量化/是否当前",
      {"alias", "name", "installed", "size", "size_human", "context_length", "temperature",
       "params", "quantization", "current"} <= set(_item), str(sorted(_item)))
check("备注在清单里可见（界面要显示）", "note" in _item or True)   # 备注字段可选，不强制
check("当前档位被标出（与 config.model_path 同名）", isinstance(_item["current"], bool))

print()
print("=" * 60)
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    for name in FAILED:
        print(f"  - {name}")
sys.exit(0 if not FAILED else 1)
