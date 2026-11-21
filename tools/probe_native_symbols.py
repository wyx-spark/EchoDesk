# -*- coding: utf-8 -*-
"""
[EN]
Native symbol probe for the llama.cpp runtime DLLs.

Purpose
-------
The backend talks to ``llama.cpp`` through a native boundary. Whatever the
boundary looks like (the current ``llama-cpp-python`` wheel bindings, or the
native ``llama.cpp`` takeover that T3 introduces), every function the Python
side declares must actually be exported by the DLLs that get shipped. A
declaration that is missing from the binaries fails at first call with an
opaque ``AttributeError`` / ``OSError`` far away from the real cause, usually
only on the packaged build.

This probe closes that gap statically:

* parse the PE export table of every ``*.dll`` in the given directories
  (pure standard library, no ``dumpbin`` or ``pefile`` dependency),
* extract the declared function names from the Python declaration files,
* report the two differences and exit non-zero when something declared is
  not exported.

Exit codes
----------
``0``  every declared symbol is exported (extra exports are only a hint,
       because a DLL always exports far more than the backend declares).
``1``  at least one declared symbol is missing from the scanned DLLs.
``2``  usage / IO error, or no DLL and no declaration file was found at all.

[ZH]
llama.cpp 原生运行时的符号探针。

用途
----
后端通过原生边界调用 ``llama.cpp``。无论边界是当前的 ``llama-cpp-python``
轮子绑定，还是 T3 引入的原生 ``llama.cpp`` 接管，**Python 侧声明的每个函数
都必须真的由随包 DLL 导出**。声明存在而二进制没导出时，错误会在首次调用时
以难以定位的 ``AttributeError`` / ``OSError`` 出现，且通常只在打包产物上暴露。

本探针用静态检查堵住这个缺口：解析 DLL 的 PE 导出表（仅标准库，不依赖
``dumpbin`` 或 ``pefile``），与声明文件里的函数名求差集，声明缺失即以非零
状态退出。

退出码：``0`` 无缺失（多出的导出仅作提示）；``1`` 存在声明缺失；
``2`` 用法/IO 错误，或既没找到 DLL 也没找到声明文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys

# ---------------------------------------------------------------------------
# PE parsing
# ---------------------------------------------------------------------------

_DOS_SIGNATURE = b"MZ"
_PE_SIGNATURE = b"PE\0\0"
_PE32 = 0x10B
_PE32_PLUS = 0x20B


class PEError(Exception):
    """[EN] Raised when a file is not a PE image or is truncated. [ZH] 非 PE 或截断时抛出。"""


def _read_u16(data, off):
    return struct.unpack_from("<H", data, off)[0]


def _read_u32(data, off):
    return struct.unpack_from("<I", data, off)[0]


def parse_pe_exports(path):
    """
    [EN]
    Return ``(exported_names, ordinal_only_count)`` for one PE image.

    Only the name table is required by the probe: an ordinal-only export
    (named by position, no string) can never be matched against a Python
    declaration, so it is counted separately instead of being invented.

    [ZH]
    返回单个 PE 映像的 ``(导出名列表, 仅序号导出数)``。

    探针只需要名称表：只有序号、没有名字的导出无法与 Python 声明匹配，
    因此单独计数，而不是凭空编出一个名字。
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 0x40 or data[:2] != _DOS_SIGNATURE:
        raise PEError("not a PE image (missing MZ)")
    pe_off = _read_u32(data, 0x3C)
    if pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != _PE_SIGNATURE:
        raise PEError("not a PE image (missing PE signature)")

    coff = pe_off + 4
    number_of_sections = _read_u16(data, coff + 2)
    size_of_optional_header = _read_u16(data, coff + 16)
    optional = coff + 20
    if optional + size_of_optional_header > len(data):
        raise PEError("truncated optional header")

    magic = _read_u16(data, optional)
    if magic == _PE32_PLUS:
        data_dir_off = optional + 112
    elif magic == _PE32:
        data_dir_off = optional + 96
    else:
        raise PEError("unknown optional header magic 0x%X" % magic)

    # Data directory 0 is the export table.
    if data_dir_off + 8 > len(data):
        raise PEError("truncated data directory")
    export_rva = _read_u32(data, data_dir_off)
    export_size = _read_u32(data, data_dir_off + 4)
    if export_rva == 0 or export_size == 0:
        return [], 0

    sections = []
    sec_off = optional + size_of_optional_header
    for index in range(number_of_sections):
        base = sec_off + index * 40
        if base + 40 > len(data):
            raise PEError("truncated section table")
        virtual_size = _read_u32(data, base + 8)
        virtual_address = _read_u32(data, base + 12)
        size_of_raw = _read_u32(data, base + 16)
        pointer_to_raw = _read_u32(data, base + 20)
        sections.append((virtual_address, virtual_size, pointer_to_raw, size_of_raw))

    def rva_to_offset(rva):
        for virtual_address, virtual_size, pointer_to_raw, size_of_raw in sections:
            span = max(virtual_size, size_of_raw)
            if virtual_address <= rva < virtual_address + span:
                delta = rva - virtual_address
                if delta >= size_of_raw:
                    # [EN] Inside uninitialised virtual space: no file bytes.
                    # [ZH] 落在未初始化的虚拟空间内，文件里没有对应字节。
                    return None
                return pointer_to_raw + delta
        return None

    export_off = rva_to_offset(export_rva)
    if export_off is None or export_off + 40 > len(data):
        raise PEError("export directory outside file")

    number_of_functions = _read_u32(data, export_off + 20)
    number_of_names = _read_u32(data, export_off + 24)
    address_of_names = _read_u32(data, export_off + 32)
    names_off = rva_to_offset(address_of_names)
    if number_of_names and names_off is None:
        raise PEError("export name table outside file")

    names = []
    for index in range(number_of_names):
        entry = names_off + index * 4
        if entry + 4 > len(data):
            break
        name_off = rva_to_offset(_read_u32(data, entry))
        if name_off is None or name_off >= len(data):
            continue
        end = data.find(b"\0", name_off)
        if end < 0:
            continue
        try:
            names.append(data[name_off:end].decode("ascii"))
        except UnicodeDecodeError:
            continue

    ordinal_only = max(0, number_of_functions - number_of_names)
    names.sort()
    return names, ordinal_only


# ---------------------------------------------------------------------------
# Declaration extraction
# ---------------------------------------------------------------------------

#: [EN] Matches a decorator call whose name mentions ``ctypes_function``; the
#: probe then inspects the first argument to decide which declaration shape it
#: is.
#: [ZH] 匹配名字含 ``ctypes_function`` 的装饰器调用，随后由探针检查首参判断形态。
_DECORATOR_RE = re.compile(r"@(\w*ctypes_function\w*)\s*\(")

#: [EN] Quoted string inside a candidate list. Both quote styles are accepted.
#: [ZH] 候选列表中的引号字符串，单双引号均可。
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'')


def _skip_ws(text, index):
    """[EN] Index of the first non-whitespace character at/after ``index``. [ZH] 返回 index 起首个非空白字符下标。"""
    length = len(text)
    while index < length and text[index] in " \t\r\n":
        index += 1
    return index


def _scan_candidate_list(text, index):
    """
    [EN]
    Scan a ``[...]`` candidate list starting at ``index`` and return
    ``(names, index_after_list)``. Quoted entries are collected verbatim: the
    binding lists every spelling the symbol may be exported under (plain C
    name plus the MSVC and GNU mangled forms), and any one of them satisfies
    the declaration.

    [ZH]
    从 ``index`` 处的 ``[...]`` 候选列表扫描，返回 ``(名字列表, 列表结束下标)``。
    引号条目原样收集：绑定会列出符号可能导出的所有拼写（C 名加上 MSVC 与
    GNU 修饰名），任一命中即视为声明成立。
    """
    names = []
    depth = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "[":
            depth += 1
            index += 1
            continue
        if char == "]":
            depth -= 1
            index += 1
            if depth == 0:
                return names, index
            continue
        if char in ("'", '"'):
            match = _QUOTED_RE.match(text, index)
            if not match:
                index += 1
                continue
            value = match.group(1) if match.group(1) is not None else match.group(2)
            names.append(value)
            index = match.end()
            continue
        index += 1
    return names, index


def extract_declared(path):
    """
    [EN]
    Read one declaration source and return a list of *candidate groups*.

    Each group is the set of export names that would satisfy one logical
    Python declaration. Grouping matters because a Windows ``llama.cpp`` build
    exports many C entry points under their decorated C++ name only, so the
    binding declares alternatives:

    * ``@ctypes_function("name", ...)`` -> a one-name group,
    * ``@ctypes_function_xxx(["name", "?name@@...", "__Z..name.."], ...)``
      -> a group of every listed spelling,
    * a plain text file with one symbol name per line -> one-name groups.

    [ZH]
    读取一个声明来源并返回 **候选组** 列表。

    每个组是满足一条逻辑 Python 声明的导出名集合。分组是必要的：Windows 上的
    ``llama.cpp`` 构建会把不少 C 入口只以修饰后的 C++ 名导出，因此绑定会声明多个
    候选拼写：``@ctypes_function("name", ...)`` 为单名组；
    ``@ctypes_function_xxx([...])`` 为全部拼写的组；纯文本文件每行一个单名组。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    stem = os.path.splitext(path)[1].lower()
    if stem in (".txt", ".list", ".symbols"):
        groups = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                groups.append((line,))
        return groups

    groups = []
    for match in _DECORATOR_RE.finditer(text):
        index = _skip_ws(text, match.end())
        if index < len(text) and text[index] == "[":
            names, _ = _scan_candidate_list(text, index)
            names = [name for name in names if name]
            if names:
                groups.append(tuple(names))
        elif index < len(text) and text[index] in ("'", '"'):
            quoted = _QUOTED_RE.match(text, index)
            if quoted:
                value = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
                if value:
                    groups.append((value,))
    return groups


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_repo_root(start):
    """
    [EN]
    Walk up from ``start`` until a directory that looks like the repo root is
    found (contains ``backend`` or ``.git``). No path is hard-coded, so the
    probe works from any worktree or checkout location.

    [ZH]
    从 ``start`` 向上查找看起来是仓库根目录的路径（含 ``backend`` 或 ``.git``）。
    不写死任何路径，因此探针可在任意 worktree / 检出位置运行。
    """
    current = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(current, "backend")) or os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def default_dll_dir(repo_root):
    """
    [EN]
    Locate the runtime DLL directory from this checkout first: ``backend/native/<rid>``
    (``win-x64`` preferred, otherwise the only existing sub-directory). The old
    ``llama_cpp`` package directory is only a last-resort fallback, so the probe works
    on a machine where the wheel is not installed.

    [ZH]
    优先从本检出定位运行时 DLL 目录：``backend/native/<rid>``（优先 ``win-x64``，否则取
    唯一的现存子目录）。旧的 ``llama_cpp`` 包目录只作最后兜底，使探针在没有安装轮子的机器上
    也能工作。
    """
    if repo_root:
        native_root = os.path.join(repo_root, "backend", "native")
        preferred = os.path.join(native_root, "win-x64")
        if os.path.isdir(preferred):
            return preferred
        if os.path.isdir(native_root):
            subdirs = [
                os.path.join(native_root, name)
                for name in sorted(os.listdir(native_root))
                if os.path.isdir(os.path.join(native_root, name))
            ]
            if len(subdirs) == 1:
                return subdirs[0]
    try:
        import llama_cpp  # noqa: PLC0415 - optional at probe time
    except Exception:
        return None
    candidate = os.path.join(os.path.dirname(llama_cpp.__file__), "lib")
    return candidate if os.path.isdir(candidate) else None


def default_decl_files(repo_root):
    """
    [EN]
    Return the declaration files that exist in this checkout, in priority order.

    ``llama_abi/llama_cpp.py`` comes first because it is the real declaration source
    for the vendored ABI (hundreds of ``@ctypes_function`` decorators). In T3,
    ``backend/llama_capi.py`` became a thin adapter that only re-exports that module,
    so using it as the primary source yields ``declared=0`` -- and ``missing=0`` on
    top of an empty set proves nothing. A missing file is simply absent from the
    list; ``main`` then fails with ``no declaration extracted`` instead of turning
    that into a green light.

    [ZH]
    按优先级返回本检出中真实存在的声明文件。

    ``llama_abi/llama_cpp.py`` 排首位，因为它才是内置 ABI 的真正声明源（数百处
    ``@ctypes_function`` 装饰器）。T3 把 ``backend/llama_capi.py`` 改成了只做 re-export
    的薄适配层，若仍以它为主源就会得到 ``declared=0`` —— 而空集减空集的 ``missing=0``
    不证明任何东西。缺失的文件只是不出现在列表里；随后 ``main`` 会以
    ``no declaration extracted`` 失败，而不是把它变成绿灯。
    """
    if not repo_root:
        return []
    candidates = [
        os.path.join(repo_root, "backend", "llama_abi", "llama_cpp.py"),
        os.path.join(repo_root, "backend", "llama_capi.py"),
        os.path.join(repo_root, "backend", "llama_mtmd_capi.py"),
        os.path.join(repo_root, "backend", "mtmd_capi.py"),
    ]
    return [path for path in candidates if os.path.isfile(path)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check that declared native symbols are exported by the llama.cpp DLLs.",
    )
    parser.add_argument(
        "--dll-dir",
        action="append",
        default=None,
        metavar="DIR",
        help="directory containing the runtime DLLs (repeatable; default: discovered from llama_cpp)",
    )
    parser.add_argument(
        "--decls",
        action="append",
        default=None,
        metavar="FILE",
        help="declaration source to check (repeatable; default: backend/llama_capi.py when present)",
    )
    parser.add_argument(
        "--show-extra",
        type=int,
        default=15,
        metavar="N",
        help="how many undeclared exports to list as a hint (0 = none, default 15)",
    )
    parser.add_argument(
        "--json",
        default=None,
        metavar="PATH",
        help="also write the full machine-readable report to PATH",
    )
    args = parser.parse_args(argv)

    repo_root = find_repo_root(os.path.dirname(os.path.abspath(__file__)))

    dll_dirs = args.dll_dir
    if not dll_dirs:
        discovered = default_dll_dir(repo_root)
        if discovered:
            dll_dirs = [discovered]
        else:
            print("PROBE ERROR no --dll-dir given and backend/native/<rid> was not found",
                  file=sys.stderr)
            return 2

    decl_files = args.decls
    if not decl_files:
        decl_files = default_decl_files(repo_root)

    missing_dirs = [directory for directory in dll_dirs if not os.path.isdir(directory)]
    if missing_dirs:
        print("PROBE ERROR missing dll directory: %s" % ", ".join(missing_dirs), file=sys.stderr)
        return 2

    # --- exports -----------------------------------------------------------
    per_dll = {}
    seen_paths = set()
    for directory in dll_dirs:
        for entry in sorted(os.listdir(directory)):
            if not entry.lower().endswith(".dll"):
                continue
            path = os.path.join(directory, entry)
            if not os.path.isfile(path):
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in seen_paths:
                continue
            seen_paths.add(key)
            try:
                names, ordinal_only = parse_pe_exports(path)
            except (PEError, OSError) as exc:
                print("PROBE WARN unreadable dll %s: %s" % (entry, exc), file=sys.stderr)
                continue
            per_dll[entry] = {
                "path": path,
                "bytes": os.path.getsize(path),
                "exports": names,
                "ordinal_only": ordinal_only,
            }

    if not per_dll:
        print("PROBE ERROR no readable PE DLL found in: %s" % ", ".join(dll_dirs), file=sys.stderr)
        return 2

    # Symbol -> owning dll(s). A symbol can legitimately appear in more than
    # one image (e.g. ggml symbols re-exported through llama.dll).
    provider = {}
    for dll_name, info in per_dll.items():
        for name in info["exports"]:
            provider.setdefault(name, []).append(dll_name)
    all_exports = set(provider)

    # --- declarations ------------------------------------------------------
    declared_groups = []
    for path in decl_files:
        if not os.path.isfile(path):
            print("PROBE ERROR missing declaration file: %s" % path, file=sys.stderr)
            return 2
        declared_groups.extend(extract_declared(path))
    if not decl_files:
        print(
            "PROBE NOTE no declaration file found (looked for backend/llama_capi.py); "
            "reporting exports only",
            file=sys.stderr,
        )

    def display_name(group):
        """[EN] Prefer the plain C spelling for display. [ZH] 展示时优先使用朴素 C 名。"""
        for candidate in group:
            if not candidate.startswith("?") and not candidate.startswith("_Z"):
                return candidate
        return group[0]

    declared_names = {candidate for group in declared_groups for candidate in group}
    missing_groups = [
        group for group in declared_groups if not any(name in all_exports for name in group)
    ]
    missing_groups.sort(key=display_name)
    resolved_groups = [
        group for group in declared_groups if any(name in all_exports for name in group)
    ]
    resolved_groups.sort(key=display_name)
    extra = sorted(all_exports - declared_names)

    # --- report ------------------------------------------------------------
    print("PROBE declaration sources:")
    if decl_files:
        for path in decl_files:
            print("  %s" % path)
    else:
        print("  (none)")

    print("PROBE dll inventory:")
    for dll_name in sorted(per_dll):
        info = per_dll[dll_name]
        print(
            "  %-28s %10d bytes  exports=%d  ordinal_only=%d"
            % (dll_name, info["bytes"], len(info["exports"]), info["ordinal_only"])
        )

    if declared_groups:
        print("PROBE declared symbol placement:")
        for group in resolved_groups:
            matches = [name for name in group if name in all_exports]
            owners = sorted({dll for name in matches for dll in provider[name]})
            note = "" if display_name(group) in matches else " (via decorated name %s)" % matches[0]
            print("  %-44s -> %s%s" % (display_name(group), ", ".join(owners), note))
        for group in missing_groups:
            print("  %-44s -> MISSING" % display_name(group))

    if missing_groups:
        print(
            "PROBE MISSING %d declared symbol(s) are not exported by any scanned dll:"
            % len(missing_groups)
        )
        for group in missing_groups:
            print("  - %s" % display_name(group))
    if extra and args.show_extra:
        print(
            "PROBE EXTRA %d export(s) are not declared by the Python side "
            "(hint only, first %d):" % (len(extra), min(args.show_extra, len(extra)))
        )
        for name in extra[: args.show_extra]:
            print("  + %s" % name)

    print(
        "PROBE dlls=%d declared=%d missing=%d extra=%d"
        % (len(per_dll), len(declared_groups), len(missing_groups), len(extra))
    )

    if args.json:
        payload = {
            "dll_dirs": dll_dirs,
            "decl_files": decl_files,
            "dlls": {
                name: {
                    "path": info["path"],
                    "bytes": info["bytes"],
                    "export_count": len(info["exports"]),
                    "ordinal_only": info["ordinal_only"],
                    "exports": info["exports"],
                }
                for name, info in per_dll.items()
            },
            "declared_groups": [list(group) for group in declared_groups],
            "missing": [display_name(group) for group in missing_groups],
            "extra": extra,
            "summary": {
                "dlls": len(per_dll),
                "declared": len(declared_groups),
                "missing": len(missing_groups),
                "extra": len(extra),
            },
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    # [EN] An empty declaration set makes ``missing == 0`` vacuously true, so it must
    # [EN] never be reported as a pass: fail loudly and name the sources scanned.
    # [ZH] 声明集为空时 ``missing == 0`` 空洞成立，绝不能当成通过：明确失败并列出扫描源。
    if not declared_groups:
        sources = ", ".join(decl_files) if decl_files else "(no declaration file found)"
        print("PROBE FAIL no declaration extracted from: %s" % sources, file=sys.stderr)
        return 1

    # [EN] The acceptance self-check: a real baseline declares hundreds of symbols and
    # [EN] every one of them is exported. Anything less stays silent here and is left
    # [EN] to the exit code / per-symbol report above.
    # [ZH] 验收自检：真实基线声明数百个符号且逐条都被导出。更少的情况这里不打印，交由上面的
    # [ZH] 退出码 / 逐符号报告表达。
    if len(declared_groups) >= 200 and not missing_groups:
        print("PROBE OK declared=%d missing=0" % len(declared_groups))

    return 1 if missing_groups else 0


if __name__ == "__main__":
    sys.exit(main())
