# Godot support (GDScript, scenes, resources, shaders)

This fork maps Godot 4 projects. It starts from upstream
[PR #4484](https://github.com/Graphify-Labs/graphify/pull/4484) (GDScript and
Godot scene/project extraction, still open upstream) and adds typed-receiver
resolution, constructors, property accessors, shaders and Godot-aware file
detection.

## What is read

| Files | Extractor | Nodes | Edges |
|---|---|---|---|
| `.gd` | `extractors/gdscript.py` (tree-sitter grammar from `tree-sitter-language-pack`) | file, `class_name` class, functions (incl. `_init`), signals, consts, enums, inner classes | `method`, `contains`, `calls`, `uses` (signal emit/connect), `references` (types, `connect` callbacks, `.new()`, property `set =`/`get =`), `inherits`, `imports_from` (`preload`/`load`) |
| `.tscn`, `.tres` | `extractors/godot_resource.py` (line parser, no grammar) | file | `imports_from` every `[ext_resource]` script, scene, resource or shader; `references` the method a `[connection]` calls |
| `project.godot` | same | file, one node per `[autoload]` | autoload `imports_from` its script/scene; `references` the main scene |
| `.gdshader`, `.gdshaderinc` | `extractors/gdshader.py` (comment-aware brace scan, no grammar) | file (`shader_type` in metadata), functions, uniforms | `method`, `contains`, `calls` between functions of the file, `imports_from` each `#include` |

Godot's sidecar files (`x.png.import`, `x.gd.uid`), the `.godot/` and `.import/`
editor caches and `.gdignore` markers are skipped instead of being reported as
unsupported files. A sidecar whose companion file is gone is still listed, so
stale `.uid` files show up.

## How a call is bound

Binding fails closed: a name binds only when exactly one script declares that
`class_name` or exactly one autoload has that name; anything else stays
unresolved rather than guessed.

- **Bare calls** `f()` bind to the script's own methods, then up the `extends`
  chain (`extends Foo`, `extends "res://…"`, `class_name Foo extends Bar`).
  `super.f()` / `super()` start at the parent.
- **Named receivers**: `Alias.f()` for `const Alias := preload("…")`,
  `GameState.f()` for an autoload, `Foo.f()` / `Foo.new()` for a `class_name`.
  `Foo.new()` also calls `Foo._init()`.
- **Typed receivers** bind through the declared or evident type:
  - parameters `func f(hero: Hero)`, locals `var h: Hero`, members
    `@export var weapon: Weapon`, `@onready var hud: Hud = $Hud`;
  - inferred types `x := Hero.new()`, `x := y as Hero`, `x := make()` where
    `func make() -> Hero` is in the same script;
  - casts `(node as Hero).attack()`, `make().attack()`, `heroes[i].attack()`
    and `for h in heroes` where `heroes: Array[Hero]`, or `for h: Hero in …`;
  - member chains `battle.model.reset()`, one declared member type per hop;
  - members declared on an ancestor (`leader.attack()`, `self.leader…`);
  - signals the same way: `hero.died.connect(_on_died)`.
- An **untyped** variable binds nothing, and a local or member shadows any
  class or autoload with the same name, as in GDScript itself.

Member types are stored on the owning class/file node as `_gd_fields`
(`{member: type}`), which `graphify watch`/`update` forward to incremental
rebuilds, so a changed script resolves the same as on a full build.

## Running it on a Godot project

Install the optional grammar (`pip install "graphifyy[gdscript]"`, or
`uv sync --extra gdscript` in a checkout). Scenes, resources, `project.godot`
and shaders need no grammar.

For a code map that never leaves the machine and needs no API key:

```bash
graphify extract path/to/godot/project --code-only --out path/to/output
```

`--code-only` matters on game projects: without it every texture and sprite
(`.png`, `.jpg`) is sent to the semantic (LLM) pass. `--out` keeps
`graphify-out/` out of the game repository.

## Known gaps

- `$Node.f()`, `%Unique.f()` and `get_node("…").f()`: the receiver's script
  would have to be read from the scene the script is attached to.
- `uid://` references (`preload("uid://…")`, an `[ext_resource]` with a uid
  but no path): Godot writes `path=` next to the uid, so this is rare.
- Untyped variables, return types of functions in other scripts
  (`Factory.make().f()`), qualified inner types (`var c: Grid.Cell`), and
  dynamic calls (`call("f")`, `callv`, `Callable(obj, "f")`).
- Calls from a script into a shader's uniforms
  (`set_shader_parameter("name", …)`) are not linked.

## Measured on a production project

A Godot 4.7 mobile game (about 520 scripts / 105k lines of GDScript, 420
scenes, 37 shaders), `--code-only`, about 5 s:

| | PR #4484 | this fork |
|---|---|---|
| `calls` edges from `.gd` | 13,327 | 17,794 |
| of which cross-file | 5,648 | 9,997 |
| signal `uses` edges | 220 | 474 |
| `_init()` constructors modelled | 12 | 149 |
| edges into shaders | 0 | 111 |
| files reported as unsupported | 593 (38%) | 4 (stale `.uid`) |
