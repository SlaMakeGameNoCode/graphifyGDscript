"""GDScript (Godot ``.gd``) extraction and cross-file resolution.

A script is a class. Its members (``func``, ``_init``, ``signal``, ``const``,
``enum``, inner ``class``) hang off the file node, or off a class node when the
script declares ``class_name``, so the global name is what owns them.

The per-file pass only reads the file it is given. What it can settle there it
emits as edges: same-file calls, ``self.f()``, local signal ``connect``/``emit``,
``preload``/``load`` of another script or scene, ``extends "res://…"``, and calls
through a receiver whose type is an inner class of the script. Everything that
needs another file (a ``class_name``, an autoload, an inherited method or
signal, a ``preload`` alias's members, the class a typed variable holds) is left
as a ``raw_calls`` entry with ``language: "gdscript"`` and resolved by
``resolve_gdscript_references`` against the whole corpus. That keeps a cached
per-file result valid when some other script changes, and lets an incremental
rebuild resolve against the unchanged scripts it receives as context.

A bare call is only ever matched to the script's own methods or to a method of
a script it inherits from: that is all GDScript itself allows. Engine methods
and builtins (``add_child``, ``print``) find no script and stay unresolved. All
entries are flagged ``is_member_call`` so the shared name-matching pass never
binds them to an unrelated script that happens to define the same name.

A receiver binds through its static type only: a parameter, local or member
declared ``x: Foo`` (or ``x := Foo.new()``, ``x := y as Foo``, ``x := f()`` with
``f() -> Foo``, a loop variable over ``Array[Foo]``, ``arr[i]`` of an
``Array[Foo]``), a cast ``(x as Foo)``, and member chains ``x.a.f()`` where each
hop is a typed member. An untyped variable binds nothing: its name shadows any
class or autoload it happens to match. The member types a scope declares ride
on that scope's node as ``_gd_fields`` so the resolver can walk chains and
inherited members across files, on a full build and on an incremental one.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from graphify.extractors.base import _file_stem, _make_id, _read_source_bytes, _read_text

_LANGUAGE = "gdscript"
_SIGNAL_METHODS = frozenset({"connect", "disconnect", "emit", "is_connected"})
_LOADABLE_SUFFIXES = frozenset({".gd", ".tscn", ".tres", ".gdshader"})
# Variant types are never a script: a member typed with one is left out of the
# `_gd_fields` table, and a receiver typed with one is not handed to the resolver.
_BUILTIN_TYPES = frozenset({
    "void", "bool", "int", "float", "String", "StringName", "NodePath", "Variant",
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i", "Color",
    "Rect2", "Rect2i", "Transform2D", "Transform3D", "Basis", "Quaternion", "Plane",
    "AABB", "Projection", "RID", "Callable", "Signal", "Array", "Dictionary",
    "PackedByteArray", "PackedInt32Array", "PackedInt64Array", "PackedFloat32Array",
    "PackedFloat64Array", "PackedStringArray", "PackedVector2Array",
    "PackedVector3Array", "PackedVector4Array", "PackedColorArray",
})


def godot_project_root(path: Path) -> Path | None:
    """The directory holding ``project.godot`` above ``path``, or None."""
    for parent in path.parents:
        if (parent / "project.godot").is_file():
            return parent
    return None


def resolve_res_path(ref: str, path: Path, project_root: Path | None) -> Path | None:
    """The file a ``res://`` or relative resource path names, or None.

    ``uid://`` and ``user://`` are left alone: neither names a file in the
    source tree that this pass can see.
    """
    if ref.startswith("res://"):
        if project_root is None:
            return None
        target = project_root / ref[len("res://"):]
    elif "://" in ref or not ref:
        return None
    else:
        target = path.parent / ref
    # Lexical, not resolved: the id must match the one the target's own
    # extraction mints from the same path form, even through a symlink.
    return Path(os.path.normpath(target))


def extract_gdscript(path: Path) -> dict:
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language
    except ImportError:
        return {"nodes": [], "edges": [], "error": "tree-sitter-language-pack not installed"}

    try:
        source = _read_source_bytes(path)
        root = Parser(get_language("gdscript")).parse(source).root_node
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": f"GDScript grammar failed to load: {exc}"}

    source_file = str(path)
    stem = _file_stem(path)
    file_id = _make_id(source_file)
    project_root = godot_project_root(path)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    raw_calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    node_by_id: dict[str, dict[str, Any]] = {}

    def text(node) -> str:
        return _read_text(node, source)

    def line_of(node) -> int:
        return node.start_point[0] + 1

    def add_node(nid: str, label: str, line: int, kind: str, name: str | None = None,
                 *, callable_node: bool = False) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        metadata = {"language": _LANGUAGE, "kind": kind}
        if name is not None:
            metadata["name"] = name
        item: dict[str, Any] = {
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": source_file,
            "source_location": f"L{line}",
            "metadata": metadata,
        }
        if callable_node:
            item["_callable"] = True
        nodes.append(item)
        node_by_id[nid] = item

    def add_edge(src: str, tgt: str, relation: str, line: int, *,
                 context: str | None = None, **extra: Any) -> None:
        key = (src, tgt, relation)
        if src == tgt or key in seen_edges:
            return
        seen_edges.add(key)
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
        edge.update(extra)
        edges.append(edge)

    def add_ref(caller: str, line: int, kind: str, **fields: Any) -> None:
        raw_calls.append({
            "caller_nid": caller,
            "callee": fields.pop("callee", fields.get("name", "")),
            "is_member_call": True,
            "language": _LANGUAGE,
            "gd_kind": kind,
            "source_file": source_file,
            "source_location": f"L{line}",
            **fields,
        })

    def string_value(node) -> str | None:
        if node is None or node.type != "string":
            return None
        return text(node)[1:-1]

    def plain_type(node) -> str | None:
        """``Foo`` from a ``type`` node that is a single identifier, else None."""
        if node is None:
            return None
        named = node.named_children
        if len(named) == 1 and named[0].type == "identifier":
            return text(named[0])
        return None

    def elem_type(node) -> str | None:
        """``Foo`` from an ``Array[Foo]`` type node, else None."""
        if node is None or node.type != "type":
            return None
        named = node.named_children
        if len(named) != 1 or named[0].type != "subscript":
            return None
        parts = named[0].named_children
        if len(parts) != 2 or parts[0].type != "identifier" or text(parts[0]) != "Array":
            return None
        args = parts[1].named_children
        if len(args) == 1 and args[0].type == "identifier":
            return text(args[0])
        return None

    add_node(file_id, path.name, 1, "file")

    owner_id = file_id
    class_extends = None   # `class_name Foo extends Bar` on one line
    for child in root.named_children:
        if child.type == "class_name_statement":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = text(name_node)
                owner_id = _make_id(stem, "class", name)
                add_node(owner_id, name, line_of(child), "class", name)
                add_edge(file_id, owner_id, "contains", line_of(child))
            class_extends = child.child_by_field_name("extends")
            break

    # One scope per class body: the script itself and each inner class.
    # members maps (kind, name) -> node id inside that scope.
    scopes: dict[str, dict[tuple[str, str], str]] = {owner_id: {}}
    bodies: list[tuple[str, Any, str, str, Any]] = []   # (caller, body, scope, func name, func node)
    member_values: list[tuple[Any, str]] = []      # top-level var/const initialisers
    accessor_bodies: list[tuple[Any, str]] = []    # property `set(v):` / `get:` bodies
    accessor_refs: list[tuple[str, str, int, str]] = []   # `set = _set_x`: (scope, func, line, kind)
    type_refs: list[tuple[str, str, int]] = []     # (scope, `var x: Name` type, line)
    aliases: dict[str, Path] = {}                  # const Alias := preload("x.gd")
    # (scope, name, declared type, Array element type, initialiser): typed once
    # every return type in the file is known.
    pending_members: list[tuple[str, str, str | None, str | None, Any]] = []
    member_types: dict[str, dict[str, str | None]] = {}   # scope -> var -> type (None: untyped)
    member_elems: dict[str, dict[str, str]] = {}          # scope -> var -> Array element type
    return_types: dict[str, dict[str, str]] = {}          # scope -> func -> declared return type

    def emit_extends(node, owner: str) -> None:
        spec = node.named_children[0] if node.named_children else None
        if spec is None:
            return
        ref = string_value(spec)
        if ref is not None:
            target = resolve_res_path(ref, path, project_root)
            if target is not None and target.suffix == ".gd":
                add_edge(owner, _make_id(str(target)), "inherits", line_of(node),
                         target_file=str(target))
            return
        name = plain_type(spec) if spec.type == "type" else None
        if name is not None:
            add_ref(owner, line_of(node), "extends", name=name)

    def load_target(call) -> Path | None:
        """The script or scene a ``preload("…")`` / ``load("…")`` names."""
        kids = call.named_children
        if len(kids) < 2 or kids[0].type != "identifier" or text(kids[0]) not in ("preload", "load"):
            return None
        args = kids[1].named_children
        ref = string_value(args[0]) if args else None
        target = resolve_res_path(ref, path, project_root) if ref else None
        if target is None or target.suffix not in _LOADABLE_SUFFIXES:
            return None
        return target

    def declare(node, scope: str, scope_stem: str, top: bool) -> None:
        t = node.type
        name_node = node.child_by_field_name("name")
        name = text(name_node) if name_node is not None else None
        if t == "constructor_definition":   # `func _init()` has no name field
            name = "_init"
        line = line_of(node)
        members = scopes[scope]
        if t == "extends_statement":
            emit_extends(node, scope)
            return
        if name is None:
            return
        if t in ("function_definition", "constructor_definition"):
            nid = _make_id(scope_stem, name)
            add_node(nid, f".{name}()", line, "function", name, callable_node=True)
            add_edge(scope, nid, "method", line)
            members[("function", name)] = nid
            returns = plain_type(node.child_by_field_name("return_type"))
            if returns is not None:
                return_types.setdefault(scope, {})[name] = returns
            body = node.child_by_field_name("body")
            if body is not None:
                bodies.append((nid, body, scope, name, node))
        elif t == "signal_statement":
            nid = _make_id(scope_stem, "signal", name)
            add_node(nid, f"signal {name}", line, "signal", name)
            add_edge(scope, nid, "contains", line)
            members[("signal", name)] = nid
        elif t == "enum_definition":
            nid = _make_id(scope_stem, "enum", name)
            add_node(nid, name, line, "enum", name)
            add_edge(scope, nid, "contains", line)
        elif t in ("const_statement", "variable_statement"):
            if t == "const_statement":
                nid = _make_id(scope_stem, "const", name)
                add_node(nid, name, line, "constant", name)
                add_edge(scope, nid, "contains", line)
            type_node = node.child_by_field_name("type")
            type_name = plain_type(type_node)
            if type_name is not None:
                type_refs.append((scope, type_name, line))
            value = node.child_by_field_name("value")
            if t == "variable_statement":
                pending_members.append((scope, name, type_name, elem_type(type_node), value))
                setget = node.child_by_field_name("setget")
                for accessor in setget.named_children if setget is not None else ():
                    if accessor.type in ("setter", "getter"):    # `set = _set_x`
                        accessor_refs.append((scope, text(accessor), line_of(accessor), accessor.type))
                        continue
                    accessor_body = accessor.child_by_field_name("body")
                    if accessor_body is not None:
                        accessor_bodies.append((accessor_body, scope))
            if value is not None:
                if top:
                    member_values.append((value, scope))
                if t == "const_statement" and value.type == "call":
                    target = load_target(value)
                    if target is not None:
                        aliases[name] = target
        elif t == "class_definition":
            nid = _make_id(scope_stem, "class", name)
            add_node(nid, name, line, "inner_class", name)
            add_edge(scope, nid, "contains", line)
            members[("class", name)] = nid
            scopes[nid] = {}
            ext = node.child_by_field_name("extends")
            if ext is not None:
                emit_extends(ext, nid)
            body = node.child_by_field_name("body")
            for member in body.named_children if body is not None else ():
                declare(member, nid, f"{scope_stem}/{name}", False)

    if class_extends is not None:
        emit_extends(class_extends, owner_id)
    for child in root.named_children:
        if child.type != "class_name_statement":
            declare(child, owner_id, stem, True)

    def value_type(node, scope: str, local: dict[str, str | None] | None = None,
                   elems: dict[str, str] | None = None) -> str | None:
        """The type an expression evidently has, or None: ``Foo.new()``,
        ``x as Foo``, ``(…)``, a call to a function of this scope declared
        ``-> Foo``, ``arr[i]`` of an ``Array[Foo]``, a typed local or member."""
        if node is None:
            return None
        t = node.type
        if t == "parenthesized_expression":
            inner = node.named_children
            return value_type(inner[0], scope, local, elems) if len(inner) == 1 else None
        if t == "binary_operator":
            op = next((c for c in node.children if not c.is_named), None)
            right = node.child_by_field_name("right")
            if op is not None and text(op) == "as" and right is not None and right.type == "identifier":
                return text(right)
            return None
        if t == "attribute":
            kids = node.named_children
            if len(kids) == 2 and kids[0].type == "identifier" and kids[1].type == "attribute_call":
                method = next((c for c in kids[1].named_children if c.type == "identifier"), None)
                if method is not None and text(method) == "new":
                    return text(kids[0])
            return None
        if t == "call":
            kids = node.named_children
            if kids and kids[0].type == "identifier":
                return return_types.get(scope, {}).get(text(kids[0]))
            return None
        if local is None:
            return None
        if t == "subscript":
            kids = node.named_children
            if kids and kids[0].type == "identifier":
                base = text(kids[0])
                if base in local:
                    return (elems or {}).get(base)
                return member_elems.get(scope, {}).get(base)
            return None
        if t == "identifier":
            name = text(node)
            if name in local:
                return local[name]
            return member_types.get(scope, {}).get(name)
        return None

    # Member types, now that every return type in the file is known.
    for scope, name, type_name, elem, value in pending_members:
        member_types.setdefault(scope, {})[name] = (
            type_name if type_name is not None else value_type(value, scope))
        if elem is not None:
            member_elems.setdefault(scope, {})[name] = elem
    for scope, table in member_types.items():
        fields = {k: v for k, v in sorted(table.items()) if v and v not in _BUILTIN_TYPES}
        if fields and scope in node_by_id:
            node_by_id[scope]["_gd_fields"] = fields

    # Member type annotations (`var weapon: Weapon`): an inner class of this
    # script, or another script's class_name for the resolver.
    for scope, type_name, line in type_refs:
        inner = scopes[owner_id].get(("class", type_name))
        if inner is not None:
            add_edge(scope, inner, "references", line, context="type")
        elif scope == owner_id:
            add_ref(owner_id, line, "type", name=type_name)

    # `var x: int: set = _set_x, get = _get_x`: the property uses those methods.
    for scope, func, line, kind in accessor_refs:
        nid = scopes[scope].get(("function", func))
        if nid is not None:
            add_edge(scope, nid, "references", line, context=kind)

    # preload/load anywhere in the file: an import of that script or scene.
    # A const alias is tagged so the resolver can follow `Alias.method()`.
    alias_of = {target: name for name, target in aliases.items()}

    def walk_loads(node) -> None:
        if node.type == "call":
            target = load_target(node)
            if target is not None:
                extra: dict[str, Any] = {"target_file": str(target)}
                if target in alias_of:
                    extra["_gd_alias"] = alias_of[target]
                add_edge(file_id, _make_id(str(target)), "imports_from", line_of(node),
                         context="preload", **extra)
        for c in node.named_children:
            walk_loads(c)

    walk_loads(root)

    def local_signal(scope: str, name: str, caller: str, line: int, top: bool, context: str) -> None:
        nid = scopes[scope].get(("signal", name))
        if nid is not None:
            add_edge(caller, nid, "uses", line, context=context)
        elif top:
            add_ref(caller, line, "signal", name=name, context=context)

    def callback(args, scope: str, caller: str, line: int) -> None:
        first = args.named_children[0] if args is not None and args.named_children else None
        if first is None:
            return
        if first.type == "attribute":
            parts = first.named_children
            if len(parts) == 2 and all(p.type == "identifier" for p in parts) and text(parts[0]) == "self":
                first = parts[1]
        if first.type != "identifier":
            return
        nid = scopes[scope].get(("function", text(first)))
        if nid is not None:
            add_edge(caller, nid, "references", line, context="connect")

    def declare_local(table: dict[str, str | None], name: str, vtype: str | None) -> None:
        # Two declarations of one name with different types: trust neither.
        table[name] = None if name in table and table[name] != vtype else vtype

    def collect_locals(fn, scope: str) -> tuple[dict[str, str | None], dict[str, str]]:
        """Every parameter and local of ``fn`` -> its type (None: untyped), and
        the element type of those declared ``Array[Foo]``. Flow-insensitive: one
        name keeps one type for the whole function."""
        types: dict[str, str | None] = {}
        elems: dict[str, str] = {}

        def params(node) -> None:
            for p in node.named_children if node is not None else ():
                if p.type == "identifier":
                    declare_local(types, text(p), None)
                    continue
                ident = next((c for c in p.named_children if c.type == "identifier"), None)
                if ident is None:
                    continue
                type_node = p.child_by_field_name("type")
                ptype = plain_type(type_node) if type_node is not None and type_node.type == "type" else None
                if ptype is None:   # `x := Foo.new()`
                    ptype = value_type(p.child_by_field_name("value"), scope, types, elems)
                declare_local(types, text(ident), ptype)
                elem = elem_type(type_node)
                if elem is not None:
                    elems[text(ident)] = elem

        def walk(node) -> None:
            if node.type == "variable_statement":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    type_node = node.child_by_field_name("type")
                    vtype = plain_type(type_node)
                    if vtype is None:
                        vtype = value_type(node.child_by_field_name("value"), scope, types, elems)
                    declare_local(types, text(name_node), vtype)
                    elem = elem_type(type_node)
                    if elem is not None:
                        elems[text(name_node)] = elem
            elif node.type == "for_statement":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    ltype = plain_type(node.child_by_field_name("type"))   # `for x: Foo in …`
                    right = node.child_by_field_name("right")
                    if ltype is None and right is not None and right.type == "identifier":
                        rname = text(right)
                        ltype = elems.get(rname) if rname in types else member_elems.get(scope, {}).get(rname)
                    declare_local(types, text(left), ltype)
            elif node.type == "lambda":
                params(node.child_by_field_name("parameters"))
            for c in node.named_children:
                walk(c)

        params(fn.child_by_field_name("parameters"))
        body = fn.child_by_field_name("body")
        if body is not None:
            walk(body)
        return types, elems

    def typed_target(caller: str, line: int, vtype: str, path: list[str], member: str,
                     signal_context: str | None = None) -> None:
        """Bind ``<a value of type vtype>.path….member`` here when the type is an
        inner class of this script, else hand it to the resolver."""
        current = scopes[owner_id].get(("class", vtype))
        hops = list(path)
        while current is not None and hops:
            nxt = member_types.get(current, {}).get(hops[0])
            if not nxt:
                return   # untyped or unknown member of the inner class
            hops.pop(0)
            vtype = nxt
            current = scopes[owner_id].get(("class", vtype))
        if current is not None:
            kind = "signal" if signal_context else "function"
            nid = scopes.get(current, {}).get((kind, member))
            if nid is not None:
                if signal_context:
                    add_edge(caller, nid, "uses", line, context=signal_context)
                else:
                    add_edge(caller, nid, "calls", line, context="call")
            return
        if vtype in _BUILTIN_TYPES:
            return
        if signal_context:
            add_ref(caller, line, "typed_signal", recv_type=vtype, path=hops, name=member,
                    context=signal_context)
        else:
            add_ref(caller, line, "typed_call", recv_type=vtype, path=hops, callee=member)

    def bind_receiver(caller: str, scope: str, line: int, top: bool, recv: str, path: list[str],
                      member: str, local: dict[str, str | None], *,
                      signal_context: str | None = None, via_self: bool = False) -> None:
        """``recv.path….member``: bind through the receiver's declared type."""
        if recv in local:   # a parameter or local shadows every global name
            if local[recv]:
                typed_target(caller, line, str(local[recv]), path, member, signal_context)
            return
        table = member_types.get(scope, {})
        if recv in table:   # a member of this script or inner class
            if table[recv]:
                typed_target(caller, line, str(table[recv]), path, member, signal_context)
            return
        kind = "typed_signal" if signal_context else "typed_call"
        if via_self:        # `self.x…`: x is declared on an ancestor
            add_ref(caller, line, kind, receiver=recv, path=path, member_only=True,
                    **({"name": member, "context": signal_context} if signal_context else {"callee": member}))
            return
        inner = scopes[owner_id].get(("class", recv))
        if inner is not None:
            if path or signal_context:
                return   # `Inner.static_var.f()`: not modelled
            if member == "new":
                add_edge(caller, inner, "references", line, context="instantiates")
                init = scopes.get(inner, {}).get(("function", "_init"))
                if init is not None:
                    add_edge(caller, init, "calls", line, context="constructor")
            else:
                nid = scopes.get(inner, {}).get(("function", member))
                if nid is not None:
                    add_edge(caller, nid, "calls", line, context="call")
            return
        # Not declared in this file: an alias, an autoload, a class_name, or a
        # member an ancestor declares. The resolver tries them in that order.
        if not path:
            if signal_context:
                if top:
                    add_ref(caller, line, "receiver_signal", receiver=recv, name=member,
                            context=signal_context)
            else:
                add_ref(caller, line, "receiver_call", receiver=recv, callee=member)
            return
        add_ref(caller, line, kind, receiver=recv, path=path,
                **({"name": member, "context": signal_context} if signal_context else {"callee": member}))

    def handle_call(node, caller: str, scope: str, func_name: str, top: bool) -> None:
        kids = node.named_children
        if not kids or kids[0].type != "identifier":
            return
        fn = text(kids[0])
        line = line_of(node)
        if fn in ("preload", "load"):
            return
        if fn == "emit_signal":
            args_node = node.child_by_field_name("arguments")
            args = args_node.named_children if args_node is not None else []
            name = string_value(args[0]) if args else None
            if name:
                local_signal(scope, name, caller, line, top, "emit")
            return
        if fn == "super":
            if top:
                add_ref(caller, line, "call", callee=func_name, super=True)
            return
        nid = scopes[scope].get(("function", fn))
        if nid is not None:
            add_edge(caller, nid, "calls", line, context="call")
        elif top:
            add_ref(caller, line, "call", callee=fn)

    def handle_attribute(node, caller: str, scope: str, top: bool,
                         local: dict[str, str | None], elems: dict[str, str]) -> None:
        kids = node.named_children
        call_at = next((i for i, c in enumerate(kids) if c.type == "attribute_call"), None)
        if call_at is None:
            return
        chain = kids[:call_at]
        call = kids[call_at]
        method_node = next((c for c in call.named_children if c.type == "identifier"), None)
        if not chain or method_node is None:
            return
        if not all(c.type == "identifier" for c in chain[1:]):
            return
        method = text(method_node)
        args = call.child_by_field_name("arguments")
        line = line_of(node)
        head_node = chain[0]
        rest = [text(c) for c in chain[1:]]

        if head_node.type in ("parenthesized_expression", "call", "subscript"):
            # `(x as Foo).f()`, `make_foo().f()`, `foos[i].f()`: the evident type
            vtype = value_type(head_node, scope, local, elems)
            if not vtype:
                return
            if method in _SIGNAL_METHODS and rest:
                typed_target(caller, line, vtype, rest[:-1], rest[-1], method)
                if method == "connect":
                    callback(args, scope, caller, line)
            elif method != "new":
                typed_target(caller, line, vtype, rest, method)
            return
        if head_node.type != "identifier":
            return   # `$Node.f()`, `%Unique.f()`, `get_node(...).f()`: receiver unknown here

        head = text(head_node)
        if head == "super":
            if not rest and top:
                add_ref(caller, line, "call", callee=method, super=True)
            return
        via_self = head == "self"
        names = rest if via_self else [head, *rest]
        view = {} if via_self else local   # `self.x` is never a local

        if method in _SIGNAL_METHODS and names:
            if len(names) == 1:
                local_signal(scope, names[0], caller, line, top, method)
            else:
                bind_receiver(caller, scope, line, top, names[0], names[1:-1], names[-1], view,
                              signal_context=method, via_self=via_self)
            if method == "connect":
                callback(args, scope, caller, line)
            return

        if not names:   # `self.f()`
            nid = scopes[scope].get(("function", method))
            if nid is not None:
                add_edge(caller, nid, "calls", line, context="call")
            elif top:
                add_ref(caller, line, "call", callee=method)
            return

        bind_receiver(caller, scope, line, top, names[0], names[1:], method, view, via_self=via_self)

    def walk_calls(node, caller: str, scope: str, func_name: str, top: bool,
                   local: dict[str, str | None], elems: dict[str, str]) -> None:
        if node.type == "call":
            handle_call(node, caller, scope, func_name, top)
        elif node.type == "attribute":
            handle_attribute(node, caller, scope, top, local, elems)
        for c in node.named_children:
            walk_calls(c, caller, scope, func_name, top, local, elems)

    for caller, body, scope, func_name, fn in bodies:
        local, elems = collect_locals(fn, scope)
        walk_calls(body, caller, scope, func_name, scope == owner_id, local, elems)
    for value, scope in member_values:
        walk_calls(value, scope, scope, "", True, {}, {})
    for body, scope in accessor_bodies:
        walk_calls(body, scope, scope, "", scope == owner_id, {}, {})

    return {"nodes": nodes, "edges": edges, "raw_calls": raw_calls}


def resolve_gdscript_references(
    per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]
) -> None:
    """Resolve the cross-file GDScript references the per-file pass left behind.

    A name binds only when exactly one candidate exists: one script with that
    ``class_name``, one autoload with that name. Every edge it adds is sourced by
    a re-extracted file; unchanged files only serve as targets.
    """
    def meta(node: dict) -> dict:
        m = node.get("metadata")
        return m if isinstance(m, dict) and m.get("language") == _LANGUAGE else {}

    gd = {n["id"]: meta(n) for n in all_nodes if n.get("id") and meta(n)}
    classes: dict[str, list[str]] = {}
    autoloads: dict[str, list[str]] = {}
    for nid, m in gd.items():
        if m.get("kind") == "class":
            classes.setdefault(str(m.get("name")), []).append(nid)
        elif m.get("kind") == "autoload":
            autoloads.setdefault(str(m.get("name")), []).append(nid)
    # scope node id -> member -> declared type (see `_gd_fields` in the module doc)
    fields: dict[str, dict[str, str]] = {
        n["id"]: n["_gd_fields"] for n in all_nodes
        if n.get("id") in gd and isinstance(n.get("_gd_fields"), dict)
    }

    members: dict[str, dict[tuple[str, str], str]] = {}
    owner_of: dict[str, str] = {}
    class_of_file: dict[str, str] = {}
    autoload_target: dict[str, str] = {}
    aliases: dict[tuple[str, str], str] = {}
    for e in all_edges:
        rel, src, tgt = e.get("relation"), e.get("source"), e.get("target")
        if rel in ("method", "contains") and tgt in gd and src in gd:
            kind = gd[tgt].get("kind")
            if kind in ("function", "signal"):
                members.setdefault(src, {})[(kind, str(gd[tgt].get("name")))] = tgt
                owner_of[tgt] = src
            elif kind == "class":
                class_of_file[src] = tgt
        elif rel == "imports_from" and src in gd:
            if gd[src].get("kind") == "autoload":
                autoload_target[src] = tgt
            alias = e.pop("_gd_alias", None)
            if alias:
                aliases[(str(e.get("source_file")), alias)] = tgt

    def owner(nid: str) -> str:
        return class_of_file.get(nid, nid)

    def unique(table: dict[str, list[str]], name: str) -> str | None:
        hits = table.get(name, [])
        return hits[0] if len(hits) == 1 else None

    existing = {(e.get("source"), e.get("target"), e.get("relation")) for e in all_edges}

    def add(rc: dict, src: str, tgt: str, relation: str, context: str) -> None:
        if src == tgt or (src, tgt, relation) in existing:
            return
        existing.add((src, tgt, relation))
        all_edges.append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "context": context,
            "confidence": "EXTRACTED",
            "confidence_score": 1.0,
            "source_file": rc.get("source_file", ""),
            "source_location": rc.get("source_location"),
            "weight": 1.0,
        })

    refs = [
        rc for result in per_file for rc in result.get("raw_calls", [])
        if rc.get("language") == _LANGUAGE and rc.get("gd_kind")
    ]

    # Inheritance first: the member lookups below walk it.
    for rc in refs:
        if rc["gd_kind"] == "extends":
            target = unique(classes, str(rc.get("name")))
            if target is not None:
                add(rc, rc["caller_nid"], target, "inherits", "extends")

    parent: dict[str, str] = {}
    for e in all_edges:
        if e.get("relation") == "inherits" and e.get("source") in gd and e.get("target") in gd:
            parent[owner(e["source"])] = owner(e["target"])

    def lineage(start: str, include_self: bool) -> list[str]:
        out = [start] if include_self else []
        seen = {start}
        current = start
        while current in parent and parent[current] not in seen:
            current = parent[current]
            seen.add(current)
            out.append(current)
        return out

    def lookup(start: str, kind: str, name: str, include_self: bool = True) -> str | None:
        for scope in lineage(owner(start), include_self):
            hit = members.get(scope, {}).get((kind, name))
            if hit is not None:
                return hit
        return None

    def field_type(start: str, name: str) -> str | None:
        """The declared type of member ``name`` on ``start`` or an ancestor."""
        for scope in lineage(owner(start), True):
            hit = fields.get(scope, {}).get(name)
            if hit:
                return hit
        return None

    def class_owner(type_name: str | None) -> str | None:
        target = unique(classes, type_name) if type_name else None
        return owner(target) if target is not None else None

    def type_owner(rc: dict, type_name: str) -> str | None:
        """A type as the caller's file names it: a preload alias, else a class_name."""
        target = aliases.get((str(rc.get("source_file")), type_name))
        return owner(target) if target is not None else class_owner(type_name)

    def receiver(rc: dict) -> tuple[str | None, bool]:
        """(owner, named): the scope a receiver name stands for, and whether it
        named a script itself (an alias, autoload or class_name) rather than a
        member variable an ancestor of the caller declares."""
        name = str(rc.get("receiver"))
        if not rc.get("member_only"):
            target = aliases.get((str(rc.get("source_file")), name))
            if target is None:
                auto = unique(autoloads, name)
                target = autoload_target.get(auto) if auto is not None else None
            if target is None:
                target = unique(classes, name)
            if target is not None:
                return owner(target), True
        caller = rc["caller_nid"]
        held = field_type(owner_of.get(caller, caller), name)
        return (class_owner(held) if held else None), False

    def follow(start: str | None, path: list[str]) -> str | None:
        """Walk ``x.a.b`` member hops: each hop is the declared type of a member."""
        current = start
        for hop in path:
            if current is None:
                return None
            current = class_owner(field_type(current, hop))
        return current

    for rc in refs:
        kind = rc["gd_kind"]
        caller = rc["caller_nid"]
        scope = owner_of.get(caller, caller)
        if kind == "type":
            target = unique(classes, str(rc.get("name")))
            if target is not None:
                add(rc, caller, target, "references", "type")
        elif kind == "call":
            target = lookup(scope, "function", str(rc.get("callee")),
                            include_self=not rc.get("super"))
            if target is not None:
                add(rc, caller, target, "calls", "call")
        elif kind == "signal":
            target = lookup(scope, "signal", str(rc.get("name")))
            if target is not None:
                add(rc, caller, target, "uses", str(rc.get("context")))
        elif kind in ("receiver_call", "receiver_signal"):
            target_owner, named = receiver(rc)
            if target_owner is None:
                continue
            if kind == "receiver_signal":
                target = lookup(target_owner, "signal", str(rc.get("name")))
                if target is not None:
                    add(rc, caller, target, "uses", str(rc.get("context")))
            elif rc.get("callee") == "new":
                if named:
                    add(rc, caller, target_owner, "references", "instantiates")
                    init = lookup(target_owner, "function", "_init")
                    if init is not None:
                        add(rc, caller, init, "calls", "constructor")
            else:
                target = lookup(target_owner, "function", str(rc.get("callee")))
                if target is not None:
                    add(rc, caller, target, "calls", "call")
        elif kind in ("typed_call", "typed_signal"):
            if rc.get("recv_type"):
                start = type_owner(rc, str(rc["recv_type"]))
            else:
                start, _named = receiver(rc)
            target_owner = follow(start, list(rc.get("path") or ()))
            if target_owner is None:
                continue
            if kind == "typed_signal":
                target = lookup(target_owner, "signal", str(rc.get("name")))
                if target is not None:
                    add(rc, caller, target, "uses", str(rc.get("context")))
            else:
                target = lookup(target_owner, "function", str(rc.get("callee")))
                if target is not None:
                    add(rc, caller, target, "calls", "call")
