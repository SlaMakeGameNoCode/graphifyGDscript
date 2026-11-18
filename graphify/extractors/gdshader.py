"""Godot shaders (``.gdshader``) and shader includes (``.gdshaderinc``).

Godot's shading language is GLSL-like. Only the parts that tie a project
together are read, so a comment-aware brace scan is enough and no grammar is
needed:

* top-level functions (``void fragment()``, helpers) -> function nodes, with
  ``calls`` between the functions of the same file;
* ``uniform`` declarations -> uniform nodes: the names a material or a script's
  ``set_shader_parameter("name", …)`` sets;
* ``#include "res://…"`` -> ``imports_from`` the include file.

Materials and scenes that use a shader reach it through their own
``[ext_resource]`` lines (``godot_resource.py``), scripts through
``preload``/``load`` (``gdscript.py``).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id
from graphify.extractors.gdscript import godot_project_root, resolve_res_path

_LANGUAGE = "gdshader"
_SHADER_SUFFIXES = frozenset({".gdshader", ".gdshaderinc"})
# A string is matched first so the `//` inside `"res://…"` is not taken for a comment.
_COMMENT_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)
_SHADER_TYPE_RE = re.compile(r"^\s*shader_type\s+(\w+)\s*;", re.M)
_INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"', re.M)
_UNIFORM_RE = re.compile(
    r"^\s*(?:global\s+|instance\s+)?uniform\s+(?:(?:lowp|mediump|highp)\s+)?(\w+)\s+(\w+)", re.M)
# The text between the previous top-level `;`/`}` and a top-level `{`.
_FUNC_HEAD_RE = re.compile(r"(?:(?:lowp|mediump|highp)\s+)?(\w+)\s+(\w+)\s*\([^()]*\)\s*$")
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def extract_gdshader(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": f"cannot read {path}: {exc}"}

    # Blank comments out but keep their newlines, so offsets map to lines.
    text = _COMMENT_RE.sub(
        lambda m: m.group(0) if m.group(0).startswith('"') else "\n" * m.group(0).count("\n"), raw)
    source_file = str(path)
    # The suffix keeps ids apart from a same-named script beside the shader
    # (`water.gd` + `water.gdshader` both defining `vertex`).
    stem = f"{_file_stem(path)}{path.suffix}"
    file_id = _make_id(source_file)
    project_root = godot_project_root(path)
    metadata: dict[str, Any] = {"language": _LANGUAGE, "kind": "file"}
    shader_type = _SHADER_TYPE_RE.search(text)
    if shader_type:
        metadata["shader_type"] = shader_type.group(1)
    nodes: list[dict[str, Any]] = [{
        "id": file_id,
        "label": path.name,
        "file_type": "code",
        "source_file": source_file,
        "source_location": "L1",
        "metadata": metadata,
    }]
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def line_at(offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    def add_node(nid: str, label: str, line: int, kind: str, name: str,
                 *, callable_node: bool = False) -> None:
        item: dict[str, Any] = {
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": source_file,
            "source_location": f"L{line}",
            "metadata": {"language": _LANGUAGE, "kind": kind, "name": name},
        }
        if callable_node:
            item["_callable"] = True
        nodes.append(item)

    def add_edge(src: str, tgt: str, relation: str, line: int, context: str | None = None,
                 target_file: Path | None = None) -> None:
        if src == tgt or (src, tgt, relation) in seen:
            return
        seen.add((src, tgt, relation))
        edge: dict[str, Any] = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": source_file,
            "source_location": f"L{line}",
            "weight": 1.0,
        }
        if context:
            edge["context"] = context
        if target_file is not None:
            edge["target_file"] = str(target_file)
        edges.append(edge)

    for match in _INCLUDE_RE.finditer(text):
        target = resolve_res_path(match.group(1), path, project_root)
        if target is not None and target.suffix in _SHADER_SUFFIXES:
            add_edge(file_id, _make_id(str(target)), "imports_from", line_at(match.start()),
                     "include", target)

    for match in _UNIFORM_RE.finditer(text):
        name = match.group(2)
        nid = _make_id(stem, "uniform", name)
        if all(n["id"] != nid for n in nodes):
            add_node(nid, f"uniform {name}", line_at(match.start()), "uniform", name)
            add_edge(file_id, nid, "contains", line_at(match.start()))

    # Top-level functions: a `{` at depth 0 whose header looks like `type name(...)`.
    functions: dict[str, str] = {}
    bodies: list[tuple[str, int, int]] = []   # (function id, body start, body end)
    depth = 0
    boundary = 0
    open_at = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                open_at = i
                head = _FUNC_HEAD_RE.search(text[boundary:i])
                if head is not None and head.group(1) not in ("struct", "return", "else"):
                    name = head.group(2)
                    nid = _make_id(stem, name)
                    if name not in functions:
                        add_node(nid, f".{name}()", line_at(boundary + head.start(2)), "function",
                                 name, callable_node=True)
                        add_edge(file_id, nid, "method", line_at(boundary + head.start(2)))
                        functions[name] = nid
                    bodies.append((nid, i, -1))
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
            if depth == 0:
                if bodies and bodies[-1][1] == open_at and bodies[-1][2] == -1:
                    bodies[-1] = (bodies[-1][0], open_at, i)
                boundary = i + 1
        elif ch == ";" and depth == 0:
            boundary = i + 1

    for nid, start, end in bodies:
        if end < 0:
            continue
        for call in _CALL_RE.finditer(text, start, end):
            callee = functions.get(call.group(1))
            if callee is not None:
                add_edge(nid, callee, "calls", line_at(call.start()), "call")

    return {"nodes": nodes, "edges": edges}
