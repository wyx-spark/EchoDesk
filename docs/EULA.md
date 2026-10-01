# EchoDesk End User License Agreement (EULA)

> **Chinese version:** a reference translation of this agreement is available at
> [`EULA.zh-Hans.md`](EULA.zh-Hans.md). If the two ever disagree, this English version prevails.

Version: v0.2.1
Copyright: Copyright (C) Void Entropia Studio
Publisher: Void Entropia Studio, Online only (hereinafter "the Studio")
> Studio name mapping: **Void Entropia Studio = 熵寂工作室**. `Void Entropia Studio` /
> `VoidEntropiaStudio` is the legal-entity identifier used in this agreement and in all
> English documents; the Chinese translation uses 熵寂工作室. The legal entity is
> governed by **this English version**.
License for the project itself: **Apache License 2.0** (see [`../LICENSE`](../LICENSE) and [`../NOTICE`](../NOTICE) in the repository root)

Please read this agreement before using EchoDesk (hereinafter "the Software"). By downloading,
installing or using the Software you confirm that you have read, understood and agreed to all its
terms. If you do not agree, do not use the Software and delete it.

Related documents: [`PRIVACY.md`](PRIVACY.md) (Privacy Policy: network behaviour and data
processing), [`SECURITY.md`](../SECURITY.md) (security policy and vulnerability reporting),
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) (list of third-party components and their
Licenses), [`SBOM.json`](SBOM.json) (software bill of materials),
[`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md) / [`en/COMPLIANCE.md`](en/COMPLIANCE.md)
(enterprise-environment adaptation and compliance).

---

## 1. Grant of License

1. The Software is released as open source under the **Apache License 2.0**. Under that License you
   are granted a worldwide, non-exclusive, royalty-free copyright License to **use, reproduce,
   modify, merge, publish, distribute, sublicense and sell** copies of the Software, subject to the
   conditions of Apache-2.0 (the most important of which are that you retain the copyright, License
   and disclaimer notices and state in modified files that you changed them; and if the
   distribution includes the [`../NOTICE`](../NOTICE) attribution file, your distribution must
   retain its contents as well).
2. The Studio additionally confirms here a License granted for the convenience of end users: you may
   install and run the Software on devices that you own or lawfully control, you may copy the
   Software for backup purposes, and you may share a complete copy of the Software with others,
   provided it is unmodified, undivided and retains all rights notices.
3. This agreement **does not restrict or reduce** any right already granted to you by Apache-2.0. If
   any term of this agreement conflicts with Apache-2.0, **Apache-2.0 prevails**.

## 2. Usage Restrictions

1. You must not remove or alter the rights notices in the Software, such as copyright, trademark and
   version resources (this is one of the distribution conditions required by Apache-2.0).
2. You must not pass off the Software or a derivative work of it as a product developed
   independently by you, nor use the Studio's name or trademarks to endorse any version that is not
   the Studio's.
3. You must not use the Software's automation features for purposes that violate the terms of
   service of the target website, infringe the rights or interests of others, or breach laws or
   regulations.
4. On reverse engineering: Apache-2.0 itself grants you the right to read, modify and redistribute
   the source code, and therefore **this agreement imposes no reverse-engineering restriction on the
   source code**. As to the third-party binary components provided with the distribution (for
   example an embedded native inference library), if their own Licenses provide otherwise, those
   Licenses govern.

## 3. Third-Party Components and Open-Source Licenses

1. The Software contains third-party open-source components. **The complete list, versions and
   Licenses** are in [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md), and a machine-readable
   version is in [`SBOM.json`](SBOM.json) (an SPDX-style bill of materials, which can be
   regenerated with `python tools/gen_sbom.py`).
2. Third-party components are licensed under their own respective Licenses. **This agreement does
   not change the rights you enjoy under those Licenses**, nor does it release you from the
   obligations you owe under them.
3. License status of the current release (v0.2.1):
   - The distribution contains **no AGPL components**. As of v0.2.1, PDF text extraction uses
     **BSD-3-Clause** `pypdf` instead of AGPL-3.0 PyMuPDF; the remaining components of the file
     parsing chain (Word/Excel/PPT/PDF/TXT) are all MIT / BSD-style permissive Licenses.
   - The **only GPL component in the distribution is the build tool PyInstaller** (GPLv2 together
     with the official "bundled product exception"): that exception expressly permits using it to
     bundle and distribute proprietary programs, and it is used only for building, not as a library
     within the product, and therefore **your distribution is not required to be open-sourced**.
   - The front end is a self-contained .NET deployment; what ships with it are Microsoft .NET / WPF
     runtime assemblies (MIT), and no third-party NuGet package is referenced.
4. **Note on historical versions**: the distributions of v0.2.0 and earlier contain AGPL-3.0
   PyMuPDF. If you intend to distribute those **older versions** externally, assess the AGPL
   copyleft obligations yourself, or move to v0.2.1 or later.
5. **Personal use and use within an organisation** do not constitute "external distribution", and
   the copyleft obligations above are not triggered.

## 4. Local Models

1. The Software itself **contains no** large language model weights. Models are downloaded or
   supplied by you, and the License for a model file is agreed separately by the model's publisher
   (for example the Qwen series follows its own respective model License).
2. You must confirm for yourself that the License of the model you use permits your use case; the
   availability, content and compliance of the model download sources (`hf-mirror.com` or
   `huggingface.co`) are the responsibility of those sites, and the Studio gives no warranty
   regarding them.
3. Model files are by default stored under the local program directory, and no network request is
   made unless you actively trigger a download (for the item-by-item description see section 2.1 of
   [`PRIVACY.md`](PRIVACY.md)).

## 5. Network Features and Your Choices

By default the Software runs only locally; **model download, web search, the skill marketplace,
update checks and browser automation** are the five features that access the network, all of which
require you to enable them actively, and each of which can be turned off. What each feature sends,
what it does not send, where the data goes, how long it is retained and how to turn it off are
described in section 2 of [`PRIVACY.md`](PRIVACY.md). For how to disable all network access at once
in an enterprise environment, see [`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md) /
[`en/COMPLIANCE.md`](en/COMPLIANCE.md).

If your organisation prohibits business data from leaving the jurisdiction, obtain authorisation
before enabling **web search** (search keywords leave the jurisdiction) and the **skill
marketplace** (repository names leave the jurisdiction); conversations and document contents
themselves do not leave the jurisdiction.

## 6. Responsibility for Automation Features and Safety Rules

1. Before enabling automation you **must actively tick the safety protocol**. The text of that
   protocol has been localised (language-pack key `settings.automation.safetyBody`), and the
   protocol version together with the time of your consent is recorded in the local audit log so
   that it can be traced afterwards. The protocol states expressly:
   - automation **runs only locally**, uses a headed browser, and you can observe the process at any
     time;
   - EchoDesk **does not upload** your task content, downloaded files or generated results;
   - all downloaded files first land in the local temporary directory and are cleaned up once task
     processing is complete; originals and results that need to be kept are stored in the `results\`
     directory, which you manage and delete yourself;
   - you must ensure that the operation complies with the rules of your organisation;
   - consequences arising from non-compliant use are borne by the user.
2. **Responsibility**: the automation feature is a tool that "operates the browser on your behalf
   according to your configuration". **You must confirm for yourself**:
   - that the target website's **terms of service** permit this kind of automated access (including
     access frequency, scraping scope and the manner in which accounts are used);
   - that **your organisation's rules and regulations** (information security, data classification,
     access to external websites, record-keeping and approval requirements) permit the operation;
   - that downloading, parsing and processing these files meets **data protection and
     confidentiality** requirements;
   - that your login state in the automated browser (`browser_state.json`) is properly safeguarded
     and is not left on a shared machine for long periods.
3. Any dispute, ban, data breach or compliance consequence arising from an automation task that you
   configure or run is **borne by the user**, and Void Entropia Studio accepts no liability.

## 7. Responsibility for Skills (Plug-ins)

1. Skills are provided by third parties. EchoDesk **does not redistribute** skill code in any way,
   and is not responsible for its functionality, quality or security; a skill's own License is
   declared by its author, and the settings window displays that declaration for you to judge.
2. Skill code executes inside the EchoDesk backend process and **with the permissions of your
   Windows account**. When a local plug-in is loaded for the first time, the program pops up a
   confirmation dialog showing its declared capabilities, but this is **advance notice** and does
   not constitute runtime isolation or a sandbox.
3. Therefore, **install only skills that you trust or have reviewed**; before installing, read their
   permission statements, README and License. Data breaches, file corruption or compliance problems
   caused by a skill are borne by that skill's author and the user.

## 8. Disclaimer of Warranty

1. The Software is provided "as is", without any express or implied warranty, including but not
   limited to warranties of merchantability, fitness for a particular purpose and non-infringement.
   Section 7 of Apache-2.0 likewise provides that the software is supplied "as is".
2. The Software generates content with a small local-parameter model, and **its output may contain
   errors, omissions or inaccuracies**; it does not constitute legal, financial, medical or other
   professional advice. Verify matters yourself when important decisions are involved.
3. The automation features operate the browser and download files on your behalf; the allocation of
   responsibility for them and the matters you must confirm yourself are set out in section 6, and
   responsibility in relation to skills (plug-ins) is set out in section 7.
4. The Software promises no online service of any kind: the availability, rate limits and content
   of model download sources, search sources and the GitHub API are all determined by third
   parties, and changes to or interruptions of them are not defects of the Software.

## 9. Limitation of Liability

1. To the maximum extent permitted by applicable law, **the Studio accepts no liability** for any
   direct, indirect, incidental, special, punitive or consequential loss arising from the use of or
   inability to use the Software (including but not limited to loss of profit, business
   interruption, loss of data, file corruption, or third-party claims).
2. Where applicable law does not permit liability to be excluded entirely, the Studio's aggregate
   liability is capped at **the amount you actually paid for the Software** (where the Software is
   provided free of charge, that cap is 0).
3. This section does not exclude liability that cannot be excluded by law (for example liability
   arising from intent or gross negligence), nor does it limit rights you enjoy under law beyond the
   scope of the exclusions permitted by sections 7 and 8 of Apache-2.0.

## 10. Termination

If you breach this agreement (in particular the usage restrictions in section 2), the additional
License granted to you under this agreement terminates automatically, and you must stop using the
Software and delete all copies of it. The rights granted to you by Apache-2.0 do not terminate
because this agreement terminates; the conditions for their termination are governed by Apache-2.0
itself.

## 11. Governing Law and Dispute Resolution

1. This agreement is interpreted under the laws of **the place where the Studio is established**
   (excluding conflict-of-law rules).
2. Disputes arising from this agreement shall first be settled through friendly negotiation between
   the parties; if negotiation fails, they shall be submitted to a court with jurisdiction at the
   place where the Studio is established.
3. If a term is held invalid or unenforceable, the remaining terms remain in force; that invalid
   term shall be replaced, to the minimum extent necessary, by a valid term closest to its original
   meaning.

## 12. Contact

- **Security vulnerabilities**: use the private reporting channel in [`SECURITY.md`](../SECURITY.md)
  and do **not** open a public Issue. The private advisory form is
  `https://github.com/VoidEntropiaStudio/EchoDesk/security/advisories/new`. The fallback email
  address is `VoidEntropiaStudio@users.noreply.github.com`.
- **General questions and licensing and compliance enquiries (including questions about the terms
  of this agreement and legal notices)**: submit them through GitHub Issues at
  `https://github.com/VoidEntropiaStudio/EchoDesk/issues`; for discussion use GitHub Discussions at
  `https://github.com/VoidEntropiaStudio/EchoDesk/discussions`; for usage questions you can first
  check [`zh-Hans/FAQ.md`](zh-Hans/FAQ.md) / [`en/FAQ.md`](en/FAQ.md).
- The Studio **does not provide** telephone support or instant-messaging support channels.

## 13. Changes to This Agreement

1. This agreement is updated with each version. Any change will be written into the `docs/EULA.md`
   of the new version, and its key points described in the corresponding version entry of
   [`CHANGELOG.md`](../CHANGELOG.md).
2. Changes are **not retroactive**: continued use of a new version is subject to the agreement
   shipped with that version; if you do not agree to the changed terms, stop using that new version
   (rights already acquired under the terms of the previous version are unaffected).
3. The "version" stated at the top of this file is the version of this agreement and is consistent
   with the version number of the Software.

## 14. Miscellaneous

1. Where the Software is a green portable build, deleting the program folder completes
   uninstallation (no registry entries are written and no service is installed); for the installed
   build, use the uninstaller.
2. Data left behind after uninstallation — sessions, frequently used phrases, automation results and
   so on — resides in the program directory and the user data directory, and can be cleared by
   deleting the corresponding files (see section 7 of [`PRIVACY.md`](PRIVACY.md) for the list).
3. If this agreement and [`PRIVACY.md`](PRIVACY.md) conflict in their wording, matters concerning
   **data processing** are governed by [`PRIVACY.md`](PRIVACY.md), and matters concerning
   **License grants** are governed by [`../LICENSE`](../LICENSE) (Apache-2.0).
4. **Reading protected documents.** Where the Software reads Office documents through a locally
   installed WPS installation — including documents protected by an enterprise document-encryption
   system — the behaviour, prerequisites and responsibility boundaries are described in
   [`ENCRYPTED_DOCUMENTS.md`](ENCRYPTED_DOCUMENTS.md) (Chinese:
   [`ENCRYPTED_DOCUMENTS.zh-Hans.md`](ENCRYPTED_DOCUMENTS.zh-Hans.md)), which forms part of the
   description of the Software's behaviour under this agreement. The Software does not crack, break
   or bypass any encryption mechanism, and **you are responsible** for confirming that your
   organisation's rules permit protected documents to be read programmatically on this machine and
   that the account you use lawfully holds access rights to the document.
