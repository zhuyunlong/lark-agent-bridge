"""通用 Mixin 拆分工具：把一个大 Mixin 文件按主题拆成同目录多个小 Mixin。

用法：python3.11 tmp/split_mixin.py <spec.py 路径>
spec 模块需定义：SRC, CLASS_NAME, GROUPS = {fname: (mixin_cls, doc, [methods...])},
CORE_DOC（facade 类文档行，可为 None）。facade = SRC 原文件，保留未分组方法并继承新 Mixin。
顶层名收集同时处理 Assign/AnnAssign（S2 教训）。
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

spec_path = Path(sys.argv[1])
spec_mod = importlib.util.spec_from_file_location("split_spec", spec_path)
spec = importlib.util.module_from_spec(spec_mod)
spec_mod.loader.exec_module(spec)

SRC = Path(spec.SRC)
src_text = SRC.read_text(encoding="utf-8")
lines = src_text.splitlines()
tree = ast.parse(src_text)

cls_node = next(
    n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == spec.CLASS_NAME
)
header_end = cls_node.lineno - 1  # everything before class line
header = lines[:header_end]
import_lines = [l for l in header if l.startswith(("import ", "from "))]

methods: dict[str, tuple[int, int]] = {}
order: list[str] = []
class_level_stmts: list[tuple[int, int]] = []  # 类体非方法语句（常量等）保留在 facade
for idx, n in enumerate(cls_node.body):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        start = min([n.lineno] + [d.lineno for d in n.decorator_list])
        methods[n.name] = (start - 1, n.end_lineno)
        order.append(n.name)
    elif not (idx == 0 and isinstance(n, ast.Expr)):  # 跳过类 docstring（单独处理）
        class_level_stmts.append((n.lineno - 1, n.end_lineno))

grouped = {m for _, (_c, _d, ms) in spec.GROUPS.items() for m in ms}
unknown = grouped - set(order)
assert not unknown, f"spec lists unknown methods: {sorted(unknown)}"
core = [m for m in order if m not in grouped]

mixin_imports = []
for fname, (cls, doc, members) in spec.GROUPS.items():
    body = [f"class {cls}:", f'    """{doc}（与 {spec.CLASS_NAME[1:]} 共享 self 状态）。"""', ""]
    for m in order:
        if m in members:
            s, e = methods[m]
            body.extend(lines[s:e])
    text = "\n".join(header) + "\n\n" + "\n".join(body) + "\n"
    out = SRC.parent / fname
    out.write_text(re.sub(r"\n{4,}", "\n\n\n", text), encoding="utf-8")
    mixin_imports.append(f"from .{fname[:-3]} import {cls}")

bases = ", ".join(cls for cls, _d, _m in spec.GROUPS.values())
facade = list(header)
facade.extend(mixin_imports)
facade.append("")
facade.append("")
cls_line = lines[cls_node.lineno - 1]
if cls_line.rstrip().endswith(":") and "(" not in cls_line:
    cls_line = cls_line.rstrip().rstrip(":") + f"({bases}):"
facade.append(cls_line)
# class docstring + 类体非方法语句（保留在 facade，避免常量丢失）
first_body = cls_node.body[0]
if isinstance(first_body, ast.Expr):
    facade.extend(lines[cls_node.lineno : first_body.end_lineno])
for s, e in class_level_stmts:
    facade.extend(lines[s:e])
for m in core:
    s, e = methods[m]
    facade.extend(lines[s:e])
SRC.write_text(re.sub(r"\n{4,}", "\n\n\n", "\n".join(facade) + "\n"), encoding="utf-8")

print("split ok")
for fname in list(spec.GROUPS) + [SRC.name]:
    p = SRC.parent / fname
    print(f"{p}: {len(p.read_text().splitlines())} lines")
