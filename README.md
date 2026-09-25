# miapp: plantilla uv multi-backend (CPython · PyPy · mypyc)

Una sola plantilla para scripts, juegos con **raylib** y apps con **Flet**, con tres formas
de ejecutar el mismo código y seis de distribuirlo. Todo se maneja con `./deploy`, un runner
estilo Justfile que solo necesita [uv](https://docs.astral.sh/uv/) (si falta, se ofrece a instalarlo).

| Backend | Qué es | Tipos | Ideal para |
|---|---|---|---|
| `cpython` | El intérprete de siempre | Opcionales | Scripts cortos, librerías de la C-API (numpy, pillow...) |
| `pypy` | Compilador JIT | Opcionales | Python puro de larga duración y librerías **cffi** (raylib) |
| `mypyc` | Compila tu núcleo a C (AOT) | **Obligatorios, sin `Any`** | Lógica numérica/CPU; distribuir un binario rápido |

Medido en esta máquina (Windows 11, CPython 3.14.7, PyPy 7.3.23, mypyc 2.3.1):

| Prueba | CPython | PyPy | mypyc |
|---|---|---|---|
| Criba hasta 5 millones (preset script) | 0,42 s | 0,11 s | 0,08 s |
| Collatz < 300.000 (preset script) | 2,45 s | 0,07 s | 0,13 s |
| Bunnymark 30.000 conejos (preset raylib) | 67 FPS | **217 FPS** | 152 FPS |
| Fractal 640x400 (preset flet) | 2,81 s | n/d | 0,08 s |

## Empezar

```bash
./deploy setup            # intérpretes, entornos, uv.lock y configs de VS Code
./deploy run              # ejecuta con el backend activo (pytemplate.toml)
./deploy run mypyc        # compila el núcleo con mypyc y ejecuta
./deploy test all         # pytest en cada backend (en mypyc, contra los binarios)
./deploy build mypyc      # ejecutable con PyInstaller en dist/
```

Proyecto nuevo a partir de esta plantilla (no hace falta un repo aparte por tipo de proyecto):

```bash
./deploy new ../mi-juego --preset raylib    # o: script, flet
./deploy init flet --name miapp             # o convertir ESTE proyecto a otro preset
```

Requisitos: solo **uv**. Para el backend mypyc hace falta además un compilador de C:
MSVC en Windows (`./deploy doctor` te da el comando `winget` exacto), gcc o clang en
Linux y `xcode-select --install` en macOS. Quien reciba tu programa no necesita nada.

## `./deploy` en cada shell

La lógica está en `.pytemplate/deploy.py` (solo biblioteca estándar, lo ejecuta uv). Los
lanzadores `deploy` (sh), `deploy.cmd` y `deploy.ps1` solo buscan uv y reenvían argumentos,
así que `./deploy build mypyc` se escribe igual en todas partes:

| Shell | Cómo | Notas |
|---|---|---|
| xonsh (Windows) | `./deploy ...` | xonsh usa `deploy.cmd` (solo ejecuta extensiones de PATHEXT) |
| xonsh (Linux/macOS), bash, zsh, Git Bash | `./deploy ...` | Usa `deploy` (`#!/bin/sh`: en Windows, `env bash` podría abrir WSL) |
| PowerShell 7 / 5.1 | `./deploy ...` | Si la política lo bloquea: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, o usa `.\deploy.cmd` |
| cmd | `.\deploy ...` | |

Sin `./` en cualquier proyecto de la plantilla: `./deploy shell-setup xonsh` (o `pwsh`,
`bash`) imprime un alias con autocompletado para pegarlo en `~/.xonshrc`.

## Comandos

| Comando | Qué hace |
|---|---|
| `setup` · `doctor` | Instala entornos · comprueba uv, compilador, PyPy, JIT, shells y configs |
| `mode [BACKEND] [--supports +pypy] [--typing warn] [--jit on] [--editor basedpyright]` | Muestra o cambia el modo |
| `run [BACKEND] [args...]` | Ejecuta la app (los argumentos van a tu app) |
| `check [BACKEND\|all]` · `lint [--fix]` · `fmt` | ruff + mypy con el perfil de tipado del backend + reglas de mypyc |
| `test [BACKEND\|all] [args...]` | pytest; con mypyc comprueba que se cargaron los `.pyd`/`.so` |
| `report [--open]` | Informe HTML de mypyc: líneas lentas en rojo y cómo arreglarlas |
| `build [BACKEND] [--method M]` · `pyz-merge` | Distribución (ver abajo) |
| `add PAQ [--cpython-only]` · `remove` · `lock` · `sync` | Dependencias (uv) |
| `init PRESET` · `new CARPETA` · `render` · `clean` · `tasks` | Plantilla y utilidades |

**Tareas propias** estilo Justfile en `pytemplate.toml` (igual en todos los shells, sin shell de por medio):

```toml
[tasks.gen]
help = "Genera los assets"
cmd = ["python", "scripts/gen.py", "{backend}"]   # corre con `uv run` en el entorno del backend
deps = ["check"]                                   # otras tareas o comandos de ./deploy
env = { SEED = "42" }
```

## Configuración: `pytemplate.toml` y `.pytemplate/`

`pytemplate.toml` es la única fuente de verdad: backend activo, backends soportados,
perfil de tipado, módulos a compilar y opciones de deploy. Cámbialo y ejecuta cualquier
`./deploy`: estos archivos se regeneran solos a partir de las variantes de `.pytemplate/templates/`:

- `.mypy.ini`, `pyrightconfig.json` (Pylance), `.ruff.toml`
- `.vscode/settings.json`, `launch.json`, `tasks.json`, `extensions.json`
- `.python-version`, `.github/workflows/ci.yml`

**No los edites a mano**: `./deploy` detecta la edición y no la pisa (`./deploy render --force`
para forzarlo). Edita `pytemplate.toml`, o las variantes de `.pytemplate/templates/typing/`
para cambiar un perfil. En `pyproject.toml`, `requires-python` y el bloque entre marcas
de `[tool.uv]` también son gestionados; como afectan a `uv.lock`, solo cambian con
`./deploy mode` o `./deploy lock`.

### Tipado según el backend

| Perfil | Cuándo | Qué exige |
|---|---|---|
| `off` | cpython/pypy por defecto | Nada: solo errores reales (sintaxis, nombres no definidos) |
| `warn` | `./deploy mode --typing warn` | Todo como aviso, nunca bloquea |
| `strict` | `./deploy mode --typing strict` | mypy `--strict`, `Any` permitido |
| `mypyc` | siempre con backend mypyc | `--strict` en todo `src/` y **`Any` prohibido en lo compilado** |

Con mypyc, `Any` no es solo feo, es **lento**: mypyc genera operaciones genéricas donde hay
`Any` (una `list[Any]` puede ir más lenta que CPython sin compilar). Por eso el perfil
`mypyc` activa `disallow_any_explicit/expr/decorated/unimported` en los módulos de
`compile.modules`, y el resto de `src/` (la frontera con librerías mal tipadas) sigue
estricto pero puede usar `Any`. Pylance en modo strict no marca un `Any` escrito a
propósito; si lo quieres ver también en el editor: `./deploy mode --editor basedpyright`.

## mypyc: reglas para que el binario vuele

- **Estructura**: `src/<paquete>/core/` se compila; la frontera (`app.py`, `ui/`, `gfx/`) no.
  `src/main.py` es el lanzador y nunca se compila (un módulo compilado no puede ser `__main__`).
- **Constantes con `Final`**: un global sin `Final` se busca en un diccionario en cada acceso.
- **Clases nativas**: atributos `float`/`int` tipados; solo los decoradores `@dataclass`,
  `@final`, `@trait`, `@mypyc_attr` (cualquier otro convierte la clase en una clase Python lenta).
- **Tipos concretos**: `list[bool]` se compila a accesos directos, `bytearray` va por la vía
  genérica (criba: 4,2x frente a 1,9x).
- `./deploy report --open` marca en rojo cada operación genérica ("make it Final", "Generic `*`").
- `./deploy check` añade reglas que mypy no ve: imports prohibidos en lo compilado,
  decoradores que desactivan las clases nativas, `__file__` a nivel de módulo, t-strings...
- Compilado no hay pdb, cProfile ni monkeypatch: depura interpretado (F5 en VS Code).

## PyPy

Actívalo por proyecto con `./deploy mode --supports +pypy` (el preset raylib lo trae):
comprueba que tu código es válido en Python 3.11, baja `requires-python` a `>=3.11` y
crea `.venv-pypy`. Detalles que la plantilla ya resuelve por ti:

- PyPy se fija **exacto** (`pypy@3.11.15`): PyPy 8.0 cambió el ABI de las extensiones y aún
  no hay wheels para él.
- Las herramientas (mypy, ruff, PyInstaller) corren siempre en CPython; el entorno de PyPy
  solo tiene tus dependencias y pytest.
- Librerías de la C-API de CPython (numpy, pillow, pydantic-core) van lentas en PyPy:
  añádelas con `./deploy add numpy --cpython-only`. Las de **cffi** (raylib) van muy bien.
- El JIT necesita calentarse (~1 s): en scripts muy cortos PyPy no gana.

## Distribución: `./deploy build [BACKEND] --method ...`

| Método | cpython | mypyc | pypy | Resultado |
|---|---|---|---|---|
| `exe` | ✓ | ✓ | ✗ | Ejecutable PyInstaller (Flet: `flet pack`, con el cliente Flutter dentro) |
| `portable` | ✓ | ✓ | ✓ | Carpeta con el intérprete dentro + lanzador `.cmd`/`.sh` (la única vía standalone para PyPy) |
| `pyz` | ✓ | ✓ | ✓ | **Un solo archivo** que corre con cualquier CPython o PyPy instalado |
| `wheel` | ✓ | ✓ | ✓ | Paquete para `uv tool install` (con mypyc, wheel de plataforma) |
| `nuitka` | ✓ | ✓ | ✗ | Ejecutable con Nuitka (compila también las dependencias; builds lentos) |
| `flet` | ✓ | ✓ | ✗ | `flet build`: instaladores nativos, Android/iOS/web (preset flet) |

Por defecto: `cpython` y `mypyc` usan `exe` y `pypy` usa `portable` (`[deploy] default`).

**El `.pyz` portable** lleva tu código en `.py` más, por cada plataforma, los binarios
(los `.pyd` de mypyc del sistema donde compilaste y las dependencias nativas). La primera
vez se extrae a una caché y usa lo que encaje con el intérprete que lo ejecuta. El mismo
archivo de 1,7 MB, probado aquí: CPython 3.14 usa el núcleo compilado, PyPy corre el
`.py` con su JIT y CPython 3.13 corre el `.py`. mypyc no compila para otros sistemas: la
CI (`.github/workflows/ci.yml`) compila en Windows, Linux y macOS y `./deploy pyz-merge`
une los tres en un único `.pyz`. Para dependencias nativas de otros SO sin CI:
`[deploy.pyz] targets = ["cp314-linux-x86_64", ...]`.

Otras opciones en `pytemplate.toml`: `deploy.optimize` (bytecode `-O`; con mypyc quita
los `assert`), `deploy.exe.mode` (`onefile`/`onedir`), `app.gui` (sin consola),
`deploy.portable.runtime = "system"` (carpeta sin intérprete, para el Python del destino).

## Presets

### script (por defecto)
App de consola: `src/miapp/core/bench.py` (compilado) y `src/miapp/app.py` (salida con rich).

### raylib
PyPy por defecto (su JIT acelera también las llamadas cffi), con el núcleo compilable con mypyc.

- **Siempre `import raylib as rl`**, nunca pyray en bucles: pyray envuelve cada llamada en
  Python (~700 ns frente a ~100 ns). `compile.forbid_imports` lo impide en lo compilado.
- **Crea colores y structs una vez** (`gfx.color(...)`, cdata). Pasar tuplas como `rl.RED`
  reconvierte en cada llamada: con tuplas, PyPy pierde toda su ventaja.
- **El stub oficial de raylib miente**: 55 funciones dicen devolver `bytes` y devuelven un
  puntero, `Color.r` dice `bytes` y es un `int`. Interpretado no pasa nada; compilado,
  mypyc comprueba el tipo y lanza `TypeError`. El preset trae `typings/raylib`, el stub
  corregido (regenerarlo tras actualizar raylib: `./deploy stubs`).
- `./deploy bunnymark`: mide FPS con 30.000 conejos (`./deploy run mypyc --frames 900 --bunnies 30000`
  para otro backend).

### flet
**No hace falta un repo aparte**: Flet es un preset (dependencias, esqueleto y empaquetado)
de esta misma plantilla. La UI la pinta Flutter y el Python de Flet no se puede compilar,
así que mypyc acelera tu núcleo (`src/<pkg>/core/`) y la UI va siempre interpretada.
Probado con Flet 1.0.1 y mypyc 2.3.1, con Flet en un módulo compilado:

- los handlers `async def` se llaman **sin el evento** (TypeError);
- los handlers generadores no se ejecutan nunca, sin error;
- `@ft.component` falla al importar;
- `@ft.control` pierde los tipos de sus eventos.

Por eso `forbid_imports = ["flet"]` en lo compilado. Patrón: el handler (interpretado)
convierte los valores de Flet a tipos simples y llama al núcleo en otro proceso
(`ProcessPoolExecutor`; el código compilado no suelta el GIL, un hilo congelaría la UI).
Recarga en caliente: `./deploy dev`. `./deploy build` usa `flet pack`, que mete el cliente
Flutter en el ejecutable (con PyInstaller a secas se descargarían 40 MB al arrancar).

## JIT de CPython (experimental)

`./deploy mode --jit on` activa `PYTHON_JIT=1` en `run`, `test` y en los lanzadores de
`portable`/`pyz`. Los CPython que descarga uv para Windows no traen el JIT, así que usa
un python.org 3.14 fijo (`py install 3.14`) en un entorno aparte (`.venv-jit`). En un
ejecutable de PyInstaller es imposible activarlo. Aquí dio 1,6x en Collatz y 1,15x en la
criba; mypyc y PyPy ganan de lejos.

## VS Code

Acepta las extensiones recomendadas (Python, Pylance, mypy, Ruff). F5 depura
`src/main.py` interpretado (con PyPy hay una configuración experimental). Las tareas
(`Ctrl+Shift+B`) llaman a `./deploy`. mypy es el juez de los tipos (es lo que usa mypyc);
Pylance y mypy se configuran solos según el perfil del backend activo.

## Solución de problemas

- **mypyc "Unable to find a compatible Visual Studio installation"**: `./deploy` ya añade
  el instalador de Visual Studio al PATH (el `vcvarsall.bat` de VS 2026 lo necesita).
  Si falla, `./deploy doctor` indica qué falta.
- **Rutas de más de 260 caracteres** (Windows): acorta la ruta del proyecto o activa
  `LongPathsEnabled`. Flet y PyPy tienen archivos con rutas internas largas.
- **`bash` abre WSL**: en Windows usa xonsh, PowerShell o cmd. Si usas `./deploy` desde WSL,
  el runner crea entornos separados (`.venv-wsl`) para no romper los de Windows.
- **Flet se descarga el cliente o instala paquetes solo**: `flet` y `flet-desktop` deben
  tener la misma versión (`[preset.flet] version`).

## Estructura

```
deploy, deploy.cmd, deploy.ps1   lanzadores (toda la lógica en .pytemplate/)
pytemplate.toml                  configuración y modo
pyproject.toml, uv.lock          dependencias (un solo lock para CPython y PyPy)
src/main.py                      lanzador (nunca se compila)
src/miapp/core/                  núcleo compilado por mypyc
src/miapp/*.py                   frontera interpretada
tests/                           pytest (conftest verifica los binarios de mypyc)
.pytemplate/runner/              el runner (Python stdlib)
.pytemplate/templates/           variantes de configuración (perfiles de tipado, VS Code, CI...)
.pytemplate/presets/             esqueletos script, raylib y flet
.build/, dist/                   salidas (ignoradas por git)
```
