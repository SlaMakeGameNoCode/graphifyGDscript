#!/usr/bin/env python3
"""Measure how much of a Godot project's method calls a graphify graph binds.

Every ``receiver.method(...)`` call site in the project's ``.gd`` files is
classified by what the receiver is (``class_name``, typed local, typed member,
autoload, preload alias, ``$Node``, untyped variable, ...). The report shows,
per kind, how many call sites there are and how many call a method the project
itself defines, which is what a code graph can bind at all. It then counts the
``calls`` edges the graph has from ``.gd`` files, and how many cross files.

Use it to see which receiver kinds are worth teaching the GDScript extractor
next, and to compare two graphs of the same project.

    python scripts/godot_coverage.py path/to/godot/project path/to/graph.json \
        [--exclude _scratch] [--examples 3]

Needs the GDScript grammar (``pip install "graphifyy[gdscript]"``).
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from tree_sitter import Parser
from tree_sitter_language_pack import get_language


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project", type=Path, help="directory holding project.godot")
    ap.add_argument("graph", type=Path, help="graph.json written by graphify for that project")
    ap.add_argument("--exclude", action="append", default=[],
                    help="skip scripts whose path contains this text (repeatable)")
    ap.add_argument("--examples", type=int, default=3, help="example call sites per kind")
    args = ap.parse_args()

    root: Path = args.project
    parser = Parser(get_language("gdscript"))
    files = sorted(p for p in root.rglob("*.gd")
                   if ".godot" not in p.parts and not any(x in str(p) for x in args.exclude))

    def txt(src: bytes, n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    # Project symbols: class_name declarations, function names, autoloads.
    class_names: collections.Counter[str] = collections.Counter()
    func_names: set[str] = set()
    autoloads: set[str] = set()
    project_file = root / "project.godot"
    if project_file.is_file():
        in_autoload = False
        for line in project_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("["):
                in_autoload = line.strip() == "[autoload]"
            elif in_autoload and (m := re.match(r"^(\w+)=", line)):
                autoloads.add(m.group(1))

    trees = {}
    for f in files:
        src = f.read_bytes()
        tree = parser.parse(src)
        trees[f] = (src, tree)
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            if n.type == "class_name_statement" and (nm := n.child_by_field_name("name")) is not None:
                class_names[txt(src, nm)] += 1
            elif n.type == "function_definition" and (nm := n.child_by_field_name("name")) is not None:
                func_names.add(txt(src, nm))
            elif n.type == "constructor_definition":
                func_names.add("_init")
            stack.extend(n.named_children)

    def type_name(src: bytes, t) -> str | None:
        if t is None or t.type != "type" or len(t.named_children) != 1:
            return None
        k = t.named_children[0]
        return txt(src, k) if k.type == "identifier" else None

    def evident_type(src: bytes, v) -> str | None:
        if v is None:
            return None
        if v.type == "attribute":
            k = v.named_children
            if len(k) == 2 and k[0].type == "identifier" and k[1].type == "attribute_call":
                m = next((c for c in k[1].named_children if c.type == "identifier"), None)
                if m is not None and txt(src, m) == "new":
                    return txt(src, k[0])
        if v.type == "binary_operator":
            op = next((c for c in v.children if not c.is_named), None)
            right = v.child_by_field_name("right")
            if op is not None and txt(src, op) == "as" and right is not None and right.type == "identifier":
                return txt(src, right)
        return None

    counts: collections.Counter[str] = collections.Counter()
    in_project: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)

    for f, (src, tree) in trees.items():
        members: dict[str, str | None] = {}
        inner: set[str] = set()
        aliases: set[str] = set()
        for n in tree.root_node.named_children:
            nm = n.child_by_field_name("name")
            if nm is None:
                continue
            if n.type == "variable_statement":
                members[txt(src, nm)] = (type_name(src, n.child_by_field_name("type"))
                                         or evident_type(src, n.child_by_field_name("value")))
            elif n.type == "const_statement" and "preload(" in txt(src, n):
                aliases.add(txt(src, nm))
            elif n.type == "class_definition":
                inner.add(txt(src, nm))

        def typed(kind: str, ty: str | None) -> str:
            if ty is None:
                return f"{kind} untyped"
            if ty in class_names:
                return f"{kind} typed: project class"
            if ty in inner:
                return f"{kind} typed: inner class"
            return f"{kind} typed: engine/other type"

        def head_kind(head: str, local: dict[str, str | None]) -> str:
            if head in ("self", "super"):
                return head
            if head in local:
                return typed("local/param", local[head])
            if head in autoloads:
                return "autoload"
            if head in class_names:
                return "class_name (static/new)"
            if head in inner:
                return "inner class"
            if head in aliases:
                return "const preload alias"
            if head in members:
                return typed("member", members[head])
            return "other identifier (inherited member, engine global)"

        def walk(n, local: dict[str, str | None] | None) -> None:
            if n.type in ("function_definition", "constructor_definition"):
                local = {}
                params = n.child_by_field_name("parameters")
                for p in params.named_children if params is not None else ():
                    ident = p if p.type == "identifier" else next(
                        (c for c in p.named_children if c.type == "identifier"), None)
                    if ident is not None:
                        local[txt(src, ident)] = type_name(src, p.child_by_field_name("type")) \
                            if p.type != "identifier" else None
            elif n.type == "variable_statement" and local is not None:
                nm = n.child_by_field_name("name")
                if nm is not None:
                    local[txt(src, nm)] = (type_name(src, n.child_by_field_name("type"))
                                           or evident_type(src, n.child_by_field_name("value")))
            elif n.type == "for_statement" and local is not None:
                left = n.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    local.setdefault(txt(src, left), type_name(src, n.child_by_field_name("type")))
            if n.type == "attribute" and local is not None:
                kids = n.named_children
                at = next((i for i, c in enumerate(kids) if c.type == "attribute_call"), None)
                if at:
                    m = next((c for c in kids[at].named_children if c.type == "identifier"), None)
                    method = txt(src, m) if m is not None else "?"
                    head = kids[0]
                    if head.type == "identifier":
                        kind = head_kind(txt(src, head), local)
                        if at > 1:
                            kind = "chain via " + kind
                    elif head.type == "get_node":
                        kind = "$Node / %Unique"
                    elif head.type == "call":
                        fn = head.named_children[0] if head.named_children else None
                        name = txt(src, fn) if fn is not None and fn.type == "identifier" else ""
                        kind = f"call result: {name}()" if name in ("get_node", "get_tree", "get_parent") \
                            else "call result: other"
                    else:
                        kind = "expression: " + head.type
                    counts[kind] += 1
                    if method in func_names and method != "new":
                        in_project[kind] += 1
                        if len(examples[kind]) < args.examples:
                            examples[kind].append(
                                f"{f.relative_to(root)}:{n.start_point[0] + 1}  {txt(src, n)[:70]}")
            for c in n.named_children:
                walk(c, local)

        walk(tree.root_node, None)

    dup = sorted(c for c, k in class_names.items() if k > 1)
    print(f"scripts: {len(files)}   class_name: {len(class_names)} ({len(dup)} declared twice)   "
          f"autoloads: {', '.join(sorted(autoloads)) or '-'}")
    print()
    print(f"{'receiver kind':54}{'call sites':>11}{'method defined in project':>27}")
    for kind in sorted(counts, key=lambda k: (-in_project[k], -counts[k])):
        print(f"{kind:54}{counts[kind]:>11}{in_project[kind]:>27}")
    print(f"{'total':54}{sum(counts.values()):>11}{sum(in_project.values()):>27}")
    if args.examples:
        print()
        for kind in sorted(examples, key=lambda k: -in_project[k])[:8]:
            print(f"[{kind}]")
            for e in examples[kind]:
                print("    " + e)

    graph = json.loads(args.graph.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    links = graph.get("links") or graph.get("edges") or []
    calls = [e for e in links if e.get("relation") == "calls" and str(e.get("source_file", "")).endswith(".gd")]
    cross = [e for e in calls if nodes.get(e["target"], {}).get("source_file") not in (None, e.get("source_file"))]
    print()
    print(f"graph: {len(calls)} calls edges from .gd files, {len(cross)} of them cross-file")


if __name__ == "__main__":
    main()
