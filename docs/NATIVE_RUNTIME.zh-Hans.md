# 原生运行期（简体中文译本）

> **本文为英文版 [`NATIVE_RUNTIME.md`](NATIVE_RUNTIME.md) 的中文译本。** 如与英文版有歧义，**以英文版为准**。
> 英文版为本仓库的正式版本。

EchoDesk 的推理走**随产品分发的原生 llama.cpp 运行期**。交付物里没有 Python 推理绑定：
v0.2.7 已物理移除 `llama-cpp-python`，numpy 同时退出依赖。后端通过 `ctypes` 在
`backend/native/<rid>/` 的二进制之上加载 `llama` / `mtmd` / `ggml`。

本文件说明目录布局、三平台文件清单，以及 `ECHODESK_LLAMA_RUNTIME` 每个取值的实际行为。
每个二进制的出处——wheel 文件名、下载地址、sha256 与逐文件摘要——见
[`backend/native/VENDOR.md`](../backend/native/VENDOR.md)。

## 1. 目录布局

仓库树：

```text
backend/
  native/
    VENDOR.md            # 出处：轮子来源、sha256、逐文件摘要
    win-x64/             # Windows x64（CPU 集）
    linux-x64/           # Linux x86_64（CPU 集）
    osx-arm64/           # macOS arm64（CPU + Metal + BLAS）
  llama_native.py        # 文本引擎（原生）
  llama_ctypes.py        # 库定位与 ctypes 装载
  llama_capi.py          # C API 签名
  llama_abi/mtmd_cpp.py  # 原生视觉（mtmd）ABI
  vision_native.py       # 视觉引擎（原生 mtmd）
  llm_backend.py         # 唯一的运行期选路点
```

运行期目录在导入期解析：设了 `ECHODESK_LLAMA_LIB` 就用它，否则 `current_native_rid()` 把当前
解释器映射到 `win-x64` / `linux-x64` / `osx-arm64`；没有对应 RID 子目录时回落到存放库的扁平目录。

因此打包后的树不需要自己的路径逻辑——每种构建都把目标 RID 的动态库摊平到一个与后端代码同级的
`native/` 目录里：

| 构建形态 | 原生运行期位置 | 产生者 |
| --- | --- | --- |
| Windows 便携树 / 安装程序 | `app/backend/_internal/native/` | `backend/EchoBackend.spec`（PyInstaller 的 `binaries`） |
| standalone 后端（三平台归档） | `app/native/` | `tools/pack_backend.py` 的 `copy_native_libraries` |
| 仓库检出 | `backend/native/<rid>/` | vendored，出处见 `backend/native/VENDOR.md` |

两条打包链都会在继续之前校验这套文件：缺 `llama` / `mtmd` / `ggml` / `ggml-base` 中任何一个，
或 CPU 后端模块为空，就中止构建，而不是产出一个加载不了模型的后端。

## 2. 文件清单（llama.cpp 0.4.7）

三平台统一从**同一个** llama.cpp 修订（0.4.7）vendoring。`win-x64` 只随包 CPU 集——
`libggml-cuda` 及 cudart/cublas 运行时文件全链路排除，这正是 Windows 包去掉约 190 MB CUDA 载荷的原因。

| 平台 | 文件数 | 总字节数 | 主要库 |
| --- | ---: | ---: | --- |
| `win-x64` | 19 | 29,545,528 | `llama.dll`（4,661,248）、`mtmd.dll`（2,530,304）、`ggml.dll`（86,528）、`ggml-base.dll`（887,808）、15 个 `ggml-cpu-*.dll` CPU 变体、`libomp140.x86_64.dll`（634,936） |
| `linux-x64` | 26 | 40,776,048 | `libllama.so`（4,748,184）、`libmtmd.so`（2,123,648）、`libggml*.so`（43,696 / 862,296）、15 个 `libggml-cpu-*.so` 变体、`libggml-rpc.so`（258,424） |
| `osx-arm64` | 23 | 35,797,432 | `libllama.dylib`（3,306,552）、`libmtmd.dylib`（1,547,672）、`libggml-metal.dylib`（5,664,616）、`libggml-cpu.dylib`（935,368）、`libggml-blas.dylib`（54,936）、`libggml-base.dylib`（691,400）、`libggml-rpc.dylib`（191,208） |

`.so.0` / `.so.0.29.0` 与 `.0.29.0.dylib` / `.0.dylib` 这组名字在上游是符号链接，Windows 检出
（`core.symlinks=false`）把它们实体化成完整副本；每组三份逐字节相同，因此总计里同一个库出现多次。
带 sha256 的完整逐文件表见 `backend/native/VENDOR.md` 第 3 节。

这套 vendored 文件的校验属于发布链的一环：三个 standalone 树的 `app/native/` 必须与仓库逐字节一致
（`verify_reproducible_build.ps1`、`check_crossplatform_release.py`）。

## 3. `ECHODESK_LLAMA_RUNTIME`

一个环境变量选择运行期策略。`backend/llm_backend.py` 是**唯一**的判定点，文本与视觉共用。

| 取值 | 文本推理 | 视觉推理 | 说明 |
| --- | --- | --- | --- |
| `auto`（默认） | 原生运行期 | 轮子可导入时用轮子，否则用原生 `mtmd`（并记录回落原因） | v0.2.7 交付物里没有轮子，因此 `auto` 对文本与视觉一律解析为原生。 |
| `native` | 原生运行期 | 原生 `mtmd` | 强制使用随包运行期。视觉不需要加速器：`mtmd` 在 CPU 上运行。 |
| `wheel` | `llama-cpp-python` 绑定 | `llama-cpp-python` 绑定 | **v0.2.7 不可用**：轮子已不再随包。见下文。 |

其它取值一律按 `auto` 处理，并在每个进程告警一次；服务定义里的笔误不能让应用不可用，
但必须出现在日志里。

### 3.1 v0.2.7 的 `wheel` 明确失败，绝不静默回退

v0.2.7 已物理移除 `llama-cpp-python`，请求它必然失败。这是刻意的：静默回落到原生运行期会让
操作者以为正在使用 Python 绑定（以及它在 GPU 机器上的 CUDA 后端），而实际并没有。因此失败是
结构化的、绝不被吞掉：

* **文本**：`create_llama()` 抛出 `LlamaWheelUnavailable`，消息点明被移除的包与可用替代
  （`auto` / `native`）；
* **视觉**：`vision_runtime()` 抛出 `VisionError(code="runtime_unavailable")`，附同样的说明；
* **命令行**：需要该绑定的模型对比工具 `tools/prompt_compare.py` 会拒绝 `--model` 路径并显式
  报错，以退出码 **2** 结束；只有它的 `--dry-run` 路径仍可运行。

每种情况下消息都会提示操作者把 `ECHODESK_LLAMA_RUNTIME` 设为 `native`（或 `auto`）。移除 CUDA
依赖**不是** GPU 支持：本版不含 CUDA 后端，视觉经 `mtmd` 走纯 CPU，25–69 秒/张。

### 3.2 反方向同样不回落

在 `auto` 下，原生模型加载失败**绝不**重试 Python 绑定：原生错误会带上回滚提示重新抛出，并链上
原始异常。静默重试会把原生缺陷伪装成"能跑"，我们就永远收不到该有的报告。视觉是唯一的例外，
且只发生在选路阶段：绑定不可用时切换到原生 `mtmd`，并记录原因
（`llm_backend.vision_fallback_reason()`，经 `VisionService.status()` 暴露）。

## 4. 相关环境变量

| 变量 | 作用 |
| --- | --- |
| `ECHODESK_LLAMA_RUNTIME` | 运行期策略：`auto` / `native` / `wheel`（第 3 节）。 |
| `ECHODESK_LLAMA_LIB` | 原生运行期目录的绝对路径，覆盖 RID 探测。仅用于开发与现场诊断；打包产物从不设置它。 |

## 5. 校验一个检出

```text
python tools/test_vision_gate.py        # 选路契约：native / wheel / auto / none
python tools/check_backend_boot.py      # 后端在本机能启动
tools/verify_release.ps1                # 全量发布门禁（永不加 -Skip）
```

`test_vision_gate.py` 已接入 `tools/verify_release.ps1`：它证明 `native` 会选择原生运行期且不为了
判定而导入它；`auto` 在轮子可导入时优先用轮子；轮子不可导入时 `auto` 切换到原生（用 `PYTHONPATH`
上的影子包在子进程中证明）；两种运行期都不可用时抛出 `runtime_unavailable`，而不是降级成纯文本回答。

## 6. 已知限制

* v0.2.7 **不支持 GPU 加速视觉**：Windows 侧不含 CUDA 后端，视觉经 `mtmd` 走纯 CPU
  （25–69 秒/张）。
* 未在真实 NVIDIA GPU 上执行长上下文 Qwen3-VL 压测（构建机无 CUDA 运行时）。
* 原生运行期是 vendored 二进制集合，用户无法就地重建；修订号记录在
  `backend/native/VENDOR.md`。
