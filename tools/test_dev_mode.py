"""修复一（开发者模式弹窗循环与无法关闭）回归测试。

这一项的四个验收条件都做成可自动执行的断言：

1. **批量确认**：待确认清单一次取全（不再是"每发现一个弹一个"）；
2. **记忆选择**：答复过（信任或拒绝）的插件不再出现在待确认清单里；
3. **可关闭**：``dev_mode_explicit`` 一旦写入，开关就完全由配置决定——
   这是原缺陷的根因（只要 ``skills_dev/`` 里有插件，关闭后 status 又会返回开启）；
4. **内置插件静默**：随包示例插件默认启用、不进待确认清单。

另测：关闭开发者模式时真的卸载开发插件（``unload_dev``）、被拒绝的插件不再加载、
用户手工放入的第三方插件仍然需要确认。

运行：python tools\\test_dev_mode.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

PASSED = 0
FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


class FakeConfig:
    """MarketplaceConfig 的字段子集（含修复一新增的 dev_mode_explicit）。"""

    def __init__(self, **kwargs) -> None:
        self.enabled = True
        self.github_token = ""
        self.skill_topic = "echodesk-plugin"
        self.cache_file = ""
        self.install_dir = ""
        self.registry_file = ""
        self.dev_dir = ""
        self.local_manifest = ""
        self.dev_mode = False
        self.dev_mode_explicit = False
        self.dev_log_file = ""
        self.require_verified = False
        for key, value in kwargs.items():
            setattr(self, key, value)


def write_skill(root: str, name: str, skill_id: str, command: str = "") -> str:
    """在 root 下造一个最小可用插件，返回插件目录。"""
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    manifest = {
        "name": skill_id,
        "id": skill_id,
        "version": "1.0.0",
        "api_version": "1",
        "echodesk": {"bundle": "plugin.py", "entry": "plugin.py", "api_version": "1"},
    }
    with io.open(os.path.join(path, "skill.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)
    body = "def register(api):\n    pass\n"
    if command:
        body = ("def register(api):\n"
                f"    api.register_command({command!r}, lambda args: 'ok', description='test')\n")
    with io.open(os.path.join(path, "plugin.py"), "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def make_loader(base: str, *, dev_mode: bool = False, explicit: bool = False):
    """构造 SkillLoader，目录全部落在临时目录里，互不干扰。"""
    from skills_runtime import SkillLoader

    dev_dir = os.path.join(base, "skills_dev")
    install_dir = os.path.join(base, "skills")
    examples = os.path.join(base, "examples", "skills")
    for path in (dev_dir, install_dir, examples):
        os.makedirs(path, exist_ok=True)
    cfg = FakeConfig(
        dev_dir=dev_dir, install_dir=install_dir,
        registry_file=os.path.join(base, "skills_registry.json"),
        local_manifest=os.path.join(base, "skills_local.json"),
        dev_log_file=os.path.join(base, "skills_dev.log"),
        dev_mode=dev_mode, dev_mode_explicit=explicit,
    )
    loader = SkillLoader(cfg, None)

    # examples_dir 走 config.program_root()，在测试里不可控，这里直接打桩
    real_examples = os.path.join(ROOT, "examples", "skills")
    loader.examples_dir = lambda: real_examples  # type: ignore[assignment]
    return loader, dev_dir, examples


print("=== 1. 开关双向可用（原缺陷：目录里有插件就关不掉）===")
tmp = tempfile.mkdtemp(prefix="echodesk-dev-")
try:
    loader, dev_dir, examples = make_loader(os.path.join(tmp, "s1"))
    write_skill(dev_dir, "third-party", "third-party")

    check("从未设置过开关且目录有插件 → 自动启用", loader.dev_mode is True)
    check("自动启用时 dev_detected 为真", loader.dev_detected is True)
    check("自动启用时 dev_mode_explicit 为假（界面据此显示'自动'）",
          loader.dev_mode_explicit is False)

    loader.cfg.dev_mode = False
    loader.cfg.dev_mode_explicit = True
    check("显式关闭后 dev_mode 必须为假（关得掉）", loader.dev_mode is False,
          f"dev_mode={loader.dev_mode}")
    check("显式关闭后 dev_detected 仍为真（目录里确实有插件）",
          loader.dev_detected is True)
    check("显式关闭后 dev_mode_explicit 为真", loader.dev_mode_explicit is True)

    loader.cfg.dev_mode = True
    check("显式开启后 dev_mode 为真", loader.dev_mode is True)

    loader.cfg.dev_mode = False
    loader.cfg.dev_mode_explicit = False
    check("恢复'未设置'后又变回自动启用", loader.dev_mode is True)

    print("\n=== 2. 内置示例插件静默、第三方插件需要确认 ===")
    loader, dev_dir, examples = make_loader(os.path.join(tmp, "s2"))
    # 把官方示例插件复制进 skills_dev（打包脚本就是这么做的）。
    # 注意用 loader.examples_dir()（已打桩到仓库真实目录）：make_loader 建的
    # examples 子目录是空的，照它复制会一个插件都拿不到。
    repo_examples = loader.examples_dir()
    for name in os.listdir(repo_examples):
        src = os.path.join(repo_examples, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dev_dir, name), dirs_exist_ok=True)
    write_skill(dev_dir, "user-plugin", "user-plugin")

    builtin = loader.builtin_ids()
    check("示例插件被识别为内置（builtin_ids 非空）", len(builtin) > 0, str(builtin))
    check("内置集合来自随包 examples 目录，而不是插件自报",
          "hello-world" in builtin or "ofd-reader" in builtin, str(builtin))
    check("第三方插件不算内置", "user-plugin" not in builtin)

    loader.load_all()
    records = {rec.id: rec for rec in loader.records.values()}
    check("内置示例插件已加载", any(rid in builtin and rec.loaded for rid, rec in records.items()),
          str([(r.id, r.loaded) for r in records.values()]))
    builtin_records = [rec for rid, rec in records.items() if rid in builtin]
    check("内置示例插件不弹确认框", all(not rec.trust_pending for rec in builtin_records),
          str([(rec.id, rec.trust_pending) for rec in builtin_records]))
    user_rec = records.get("user-plugin")
    check("第三方插件需要确认", user_rec is not None and user_rec.trust_pending is True,
          str(user_rec.trust_pending if user_rec else "missing"))

    print("\n=== 3. 批量确认清单与「记忆选择」 ===")
    pending = loader.pending_review()
    ids = {item["id"] for item in pending}
    check("待确认清单只含第三方插件", ids == {"user-plugin"}, str(ids))
    check("待确认清单带能力与权限字段",
          pending and {"id", "name", "path", "source", "capabilities", "permissions"}
          <= set(pending[0]), str(sorted(pending[0].keys())) if pending else "empty")

    # 用户答复"启用" → 不再询问，并真的被加载
    check("写入信任成功", loader.trust_plugin("user-plugin", True))
    loader.load_all()
    check("信任后不再出现在待确认清单",
          "user-plugin" not in {i["id"] for i in loader.pending_review()})
    check("信任后插件已加载", loader.records["user-plugin"].loaded is True)
    manifest = loader.local_manifest()
    check("信任写入 skills_local.json 的 trusted", "user-plugin" in manifest["trusted"],
          str(manifest["trusted"]))
    check("清单 schema 升到 2", manifest["schema"] == 2, str(manifest["schema"]))
    check("清单含 denied 字段（旧文件兼容）", "denied" in manifest, str(sorted(manifest)))

    print("\n=== 4. 用户拒绝过的插件不再询问、也不再加载 ===")
    write_skill(dev_dir, "bad-plugin", "bad-plugin")
    loader.load_all()
    check("新插件再次进入待确认清单",
          "bad-plugin" in {i["id"] for i in loader.pending_review()})
    loader.trust_plugin("bad-plugin", False)
    loader.load_all()
    check("拒绝后不再出现在待确认清单",
          "bad-plugin" not in {i["id"] for i in loader.pending_review()})
    bad = loader.records.get("bad-plugin")
    check("拒绝后插件不被加载", bad is not None and bad.loaded is False,
          str(bad.loaded if bad else "missing"))
    check("拒绝后 enabled=False", bad is not None and bad.enabled is False,
          str(bad.enabled if bad else "missing"))
    manifest = loader.local_manifest()
    check("拒绝写入 denied", "bad-plugin" in manifest["denied"], str(manifest["denied"]))
    check("拒绝不会残留 trusted", "bad-plugin" not in manifest["trusted"], str(manifest["trusted"]))

    # 重新读取一遍清单（模拟重启）→ 仍然不问、不加载
    loader2 = type(loader)(loader.cfg, None)
    loader2.examples_dir = loader.examples_dir  # type: ignore[assignment]
    loader2.load_all()
    check("重启后仍不问已答复过的插件",
          not ({"user-plugin", "bad-plugin"} & {i["id"] for i in loader2.pending_review()}),
          str([i["id"] for i in loader2.pending_review()]))
    check("重启后已信任的仍然加载", loader2.records["user-plugin"].loaded is True)
    check("重启后被拒绝的仍然不加载", loader2.records["bad-plugin"].loaded is False)

    print("\n=== 5. 关闭开发者模式时真的卸载开发插件 ===")
    loader3, dev_dir3, _ = make_loader(os.path.join(tmp, "s5"))
    write_skill(dev_dir3, "with-cmd", "with-cmd", command="/hello")
    loader3.cfg.dev_mode = True
    loader3.cfg.dev_mode_explicit = True
    loader3.load_all()
    check("开发插件已加载", loader3.records["with-cmd"].loaded is True)
    removed = loader3.unload_dev()
    check("unload_dev 返回被卸载的 id", "with-cmd" in removed, str(removed))
    check("卸载后不在 records 里", "with-cmd" not in loader3.records, str(list(loader3.records)))
    check("卸载后没有已加载的开发插件",
          not [r for r in loader3.records.values() if r.source in ("dev", "local") and r.loaded])

    print("\n=== 6. status() 暴露界面所需字段 ===")
    loader4, dev_dir4, _ = make_loader(os.path.join(tmp, "s6"))
    write_skill(dev_dir4, "s1", "s1")
    write_skill(dev_dir4, "s2", "s2")
    loader4.load_all()
    status = loader4.status()
    check("status 含 dev_mode", "dev_mode" in status)
    check("status 含 dev_mode_explicit", "dev_mode_explicit" in status)
    check("status 含 dev_detected", "dev_detected" in status)
    check("status 含已启用的开发插件清单", "dev_enabled" in status)
    check("status 含开发插件数量", status.get("dev_count") == 2, str(status.get("dev_count")))
    check("status 含待确认 id 列表", sorted(status.get("trust_pending_ids") or []) == ["s1", "s2"],
          str(status.get("trust_pending_ids")))
    check("status 含内置插件 id 列表", isinstance(status.get("builtin_ids"), list))

    print("\n=== 7. 清单写入保持向后兼容（schema 1 → 2）===")
    legacy = os.path.join(tmp, "legacy_manifest.json")
    with io.open(legacy, "w", encoding="utf-8") as handle:
        json.dump({"schema": 1, "plugins": [], "trusted": ["x"]}, handle)
    loader5 = type(loader)(FakeConfig(
        dev_dir=os.path.join(tmp, "d5"), install_dir=os.path.join(tmp, "i5"),
        registry_file=os.path.join(tmp, "r5.json"),
        local_manifest=legacy,
        dev_log_file=os.path.join(tmp, "l5.log"),
    ), None)
    loader5.examples_dir = loader.examples_dir  # type: ignore[assignment]
    loaded = loader5.local_manifest()
    check("旧文件缺 denied 时读成空列表", loaded["denied"] == [], str(loaded))
    check("旧文件里已有的 trusted 不丢", loaded["trusted"] == ["x"], str(loaded["trusted"]))
    check("is_reviewed 对已信任的返回真", loader5.is_reviewed("x") is True)
    check("is_reviewed 对没答复过的返回假", loader5.is_reviewed("y") is False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    for item in FAILED:
        print(f"  ✗ {item}")
    sys.exit(1)
sys.exit(0)
