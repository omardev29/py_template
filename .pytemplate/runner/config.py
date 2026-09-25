"""Carga, valida y edita pytemplate.toml.

El esquema se define con dataclasses: cada campo con su valor por defecto. Una clave
desconocida o de tipo incorrecto es un error (exit 2) con la ruta completa de la clave,
para que una errata no se ignore en silencio.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .project import CONFIG_FILE, SRC
from .ui import DeployError

BACKENDS = ("cpython", "pypy", "mypyc")
PROFILES = ("mypyc", "strict", "warn", "off")
METHODS = ("exe", "portable", "pyz", "wheel", "nuitka", "flet")
EDITORS = ("pylance", "basedpyright")

_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass
class AppConfig:
    name: str = "miapp"  # nombre del ejecutable/dist; el paquete es su versión en snake_case
    preset: str = "script"  # script | raylib | flet
    gui: bool = False  # True: exe sin consola, lanzadores con pythonw/pypyw
    assets: str = "assets"  # carpeta dentro de src/ que se empaqueta ("" = ninguna)


@dataclass
class BackendConfig:
    active: str = "cpython"
    supported: list[str] = field(default_factory=lambda: ["cpython", "mypyc"])


@dataclass
class PythonConfig:
    cpython: str = "3.14"
    pypy: str = "pypy@3.11.15"
    jit: bool = False
    jit_interpreter: str = ""  # ruta a un python.org 3.14 con JIT (vacío = buscarlo)


@dataclass
class TypingConfig:
    profile: str = "auto"  # auto | mypyc | strict | warn | off
    relaxed: str = "off"  # lo que significa "auto" con backend cpython/pypy
    editor: str = "pylance"  # pylance | basedpyright
    mypy_overrides: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CompileConfig:
    modules: list[str] = field(default_factory=lambda: ["miapp.core"])
    exclude: list[str] = field(default_factory=list)
    forbid_imports: list[str] = field(default_factory=list)
    annotate: bool = False
    opt_level: str = "3"
    multi_file: bool = False
    separate: bool = False
    strict_dunder_typing: bool = False


@dataclass
class ExeConfig:
    mode: str = "onefile"  # onefile | onedir
    console: str = "auto"  # auto (= not app.gui) | yes | no
    icon: str = ""
    hidden_imports: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class PortableConfig:
    runtime: str = "bundled"  # bundled (incluye el intérprete) | system (usa el del destino)
    prune: bool = True
    archive: bool = True
    targets: list[str] = field(default_factory=lambda: ["host"])
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class PyzConfig:
    targets: list[str] = field(default_factory=lambda: ["host"])


@dataclass
class WheelConfig:
    entry: str = ""  # "paquete.modulo:funcion" para [project.scripts]; vacío = <pkg>.app:main


@dataclass
class NuitkaConfig:
    mode: str = "standalone"  # standalone | onefile
    extra_args: list[str] = field(default_factory=list)


@dataclass
class FletBuildConfig:
    target: str = "host"  # host | windows | macos | linux | apk | aab | ipa | web
    extra_args: list[str] = field(default_factory=list)


@dataclass
class DeployConfig:
    optimize: int = 1
    default: dict[str, str] = field(
        default_factory=lambda: {"cpython": "exe", "mypyc": "exe", "pypy": "portable"}
    )
    exe: ExeConfig = field(default_factory=ExeConfig)
    portable: PortableConfig = field(default_factory=PortableConfig)
    pyz: PyzConfig = field(default_factory=PyzConfig)
    wheel: WheelConfig = field(default_factory=WheelConfig)
    nuitka: NuitkaConfig = field(default_factory=NuitkaConfig)
    flet: FletBuildConfig = field(default_factory=FletBuildConfig)


@dataclass
class TaskConfig:
    cmd: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    backend: str = ""  # entorno en el que corre (vacío = backend activo)
    uv: bool = True  # True: se ejecuta con `uv run` dentro del entorno del backend
    cwd: str = ""
    help: str = ""


@dataclass
class Config:
    schema: int = 1
    app: AppConfig = field(default_factory=AppConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    python: PythonConfig = field(default_factory=PythonConfig)
    typing: TypingConfig = field(default_factory=TypingConfig)
    compile: CompileConfig = field(default_factory=CompileConfig)
    deploy: DeployConfig = field(default_factory=DeployConfig)
    tasks: dict[str, TaskConfig] = field(default_factory=dict)
    preset: dict[str, dict[str, Any]] = field(default_factory=dict)
    vscode: dict[str, Any] = field(default_factory=dict)

    # --- valores derivados -------------------------------------------------------------

    @property
    def pkg(self) -> str:
        """Nombre del paquete Python de la app (src/<pkg>/)."""
        return self.app.name.replace("-", "_").lower()

    def supports(self, backend: str) -> bool:
        return backend in self.backend.supported

    @property
    def pypy_enabled(self) -> bool:
        return self.supports("pypy")

    @property
    def min_python(self) -> str:
        """Versión mínima de sintaxis: 3.11 si PyPy está soportado."""
        if self.pypy_enabled:
            m = re.search(r"@(\d+\.\d+)", self.python.pypy)
            return m.group(1) if m else "3.11"
        return self.python.cpython

    def profile_for(self, backend: str | None = None) -> str:
        """Perfil de tipado efectivo para un backend (por defecto, el activo)."""
        b = backend or self.backend.active
        if b == "mypyc":
            return "mypyc"
        return self.typing.relaxed if self.typing.profile == "auto" else self.typing.profile

    def preset_options(self, name: str) -> dict[str, Any]:
        return self.preset.get(name, {})


# --- carga -------------------------------------------------------------------------------


def _default_of(f: dataclasses.Field[Any]) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    raise AssertionError(f.name)


def _type_name(value: Any) -> str:
    return {
        bool: "booleano",
        int: "entero",
        str: "texto",
        list: "lista",
        dict: "tabla",
    }.get(type(value), type(value).__name__)


def _check_type(value: Any, default: Any, where: str) -> None:
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise DeployError(
            f"pytemplate.toml: '{where}' debe ser {_type_name(default)}, no {_type_name(value)}"
        )
    if isinstance(default, list) and all(isinstance(x, str) for x in default):
        wants_str = bool(default) or where.endswith(
            ("supported", "modules", "exclude", "forbid_imports", "targets", "args", "deps", "cmd", "imports")
        )
        if wants_str and not all(isinstance(x, str) for x in value):
            raise DeployError(f"pytemplate.toml: '{where}' debe ser una lista de textos")


def _build(cls: type[Any], data: Any, where: str) -> Any:
    if not isinstance(data, dict):
        raise DeployError(f"pytemplate.toml: '{where}' debe ser una tabla")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{where}.{key}" if where else key
        f = fields.get(key)
        if f is None:
            valid = ", ".join(sorted(fields))
            raise DeployError(f"pytemplate.toml: clave desconocida '{path}' (válidas: {valid})")
        default = _default_of(f)
        if dataclasses.is_dataclass(default):
            kwargs[key] = _build(type(default), value, path)
        elif key == "tasks" and cls is Config:
            kwargs[key] = {
                name: _build(TaskConfig, spec, f"tasks.{name}") for name, spec in _table(value, path).items()
            }
        else:
            _check_type(value, default, path)
            kwargs[key] = value
    return cls(**kwargs)


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeployError(f"pytemplate.toml: '{where}' debe ser una tabla")
    return value


def _one_of(value: str, allowed: tuple[str, ...], where: str) -> None:
    if value not in allowed:
        raise DeployError(f"pytemplate.toml: '{where}' = {value!r} no es válido ({' | '.join(allowed)})")


def validate(cfg: Config, builtin_commands: set[str] | None = None) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", cfg.app.name):
        raise DeployError("pytemplate.toml: 'app.name' solo admite letras, números, '-' y '_'")
    for b in cfg.backend.supported:
        _one_of(b, BACKENDS, "backend.supported")
    if not cfg.backend.supported:
        raise DeployError("pytemplate.toml: 'backend.supported' no puede estar vacío")
    _one_of(cfg.backend.active, BACKENDS, "backend.active")
    if cfg.backend.active not in cfg.backend.supported:
        raise DeployError(
            f"pytemplate.toml: backend.active = {cfg.backend.active!r} no está en backend.supported "
            f"{cfg.backend.supported}. Usa: ./deploy mode {cfg.backend.active} --supports +{cfg.backend.active}"
        )
    if not re.fullmatch(r"\d+\.\d+", cfg.python.cpython):
        raise DeployError("pytemplate.toml: 'python.cpython' debe ser una versión menor, p. ej. \"3.14\"")
    if not re.fullmatch(r"pypy@\d+\.\d+\.\d+", cfg.python.pypy):
        raise DeployError(
            "pytemplate.toml: 'python.pypy' debe ser exacto, p. ej. \"pypy@3.11.15\" "
            "(una versión suelta puede resolverse a PyPy 8.0, cuyo ABI pp80 aún no tiene wheels)"
        )
    _one_of(cfg.typing.profile, ("auto", *PROFILES), "typing.profile")
    _one_of(cfg.typing.relaxed, ("off", "warn", "strict"), "typing.relaxed")
    _one_of(cfg.typing.editor, EDITORS, "typing.editor")
    if cfg.backend.active == "mypyc" and cfg.typing.profile in ("warn", "off"):
        raise DeployError(
            "pytemplate.toml: con backend mypyc el tipado no puede ser 'warn' ni 'off' "
            "(mypyc aborta con cualquier error de mypy). Usa typing.profile = \"auto\"."
        )
    for m in [*cfg.compile.modules, *cfg.compile.exclude]:
        if not _DOTTED.match(m):
            raise DeployError(f"pytemplate.toml: módulo no válido en [compile]: {m!r}")
    for o in cfg.typing.mypy_overrides:
        if "module" not in o:
            raise DeployError("pytemplate.toml: cada [[typing.mypy_overrides]] necesita 'module'")
        if "strict" in o:
            raise DeployError(
                "pytemplate.toml: no pongas 'strict' en typing.mypy_overrides "
                "(mypy lo aplicaría a TODOS los módulos); usa opciones concretas"
            )
    _one_of(cfg.compile.opt_level, ("0", "1", "2", "3"), "compile.opt_level")
    if cfg.deploy.optimize not in (0, 1, 2):
        raise DeployError("pytemplate.toml: 'deploy.optimize' debe ser 0, 1 o 2")
    for backend, method in cfg.deploy.default.items():
        _one_of(backend, BACKENDS, "deploy.default")
        _one_of(method, METHODS, f"deploy.default.{backend}")
    _one_of(cfg.deploy.exe.mode, ("onefile", "onedir"), "deploy.exe.mode")
    _one_of(cfg.deploy.exe.console, ("auto", "yes", "no"), "deploy.exe.console")
    _one_of(cfg.deploy.portable.runtime, ("bundled", "system"), "deploy.portable.runtime")
    _one_of(cfg.deploy.nuitka.mode, ("standalone", "onefile"), "deploy.nuitka.mode")
    for name, task in cfg.tasks.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise DeployError(f"pytemplate.toml: nombre de tarea no válido: {name!r}")
        if builtin_commands and name in builtin_commands:
            raise DeployError(f"pytemplate.toml: la tarea '{name}' choca con el comando interno ./deploy {name}")
        if not task.cmd and not task.deps:
            raise DeployError(f"pytemplate.toml: la tarea '{name}' necesita 'cmd' o 'deps'")
        if task.backend:
            _one_of(task.backend, BACKENDS, f"tasks.{name}.backend")


def load(builtin_commands: set[str] | None = None) -> Config:
    if not CONFIG_FILE.is_file():
        raise DeployError(f"no existe {CONFIG_FILE.name} en la raíz del proyecto")
    text = CONFIG_FILE.read_text(encoding="utf-8-sig")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"pytemplate.toml no es TOML válido: {e}") from None
    cfg: Config = _build(Config, data, "")
    validate(cfg, builtin_commands)
    return cfg


def compiled_paths(cfg: Config) -> list[str]:
    """Rutas (relativas a src/) de los módulos/paquetes de compile.modules."""
    out: list[str] = []
    for m in cfg.compile.modules:
        base = m.replace(".", "/")
        out.append(base if (SRC / base).is_dir() else base + ".py")
    return out


# --- edición de pytemplate.toml conservando comentarios ----------------------------------------


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(value)


def set_value(text: str, table: str, key: str, value: Any) -> str:
    """Cambia `key = ...` dentro de `[table]` (o lo añade) conservando el comentario de la línea."""
    lines = text.splitlines(keepends=True)
    header = re.compile(r"^\s*\[\s*" + re.escape(table) + r"\s*\]\s*(#.*)?$")
    any_header = re.compile(r"^\s*\[")
    key_line = re.compile(r"^(\s*" + re.escape(key) + r"\s*=\s*)(.*?)(\s+#.*)?(\r?\n)?$")
    start = next((i for i, ln in enumerate(lines) if header.match(ln)), None)
    rendered = toml_value(value)
    if start is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{sep}\n[{table}]\n{key} = {rendered}\n"
    end = next((i for i in range(start + 1, len(lines)) if any_header.match(lines[i])), len(lines))
    for i in range(start + 1, end):
        m = key_line.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{rendered}{m.group(3) or ''}{m.group(4) or ''}"
            return "".join(lines)
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"{key} = {rendered}\n")
    return "".join(lines)


def update_file(changes: list[tuple[str, str, Any]]) -> None:
    text = CONFIG_FILE.read_text(encoding="utf-8-sig")
    for table, key, value in changes:
        text = set_value(text, table, key, value)
    tomllib.loads(text)  # nunca dejamos un archivo roto
    CONFIG_FILE.write_text(text, encoding="utf-8", newline="\n")
