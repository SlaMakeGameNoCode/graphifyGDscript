"""GDScript (.gd) and Godot text resources (.tscn/.tres/project.godot)."""
from __future__ import annotations

import importlib.util as _ilu
import shutil
import sys
from pathlib import Path

import pytest

from graphify.detect import CODE_EXTENSIONS
from graphify.extract import _DISPATCH, extract, extract_gdscript, extract_godot_resource

# The GDScript grammar comes from tree-sitter-language-pack, an optional extra.
# Scenes and project.godot need no grammar and are always tested.
_needs_gdscript = pytest.mark.skipif(
    _ilu.find_spec("tree_sitter_language_pack") is None,
    reason="tree-sitter-language-pack not installed (optional [gdscript] extra)",
)

FIXTURE = Path(__file__).parent / "fixtures" / "godot"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    shutil.copytree(FIXTURE, root)
    return root


def _godot_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix in (".gd", ".tscn", ".tres", ".godot"))


def _extract(root: Path, paths: list[Path] | None = None, **kwargs) -> dict:
    return extract(paths or _godot_files(root), cache_root=root, root=root, parallel=False, **kwargs)


def _name(node: dict) -> str:
    return f"{Path(str(node['source_file'])).name}:{node['label']}"


def _pairs(result: dict, relation: str) -> set[tuple[str, str]]:
    names = {n["id"]: _name(n) for n in result["nodes"]}
    return {
        (names.get(e["source"], e["source"]), names.get(e["target"], e["target"]))
        for e in result["edges"]
        if e["relation"] == relation
    }


def test_godot_extensions_are_dispatched():
    for ext in (".gd", ".tscn", ".tres", ".godot"):
        assert ext in CODE_EXTENSIONS
    assert _DISPATCH[".gd"] is extract_gdscript
    for ext in (".tscn", ".tres", ".godot"):
        assert _DISPATCH[ext] is extract_godot_resource


@_needs_gdscript
def test_gdscript_declarations_hang_off_the_class_name(tmp_path):
    root = _project(tmp_path)
    r = extract_gdscript(root / "actors" / "player.gd")
    assert "error" not in r
    labels = {n["label"]: n for n in r["nodes"]}
    for want in ("player.gd", "Player", ".clamp_speed()", "._ready()", "signal jumped",
                 "signal landed", "State", "MAX_JUMPS", "Movement", "Stats", ".speed_for()"):
        assert want in labels, want
    assert labels["Player"]["metadata"] == {"language": "gdscript", "kind": "class", "name": "Player"}
    contains = _pairs(r, "contains")
    methods = _pairs(r, "method")
    assert ("player.gd:player.gd", "player.gd:Player") in contains
    assert ("player.gd:Player", "player.gd:._ready()") in methods
    assert ("player.gd:Player", "player.gd:signal jumped") in contains
    assert ("player.gd:Stats", "player.gd:.speed_for()") in methods
    # a script without class_name: the file node owns its members
    enemy = extract_gdscript(root / "actors" / "enemy.gd")
    assert ("enemy.gd:enemy.gd", "enemy.gd:.patrol()") in _pairs(enemy, "method")


@_needs_gdscript
def test_gdscript_same_file_calls_signals_and_loads(tmp_path):
    root = _project(tmp_path)
    r = extract_gdscript(root / "actors" / "player.gd")
    calls = _pairs(r, "calls")
    assert ("player.gd:._ready()", "player.gd:.clamp_speed()") in calls   # bare and self.
    assert ("player.gd:.speed_for()", "player.gd:.base_speed()") in calls  # inner class
    uses = _pairs(r, "uses")
    assert ("player.gd:._ready()", "player.gd:signal jumped") in uses      # connect + emit
    assert ("player.gd:._ready()", "player.gd:signal landed") in uses      # emit_signal("landed")
    refs = _pairs(r, "references")
    assert ("player.gd:._ready()", "player.gd:._on_jumped()") in refs     # connect callback
    assert ("player.gd:._ready()", "player.gd:._on_landed()") in refs     # self._on_landed
    assert ("player.gd:._ready()", "player.gd:Stats") in refs             # Stats.new()
    loads = [e for e in r["edges"] if e["relation"] == "imports_from"]
    assert {Path(e["target_file"]).name for e in loads} == {"movement.gd", "bullet.tscn"}
    # what needs another file is left to the resolver, never to the shared
    # name-matching pass
    assert r["raw_calls"] and all(rc["is_member_call"] for rc in r["raw_calls"])


@_needs_gdscript
def test_gdscript_cross_file_references_resolve(tmp_path):
    root = _project(tmp_path)
    r = _extract(root)
    calls = _pairs(r, "calls")
    assert ("player.gd:._ready()", "game_state.gd:.add_score()") in calls        # autoload
    assert ("player.gd:._ready()", "movement.gd:.walk_vector()") in calls        # preload alias
    assert ("player.gd:._ready()", "actor.gd:.take_damage()") in calls           # inherited
    assert ("grunt.gd:._ready()", "actor.gd:.take_damage()") in calls            # grandparent
    assert ("grunt.gd:._ready()", "enemy.gd:.patrol()") in calls                 # super.patrol()
    assert ("grunt.gd:.patrol()", "enemy.gd:.patrol()") in calls                 # super()
    assert not any(src.startswith("grunt.gd") and dst == "grunt.gd:.patrol()"
                   for src, dst in calls)
    inherits = _pairs(r, "inherits")
    assert ("player.gd:Player", "actor.gd:Actor") in inherits                   # class_name
    assert ("enemy.gd:enemy.gd", "actor.gd:Actor") in inherits
    assert ("grunt.gd:grunt.gd", "enemy.gd:enemy.gd") in inherits               # res:// path
    uses = _pairs(r, "uses")
    assert ("player.gd:._ready()", "game_state.gd:signal paused") in uses      # autoload signal
    assert ("grunt.gd:._ready()", "actor.gd:signal died") in uses              # inherited signal
    refs = _pairs(r, "references")
    assert ("player.gd:Player", "weapon.gd:Weapon") in refs                     # var weapon: Weapon
    assert ("player.gd:._ready()", "weapon.gd:Weapon") in refs                  # Weapon.new()
    # a member declared `var weapon: Weapon` binds through its type
    assert ("player.gd:._ready()", "weapon.gd:.fire()") in calls
    # engine calls, unknown receivers and unknown members bind to nothing
    targets = {dst for _src, dst in calls}
    assert not any(t.endswith((":.unknown_method()", ":.add_child()", ":.print()"))
                   for t in targets)


@_needs_gdscript
def test_gdscript_bare_call_never_binds_an_unrelated_script(tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (tmp_path / "a.gd").write_text("extends Node\n\n\nfunc refresh() -> void:\n\tpass\n",
                                   encoding="utf-8")
    caller = tmp_path / "b.gd"
    caller.write_text("extends Node\n\n\nfunc _ready() -> void:\n\trefresh()\n", encoding="utf-8")
    r = _extract(tmp_path, [tmp_path / "a.gd", caller])
    # b.gd does not inherit a.gd, so GDScript cannot reach a.gd's refresh()
    assert not _pairs(r, "calls")


@_needs_gdscript
def test_gdscript_ambiguous_class_name_stays_unresolved(tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    for folder in ("one", "two"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "unit.gd").write_text(
            "class_name Unit\nextends Node\n\n\nfunc move() -> void:\n\tpass\n", encoding="utf-8")
    caller = tmp_path / "army.gd"
    caller.write_text("extends Unit\n\n\nfunc _ready() -> void:\n\tmove()\n\tUnit.new()\n",
                      encoding="utf-8")
    r = _extract(tmp_path)
    assert not _pairs(r, "inherits")
    assert not _pairs(r, "calls")
    assert not _pairs(r, "references")


def test_godot_scene_links_scripts_scenes_and_signal_handlers(tmp_path):
    root = _project(tmp_path)
    r = extract_godot_resource(root / "scenes" / "main.tscn")
    assert "error" not in r
    loads = {(e["context"], Path(e["target_file"]).name) for e in r["edges"]
             if e["relation"] == "imports_from"}
    assert loads == {("Script", "player.gd"), ("PackedScene", "bullet.tscn")}   # no texture
    handlers = [e for e in r["edges"] if e["relation"] == "references"]
    # the "." connection has no script on the root node, so it is dropped
    assert len(handlers) == 1
    assert handlers[0]["context"] == "signal pressed"
    assert Path(handlers[0]["target_file"]).name == "player.gd"


def test_godot_project_declares_autoloads_and_main_scene(tmp_path):
    root = _project(tmp_path)
    r = extract_godot_resource(root / "project.godot")
    autoloads = {n["label"]: n for n in r["nodes"] if n["metadata"]["kind"] == "autoload"}
    assert set(autoloads) == {"GameState", "Hud", "Probe"}
    linked = {e["source"]: Path(e["target_file"]).name for e in r["edges"]
              if e["relation"] == "imports_from"}
    # uid:// names no file this pass can see: the autoload stays, unlinked
    assert linked == {autoloads["GameState"]["id"]: "game_state.gd",
                      autoloads["Hud"]["id"]: "hud.tscn"}
    main = [e for e in r["edges"] if e.get("context") == "main_scene"]
    assert [Path(e["target_file"]).name for e in main] == ["main.tscn"]


def test_godot_scenes_and_resources_are_not_reported_as_symbolless(tmp_path, capsys):
    root = _project(tmp_path)
    # bullet.tscn and hud.tscn are a bare file node, main.tscn only adds edges
    _extract(root, [p for p in _godot_files(root) if p.suffix != ".gd"])
    assert "yielded no symbols" not in capsys.readouterr().err


@_needs_gdscript
def test_godot_scene_handler_lands_on_the_script_method(tmp_path):
    root = _project(tmp_path)
    r = _extract(root)
    assert ("main.tscn:main.tscn", "player.gd:._on_jumped()") in _pairs(r, "references")
    assert ("project.godot:GameState", "game_state.gd:game_state.gd") in _pairs(r, "imports_from")


def _as_watch_context(result: dict, changed: Path) -> tuple[list[dict], list[dict]]:
    """What `graphify watch` hands an incremental rebuild for the unchanged files:
    the same node fields, metadata keys and edge relations watch.py forwards."""
    changed_name = changed.name
    nodes = [
        {
            **{k: n.get(k) for k in ("id", "label", "source_file", "file_type", "type")},
            **({"_callable": True} if n.get("_callable") else {}),
            **({"_gd_fields": n["_gd_fields"]} if n.get("_gd_fields") else {}),
            "metadata": {k: v for k, v in (n.get("metadata") or {}).items()
                         if k in ("language", "kind", "module", "name", "arity")},
        }
        for n in result["nodes"]
        if n.get("source_file") and Path(str(n["source_file"])).name != changed_name
    ]
    edges = [
        {k: e.get(k) for k in ("source", "target", "relation", "source_file")}
        for e in result["edges"]
        if e["relation"] in ("contains", "method", "inherits", "imports", "imports_from")
        and Path(str(e["source_file"])).name != changed_name
    ]
    return nodes, edges


@_needs_gdscript
def test_gdscript_incremental_rebuild_matches_full_build(tmp_path):
    root = _project(tmp_path)
    full = _extract(root)
    player = root / "actors" / "player.gd"
    player.write_text(player.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    context_nodes, context_edges = _as_watch_context(full, player)
    changed = _extract(root, [player], resolution_context_nodes=context_nodes,
                       resolution_context_edges=context_edges)

    def from_player(result: dict) -> set[tuple[str, str, str]]:
        return {(e["source"], e["target"], e["relation"]) for e in result["edges"]
                if Path(str(e["source_file"])).name == "player.gd"}

    assert from_player(changed) == from_player(full)
    assert ("player.gd:._ready()", "game_state.gd:.add_score()") in {
        (s, t) for s, t in _pairs(full, "calls")}


def test_gdscript_missing_grammar_reports_install_hint(tmp_path, monkeypatch, capsys):
    script = tmp_path / "lone.gd"
    script.write_text("extends Node\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    r = extract([script], cache_root=tmp_path)
    assert r["nodes"] == []
    assert 'pip install "graphifyy[gdscript]"' in capsys.readouterr().err


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


@_needs_gdscript
def test_gdscript_constructor_is_a_method_and_new_calls_it(tmp_path):
    _write(tmp_path, {
        "project.godot": "config_version=5\n",
        "unit.gd": ("class_name Unit\nextends RefCounted\n\nvar hp: int\n\n\n"
                    "func _init(start_hp: int) -> void:\n\thp = start_hp\n\treset()\n\n\n"
                    "func reset() -> void:\n\tpass\n"),
        "army.gd": "extends Node\n\n\nfunc _ready() -> void:\n\tvar u := Unit.new(3)\n",
        # a script whose only function is the constructor still has a symbol
        "probe.gd": "extends SceneTree\n\n\nfunc _init() -> void:\n\tquit()\n",
    })
    r = _extract(tmp_path)
    labels = {_name(n) for n in r["nodes"]}
    assert {"unit.gd:._init()", "probe.gd:._init()"} <= labels
    calls = _pairs(r, "calls")
    assert ("unit.gd:._init()", "unit.gd:.reset()") in calls        # inside the constructor
    assert ("army.gd:._ready()", "unit.gd:._init()") in calls        # Unit.new() runs _init
    assert ("army.gd:._ready()", "unit.gd:Unit") in _pairs(r, "references")


@_needs_gdscript
def test_gdscript_class_name_with_inline_extends(tmp_path):
    _write(tmp_path, {
        "project.godot": "config_version=5\n",
        "base.gd": "class_name Base\nextends Node\n\n\nfunc hit() -> void:\n\tpass\n",
        "boss.gd": "class_name Boss extends Base\n\n\nfunc _ready() -> void:\n\thit()\n",
    })
    r = _extract(tmp_path)
    assert ("boss.gd:Boss", "base.gd:Base") in _pairs(r, "inherits")
    assert ("boss.gd:._ready()", "base.gd:.hit()") in _pairs(r, "calls")


@_needs_gdscript
def test_gdscript_property_accessors_use_their_methods(tmp_path):
    script = tmp_path / "meter.gd"
    script.write_text(
        "extends Node\n\nsignal changed\n\n"
        "var level: int = 0:\n\tset(value):\n\t\tlevel = value\n\t\trefresh()\n\t\tchanged.emit()\n"
        "\tget:\n\t\treturn level\n\n"
        "var label: String = \"\": set = _set_label, get = _get_label\n\n\n"
        "func refresh() -> void:\n\tpass\n\n\n"
        "func _set_label(v: String) -> void:\n\tpass\n\n\n"
        "func _get_label() -> String:\n\treturn \"\"\n",
        encoding="utf-8")
    r = extract_gdscript(script)
    assert ("meter.gd:meter.gd", "meter.gd:.refresh()") in _pairs(r, "calls")
    assert ("meter.gd:meter.gd", "meter.gd:signal changed") in _pairs(r, "uses")
    refs = _pairs(r, "references")
    assert ("meter.gd:meter.gd", "meter.gd:._set_label()") in refs
    assert ("meter.gd:meter.gd", "meter.gd:._get_label()") in refs


_TYPED_PROJECT = {
    "project.godot": "config_version=5\n\n[autoload]\n\nGameState=\"*res://game_state.gd\"\n",
    "game_state.gd": "extends Node\n\n\nfunc add_score(points: int) -> void:\n\tpass\n",
    "hero.gd": ("class_name Hero\nextends Node\n\nsignal died\n\nvar weapon: Weapon\n\n\n"
                "func attack() -> void:\n\tweapon.fire()\n"),
    "weapon.gd": ("class_name Weapon\nextends Node\n\nvar ammo: Ammo\n\n\n"
                  "func fire() -> void:\n\tpass\n"),
    "ammo.gd": "class_name Ammo\nextends RefCounted\n\n\nfunc spend() -> void:\n\tpass\n",
    "base_unit.gd": "class_name BaseUnit\nextends Node\n\nvar leader: Hero\n",
    "arena.gd": (
        "extends BaseUnit\n\n"
        "var heroes: Array[Hero] = []\n"
        "var spare := Weapon.new()\n\n\n"
        "func by_param(target: Hero, extra := 2) -> void:\n\ttarget.attack()\n\n\n"
        "func by_local() -> void:\n\tvar h: Hero = heroes[0]\n\th.attack()\n\n\n"
        "func by_new() -> void:\n\tvar w := Weapon.new()\n\tw.fire()\n\n\n"
        "func by_cast(node) -> void:\n\t(node as Hero).attack()\n\n\n"
        "func by_loop() -> void:\n\tfor each in heroes:\n\t\teach.attack()\n\n\n"
        "func by_index() -> void:\n\theroes[1].attack()\n\n\n"
        "func by_return() -> void:\n\tpick().attack()\n\n\n"
        "func by_chain() -> void:\n\tspare.ammo.spend()\n\n\n"
        "func by_inherited() -> void:\n\tleader.attack()\n\n\n"
        "func by_self_signal() -> void:\n\tself.leader.died.connect(_on_died)\n\n\n"
        "func untyped() -> void:\n\tvar anything = get_node(\"A\")\n\tanything.attack()\n\n\n"
        "func shadowed() -> void:\n\tvar GameState = null\n\tGameState.add_score(1)\n\n\n"
        "func direct() -> void:\n\tGameState.add_score(1)\n\n\n"
        "func pick() -> Hero:\n\treturn null\n\n\n"
        "func _on_died() -> void:\n\tpass\n"),
}


@_needs_gdscript
def test_gdscript_typed_receivers_bind_through_their_declared_type(tmp_path):
    _write(tmp_path, _TYPED_PROJECT)
    r = _extract(tmp_path)
    calls = _pairs(r, "calls")
    for caller in ("by_param", "by_local", "by_cast", "by_loop", "by_index", "by_return",
                   "by_inherited"):
        assert (f"arena.gd:.{caller}()", "hero.gd:.attack()") in calls, caller
    assert ("arena.gd:.by_new()", "weapon.gd:.fire()") in calls          # x := Weapon.new()
    assert ("hero.gd:.attack()", "weapon.gd:.fire()") in calls           # typed member
    assert ("arena.gd:.by_chain()", "ammo.gd:.spend()") in calls         # member hops across files
    assert ("arena.gd:.by_self_signal()", "hero.gd:signal died") in _pairs(r, "uses")
    assert ("arena.gd:.by_self_signal()", "arena.gd:._on_died()") in _pairs(r, "references")
    assert ("arena.gd:.direct()", "game_state.gd:.add_score()") in calls
    # an untyped variable binds nothing, and a local shadows the autoload it is named after
    assert not any(src in ("arena.gd:.untyped()", "arena.gd:.shadowed()") for src, _ in calls)


@_needs_gdscript
def test_gdscript_typed_receivers_resolve_the_same_on_an_incremental_rebuild(tmp_path):
    _write(tmp_path, _TYPED_PROJECT)
    full = _extract(tmp_path)
    arena = tmp_path / "arena.gd"
    arena.write_text(arena.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    context_nodes, context_edges = _as_watch_context(full, arena)
    changed = _extract(tmp_path, [arena], resolution_context_nodes=context_nodes,
                       resolution_context_edges=context_edges)

    def from_arena(result: dict) -> set[tuple[str, str, str]]:
        return {(e["source"], e["target"], e["relation"]) for e in result["edges"]
                if Path(str(e["source_file"])).name == "arena.gd"}

    assert from_arena(changed) == from_arena(full)
    # the chain and the inherited member need the unchanged files' member types
    assert ("arena.gd:.by_chain()", "ammo.gd:.spend()") in _pairs(full, "calls")
    assert ("arena.gd:.by_inherited()", "hero.gd:.attack()") in _pairs(full, "calls")


def test_godot_sidecars_and_cache_are_not_reported_as_unclassified(tmp_path):
    from graphify.detect import detect

    _write(tmp_path, {
        "project.godot": "config_version=5\n",
        "player.gd": "extends Node\n",
        "player.gd.uid": "uid://b1x2y3z4\n",
        "icon.png.import": "[remap]\n\nimporter=\"texture\"\n",
        "stale.gd.uid": "uid://gone\n",          # its script is gone: still listed
        "art/.gdignore": "",
        ".godot/uid_cache.bin": "x",
        ".godot/imported/icon.png-0123.ctex": "x",
    })
    (tmp_path / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    result = detect(tmp_path)
    assert {Path(p).name for p in result["unclassified"]} == {"stale.gd.uid"}
    code = {Path(p).name for p in result["files"]["code"]}
    assert {"player.gd", "project.godot"} <= code


@_needs_gdscript
def test_godot_shaders_functions_uniforms_includes_and_users(tmp_path):
    _write(tmp_path, {
        "project.godot": "config_version=5\n",
        "shaders/common.gdshaderinc": "float wave(float t) {\n\treturn sin(t);\n}\n",
        "shaders/flame.gdshader": (
            "// flame { not code }\nshader_type spatial;\nrender_mode unshaded;\n"
            '#include "res://shaders/common.gdshaderinc"\n\n'
            "uniform float intensity : hint_range(0.0, 1.0) = 1.0; // glow\n"
            "uniform sampler2D noise_tex : hint_default_white;\n"
            "/* helper {\n   below */\n"
            "vec3 tint(vec3 c) {\n\tif (c.r > 0.5) {\n\t\treturn c;\n\t}\n\treturn c * intensity;\n}\n\n"
            "void vertex() {\n}\n\n"
            "void fragment() {\n\tALBEDO = tint(vec3(wave(TIME)));\n}\n"),
        # a same-named script beside the shader, with a same-named method
        "shaders/flame.gd": ('extends Node\n\nconst FLAME := preload("res://shaders/flame.gdshader")\n\n\n'
                             "func vertex() -> void:\n\tpass\n"),
        "fx/flame.tres": ('[gd_resource type="ShaderMaterial" load_steps=2 format=3]\n\n'
                          '[ext_resource type="Shader" path="res://shaders/flame.gdshader" id="1_s"]\n\n'
                          '[resource]\nshader = ExtResource("1_s")\n'),
    })
    r = _extract(tmp_path, sorted(p for p in tmp_path.rglob("*") if p.is_file()))
    labels = [_name(n) for n in r["nodes"]]
    for want in ("flame.gdshader:.vertex()", "flame.gdshader:.fragment()", "flame.gdshader:.tint()",
                 "flame.gdshader:uniform intensity", "flame.gdshader:uniform noise_tex",
                 "common.gdshaderinc:.wave()", "flame.gd:.vertex()"):
        assert labels.count(want) == 1, want
    assert not any(label.endswith(":.if()") for label in labels)   # a block inside a function
    assert ("flame.gdshader:.fragment()", "flame.gdshader:.tint()") in _pairs(r, "calls")
    imports = _pairs(r, "imports_from")
    assert ("flame.gdshader:flame.gdshader", "common.gdshaderinc:common.gdshaderinc") in imports
    assert ("flame.tres:flame.tres", "flame.gdshader:flame.gdshader") in imports    # material
    assert ("flame.gd:flame.gd", "flame.gdshader:flame.gdshader") in imports        # preload
    shader = next(n for n in r["nodes"] if _name(n) == "flame.gdshader:flame.gdshader")
    assert shader["metadata"]["shader_type"] == "spatial"
