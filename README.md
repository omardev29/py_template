# miapp — plantilla uv + mypyc + PyInstaller

Código Python con tipado estático, compilado a C con **mypyc** y empaquetado con
**PyInstaller** en un ejecutable que no necesita Python instalado.

Probada con Python 3.14.7 (gestionado por uv 0.12), mypy/mypyc 2.3.1 y PyInstaller 6.22.3.

## Estructura

```
miapp/
├── .vscode/
│   ├── settings.json     # Pylance (extraPaths=src) + extensión de mypy
│   ├── extensions.json   # extensiones recomendadas
│   └── launch.json       # F5 = depurar src/main.py
├── src/
│   ├── main.py           # lanzador de 2 líneas. NO se compila
│   └── app.py            # toda la lógica. SE COMPILA con mypyc
├── build.py              # mypy --strict -> mypyc -> PyInstaller
├── pyproject.toml        # dependencias + config de mypy
├── uv.lock               # versiones exactas (no se edita a mano)
└── .python-version       # versión de Python congelada
```

`main.py` no se compila porque tiene que ejecutarse como `__main__`: un módulo
compilado por mypyc siempre se importa, y en él `__name__` nunca vale `"__main__"`.

## Uso

```bash
uv sync --locked      # crea .venv idéntico al lock (hazlo antes de abrir VS Code)
uv run src/main.py    # desarrollo: modo interpretado
uv run mypy           # chequeo estático (strict, config en pyproject.toml)
uv run build.py       # -> dist/miapp        un solo archivo
uv run build.py --onedir  # -> dist/miapp/  carpeta, arranca ~200 ms antes
```

Para compilar hace falta un compilador C en tu máquina (gcc/clang en Linux, Xcode
Command Line Tools en macOS, MSVC Build Tools en Windows). El usuario final no
necesita nada. `build.py` hay que ejecutarlo en cada sistema operativo destino.

## Quitar el ejemplo (criba, Collatz y la dependencia `rich`)

`src/app.py` trae un benchmark de ejemplo que usa `rich`. Para empezar un proyecto real:

**1. Quitar la dependencia** (actualiza `pyproject.toml`, `uv.lock` y `.venv`):

```bash
uv remove rich
```

**2. Dejar `src/app.py` con un esqueleto mínimo.**

Linux / macOS / Git Bash:

```bash
cat > src/app.py <<'EOF'
def main() -> int:
    print("Hola desde miapp")
    return 0
EOF
```

Windows PowerShell:

```powershell
Set-Content -Path src/app.py -Encoding utf8 -Value @'
def main() -> int:
    print("Hola desde miapp")
    return 0
'@
```

**3. Comprobar que todo sigue en pie:**

```bash
uv run mypy
uv run src/main.py
uv run build.py
```

`src/main.py` y `build.py` no se tocan: el lanzador solo llama a `app.main()`, y
`build.py` calcula solo qué módulos declarar a PyInstaller leyendo tus imports.

Para añadir tu propia dependencia: `uv add nombre-del-paquete`.

## Añadir más módulos compilados

Crea el módulo en `src/` (p. ej. `src/motor.py`), impórtalo con `from motor import ...`
y añádelo a `COMPILE` en `build.py`:

```python
COMPILE = ["app.py", "motor.py"]
```

Con 2 o más módulos mypyc genera además una librería `<hash>__mypyc`. PyInstaller no
puede ver imports dentro de un binario compilado, así que `build.py` declara con
`--hidden-import` tus módulos, esa librería y todo lo que importan.

El nombre del ejecutable se cambia en `NAME` dentro de `build.py`.

## VS Code

Al abrir la carpeta, acepta instalar las extensiones recomendadas. VS Code elige solo
el `.venv` del proyecto; si no, `Ctrl+Shift+P` → **Python: Select Interpreter** → `.venv`.

Qué hace `.vscode/settings.json`:

| Ajuste | Para qué |
|---|---|
| `python.analysis.extraPaths: ["src"]` | Pylance resuelve `from app import main` en `src/main.py`. |
| `python.analysis.exclude` | Ignora `build/` y `dist/`, donde hay copias de `src/` durante la compilación. |
| `python.analysis.typeCheckingMode: "standard"` | Pylance marca errores reales; el juez estricto es mypy. |
| `mypy-type-checker.importStrategy: "fromEnvironment"` | La extensión usa el mypy del `.venv` (el mismo que usa mypyc), no el suyo. |

Cosas a tener en cuenta:

- **mypy es la referencia**, porque es lo que usa mypyc. Si algo compila pero VS Code
  lo marca, mira qué dice la extensión de mypy: sus avisos son los que importan.
- Si añades `[tool.pyright]` a `pyproject.toml` o un `pyrightconfig.json`, Pylance
  **ignora** todos los `python.analysis.*` de `settings.json`. Mueve ahí esos ajustes.
- `"strict"` en Pylance también pasa limpio con este código, pero con librerías mal
  tipadas suele marcar avisos que mypy no da.
- F5 depura `src/main.py` en modo interpretado: los breakpoints no funcionan en el
  binario compilado.
- Si añades tests con pytest, pon `pythonpath = ["src"]` en
  `[tool.pytest.ini_options]` para que encuentren tus módulos.
