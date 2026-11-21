# -*- coding: utf-8 -*-
"""
[EN]
The single vision error type shared by the wheel path and the native path.

Why this module exists: ``vision.py`` (wheel bindings) and ``vision_native.py``
(mtmd bindings) must fail with the *same* exception type. Two parallel hierarchies
would force the dispatcher to translate between them, and that translation table
would drift. The product contract is exactly this shape:

* ``code`` -- stable, machine-readable, used for routing and diagnostics;
* ``key`` -- language-pack key rendered through ``localize(translate)``;
* ``kwargs`` -- placeholders for the localized text (path / name / detail ...).

The class lives in a neutral, dependency-free module so both implementations can
import it without importing each other (no circular import, no runtime DLL load).

[ZH]
轮子路径与原生路径共用的唯一视觉错误类型。

为什么存在本模块：``vision.py``（轮子绑定）与 ``vision_native.py``（mtmd 绑定）必须以
**同一个**异常类型失败。两套并行层级会迫使分发层写一张翻译表，而翻译表必然会漂移。
产品契约正是这个形状：

* ``code`` —— 稳定、机器可读，用于路由与诊断；
* ``key`` —— 语言包键，经 ``localize(translate)`` 渲染；
* ``kwargs`` —— 本地化文本的占位符（path / name / detail …）。

该类放在一个中立、零依赖的模块里，使两套实现都能导入它，又不必互相导入
（无循环依赖，也不加载任何运行期 DLL）。
"""
from __future__ import annotations


class VisionError(Exception):
    """
    [EN]
    A rejected or failed vision operation, carrying a language-pack key.

    [ZH]
    被拒绝或失败的视觉操作，携带语言包键。
    """

    def __init__(self, code: str, key: str, **kwargs: object) -> None:
        super().__init__(code)
        self.code = str(code)
        self.key = str(key)
        self.kwargs = dict(kwargs)

    def localize(self, translate) -> str:
        """[EN] Render through the supplied translator. / [ZH] 通过给定翻译函数渲染。"""
        try:
            return str(translate(self.key, **self.kwargs))
        except Exception:  # noqa: BLE001 - [EN] never mask the original reason
                           # [ZH] 绝不掩盖原始原因
            return self.code
