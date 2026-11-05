# 代码签名策略 / Code Signing Policy

> 本文档中英并排发布，两种语言具有同等效力；如有歧义，以英文版本为准。
> This document is published bilingually; both language versions are equivalent. The English version prevails in case of ambiguity.

---

## 简体中文

### 声明

Free code signing provided by SignPath.io, certificate by SignPath Foundation.

EchoDesk 的 Windows 交付物由 SignPath.io 提供的免费开源代码签名服务进行签名，签名证书由 SignPath Foundation 颁发。签名仅用于证明交付物来源与完整性，不改变项目的开源性质，也不代表商业背书。

### 签名范围

以下交付物包含于签名范围：

- Windows 安装程序 `EchoDesk-vX.Y.Z-Setup.exe`；
- 便携包内可执行文件 `app\EchoDesk.exe` 与 `app\backend\EchoDeskBackend.exe`。

macOS 与 Linux 归档当前不进行代码签名或公证；macOS 首次运行时需要用户在系统设置中手动允许。

### 角色与职责

发布流程中的角色均由项目当前维护者担任，具体如下：

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

1. 发布物由本仓库的源代码构建；构建脚本与依赖声明均在本仓库内可审计。
2. 只有带版本标签的提交才会进入签名流程，标签由 Approver 创建并推送。
3. 每次签名请求由 Approver 人工批准，批准前核对构建来源、提交哈希与交付物清单。
4. 签名完成后，交付物的 SHA-256 记录在发布清单（`SHA256SUMS.txt` 与 `MANIFEST.json`）中，供下载方核对。

### 验证

- Windows：在文件属性中查看“数字签名”页，签名者应显示为 SignPath Foundation。
- 所有平台：使用发布页提供的 `SHA256SUMS.txt` 核对下载文件的摘要。

### 相关文档

- 许可证：[LICENSE](LICENSE)（Apache License 2.0）
- 第三方组件清单：[docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md)
- 安全策略：[SECURITY.md](SECURITY.md)

---

## English

### Statement

Free code signing provided by SignPath.io, certificate by SignPath Foundation.

EchoDesk's Windows deliverables are signed through the free code-signing service provided by SignPath.io, with the signing certificate issued by SignPath Foundation. Signing attests to the origin and integrity of the deliverables only; it does not alter the open-source nature of the project and does not constitute a commercial endorsement.

### Scope

The following deliverables are covered by signing:

- the Windows installer `EchoDesk-vX.Y.Z-Setup.exe`;
- the executables inside the portable package: `app\EchoDesk.exe` and `app\backend\EchoDeskBackend.exe`.

The macOS and Linux archives are not code-signed or notarised at this time; on first launch, macOS requires the user to allow the application manually in System Settings.

### Roles and responsibilities

All roles in the release process are held by the project's current maintainer:

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

1. Deliverables are built from this repository's source; build scripts and dependency declarations are auditable in this repository.
2. Only commits carrying a version tag enter the signing workflow; tags are created and pushed by an Approver.
3. Each signing request is approved manually by an Approver, who verifies the build origin, commit hash, and artifact inventory before approval.
4. After signing, the SHA-256 of each deliverable is recorded in the release manifests (`SHA256SUMS.txt` and `MANIFEST.json`) so downloaders can verify them.

### Verification

- Windows: open the file's Properties and check the Digital Signatures tab; the signer should read SignPath Foundation.
- All platforms: compare downloaded files against the `SHA256SUMS.txt` published with the release.

### Related documents

- License: [LICENSE](LICENSE) (Apache License 2.0)
- Third-party component inventory: [docs/THIRD_PARTY_LICENSES.md](docs/THIRD_PARTY_LICENSES.md)
- Security policy: [SECURITY.md](SECURITY.md)
