"""The manual (README.md) against the code: checks that fail when the documentation drifts.

The manual is README.md in the template repository and .pytemplate/README.md in a project made
with `./deploy new` (the template's README of that version, next to the runner it describes).
It must name every command with its exact usage, every pytemplate.toml key, the build methods
and backends the runner knows, the pinned versions, and nothing that is internal or removed.
It is also read inside projects, so its links are absolute or in-page. Fast and offline.
"""

from __future__ import annotations

import dataclasses
import re
import sys
import tomllib
import typing
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cli, config, envs, lintc, presets, render, upx  # noqa: E402
from runner.cmd_build import COMPAT  # noqa: E402
from runner.cmd_dev import BASEDPYRIGHT, BASEDPYRIGHT_NODE  # noqa: E402
from runner.cmd_nvim import MIN_LAZYVIM  # noqa: E402
from runner.config import Config, PythonConfig, TaskConfig  # noqa: E402
from runner.editors import vscode  # noqa: E402
from runner.methods import nuitka  # noqa: E402
from runner.project import PRESETS, ROOT, TEMPLATE  # noqa: E402


def _manual_path() -> Path | None:
    if (TEMPLATE / "template-repo").is_file():
        return ROOT / "README.md"
    copy = ROOT / presets.TEMPLATE_DOCS["README.md"]  # a project made with ./deploy new
    return copy if copy.is_file() else None


MANUAL = _manual_path()
pytestmark = pytest.mark.skipif(MANUAL is None, reason="no manual: neither the template's README.md nor .pytemplate/README.md")
FENCE = re.compile(r"^```[^\n]*\n.*?^```[ \t]*$", re.M | re.S)
TOML_FENCE = re.compile(r"^```toml\n(.*?)^```[ \t]*$", re.M | re.S)
SPAN = re.compile(r"`([^`\n]+)`")
TOP_TABLES = {f.name for f in dataclasses.fields(Config)}
# The last part of a span such as `app.py` or `deploy.cmd`: a file name, not a key path
FILE_SUFFIXES = {"py", "pyi", "json", "toml", "lua", "cmd", "ps1", "sh", "pyz", "whl", "md", "yml", "exe", "dll", "so", "pyd"}


def _text() -> str:
    assert MANUAL is not None
    return MANUAL.read_text(encoding="utf-8")


def _prose(text: str) -> str:
    """The text without its fenced code blocks."""
    return FENCE.sub("", text)


def _spans(text: str) -> list[str]:
    """The inline code spans outside fenced blocks, `\\|` (a pipe escaped for a table) unescaped."""
    return [s.replace("\\|", "|") for s in SPAN.findall(_prose(text))]


def _headings(text: str) -> list[tuple[int, str]]:
    return [(len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{1,6})\s+(.*)$", _prose(text), re.M)]


def _section(text: str, title: str) -> str:
    """The body of the heading `title` (up to the next heading of the same or a higher level)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m and m.group(2).strip() == title:
            level = len(m.group(1))
            body: list[str] = []
            for rest in lines[i + 1 :]:
                n = re.match(r"^(#{1,6})\s", rest)
                if n and len(n.group(1)) <= level:
                    break
                body.append(rest)
            return "\n".join(body)
    raise AssertionError(f"the manual has no heading {title!r}")


def _cells(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip())[1:-1]
    return [p.strip().replace("\\|", "|") for p in parts]


def _tables(text: str) -> list[list[list[str]]]:
    """Every markdown table of the text: its rows of cells, header first, separator dropped."""
    out: list[list[list[str]]] = []
    rows: list[list[str]] = []
    for line in _prose(text).split("\n") + [""]:
        if line.lstrip().startswith("|"):
            if not re.fullmatch(r"\s*\|(\s*:?-+:?\s*\|)+\s*", line):
                rows.append(_cells(line))
        elif rows:
            out.append(rows)
            rows = []
    return out


def _table(text: str, *header: str) -> list[list[str]]:
    """The table whose header starts with these cells."""
    for table in _tables(text):
        if table[0][: len(header)] == list(header):
            return table
    raise AssertionError(f"the manual has no table whose header starts with {header}")


def _unquote(cell: str) -> str:
    """A cell holding one code span: its text."""
    m = re.fullmatch(r"`([^`]+)`(?:\s.*)?", cell)
    return m.group(1) if m else cell


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading: the rendered text in lower case, punctuation dropped,
    spaces -> '-' (code spans render as their text)."""
    text = heading.replace("`", "").strip().lower()
    return re.sub(r"[^a-z0-9_\- ]", "", text).replace(" ", "-")


# --- commands ------------------------------------------------------------------------------------


def test_the_manual_is_ascii() -> None:
    bad = [f"{n}: {line}" for n, line in enumerate(_text().split("\n"), 1) if not line.isascii()]
    assert not bad, "non-ASCII lines (write ... and ->):\n" + "\n".join(bad[:20])


def test_the_commands_table_is_the_cli_with_its_usages() -> None:
    table = _table(_text(), "Command (as `./deploy help COMMAND` shows it)")
    shown = [_unquote(row[0]) for row in table[1:]]
    names = [s.split()[0] for s in shown]
    assert len(names) == len(set(names)), f"a command is listed twice: {names}"
    assert set(names) == set(cli.COMMANDS), (
        f"missing: {sorted(set(cli.COMMANDS) - set(names))}, not commands: {sorted(set(names) - set(cli.COMMANDS))}"
    )
    for line in shown:
        name = line.split()[0]
        expected = f"{name} {cli.COMMANDS[name].usage}".strip()
        assert line == expected, f"the usage of {name} differs from `./deploy help {name}`: {line!r} != {expected!r}"


def test_nothing_internal_or_removed_is_documented() -> None:
    text = _text()
    for name in [*cli.INTERNAL, "__probe"]:
        assert name not in text, f"{name} is an internal route: it must not be documented"
    for removed in ("./deploy init", "PYTHON_JIT", ".venv-jit", "--jit", "python.jit", "jit_interpreter", "CPython JIT"):
        assert removed not in text, f"{removed!r}: the feature was removed"
    bad = [s for s in _spans(text) if s == "init" or s.startswith("init ")]
    assert not bad, f"init is not a command (./deploy new DIR --preset P): {bad}"


def test_every_deploy_word_is_a_command_or_a_task() -> None:
    text = _text()
    tasks = {name for block in TOML_FENCE.findall(text) for name in tomllib.loads(block).get("tasks", {})}
    for preset in presets.available():
        data = tomllib.loads((PRESETS / preset / "files" / "pytemplate.toml").read_text(encoding="utf-8-sig"))
        tasks |= set(data.get("tasks", {}))
    words = re.findall(r"(?:\./|\.\./|\.\\)deploy((?:\s+-{1,2}[a-z][\w-]*)*)\s+([A-Za-z_][\w-]*)", text)
    unknown = sorted({w for _, w in words if not w.isupper() and w not in cli.COMMANDS and w not in tasks})
    assert not unknown, f"./deploy WORD that is neither a command nor a task: {unknown}"


def test_the_custom_tasks_section_names_every_key_and_placeholder() -> None:
    section = _section(_text(), "Custom tasks")
    spans = set(_spans(section))
    missing = [f.name for f in dataclasses.fields(TaskConfig) if f.name not in spans]
    missing += [p for p in config.TASK_PLACEHOLDERS if "{" + p + "}" not in spans]
    assert not missing, f"undocumented task keys or placeholders: {missing}"


def test_the_exit_codes_are_listed() -> None:
    section = _section(_text(), "Output, exit codes and environment")
    listed = set(re.findall(r"^- (\d+):", section, re.M))
    assert {"0", "1", "2", "3", "130"} <= listed, f"exit codes listed: {sorted(listed)}"


# --- pytemplate.toml -----------------------------------------------------------------------------


def _option_names(preset: str) -> set[str]:
    return set(presets.load(preset).get("options", {}))


def _key_path_ok(path: list[str]) -> bool:
    """Whether a dotted pytemplate.toml path (`deploy.exe.icon`, `tasks.<name>.cwd`) exists in
    the schema. `<...>` and `*` stand for any name."""

    def wild(part: str) -> bool:
        return part == "*" or part.startswith("<")

    head, rest = path[0], path[1:]
    if head == "tasks":
        return len(rest) < 2 or (len(rest) == 2 and (wild(rest[1]) or rest[1] in {f.name for f in dataclasses.fields(TaskConfig)}))
    if head == "preset":
        if not rest or wild(rest[0]):
            return len(rest) <= 2
        if rest[0] not in presets.available():
            return False
        return len(rest) == 1 or (len(rest) == 2 and (wild(rest[1]) or rest[1] in _option_names(rest[0])))
    node: Any = Config
    for i, part in enumerate(path):
        if wild(part):
            return True
        if not (isinstance(node, type) and dataclasses.is_dataclass(node)):
            # below a table-valued key (deploy.default.cpython): one free name
            return typing.get_origin(node) is dict and i == len(path) - 1
        hints = typing.get_type_hints(node)
        if part not in hints:
            return False
        node = hints[part]
    return True


def _named_key_paths(text: str) -> list[str]:
    """The pytemplate.toml keys and tables the manual names in code spans."""
    found: list[str] = []
    for span in _spans(text):
        span = span.strip()
        table = re.fullmatch(r"\[{1,2}([\w.<>*-]+)\]{1,2}(?:\s+([a-z_]+)\b.*)?", span)
        if table:
            parts = table.group(1).split(".")
            if parts[0] in TOP_TABLES:
                found.append(".".join(parts + ([table.group(2)] if table.group(2) else [])))
            continue
        key = re.fullmatch(r"([a-z_]+(?:\.[\w<>*-]+)+)(?:\s*=.*)?", span)
        if key:
            parts = key.group(1).split(".")
            # pytemplate.toml keys are lower case: `python.languageServer` is a VS Code setting
            lower = all(re.fullmatch(r"[a-z0-9_<>*-]+", part) for part in parts)
            if parts[0] in TOP_TABLES and parts[-1] not in FILE_SUFFIXES and lower:
                found.append(key.group(1))
    return found


def test_every_pytemplate_key_the_manual_names_exists() -> None:
    bad = sorted({p for p in _named_key_paths(_text()) if not _key_path_ok(p.split("."))})
    assert not bad, f"the manual names pytemplate.toml keys that the schema does not have: {bad}"


def test_the_toml_examples_are_valid_pytemplate_toml() -> None:
    for block in TOML_FENCE.findall(_text()):
        config._build(Config, tomllib.loads(block), "")  # DeployError on an unknown key or a wrong type


def _leaf_paths() -> list[str]:
    """Every key of pytemplate.toml: the dataclass leaves, a task's table, each preset's options."""
    out: list[str] = []

    def walk(cls: type[Any], prefix: str) -> None:
        for name, hint in typing.get_type_hints(cls).items():
            path = f"{prefix}{name}"
            if isinstance(hint, type) and dataclasses.is_dataclass(hint):
                walk(hint, path + ".")
            elif path == "tasks":
                out.append("tasks.<name>")
            elif path == "preset":
                out.extend(f"preset.{p}.{o}" for p in presets.available() for o in sorted(_option_names(p)))
            else:
                out.append(path)

    walk(Config, "")
    return out


def test_every_pytemplate_key_is_in_the_reference() -> None:
    spans = _spans(_text())
    missing = [p for p in _leaf_paths() if not any(s == p or s.startswith(p + " ") for s in spans)]
    assert not missing, f"pytemplate.toml keys missing from the manual (reference table): {missing}"


def test_the_generated_files_are_listed() -> None:
    spans = set(_spans(_text()))
    generated = [*render.outputs(config.load(set(cli.COMMANDS))), ".pytemplate/state.json"]
    missing = [path for path in generated if path not in spans]
    assert not missing, f"generated files the manual does not list: {missing}"


# --- backends, profiles, build methods -----------------------------------------------------------


def test_the_backends_table_is_the_runners() -> None:
    table = _table(_text(), "Backend")
    assert [_unquote(row[0]) for row in table[1:]] == list(config.BACKENDS)


def test_the_typing_profiles_table_is_the_runners() -> None:
    table = _table(_text(), "Profile")
    assert {_unquote(row[0]) for row in table[1:]} == set(config.PROFILES)


def test_the_build_methods_table_follows_compat() -> None:
    table = _table(_text(), "Method", "cpython")
    header = table[0]
    backends = header[1:-1]
    assert set(backends) == set(config.BACKENDS), f"backend columns: {backends}"
    rows = {_unquote(row[0]): row for row in table[1:]}
    assert list(rows) == list(config.METHODS), f"methods: {list(rows)} != {list(config.METHODS)}"
    for method, row in rows.items():
        for backend, cell in zip(backends, row[1:-1], strict=True):
            expected = "no" if COMPAT[method].get(backend) else "yes"
            assert cell == expected, f"{method} + {backend}: the table says {cell!r}, cmd_build.COMPAT says {expected!r}"


def test_the_native_class_decorators_are_the_ones_lintc_accepts() -> None:
    text = _text()
    labels = {f"attr.{n.rsplit('.', 1)[1]}" if n.startswith("attr.") else n.rsplit(".", 1)[1] for n in lintc.NATIVE_CLASS_DECORATORS}
    missing = sorted(label for label in labels if f"@{label}`" not in text)
    assert not missing, f"native class decorators the manual does not name: {missing}"


# --- pinned versions -----------------------------------------------------------------------------


def test_pinned_versions_match_the_runner() -> None:
    text = _text()
    for pin in (BASEDPYRIGHT, BASEDPYRIGHT_NODE, nuitka.NUITKA):
        name = pin.split("==")[0]
        assert pin in text, f"the manual does not name the pin {pin}"
        others = set(re.findall(rf"\b{re.escape(name)}==([0-9][\w.]*)", text)) - {pin.split("==")[1]}
        assert not others, f"{name}: the manual names other versions {others} than the runner's {pin}"
    assert f"UPX {upx.VERSION}" in text
    assert set(re.findall(r"\bUPX (\d+\.\d+\.\d+)", text)) == {upx.VERSION}
    assert f"{envs.MIN_UV} or newer" in text and f">={envs.MIN_UV}" in text
    assert f"Neovim {'.'.join(map(str, MIN_LAZYVIM))} or newer" in text
    assert f"up to {nuitka.NUITKA_PYTHON}" in text
    assert f'`"{PythonConfig.cpython}"`' in text and PythonConfig.pypy in text


def test_the_runner_gives_the_manuals_flutter_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The runner said ~1 GB (the flet method's docstring, the e2e SKIP reason) where the manual
    # measured about 3 GB in ~/flutter
    from runner import e2e
    from runner.methods import flet

    sizes = set(re.findall(r"about (\d+) GB", _text()))
    assert len(sizes) == 1, f"the manual gives several Flutter sizes: {sizes}"
    size = f"~{sizes.pop()} GB"
    monkeypatch.setattr(e2e.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(e2e.Path, "home", lambda: tmp_path)  # no ~/flutter
    assert size in e2e.flet_build_reason("linux")
    assert flet.__doc__ is not None and size in flet.__doc__
    assert not re.search(r"~\d+ GB", (flet.__doc__ or "").replace(size, "")), "another size in the flet method's docstring"


# --- links and style -----------------------------------------------------------------------------


def _link_targets(text: str) -> list[str]:
    prose = SPAN.sub("", _prose(text))
    targets = re.findall(r"\]\(\s*<?([^)\s>]+)", prose)
    targets += re.findall(r"^\s*\[[^\]]+\]:\s*(\S+)", prose, re.M)  # reference definitions
    targets += re.findall(r"(?:href|src)\s*=\s*[\"']([^\"']+)", prose, re.I)
    return targets


def test_links_are_absolute_or_in_page() -> None:
    # Inside a project this page is .pytemplate/README.md: a relative link would point elsewhere
    bad = [t for t in _link_targets(_text()) if not re.match(r"(https?://|mailto:|#)", t)]
    assert not bad, f"relative links (use https://github.com/omardev29/py_template/blob/main/... or #anchor): {bad}"


def test_in_page_links_resolve() -> None:
    text = _text()
    anchors: set[str] = set()
    for _level, title in _headings(text):
        base = slug(title)
        n, anchor = 0, base
        while anchor in anchors:
            n += 1
            anchor = f"{base}-{n}"
        anchors.add(anchor)
    broken = sorted({t[1:] for t in _link_targets(text) if t.startswith("#") and t[1:] not in anchors})
    assert not broken, f"links to headings that do not exist: {broken}"


def test_code_spans_stay_on_one_line() -> None:
    # A command split across two lines cannot be copied from the raw file (.pytemplate/README.md
    # in an editor), and the span checks above only see spans on one line
    broken = [line for line in _prose(_text()).split("\n") if line.count("`") % 2]
    assert not broken, "code spans broken across lines:\n" + "\n".join(broken[:20])


def test_headings_are_statements() -> None:
    text = _text()
    questions = [title for _level, title in _headings(text) if title.rstrip().endswith("?")]
    questions += re.findall(r"\*\*([^*\n]+\?)\*\*", _prose(text))
    assert not questions, f"headings written as questions: {questions}"


@pytest.mark.parametrize("phrase", ["no need for", "win by far", "make the binary fly", "this machine", "tested here", "whoever receives"])
def test_no_marketing_phrases(phrase: str) -> None:
    assert phrase not in _text().lower()


def test_the_template_workflows_are_named() -> None:
    # A new template-*.yml lands with a line in "Testing the template" (projects have none)
    workflows = sorted(p.name for p in (ROOT / ".github" / "workflows").glob("template-*.yml"))
    missing = [name for name in workflows if f"`{name}`" not in _text()]
    assert not missing, f"template workflows the manual does not name: {missing}"


def test_the_manual_says_what_a_new_project_gets() -> None:
    text = _text()
    for target in presets.TEMPLATE_DOCS.values():
        assert f"`{target}`" in text, f"the manual does not say that a project keeps {target}"


# --- editors -------------------------------------------------------------------------------------


def _full_config() -> Config:
    """The template's defaults with every backend supported (every editor item is generated)."""
    cfg: Config = config._build(Config, {"backend": {"supported": list(config.BACKENDS)}}, "")
    return cfg


def test_the_debug_configurations_are_the_generated_ones() -> None:
    table = _table(_text(), "Configuration")
    documented = [_unquote(row[0]) for row in table[1:]]
    generated = [c["name"] for c in vscode.launch(_full_config())["configurations"]]
    assert documented == generated


def test_the_vscode_tasks_table_names_every_catalog_command() -> None:
    table = _table(_text(), "Task")
    words = {w for row in table[1:] for span in re.findall(r"`([^`]+)`", row[0]) for w in span.split()[:1]}
    commands = {entry.args[0] for entry in vscode.catalog(_full_config())} - set(_full_config().tasks)
    assert commands <= words, f"VS Code tasks the manual does not list: {sorted(commands - words)}"


def _lua_keys() -> set[str]:
    lua = (TEMPLATE / "nvim" / "lua" / "pytemplate" / "tasks.lua").read_text(encoding="utf-8")
    body = lua.split("M.KEYS = {", 1)[1].split("\n}\n", 1)[0]
    return set(re.findall(r'^\s*\{ "([^"]+)",', body, re.M))


def test_the_keymaps_are_the_plugins() -> None:
    keys = _lua_keys()
    assert keys, "tasks.lua has no M.KEYS table"
    section = _section(_text(), "Neovim (LazyVim)")
    documented = set(re.findall(r"`<leader>j([^`]+)`", section))
    assert documented == keys, f"README keymaps differ from tasks.KEYS: {sorted(documented ^ keys)}"
    plugin_readme = TEMPLATE / "nvim" / "README.md"
    if plugin_readme.is_file():
        keymaps = _section(plugin_readme.read_text(encoding="utf-8"), 'Keymaps (`<leader>j`, which-key group "deploy")')
        listed = {s for row in _tables(keymaps)[0][1:] for cell in row[::2] for s in re.findall(r"`([^`]+)`", cell)}
        assert listed == keys, f".pytemplate/nvim/README.md keymaps differ from tasks.KEYS: {sorted(listed ^ keys)}"


# --- the quality bar -----------------------------------------------------------------------------


def test_the_quality_bar_matches_claude_md() -> None:
    claude = ROOT / "CLAUDE.md"
    if not claude.is_file():
        pytest.skip("no CLAUDE.md")
    rules = " ".join(claude.read_text(encoding="utf-8").split())  # line breaks are layout
    section = " ".join(_section(_text(), "Quality bar").split())
    facts = [f"per {n}" for n in re.findall(r"\bper (\d[\d,]*)", section)]
    facts += re.findall(r"\b\d{1,3}(?:,\d{3})+ lines", section)
    facts += re.findall(r"\b20\d\d-\d\d-\d\d\b", section)
    facts += re.findall(r"\bcommit ([0-9a-f]{7,40})\b", section)
    facts += re.findall(r"\b(ACCEPTABLE|TOLERABLE|UNACCEPTABLE|UNRELIABLE)\b", section)
    assert len(facts) >= 10, f"the Quality bar section lost its numbers: {facts}"
    missing = [f for f in facts if f not in rules]
    assert not missing, f"the Quality bar says what CLAUDE.md (rule 1.10, section 13.4) does not: {missing}"


def test_slug_follows_github() -> None:
    assert slug("After editing `pytemplate.toml`") == "after-editing-pytemplatetoml"
    assert slug("Neovim (LazyVim)") == "neovim-lazyvim"
    assert slug("Fast integers: `i64`") == "fast-integers-i64"
    assert slug("Output, exit codes and environment") == "output-exit-codes-and-environment"
    assert slug("py_template") == "py_template"
