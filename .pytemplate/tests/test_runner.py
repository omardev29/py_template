"""Tests del runner de ./deploy (se ejecutan con `./deploy selftest`)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, imports, lintc, render  # noqa: E402
from runner.config import Config, set_value  # noqa: E402
from runner.methods.common import parse_key  # noqa: E402
from runner.ui import DeployError  # noqa: E402


def make(data: dict[str, object]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


# --- config ------------------------------------------------------------------------------------


def test_defaults_are_valid() -> None:
    cfg = make({})
    assert cfg.backend.active == "cpython"
    assert cfg.profile_for("mypyc") == "mypyc"
    assert cfg.profile_for("cpython") == "off"
    assert cfg.min_python == "3.14"


def test_unknown_key_is_an_error() -> None:
    with pytest.raises(DeployError, match="clave desconocida 'backend.activo'"):
        make({"backend": {"activo": "pypy"}})


def test_wrong_type_is_an_error() -> None:
    with pytest.raises(DeployError, match="booleano"):
        make({"python": {"jit": "yes"}})


def test_active_must_be_supported() -> None:
    with pytest.raises(DeployError, match="no está en backend.supported"):
        make({"backend": {"active": "pypy", "supported": ["cpython"]}})


def test_pypy_must_be_exact() -> None:
    with pytest.raises(DeployError, match="exacto"):
        make({"python": {"pypy": "pypy@3.11"}})


def test_mypyc_forbids_relaxed_typing() -> None:
    with pytest.raises(DeployError, match="mypyc"):
        make({"backend": {"active": "mypyc"}, "typing": {"profile": "off"}})


def test_pypy_lowers_min_python() -> None:
    cfg = make({"backend": {"supported": ["cpython", "pypy"]}})
    assert cfg.min_python == "3.11"


def test_task_cannot_shadow_builtin() -> None:
    with pytest.raises(DeployError, match="choca"):
        cfg: Config = config._build(Config, {"tasks": {"run": {"cmd": ["x"]}}}, "")
        config.validate(cfg, {"run"})


def test_set_value_keeps_comments() -> None:
    text = '[backend]\nactive = "cpython"   # el modo\nsupported = ["cpython"]\n\n[python]\njit = false\n'
    out = set_value(text, "backend", "active", "mypyc")
    assert 'active = "mypyc"   # el modo' in out
    out = set_value(out, "python", "jit", True)
    assert "jit = true" in out
    out = set_value(out, "typing", "relaxed", "warn")
    assert tomllib.loads(out)["typing"]["relaxed"] == "warn"


# --- render ------------------------------------------------------------------------------------


def test_managed_block_bounds_cpython_minor() -> None:
    block = render.managed_block(make({}))
    assert "python_full_version < '3.15'" in block
    assert "pypy" not in block
    assert block.rstrip().endswith("# <<< pytemplate")


def test_managed_block_with_pypy() -> None:
    block = render.managed_block(make({"backend": {"supported": ["cpython", "pypy"]}}))
    assert "implementation_name == 'pypy' and python_full_version >= '3.11'" in block
    assert "override-dependencies" in block


def test_pyproject_rewrite_is_idempotent() -> None:
    cfg = make({})
    text = '[project]\nname = "x"\nrequires-python = ">=3.11"\n\n[tool.uv]\nfoo = 1\n'
    once = render.pyproject_expected(cfg, text)
    assert 'requires-python = ">=3.14"' in once
    assert render.pyproject_expected(cfg, once) == once
    tomllib.loads(once)


def test_toml_serializer_roundtrip() -> None:
    data = {"a": 1, "b": ["x", "y"], "lint": {"select": ["E"], "per-file-ignores": {"tests/**": ["ANN"]}}}
    assert tomllib.loads(render.to_toml(data)) == data


# --- imports / lint de mypyc ----------------------------------------------------------------------


def test_imports_skip_type_checking(tmp_path: Path) -> None:
    pkg = tmp_path / "app" / "core"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "util.py").write_text("")
    mod = pkg / "m.py"
    mod.write_text(
        "from typing import TYPE_CHECKING\nimport json\nfrom . import util\n"
        "if TYPE_CHECKING:\n    import raylib\n"
    )
    found = imports.imports_of(mod, "app.core.m", tmp_path)
    assert {"json", "app.core", "app.core.util"} <= found
    assert "raylib" not in found


def test_lintc_rules(tmp_path: Path) -> None:
    mod = tmp_path / "m.py"
    mod.write_text(
        "import flet as ft\nfrom functools import cache\nX = __file__\n"
        "@cache\nclass A:\n    class B: ...\n"
        "if __name__ == '__main__':\n    pass\n"
    )
    cfg = make({"compile": {"forbid_imports": ["flet"]}})
    messages = [f.message for f in lintc.lint_file(cfg, mod, {})]
    assert any("flet" in m for m in messages)
    assert any("@cache" in m for m in messages)
    assert any("anidada" in m for m in messages)
    assert any("__file__" in m for m in messages)
    assert any("__main__" in m for m in messages)


def test_platform_keys() -> None:
    t = parse_key("cp314-linux-x86_64")
    assert (t.impl, t.version, t.os, t.arch) == ("cp", "3.14", "linux", "x86_64")
    with pytest.raises(DeployError):
        parse_key("cp314-plan9-x86_64")
