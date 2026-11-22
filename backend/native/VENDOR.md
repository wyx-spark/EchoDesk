# 原生推理库出处（vendored native runtime: llama.cpp via llama-cpp-python wheels）

本目录下的 `linux-x64/`、`osx-arm64/`、`win-x64/` 是**构建期从 wheel 中抽取的二进制**，
三种平台统一到同一修订（`llama-cpp-python` 0.4.7 系列）。

## 1. 来源

| 平台 | wheel 文件名 | 版本 | 索引 | 下载日期 |
| --- | --- | --- | --- | --- |
| win-x64 | `llama_cpp_python-0.4.7+cu132-cp313-cp313-win_amd64.whl` | `0.4.7+cu132` | `https://github.com/JamePeng/llama-cpp-python/releases/tag/v0.4.7-cu132-win-20261113` | 2026-11-13（随 T3-3 引入） |
| linux-x64 | `llama_cpp_python-0.4.7+cu132-cp313-cp313-linux_x86_64.whl` | `0.4.7+cu132` | `https://github.com/JamePeng/llama-cpp-python/releases/tag/v0.4.7-cu132-linux-20261112` | 2026-11-22 |
| osx-arm64 | `llama_cpp_python-0.4.7-cp313-cp313-macosx_11_0_arm64.whl` | `0.4.7` | `https://github.com/JamePeng/llama-cpp-python/releases/tag/v0.4.7-Metal-macos-20261113` | 2026-11-22 |

具体资产的下载地址（GitHub release asset）：

- linux: `https://github.com/JamePeng/llama-cpp-python/releases/download/v0.4.7-cu132-linux-20261112/llama_cpp_python-0.4.7%2Bcu132-cp313-cp313-linux_x86_64.whl`
- osx: `https://github.com/JamePeng/llama-cpp-python/releases/download/v0.4.7-Metal-macos-20261113/llama_cpp_python-0.4.7-cp313-cp313-macosx_11_0_arm64.whl`

## 2. wheel 摘要

| wheel | 字节数 | sha256 | 说明 |
| --- | --- | --- | --- |
| `llama-0.4.7-linux.whl` | 206,436,541 | `f125db647857e603f15c360555105d7d717f2f6c0524a15877d5b0cdf773879c` | 取自 GitHub release asset `digest`；本机未整包落盘（仅用 HTTP Range 读取中央目录与所需成员） |
| `llama-0.4.7-macos.whl` | 18,525,463 | `d0267e6ee0ce8ad1967ea0dfaa3ff88498840ee104c993107c3e41be8f62b076` | 本机落盘文件 sha256 与 GitHub release asset `digest` 一致 |

## 3. 抽取清单

只抽取 `llama_cpp/lib/` 下的 CPU/加速后端库；CUDA 专属库（`libggml-cuda.so` 等）不随本仓库分发。

| 平台 | 文件 | 字节数 | sha256 |
| --- | --- | --- | --- |
| linux-x64 | `libggml-base.so` | 862,296 | `4c84d668eae56051d1c9de5b81d31fe9949323bc57949691c94045e300eaa106` |
| linux-x64 | `libggml-base.so.0` | 862,296 | `4c84d668eae56051d1c9de5b81d31fe9949323bc57949691c94045e300eaa106` |
| linux-x64 | `libggml-base.so.0.29.0` | 862,296 | `4c84d668eae56051d1c9de5b81d31fe9949323bc57949691c94045e300eaa106` |
| linux-x64 | `libggml-cpu-alderlake.so` | 1,358,288 | `947db45024ab8e29976b5bfbdac7fdab3941925ef770a47e97de6f54a8202b58` |
| linux-x64 | `libggml-cpu-cannonlake.so` | 1,513,936 | `2b69fb6145c45e66f44a9a910282251985206d51c33d363f86326c7e36071d9e` |
| linux-x64 | `libggml-cpu-cascadelake.so` | 1,526,224 | `718411f95a7c54dfca0da37c2c52451f082b23e44cc6280d5ccee85517821aad` |
| linux-x64 | `libggml-cpu-cooperlake.so` | 1,526,224 | `50245f9190cad6b71f3e12a60573716e2fea7b674fca3cd8a1aebad2fd3223b4` |
| linux-x64 | `libggml-cpu-haswell.so` | 1,362,384 | `68c8ac90f2a64786f5346aa020ec50eaa99c6ee7a6bafefc14077f3641fe4920` |
| linux-x64 | `libggml-cpu-icelake.so` | 1,526,224 | `c8fe43d2d7e5763add07c18076d30adf503d9a2da6ab6f404ed64c72c458ebc6` |
| linux-x64 | `libggml-cpu-ivybridge.so` | 1,301,072 | `9a141c644c70fbfe094dd4d0011b89c62ce22b8024c3f3f5cfcf632924d1950b` |
| linux-x64 | `libggml-cpu-piledriver.so` | 1,296,976 | `92d57a7cff4d24079769ad3af231147ed64ef3a7737f3f297951f85825da87b1` |
| linux-x64 | `libggml-cpu-sandybridge.so` | 1,272,400 | `a3404c9f7488be51e7f2f0c7cb3286b0efe977444e48fd24ab42633835511ac0` |
| linux-x64 | `libggml-cpu-sapphirerapids.so` | 1,776,328 | `0231d8241084fcb8b4764423d8dbc9434a8c9639af0089fe2269103cedfa3ea9` |
| linux-x64 | `libggml-cpu-skylakex.so` | 1,513,936 | `1c18ef7c874a0acb5c0393590c92dab3ab2b6794594272065881b48948e10d66` |
| linux-x64 | `libggml-cpu-sse42.so` | 903,792 | `1f1977049f93d8b62c8df991f850fbcf27b5f130e34c2a5227a044c08fa549f2` |
| linux-x64 | `libggml-cpu-x64.so` | 903,792 | `d6ed857df8ab564994e4c75268f677d192bc21e487d487127d7edc8ae6d567de` |
| linux-x64 | `libggml-cpu-zen4.so` | 1,526,224 | `d0164be08c1e6df0d2abc4b018d3f736e7a210a6a767759a8f92946945427fe2` |
| linux-x64 | `libggml-rpc.so` | 258,424 | `6d7e74072fc5df17b48add64e25212a1c66cbc27fd745b3741193f6ec97d0e78` |
| linux-x64 | `libggml.so` | 43,696 | `03c49a3a865338a9d668670e97ac9f6efdc17a6b6f1829635d5978adfa7af0bc` |
| linux-x64 | `libggml.so.0` | 43,696 | `03c49a3a865338a9d668670e97ac9f6efdc17a6b6f1829635d5978adfa7af0bc` |
| linux-x64 | `libggml.so.0.29.0` | 43,696 | `03c49a3a865338a9d668670e97ac9f6efdc17a6b6f1829635d5978adfa7af0bc` |
| linux-x64 | `libllama.so` | 4,748,184 | `d905ae4291e57d128f9b194ed4280c24926afae91674a8af5f15c9eebea2e90d` |
| linux-x64 | `libllama.so.0` | 4,748,184 | `d905ae4291e57d128f9b194ed4280c24926afae91674a8af5f15c9eebea2e90d` |
| linux-x64 | `libllama.so.0.9.0` | 4,748,184 | `d905ae4291e57d128f9b194ed4280c24926afae91674a8af5f15c9eebea2e90d` |
| linux-x64 | `libmtmd.so` | 2,123,648 | `32b9870e34560769c332557212ac79a20889f3f97f1fa698363fb347638f0d2e` |
| linux-x64 | `libmtmd.so.SOVERSION` | 2,123,648 | `32b9870e34560769c332557212ac79a20889f3f97f1fa698363fb347638f0d2e` |
| osx-arm64 | `libggml-base.0.29.0.dylib` | 691,400 | `c11fc238e8698a2be62411355a494e4e985a705087c51948f0833c8b91c2003e` |
| osx-arm64 | `libggml-base.0.dylib` | 691,400 | `c11fc238e8698a2be62411355a494e4e985a705087c51948f0833c8b91c2003e` |
| osx-arm64 | `libggml-base.dylib` | 691,400 | `c11fc238e8698a2be62411355a494e4e985a705087c51948f0833c8b91c2003e` |
| osx-arm64 | `libggml-blas.0.29.0.dylib` | 54,936 | `53c3f285729460f8e8d44245d2bbe3e737194ffda53e21f64c65248799393bdb` |
| osx-arm64 | `libggml-blas.0.dylib` | 54,936 | `53c3f285729460f8e8d44245d2bbe3e737194ffda53e21f64c65248799393bdb` |
| osx-arm64 | `libggml-blas.dylib` | 54,936 | `53c3f285729460f8e8d44245d2bbe3e737194ffda53e21f64c65248799393bdb` |
| osx-arm64 | `libggml-cpu.0.29.0.dylib` | 935,368 | `470afaef744c664e681a375bdbadd5cbc7c846e6595561112d0411f3c298f9f8` |
| osx-arm64 | `libggml-cpu.0.dylib` | 935,368 | `470afaef744c664e681a375bdbadd5cbc7c846e6595561112d0411f3c298f9f8` |
| osx-arm64 | `libggml-cpu.dylib` | 935,368 | `470afaef744c664e681a375bdbadd5cbc7c846e6595561112d0411f3c298f9f8` |
| osx-arm64 | `libggml-metal.0.29.0.dylib` | 5,664,616 | `bee4c2bd05b5d153eb374155f7cb0f44ad8628b14bace937d3387e634b98d043` |
| osx-arm64 | `libggml-metal.0.dylib` | 5,664,616 | `bee4c2bd05b5d153eb374155f7cb0f44ad8628b14bace937d3387e634b98d043` |
| osx-arm64 | `libggml-metal.dylib` | 5,664,616 | `bee4c2bd05b5d153eb374155f7cb0f44ad8628b14bace937d3387e634b98d043` |
| osx-arm64 | `libggml-rpc.0.29.0.dylib` | 191,208 | `fedbdd39f6cfb8daaba8ebe031ee4a0d023eeb9480bb0456b47a48fa829d8503` |
| osx-arm64 | `libggml-rpc.0.dylib` | 191,208 | `fedbdd39f6cfb8daaba8ebe031ee4a0d023eeb9480bb0456b47a48fa829d8503` |
| osx-arm64 | `libggml-rpc.dylib` | 191,208 | `fedbdd39f6cfb8daaba8ebe031ee4a0d023eeb9480bb0456b47a48fa829d8503` |
| osx-arm64 | `libggml.0.29.0.dylib` | 56,616 | `fa2e0e40cc769a8fbfcef03e7898db9a46270e7da951f9342776459cb178a2b3` |
| osx-arm64 | `libggml.0.dylib` | 56,616 | `fa2e0e40cc769a8fbfcef03e7898db9a46270e7da951f9342776459cb178a2b3` |
| osx-arm64 | `libggml.dylib` | 56,616 | `fa2e0e40cc769a8fbfcef03e7898db9a46270e7da951f9342776459cb178a2b3` |
| osx-arm64 | `libllama.0.9.0.dylib` | 3,306,552 | `5143eeddd6908f808dd34c1ee97544297e35911a3bc4bc2bb75e0b8d6a875bbc` |
| osx-arm64 | `libllama.0.dylib` | 3,306,552 | `5143eeddd6908f808dd34c1ee97544297e35911a3bc4bc2bb75e0b8d6a875bbc` |
| osx-arm64 | `libllama.dylib` | 3,306,552 | `5143eeddd6908f808dd34c1ee97544297e35911a3bc4bc2bb75e0b8d6a875bbc` |
| osx-arm64 | `libmtmd.SOVERSION.dylib` | 1,547,672 | `cdcf108da0cd35f0c388e8e0ef041edf5dcfcf7504440ea2275e3191fa8ad597` |
| osx-arm64 | `libmtmd.dylib` | 1,547,672 | `cdcf108da0cd35f0c388e8e0ef041edf5dcfcf7504440ea2275e3191fa8ad597` |

## 4. 运行期依赖说明

这些二进制是**构建期从 wheel 中抽取**的，`llama-cpp-python` **不是本项目的运行期依赖**：
`requirements.txt` / `requirements.lock` 均不包含该包，运行期 `import llama_cpp` 不可用；
推理只经 `backend/llama_native.py` → `backend/llama_ctypes.py` / `llama_capi.py` 调用本目录的动态库。

## 5. 许可

llama.cpp 为 MIT 许可（上游 `https://github.com/ggml-org/llama.cpp`），
此处二进制来自 `JamePeng/llama-cpp-python` 0.4.7 的分发 wheel；
`llama-cpp-python` 本身同为 MIT。

## 6. 修订一致性证明

三平台 `libllama` / `llama.dll` 的修订标记一致（14 个该修订独有记号三套齐全，
3 个仅旧修订（0.3.x）存在的记号三套均缺席）：

```text
MARKER llama_model_muse_spark   win=yes linux=yes osx=yes
MARKER llama_model_beam         win=yes linux=yes osx=yes
MARKER llama_model_flint        win=yes linux=yes osx=yes
MARKER llama_moe_cache          win=yes linux=yes osx=yes
MARKER attn_vres_lambda         win=yes linux=yes osx=yes
MARKER is_scaled_low_precision  win=yes linux=yes osx=yes
MARKER muse-spark               win=yes linux=yes osx=yes
MARKER kimi-k31                 win=yes linux=yes osx=yes
MARKER deepseek41               win=yes linux=yes osx=yes
MARKER lilicorr                 win=yes linux=yes osx=yes
MARKER prism.hadamard           win=yes linux=yes osx=yes
MARKER attention rotation       win=yes linux=yes osx=yes
MARKER PLE head ranges          win=yes linux=yes osx=yes
MARKER minimax-01               win=yes linux=yes osx=yes
NEGATIVE Gated Delta Net        win=no  linux=no  osx=no
NEGATIVE autoregressive         win=no  linux=no  osx=no
NEGATIVE chunked                win=no  linux=no  osx=no
VERSION libllama 0.9.0          win=yes linux=yes osx=yes
VERSION ggml-base 0.29.0        win=yes linux=yes osx=yes
REVISION consistent=yes
```

旧对照（已废弃，留作证据）：`reader` 分支的 linux/osx 库来自 `0.3.36` 与 `0.3.30`，
与 win 的 `0.4.7` 不是同一修订，因此不再从该分支取用。
