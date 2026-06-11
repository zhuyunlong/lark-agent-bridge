"""通用模块级拆分：把模块顶层 函数/类/常量 按组移到子模块，未分组的留在 facade。

用法：python3.11 tmp/split_module.py <spec.py>
spec 定义：SRC, PKG_DIR(目标目录), PKG_IMPORT(facade 引用子模块的 from 前缀，如 ".investigation"),
ADD_DOTS(子模块相对 import 需加的点数), GROUPS={fname:(doc,[names])}
未分组节点按原顺序留在 facade；facade 对移走的名字做显式 re-export。
循环检测：若某组成员引用了留在 facade 的名字 → 报错退出。
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

spec_path = Path(sys.argv[1])
mod_spec = importlib.util.spec_from_file_location("split_spec", spec_path)
spec = importlib.util.module_from_spec(mod_spec)
mod_spec.loader.exec_module(spec)

SRC = Path(spec.SRC)
PKG = Path(spec.PKG_DIR)
src_text = SRC.read_text(encoding="utf-8")
src_lines = src_text.splitlines()
tree = ast.parse(src_text)

g_of = {n: g for g, (_d, ns) in spec.GROUPS.items() for n in ns}
nodes: dict[str, ast.AST] = {}
order_all: list[tuple[str | None, ast.AST]] = []  # (name|None, node) in source order
import_nodes: list[ast.AST] = []
for node in tree.body:
    name = None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        name = node.name
    elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        name = node.target.id
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        import_nodes.append(node)
        continue
    if name:
        nodes[name] = node
    order_all.append((name, node))

unknown = set(g_of) - set(nodes)
assert not unknown, f"spec lists unknown names: {sorted(unknown)}"
facade_names = {n for n in nodes if n not in g_of}


def seg(node: ast.AST) -> str:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "\n".join(src_lines[start - 1 : node.end_lineno])


def refs_of(node: ast.AST) -> set[str]:
    return {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}


# 循环检测：移走的成员不得引用留在 facade 的名字
for g, (_d, members) in spec.GROUPS.items():
    for m in members:
        bad = refs_of(nodes[m]) & facade_names
        assert not bad, f"{g}/{m} references facade-only names {sorted(bad)} -> would create import cycle"

import_lines = [
    "\n".join(src_lines[n.lineno - 1 : n.end_lineno]) for n in import_nodes
    if "from __future__" not in src_lines[n.lineno - 1]
]


def _import_names(line: str) -> list[str]:
    flat = line.replace("(", "").replace(")", "").replace("\n", " ")
    m = re.match(r"from [\w.]+ import (.+)", flat)
    if m:
        return [p.split(" as ")[-1].strip() for p in m.group(1).split(",") if p.strip()]
    m = re.match(r"import ([\w.]+)", flat)
    return [m.group(1).split(".")[0]] if m else []


def filtered_imports(body: str, lines_list: list[str], add_dots: int) -> list[str]:
    kept = []
    for line in lines_list:
        names = _import_names(line)
        if any(re.search(rf"\b{re.escape(x)}\b", body) for x in names):
            if add_dots:
                line = re.sub(r"^from \.", "from " + "." * (add_dots + 1), line, count=1) \
                    if line.startswith("from .") else line
            kept.append(line)
    return kept


PKG.mkdir(exist_ok=True)
group_order = list(spec.GROUPS)
for g in group_order:
    doc, members = spec.GROUPS[g]
    parts = [seg(nodes[m]) for m in members]
    used = set()
    for m in members:
        used |= refs_of(nodes[m])
    used -= set(members)
    body = "\n\n\n".join(parts)
    header = [f'"""{doc}"""', "", "from __future__ import annotations", ""]
    header += filtered_imports(body, import_lines, spec.ADD_DOTS)
    for dep in group_order:
        if dep == g:
            continue
        dep_used = sorted(used & set(spec.GROUPS[dep][1]))
        if dep_used:
            header.append(f"from .{dep[:-3]} import (\n    " + ",\n    ".join(dep_used) + ",\n)")
    (PKG / g).write_text("\n".join(header) + "\n\n\n" + body + "\n", encoding="utf-8")

init_path = PKG / "__init__.py"
if not init_path.exists():
    init_path.write_text(f'"""{getattr(spec, "PKG_DOC", "拆分子包。")}"""\n', encoding="utf-8")

# facade 重建：docstring + __future__ + 原 imports + re-export + 未分组节点（原顺序）
facade: list[str] = []
first = tree.body[0]
if isinstance(first, ast.Expr):
    facade.extend(src_lines[: first.end_lineno])
facade += ["", "from __future__ import annotations", ""]
remaining_body = "\n".join(seg(nodes[n]) for n in facade_names)
reexport_names_body = " ".join(g_of)  # re-export 行自身也算引用
facade += filtered_imports(remaining_body + " " + reexport_names_body, import_lines, 0)
facade.append("")
for g in group_order:
    members = spec.GROUPS[g][1]
    facade.append(
        f"from {spec.PKG_IMPORT}.{g[:-3]} import (  # noqa: F401\n    "
        + ",\n    ".join(members) + ",\n)"
    )
facade.append("")
for name, node in order_all:
    if name and name in facade_names:
        facade.append("")
        facade.append(seg(node))
SRC.write_text(re.sub(r"\n{4,}", "\n\n\n", "\n".join(facade) + "\n"), encoding="utf-8")

print("split ok")
for g in group_order:
    p = PKG / g
    print(f"{p}: {len(p.read_text().splitlines())} lines")
print(f"{SRC}: {len(SRC.read_text().splitlines())} lines")
