# 代码签名策略 / Code Signing Policy

> 本文档中英并排发布，两种语言具有同等效力；如有歧义，以英文版本为准。
> This document is published bilingually; both language versions are equivalent. The English version prevails in case of ambiguity.

---

## 简体中文

### 当前状态：未签名

**EchoDesk 的 Windows 交付物目前没有代码签名。**

我们向 SignPath Foundation 提交了免费开源代码签名申请，本次申请**未获批准**，反馈理由是项目当前知名度不足，尚未达到该计划的准入标准。我们计划在项目满足条件后重新申请。**在证书真正签发之前，所有 Windows 构建一律视为未签名。**

因此，本文档中「目标签名范围」「发布与签名流程」和「验证」里关于数字签名的部分，描述的是**证书签发后的目标状态**，不是现状。我们不会把计划中的能力写成已经具备的能力；本节以下的内容都按这个前提阅读。

目标签名服务为 SignPath.io 提供的免费开源代码签名，证书由 SignPath Foundation 颁发（**这是申请计划中的服务与证书来源，不是当前状态**）：Free code signing provided by SignPath.io, certificate by SignPath Foundation.

### 这对下载和使用意味着什么

- Windows 可能显示「未知发布者」，SmartScreen 可能提示「Windows 已保护你的电脑」。这是未签名程序的预期行为：点击「更多信息」→「仍要运行」。
- 未签名**不等于**不安全，但它确实意味着「发布者身份」没有第三方背书。请用下一节的手段自行核对来源与完整性。
- 如果所在组织的策略禁止运行未签名程序，请由管理员评估是否放行，或直接从源码自行构建 —— 构建脚本、依赖锁定与可复现构建实现都在仓库内。

### 当前如何验证（不依赖代码签名）

1. **核对 SHA-256。** 发布页提供 `SHA256SUMS.txt`，每个归档内另有 `MANIFEST.json`；两者都列出交付物的摘要。
2. **自己重建。** 按仓库中的构建说明，从同一个版本标签执行两次完整构建，逐项比对两次产物的 SHA-256。仓库提供 `tools/verify_reproducible_build.ps1`，它会在两次构建之间清除 `bin/`、`obj/`、`__pycache__/`、`dist/` 与发布输出，然后比对安装程序、三平台归档、清单与发布树内全部文件。
3. **核对源码与标签。** 发布物对应的提交由版本标签指向；打包脚本在构建前校验工作区是否干净，不干净的树下不会产出发布物。

以上三条都不需要信任我们的服务器或证书 —— 这正是可复现构建的意义。

### 目标签名范围（证书签发后）

以下交付物**计划**纳入签名范围：

- Windows 安装程序 `EchoDesk-vX.Y.Z-Setup.exe`；
- 便携包内可执行文件 `app\EchoDesk.exe` 与 `app\backend\EchoDeskBackend.exe`。

macOS 与 Linux 归档当前不进行代码签名或公证；macOS 首次运行时需要用户在系统设置中手动允许。这一状态不因 Windows 签名申请而改变。

### 角色与职责

以下角色说明沿用申请材料，反映项目当前的实际维护者结构；它本身不构成「已签名」的声明。

| 角色 | 人员 | GitHub |
| :--- | :--- | :--- |
| Authors / 作者 | wyx-spark (Void-Entropia-Studio) | [@wyx-spark](https://github.com/wyx-spark) |
| Reviewers / 审核者 | wyx-spark | [@wyx-spark](https://github.com/wyx-spark) |
| Approvers / 批准者 | wyx-spark | [@wyx-spark](https://github.com/wyx-spark) |

- **Authors（作者）**：编写并提交代码、构建脚本与发布配置。
- **Reviewers（审核者）**：在合并与发布前审阅变更，确认构建输入、依赖与产物的对应关系。
- **Approvers（批准者）**：对每一次签名请求作出批准决定，确认该构建来自本仓库的受信任提交。

### 多因素认证

所有参与发布流程的成员均已启用 GitHub 多因素认证（MFA / 2FA）。

### 发布与签名流程

**当前（未签名）**：只有带版本标签的提交才会进入发布流程，标签由 Approver 创建并推送；打包前校验工作区干净，发布物的 SHA-256 记录在发布清单（`SHA256SUMS.txt` 与 `MANIFEST.json`）中，供下载方核对。区别只在于「签名」这一步尚不存在。

**证书签发后**：在上述流程之上，每次签名请求由 Approver 人工批准，批准前核对构建来源、提交哈希与交付物清单；签名完成后，摘要照常写入发布清单。

### 验证

- **当前**：使用发布页的 `SHA256SUMS.txt` 核对下载文件，或按上文自行重建后逐字节比对。
- **证书签发后**：在文件属性中查看「数字签名」页，签名者应显示为 SignPath Foundation。

### 相关文档

- 许可证：[LICENSE](LICENSE)（Apache License 2.0）
- 第三方组件清单：[docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md)
- 安全策略：[SECURITY.md](SECURITY.md)
- 可复现构建说明：[docs/DEVELOPER.md](docs/DEVELOPER.md)

---

## English

### Current status: unsigned

**EchoDesk's Windows deliverables are not code-signed at this time.**

We applied for free open-source code signing through SignPath Foundation. **The application was not approved this time**, on the grounds that the project is not yet widely known enough to meet the programme's criteria. We intend to re-apply once the project meets them. **Until a certificate is actually issued, every Windows build must be treated as unsigned.**

Sections below that describe digital signatures — “Intended scope”, “Release and signing workflow” and “Verification” — therefore describe the **target state after a certificate is issued**, not the current state. We will not present a planned capability as an existing one; please read the rest of this document with that in mind.

The intended service is free code signing provided by SignPath.io, with the certificate issued by SignPath Foundation (**this is the service and certificate source in the application, not the current state**): Free code signing provided by SignPath.io, certificate by SignPath Foundation.

### What this means for downloads

- Windows may show an “Unknown publisher” warning, and SmartScreen may say “Windows protected your PC”. This is the expected behaviour for an unsigned program: choose **More info** → **Run anyway**.
- Unsigned does **not** mean unsafe, but it does mean the publisher's identity carries no third-party attestation. Verify origin and integrity yourself with the steps in the next section.
- If your organisation's policy forbids running unsigned programs, have an administrator review it, or build from source — the build scripts, dependency locks and reproducible-build implementation are all in the repository.

### How to verify today (without a signature)

1. **Check the SHA-256.** Each release publishes `SHA256SUMS.txt`, and every archive contains a `MANIFEST.json`; both list the digests of the deliverables.
2. **Rebuild it yourself.** Following the build instructions, run two complete builds from the same version tag and compare the SHA-256 of every artifact. The repository ships `tools/verify_reproducible_build.ps1`, which clears `bin/`, `obj/`, `__pycache__/`, `dist/` and the publish output between the two runs, then compares the installer, the three platform archives, the manifests and every file inside the release tree.
3. **Check the source and the tag.** The commit a release was built from is the one its version tag points to; the packaging scripts refuse to produce a release from a dirty worktree.

None of these three steps require trusting our servers or our certificate — which is the point of reproducible builds.

### Intended signing scope (after a certificate is issued)

The following deliverables are **planned** to be covered by signing:

- the Windows installer `EchoDesk-vX.Y.Z-Setup.exe`;
- the executables inside the portable package: `app\EchoDesk.exe` and `app\backend\EchoDeskBackend.exe`.

The macOS and Linux archives are not code-signed or notarised at this time; on first launch, macOS requires the user to allow the application manually in System Settings. That status is unaffected by the Windows signing application.

### Roles and responsibilities

The roles below are carried over from the application materials and reflect the project's actual maintainer structure; they are not, by themselves, a claim that signing is in place.

| Role | Person | GitHub |
| :--- | :--- | :--- |
| Authors | wyx-spark (Void-Entropia-Studio) | [@wyx-spark](https://github.com/wyx-spark) |
| Reviewers | wyx-spark | [@wyx-spark](https://github.com/wyx-spark) |
| Approvers | wyx-spark | [@wyx-spark](https://github.com/wyx-spark) |

- **Authors** write and commit the source code, build scripts, and release configuration.
- **Reviewers** review changes before merge and release, confirming that build inputs, dependencies, and artifacts correspond.
- **Approvers** decide on each signing request, confirming that the build originates from a trusted commit of this repository.

### Multi-factor authentication

All members involved in the release process have GitHub multi-factor authentication (MFA / 2FA) enabled.

### Release and signing workflow

**Today (unsigned):** only commits carrying a version tag enter the release workflow, and tags are created and pushed by an Approver. Packaging verifies that the worktree is clean, and the SHA-256 of every deliverable is recorded in the release manifests (`SHA256SUMS.txt` and `MANIFEST.json`) for downloaders to verify. The only missing step is signing itself.

**After a certificate is issued:** on top of the workflow above, each signing request is approved manually by an Approver, who verifies the build origin, commit hash, and artifact inventory before approval; the digests are then recorded in the release manifests as usual.

### Verification

- **Today:** compare downloaded files against the `SHA256SUMS.txt` published with the release, or rebuild as described above and compare byte for byte.
- **After a certificate is issued:** open the file's Properties and check the Digital Signatures tab; the signer should read SignPath Foundation.

### Related documents

- License: [LICENSE](LICENSE) (Apache License 2.0)
- Third-party component inventory: [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md)
- Security policy: [SECURITY.md](SECURITY.md)
- Reproducible-build instructions: [docs/DEVELOPER.md](docs/DEVELOPER.md)
