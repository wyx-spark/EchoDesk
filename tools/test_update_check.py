# -*- coding: utf-8 -*-
"""
[EN]
Unit tests for the v0.2.4 auto-update patch (``backend/update_check.py``).

The suite deliberately runs without network access: the GitHub API and the
asset HTTP response are replaced with deterministic fakes. It verifies the
three security properties added by the patch:

  1. only a release asset of the configured repository may be downloaded;
  2. a truncated or digest-mismatched download is discarded (the ``.part``
     file is removed and no usable file is left behind);
  3. user data is copied to an isolated backup before an update, while model
     files are recorded but never copied.

[ZH]
v0.2.4 自动更新补丁（``backend/update_check.py``）的单元测试。

本套测试刻意不联网：GitHub API 与附件 HTTP 响应都换成确定性的假实现。
它验证补丁新增的三条安全属性：

  1. 只能下载配置仓库自己的发布附件；
  2. 截断或摘要不匹配的下载会被丢弃（``.part`` 被删除，不留可用文件）；
  3. 更新前用户数据被复制到隔离备份，模型文件只登记、不复制。

Run: ``python tools\\test_update_check.py``. Exit code 0 means all passed.
"""
import hashlib
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import update_check as uc  # noqa: E402

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    """[EN] Record one assertion / [ZH] 记录一条断言。"""
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def cfg(**overrides):
    base = dict(enabled=True, repo="wyx-spark/EchoDesk",
                api_base="https://api.github.com", check_interval_hours=24,
                last_check="", include_prerelease=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def release(tag, assets):
    return {"tag_name": tag, "name": tag, "draft": False, "prerelease": False,
            "html_url": f"https://github.com/wyx-spark/EchoDesk/releases/tag/{tag}",
            "body": "notes", "assets": assets}


def asset(name, url, size, digest=""):
    return {"name": name, "browser_download_url": url, "size": size,
            "digest": digest, "content_type": "application/octet-stream"}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            data, self.payload = self.payload, b""
            return data
        data, self.payload = self.payload[:size], self.payload[size:]
        return data


def test_versions():
    check("版本解析处理 v 前缀", uc.parse_version("v0.2.10") == (0, 2, 10))
    check("版本解析丢弃预发布后缀", uc.parse_version("0.2.4-beta.1") == (0, 2, 4))
    check("v0.2.10 高于 v0.2.9", uc.compare_versions("v0.2.9", "v0.2.10") == 1)
    check("相同版本比较为 0", uc.compare_versions("v0.2.4", "0.2.4") == 0)
    check("降级比较为 -1", uc.compare_versions("v0.2.5", "v0.2.4") == -1)


def test_asset_pick():
    assets = [
        {"name": "EchoDesk-v0.2.4-Setup.exe", "size": 1, "url": "https://github.com/a"},
        {"name": "backend-linux-x64.tar.gz", "size": 1, "url": "https://github.com/b"},
    ]
    chosen = uc.pick_update_asset(assets)
    check("优先选择 Windows Setup 附件",
          chosen is not None and chosen["name"].endswith("Setup.exe"), str(chosen))
    chosen = uc.pick_update_asset([a for a in assets if not a["name"].endswith(".exe")])
    check("没有 Setup 时回退到便携归档",
          chosen is not None and chosen["name"].endswith(".tar.gz"), str(chosen))
    check("没有可下载产物时返回 None", uc.pick_update_asset([]) is None)


def test_check_available():
    url = "https://github.com/wyx-spark/EchoDesk/releases/download/v0.2.5/EchoDesk-v0.2.5-Setup.exe"
    fake = [release("v0.2.5", [asset("EchoDesk-v0.2.5-Setup.exe", url, 7)])]
    with mock.patch.object(uc, "_fetch_releases", return_value=fake):
        result = uc.check_for_update(cfg(), force=True)
    check("检测到新版本", result["status"] == "available", str(result))
    check("返回附件清单", len(result.get("assets", [])) == 1, str(result))
    check("返回首选附件", result.get("asset", {}).get("name") == "EchoDesk-v0.2.5-Setup.exe",
          str(result.get("asset")))


def test_url_guard():
    good = "https://github.com/wyx-spark/EchoDesk/releases/download/v0.2.4/x.exe"
    try:
        uc._validate_download_url(good, cfg())
        check("接受本仓库的 Release 下载地址", True)
    except Exception as exc:
        check("接受本仓库的 Release 下载地址", False, str(exc))
    for bad, label in [
        ("https://evil.example/x.exe", "拒绝非 GitHub 主机"),
        ("http://github.com/wyx-spark/EchoDesk/releases/download/v0.2.4/x.exe", "拒绝非 https"),
        ("https://github.com/other/repo/releases/download/v0.2.4/x.exe", "拒绝其他仓库"),
    ]:
        try:
            uc._validate_download_url(bad, cfg())
            check(label, False, "未抛出异常")
        except ValueError:
            check(label, True)


def test_download_ok():
    payload = b"installer-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    url = "https://github.com/wyx-spark/EchoDesk/releases/download/v0.2.4/x.exe"
    fake = [release("v0.2.4", [asset("x.exe", url, len(payload), f"sha256:{digest}")])]
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(uc, "_fetch_releases", return_value=fake), \
             mock.patch.object(uc.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            result = uc.download_update_asset(cfg(), "v0.2.4", "x.exe", tmp)
        check("下载返回成功", result["ok"] is True, str(result))
        check("落盘内容与源一致", open(result["path"], "rb").read() == payload)
        check("SHA-256 与附件摘要一致", result["sha256"] == digest, result["sha256"])
        check("不残留 .part 文件", not os.path.exists(result["path"] + ".part"))


def test_download_truncated():
    payload = b"0123456789"
    url = "https://github.com/wyx-spark/EchoDesk/releases/download/v0.2.4/x.exe"
    fake = [release("v0.2.4", [asset("x.exe", url, len(payload) + 5)])]
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(uc, "_fetch_releases", return_value=fake), \
             mock.patch.object(uc.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            try:
                uc.download_update_asset(cfg(), "v0.2.4", "x.exe", tmp)
                check("截断下载必须失败", False, "未抛出异常")
            except Exception:
                check("截断下载必须失败", True)
        check("截断下载不留下最终文件", not os.path.exists(os.path.join(tmp, "x.exe")))
        check("截断下载清理 .part 文件", not os.path.exists(os.path.join(tmp, "x.exe.part")))


def test_download_digest_mismatch():
    payload = b"tampered"
    url = "https://github.com/wyx-spark/EchoDesk/releases/download/v0.2.4/x.exe"
    fake = [release("v0.2.4", [asset("x.exe", url, len(payload), "sha256:" + "0" * 64)])]
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(uc, "_fetch_releases", return_value=fake), \
             mock.patch.object(uc.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            try:
                uc.download_update_asset(cfg(), "v0.2.4", "x.exe", tmp)
                check("摘要不匹配必须失败", False, "未抛出异常")
            except Exception:
                check("摘要不匹配必须失败", True)
        check("摘要不匹配不留下最终文件", not os.path.exists(os.path.join(tmp, "x.exe")))


def test_backup_user_data():
    with tempfile.TemporaryDirectory() as tmp:
        program = os.path.join(tmp, "app", "backend")
        data_root = os.path.join(tmp, "app")
        os.makedirs(os.path.join(program, "models"))
        os.makedirs(data_root, exist_ok=True)
        with open(os.path.join(program, "config.json"), "w", encoding="utf-8") as handle:
            json.dump({"theme": "dark"}, handle)
        with open(os.path.join(program, "session.json"), "w", encoding="utf-8") as handle:
            json.dump([{"role": "user"}], handle)
        with open(os.path.join(data_root, "ui_state.json"), "w", encoding="utf-8") as handle:
            json.dump({"lang": "en"}, handle)
        with open(os.path.join(program, "models", "qwen.gguf"), "wb") as handle:
            handle.write(b"model")
        result = uc.backup_user_data(program, data_root, "v0.2.4")
        backup = result["dir"]
        check("备份目录位于用户数据根下",
              os.path.commonpath([backup, data_root]) == os.path.abspath(data_root), backup)
        check("备份复制 config.json", os.path.isfile(os.path.join(backup, "config.json")))
        check("备份复制 session.json", os.path.isfile(os.path.join(backup, "session.json")))
        check("备份复制外层 ui_state.json", os.path.isfile(os.path.join(backup, "root-ui_state.json")))
        check("模型文件不被复制", not os.path.exists(os.path.join(backup, "models", "qwen.gguf")))
        manifest = json.load(open(os.path.join(backup, "manifest.json"), encoding="utf-8"))
        check("清单登记留在原位的模型",
              any(m["name"] == "qwen.gguf" for m in manifest["models_left_in_place"]), str(manifest))
        check("模型原文件仍在原位", os.path.isfile(os.path.join(program, "models", "qwen.gguf")))


def main():
    print("== 自动更新补丁测试 (test_update_check) ==")
    test_versions()
    test_asset_pick()
    test_check_available()
    test_url_guard()
    test_download_ok()
    test_download_truncated()
    test_download_digest_mismatch()
    test_backup_user_data()
    print("-" * 42)
    print(f"共 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  失败：{name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
