# EchoDesk Encrypted Documents

> **Chinese version:** a reference translation of this document is available at
> [`ENCRYPTED_DOCUMENTS.zh-Hans.md`](ENCRYPTED_DOCUMENTS.zh-Hans.md). If the two ever disagree, this English version prevails.

Version: v0.2.2
Publisher: Void Entropia Studio
Applies to: reading Office documents (Word / Excel / PowerPoint) with EchoDesk on a machine that has an enterprise document-encryption client installed

Related documents: [`PRIVACY.md`](PRIVACY.md) (Privacy Policy: network behaviour and data
processing), [`EULA.md`](EULA.md) (license agreement), [`../SECURITY.md`](../SECURITY.md) (security
policy and vulnerability reporting), [`zh-Hans/COMPLIANCE.md`](zh-Hans/COMPLIANCE.md) /
[`en/COMPLIANCE.md`](en/COMPLIANCE.md) (enterprise-environment adaptation and compliance).

This document describes, in user-readable terms, how EchoDesk behaves when it reads Office documents
that are protected by an enterprise document-encryption system: what it does, what it requires from
the machine, what it deliberately does not do, and where responsibility lies. It is a behaviour and
compliance statement, not a promise of functionality — whether the behaviour below happens at all
depends entirely on the encryption product installed on your machine and on the access rights your
organisation has granted you.

---

## 1. Behavior

When EchoDesk reads an Office document, it **first tries to open the file through the WPS
installation on this machine** and reads the document content from WPS.

If the file is protected by an enterprise document-encryption system (for example a
document-encryption product such as Cobra DocGuard), and the corresponding client is installed on
this machine and has granted the current user access to that document, then WPS completes the
decryption **in memory**. The file on disk stays encrypted; WPS hands the plaintext to the
requesting application through the ordinary interfaces of an authorised office application, without
writing an unencrypted copy to disk. EchoDesk reads that plaintext from WPS and uses it for local
processing.

The sequence is:

| Step | What happens | Where the plaintext is |
| --- | --- | --- |
| 1 | You ask EchoDesk to read an Office document (Word / Excel / PowerPoint) | — |
| 2 | EchoDesk hands the file to the WPS installation on this machine | — |
| 3 | If the file is protected, the enterprise encryption client authorises WPS to open it for the current user | in memory only (the file on disk remains ciphertext) |
| 4 | WPS returns the document content to EchoDesk | in memory only |
| 5 | EchoDesk processes that content locally (parsing, chunked summarization, local model inference) | in memory only |

This is the **preferred path** for reading Office documents. If WPS is not installed, or if the file
cannot be read through WPS for any reason, EchoDesk falls back to the generic document parser (see
section 2).

## 2. Prerequisites

The behaviour described in section 1 depends on components that are **installed and configured on
this machine, and not by EchoDesk**:

| Prerequisite | Provided by | Consequence if it is missing |
| --- | --- | --- |
| A WPS installation on this machine | you or your IT department | EchoDesk cannot use the in-memory decryption path at all |
| The enterprise document-encryption client that matches the file | your organisation's encryption system | WPS cannot decrypt the file, and what is read is ciphertext |
| Access rights to that document for the current Windows account | your organisation's encryption policy | WPS refuses to open the document, or opens it as ciphertext |

If either the WPS installation or the enterprise encryption client is missing, a protected file
**cannot be read**. In that case EchoDesk falls back to the generic parser, which receives ciphertext
rather than document content, so the parse fails and the program reports a parsing failure. No
message from EchoDesk grants access that the machine and the encryption system do not already grant.

EchoDesk **installs none of these components and configures none of them**: it does not install WPS,
does not install or register an encryption client, does not obtain or hold keys, and does not change
encryption policy.

## 3. No Circumvention

**EchoDesk does not crack, break, bypass or weaken any encryption mechanism.**

- It does not decrypt protected files itself, and it contains no decryption routine for any protected
  format.
- It does not read keys, key stores or licence material belonging to the encryption system.
- It does not intercept, patch, unload or disable the encryption client, its drivers or its processes.
- It does not obtain plaintext by any route other than the ordinary interfaces that a locally
  installed, locally authorised office application already exposes.
- It uses only office software that is **already installed and already authorised on this machine**,
  and it stays within the permissions that software has been given.

The scope and strength of the protection therefore remain exactly what your organisation's existing
encryption system decides. EchoDesk neither extends that protection nor reduces it — the decision
about what may be opened, by whom and on which machine stays entirely with the encryption system.

## 4. Responsibility

By using EchoDesk to read protected documents you must confirm the following **for yourself**:

- that the rules and regulations of your organisation **permit** protected documents to be read
  programmatically on this machine, including any requirement for prior approval, registration or a
  record of the operation;
- that **the account you are using lawfully holds access rights** to that document, and that the
  access falls within what your organisation has granted you — a document you are not allowed to open
  by hand is not one that EchoDesk may open for you either;
- that the **plaintext you read is used only for work purposes inside the authorised scope**, and is
  not copied, forwarded, published or otherwise used beyond that scope.

Using EchoDesk creates no permission that you did not already have. If any of the three points above
is unclear, resolve it with your organisation's information-security or compliance function **before**
you read the document.

## 5. No Persistence on Disk

The plaintext that EchoDesk reads is processed **only in memory**:

- it is **not written to disk** — not as a temporary file, not as a cache, and not as a converted or
  re-saved copy;
- it is **not written to logs**: EchoDesk's logs record times, events, exception stack traces and
  **file names only**, never document body text (see section 5 of [`PRIVACY.md`](PRIVACY.md));
- it is **not sent to any network location**: EchoDesk has no server side and calls no cloud
  document-processing API, so the plaintext has no path off this machine;
- it is held only for as long as the local processing needs it, and the memory holding it is
  **released when the program exits**.

The protected file on disk is left as it was found: EchoDesk does not re-encrypt it, does not change
its protection attributes and does not overwrite it with a modified copy.

## 6. Behavior on Unauthorized Machines

On a computer where the enterprise encryption client is **not installed** — a personal machine, an
unmanaged machine, or a machine outside the organisation's encryption coverage — a protected file
behaves as follows:

- there is no encryption client there to authorise decryption, so WPS cannot obtain the plaintext;
- what EchoDesk reads is therefore **ciphertext**, not document content;
- the generic parser cannot make sense of that ciphertext, and the program reports a **parsing
  failure**;
- the file itself **remains ciphertext** on that machine: it cannot be opened or read there, and
  copying it to such a machine does not expose its content.

In other words, the protection travels with the file. EchoDesk offers no way around the absence of
the encryption client, and that is intended behaviour rather than a defect.

## 7. Consistency with Manual Operation

When EchoDesk asks WPS to read a file, this is the **same kind of operation** as an employee opening
that same file by hand in WPS:

- from the encryption system's point of view the requesting application is the same authorised office
  application, running under the **same Windows account** and the **same granted rights**;
- the encryption system's **audit log records the file access by WPS**, making no distinction between
  the two cases;
- EchoDesk introduces no privileged route, no separate credential and no hidden interface of its own.

The practical consequence is that reading a document through EchoDesk is visible in exactly the place
your organisation already uses to review document access. If your organisation expects document
accesses to be traceable, that expectation is satisfied by the encryption system's existing audit log,
and that log is what you can point to when the operation needs to be explained.

---

**Feedback and contact**: general questions, compliance questions and corrections to this document go
to GitHub Issues at `https://github.com/wyx-spark/EchoDesk/issues` or GitHub Discussions at
`https://github.com/wyx-spark/EchoDesk/discussions`. Security-sensitive reports — including
anything that looks like a way to bypass an encryption system — must **not** be opened as a public
Issue; use the private advisory form at
`https://github.com/wyx-spark/EchoDesk/security/advisories/new`, which is the private channel
described in [`SECURITY.md`](../SECURITY.md). The fallback email address is
`Void-Entropia-Studio@users.noreply.github.com`. Void Entropia Studio provides no telephone or instant
messaging support channel.
