#!/bin/sh
# ./pyt: POSIX launcher of the pytemplate runner.
# pytemplate-launcher: `pyt install` copies this file into uv's tool bin folder and `pyt uninstall` removes it.
#
# Finds the project root and uv, then hands every argument to
# .pytemplate/pyt.py, where all the logic lives. Outside any project it runs
# the copy of the template that `pyt install` made, in its global mode
# (PYTEMPLATE_GLOBAL=1). It must keep working under
# dash, bash 3.2+, zsh, busybox ash and ksh on Linux, macOS and WSL, and under
# Git Bash, MSYS2 (any MSYSTEM, login or not), Cygwin, busybox-w32 and niubash
# on Windows. CLAUDE.md ("Launchers") explains each rule:
#   * POSIX sh only: no arrays, [[ ]], ${v//a/b}, local, $'..', set -e/-u.
#     printf '%s\n', never echo, for anything that may hold a backslash.
#   * A caller's set -e / set -u must not stop it (sh -eu pyt, niubash with
#     errexit): ${v:-} for variables that may be unset, and every command that
#     may fail sits in a condition or ends in `|| :` / `|| _pt_x=`.
#   * niubash runs this file inside the calling shell: $0 is the caller's,
#     variables, functions and cd leak into it and its aliases expand here.
#     So: $BASH_SOURCE before $0, every name starts with _pt_, no cd, and
#     every name is unset before the one exit/exec at the end (errors too).
#   * niubash ignores `exit` inside `a || b`, `a && b` and `{ ..; }`: exit
#     only at top level or inside if/case bodies (`return` is fine anywhere).
#   * niubash rejects a comment on a `name() {` line: comment above it.
#   * Never "..$(cmd "$x").." (niubash keeps the inner quotes): assign first.
#   * Windows shells can have a minimal PATH (MSYS2 login shells, Cygwin with
#     CYGWIN_NOWINPATH): uv is also searched in the usual install folders and,
#     as a last resort, in the user and machine PATH stored in the registry.
#   * Windows means OS=Windows_NT without WSL_DISTRO_NAME; never trust uname,
#     cygpath or OSTYPE output formats (niubash fakes them). Backslashes are
#     separators only there: on POSIX they are part of a file name.

_pt_self=
if [ -n "${ZSH_VERSION:-}" ]; then
    # zsh: the path of this file even when sourced; eval keeps other shells
    # from parsing ${(%)..}. Then behave like sh (word splitting, globbing).
    eval '_pt_self=${(%):-%x}'
    emulate sh
fi

# Windows: OS=Windows_NT without WSL_DISTRO_NAME (uname only when OS is unset).
_pt_win=
case ${OS:-} in
    Windows_NT)
        if [ -z "${WSL_DISTRO_NAME:-}" ]; then
            _pt_win=1
        fi ;;
    *)
        _pt_t=$(uname -s 2>/dev/null) || _pt_t=
        case $_pt_t in
            CYGWIN* | MINGW* | MSYS* | *_NT* | Windows*) _pt_win=1 ;;
        esac ;;
esac

# --- path helpers (result in $_pt_r) -----------------------------------------

# C:\a\b -> C:/a/b on Windows. Elsewhere unchanged: a backslash is part of a
# POSIX file name.
_pt_slashes() {
    _pt_r=$1
    if [ -z "$_pt_win" ]; then
        return 0
    fi
    _pt_r=
    _pt_s=$1
    while :; do
        case $_pt_s in
            *\\*)
                _pt_r=$_pt_r${_pt_s%%\\*}/
                _pt_s=${_pt_s#*\\} ;;
            *)
                _pt_r=$_pt_r$_pt_s
                return 0 ;;
        esac
    done
}

# C:/a/b -> C:\a\b
_pt_backslashes() {
    _pt_r=
    _pt_s=$1
    while :; do
        case $_pt_s in
            */*)
                _pt_r=$_pt_r${_pt_s%%/*}\\
                _pt_s=${_pt_s#*/} ;;
            *)
                _pt_r=$_pt_r$_pt_s
                return 0 ;;
        esac
    done
}

# c -> C (the drive letter of a /c/... path); anything else is kept.
_pt_drive() {
    _pt_r=$1
    case $1 in
        [abcdefghijklmnopqrstuvwxyz])
            _pt_r=aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsStTuUvVwWxXyYzZ
            _pt_r=${_pt_r#*"$1"}
            _pt_r=${_pt_r%"${_pt_r#?}"} ;;
    esac
}

# Absolute path as this shell sees it (/c/x, /cygdrive/c/x, C:/x, /home/x)
# -> Windows form (C:\x).
_pt_winpath() {
    _pt_slashes "$1"
    _pt_p=$_pt_r
    case $_pt_p in
        /cygdrive/[A-Za-z] | /cygdrive/[A-Za-z]/*)
            _pt_p=${_pt_p#/cygdrive} ;;
    esac
    case $_pt_p in
        /[A-Za-z] | /[A-Za-z]/*)
            _pt_t=${_pt_p#/?}
            _pt_d=${_pt_p#/}
            _pt_d=${_pt_d%"$_pt_t"}
            _pt_drive "$_pt_d"
            _pt_p=$_pt_r:${_pt_t:-/} ;;
        //*) ;;
        /*)
            # Inside the MSYS2/Cygwin root: only cygpath knows the mounts. The
            # root's own first: a non-login shell may have no /usr/bin on PATH,
            # or WinuxCmd's cygpath (niubash, xonsh-shell-kit) there.
            _pt_t=
            if [ -x /usr/bin/cygpath ]; then
                _pt_t=$(/usr/bin/cygpath -m -- "$_pt_p" 2>/dev/null) || _pt_t=
            fi
            case $_pt_t in
                [A-Za-z]:/*) ;;
                *)
                    _pt_t=
                    if command -v cygpath >/dev/null 2>&1; then
                        _pt_t=$(cygpath -m -- "$_pt_p" 2>/dev/null) || _pt_t=
                    fi ;;
            esac
            case $_pt_t in
                [A-Za-z]:/*) _pt_p=$_pt_t ;;
                *)
                    # Keep the POSIX path: MSYS converts it for uv.exe itself,
                    # while \home\x would name a folder on the current drive.
                    _pt_r=$_pt_p
                    return 0 ;;
            esac ;;
    esac
    _pt_backslashes "$_pt_p"
}

# --- project root ---------------------------------------------------------------

# $1 = a folder. Sets _pt_entry to the runner it holds: .pytemplate/pyt.py, else
# .pytemplate/deploy.py (a project made before the launchers were renamed).
_pt_entry_in() {
    if [ -f "${1%/}/.pytemplate/pyt.py" ]; then
        _pt_entry=pyt.py
        return 0
    fi
    if [ -f "${1%/}/.pytemplate/deploy.py" ]; then
        _pt_entry=deploy.py
        return 0
    fi
    return 1
}

# $1 = this file as the shell named it. Sets _pt_root when its directory
# holds a runner (_pt_entry_in). A symlink (~/bin/mypyt -> proj/pyt) is
# followed to the launcher it names, at most 40 links; a relative target is
# joined to its link's folder, and the kernel resolves the '..' in it. A name
# that is no file here (the shell itself, dash for a file sourced under
# dash -c) names no folder of this file.
_pt_from_launcher() {
    _pt_slashes "$1"
    _pt_link=$_pt_r
    _pt_n=0
    while [ -h "$_pt_link" ] && [ "$_pt_n" -lt 40 ]; do
        _pt_n=$((_pt_n + 1))
        case $_pt_link in
            -*) _pt_link=./$_pt_link ;;
        esac
        _pt_t=$(readlink "$_pt_link" 2>/dev/null) || _pt_t=
        _pt_slashes "$_pt_t"
        case $_pt_r in
            '') _pt_n=40 ;;
            /* | [A-Za-z]:/*) _pt_link=$_pt_r ;;
            *)
                case $_pt_link in
                    */*) _pt_link=${_pt_link%/*}/$_pt_r ;;
                    *) _pt_link=$_pt_r ;;
                esac ;;
        esac
    done
    if [ ! -f "$_pt_link" ]; then
        return 1
    fi
    case $_pt_link in
        */*) _pt_c=${_pt_link%/*} ;;
        *) _pt_c=. ;;
    esac
    case $_pt_c in
        '') _pt_c=/ ;;
        [A-Za-z]:) _pt_c=$_pt_c/ ;;
    esac
    if _pt_entry_in "$_pt_c"; then
        _pt_root=$_pt_c
        return 0
    fi
    return 1
}

# $1 = a folder holding .pytemplate/$_pt_entry, found by walking up from $PWD
# (or the installed template). Its code is not run when another user owns it:
# anyone may create /tmp/.pytemplate/pyt.py (on Windows, whose owners are not
# read here: a drive root, where any user may create folders).
_pt_foreign() {
    if [ -n "$_pt_win" ]; then
        case $1 in
            / | [A-Za-z]: | [A-Za-z]:/ | /[A-Za-z] | /cygdrive/[A-Za-z]) return 0 ;;
        esac
        return 1
    fi
    if [ -O "${1%/}/.pytemplate/$_pt_entry" ]; then
        return 1
    fi
    return 0
}

# The copy of the template that `pyt install` made (the runner's
# cmd_install.snapshot_dir): %LOCALAPPDATA%\pytemplate\template on Windows,
# else $XDG_DATA_HOME/pytemplate/template (an absolute XDG_DATA_HOME only) or
# ~/.local/share/pytemplate/template (an absolute HOME only). Sets _pt_r ('' when
# nothing names it).
_pt_installed() {
    _pt_r=
    if [ -n "$_pt_win" ]; then
        _pt_slashes "${LOCALAPPDATA:-}"
        if [ -z "$_pt_r" ] && [ -n "${USERPROFILE:-}" ]; then
            _pt_slashes "$USERPROFILE/AppData/Local"
        fi
        if [ -n "$_pt_r" ]; then
            _pt_r=${_pt_r%/}/pytemplate/template
        fi
        return 0
    fi
    case ${XDG_DATA_HOME:-} in
        /*) _pt_r=${XDG_DATA_HOME%/}/pytemplate/template ;;
        *)
            case ${HOME:-} in
                /*) _pt_r=${HOME%/}/.local/share/pytemplate/template ;;
            esac ;;
    esac
    return 0
}

_pt_pwd=${PWD:-}
if [ -z "$_pt_pwd" ]; then
    _pt_pwd=$(pwd 2>/dev/null) || _pt_pwd=
fi
_pt_slashes "$_pt_pwd"
_pt_pwd=$_pt_r
_pt_root=
_pt_other=
_pt_entry=pyt.py
_pt_global=

# The shell names this file: zsh in $_pt_self, bash and niubash in
# $BASH_SOURCE, the others in $0. Only the first name it gives counts: in a run
# inside the calling shell (niubash, a sourced file) $0 is the caller's (niu,
# or a script of another project), and its folder is not this file's. When
# that folder holds no runner, walk up from $PWD.
_pt_t=$_pt_self
if [ -z "$_pt_t" ] && [ -n "${BASH_SOURCE:-}" ]; then
    _pt_t=$BASH_SOURCE
fi
if [ -z "$_pt_t" ]; then
    _pt_t=$0
fi
if _pt_from_launcher "$_pt_t"; then
    :
else
    _pt_d=$_pt_pwd
    while :; do
        if _pt_entry_in "$_pt_d"; then
            if _pt_foreign "$_pt_d"; then
                _pt_other=$_pt_d
            else
                _pt_root=$_pt_d
            fi
            break
        fi
        _pt_n=${_pt_d%/*}
        case $_pt_n in
            '') _pt_n=/ ;;
            [A-Za-z]:) _pt_n=$_pt_n/ ;;
        esac
        if [ "$_pt_n" = "$_pt_d" ]; then
            break
        fi
        _pt_d=$_pt_n
    done
    # No project: the installed template, in its global mode (pyt new...),
    # under the same ownership rule as a folder found by walking up.
    if [ -z "$_pt_root" ] && [ -z "$_pt_other" ]; then
        _pt_installed
        if [ -n "$_pt_r" ] && [ -f "$_pt_r/.pytemplate/pyt.py" ]; then
            _pt_entry=pyt.py
            if _pt_foreign "$_pt_r"; then
                _pt_other=$_pt_r
            else
                _pt_root=$_pt_r
                _pt_global=1
            fi
        fi
    fi
fi

case $_pt_root in
    '' | /* | [A-Za-z]:/*) ;;
    *)
        # Relative (./pyt, ../pyt, pyt): resolve against $PWD.
        _pt_d=${_pt_pwd%/}
        _pt_c=${_pt_root#./}
        while :; do
            case $_pt_c in
                . | '') _pt_c= ; break ;;
                ..) _pt_d=${_pt_d%/*} ; _pt_c= ; break ;;
                ../*) _pt_d=${_pt_d%/*} ; _pt_c=${_pt_c#../} ;;
                ./*) _pt_c=${_pt_c#./} ;;
                *) break ;;
            esac
        done
        _pt_t=$_pt_d${_pt_c:+/$_pt_c}
        case $_pt_t in
            '') _pt_t=/ ;;
            [A-Za-z]:) _pt_t=$_pt_t/ ;;
        esac
        # $PWD is logical: below a symlink its '..' is not the folder the
        # kernel found this file in. Then keep the relative path, which uv
        # resolves the way the kernel did (the launcher never changes folder).
        if [ -f "${_pt_t%/}/.pytemplate/$_pt_entry" ]; then
            _pt_root=$_pt_t
        fi ;;
esac

# --- uv --------------------------------------------------------------------------

_pt_exe=uv
if [ -n "$_pt_win" ]; then
    _pt_exe=uv.exe
fi

# $1 = candidate uv executable
_pt_try_uv() {
    if [ -n "$1" ] && [ -f "$1" ] && [ -x "$1" ]; then
        _pt_uv=$1
        return 0
    fi
    return 1
}

# $1 = base dir (may be empty, may be C:\..), $2 = suffix
_pt_try_dir() {
    if [ -z "$1" ]; then
        return 1
    fi
    _pt_slashes "$1$2"
    _pt_try_uv "${_pt_r%/}/$_pt_exe"
}

# The usual install folders, uv's own installer order first.
_pt_find_uv_dirs() {
    _pt_h=${HOME:-}
    if [ -n "$_pt_win" ] && [ -n "${USERPROFILE:-}" ]; then
        _pt_slashes "$USERPROFILE"
        _pt_h=$_pt_r
    fi
    _pt_try_dir "${UV_INSTALL_DIR:-}" "" && return 0
    _pt_try_dir "${UV_INSTALL_DIR:-}" /bin && return 0
    _pt_try_dir "${XDG_BIN_HOME:-}" "" && return 0
    _pt_try_dir "${XDG_DATA_HOME:-}" /../bin && return 0
    _pt_try_dir "$_pt_h" /.local/bin && return 0
    _pt_try_dir "${CARGO_HOME:-}" /bin && return 0
    _pt_try_dir "$_pt_h" /.cargo/bin && return 0
    if [ -n "$_pt_win" ]; then
        _pt_slashes "${LOCALAPPDATA:-}"
        _pt_l=${_pt_r:-${_pt_h:+$_pt_h/AppData/Local}}
        _pt_slashes "${ProgramFiles:-${PROGRAMFILES:-}}"
        _pt_f=$_pt_r
        _pt_slashes "${ProgramData:-${PROGRAMDATA:-}}"
        _pt_a=$_pt_r
        _pt_try_dir "$_pt_l" /Microsoft/WinGet/Links && return 0
        if [ -n "$_pt_l" ]; then
            for _pt_g in "$_pt_l"/Microsoft/WinGet/Packages/astral-sh.uv_*; do
                _pt_try_dir "$_pt_g" "" && return 0
            done
        fi
        _pt_try_dir "$_pt_f" /WinGet/Links && return 0
        if [ -n "$_pt_f" ]; then
            for _pt_g in "$_pt_f"/WinGet/Packages/astral-sh.uv_*; do
                _pt_try_dir "$_pt_g" "" && return 0
            done
        fi
        _pt_try_dir "${SCOOP:-}" /shims && return 0
        _pt_try_dir "$_pt_h" /scoop/shims && return 0
        _pt_try_dir "${SCOOP_GLOBAL:-}" /shims && return 0
        _pt_try_dir "$_pt_a" /scoop/shims && return 0
        _pt_try_dir "${ChocolateyInstall:-${CHOCOLATEYINSTALL:-}}" /bin && return 0
        _pt_try_dir "$_pt_a" /chocolatey/bin && return 0
    else
        _pt_try_dir /opt/homebrew /bin && return 0
        _pt_try_dir /usr/local /bin && return 0
        _pt_try_dir /home/linuxbrew/.linuxbrew /bin && return 0
        _pt_try_dir "$_pt_h" /.nix-profile/bin && return 0
    fi
    return 1
}

# %NAME% -> its value (exact name, then upper case: MSYS2/Cygwin upper-case
# SYSTEMROOT, PROGRAMFILES...). Fails on an unknown name.
_pt_expand() {
    _pt_r=
    _pt_s=$1
    while :; do
        case $_pt_s in
            *%*%*) ;;
            *)
                _pt_r=$_pt_r$_pt_s
                return 0 ;;
        esac
        _pt_r=$_pt_r${_pt_s%%\%*}
        _pt_s=${_pt_s#*%}
        _pt_n=${_pt_s%%\%*}
        _pt_s=${_pt_s#*%}
        _pt_v=
        case $_pt_n in
            '' | [0-9]* | *[!A-Za-z0-9_]*) ;;
            *)
                eval "_pt_v=\${$_pt_n-}"
                if [ -z "$_pt_v" ] && command -v tr >/dev/null 2>&1; then
                    _pt_n=$(printf '%s' "$_pt_n" | tr '[:lower:]' '[:upper:]') || _pt_n=
                    case $_pt_n in
                        '' | [0-9]* | *[!A-Za-z0-9_]*) ;;
                        *) eval "_pt_v=\${$_pt_n-}" ;;
                    esac
                fi ;;
        esac
        if [ -z "$_pt_v" ]; then
            return 1
        fi
        _pt_r=$_pt_r$_pt_v
    done
}

# $1 = a Windows PATH value (C:\a;%USERPROFILE%\b;"C:\c d";...)
_pt_uv_in_list() {
    _pt_rest=$1
    while [ -n "$_pt_rest" ]; do
        _pt_e=${_pt_rest%%\;*}
        case $_pt_rest in
            *\;*) _pt_rest=${_pt_rest#*\;} ;;
            *) _pt_rest= ;;
        esac
        # Some installers write quoted entries ("C:\Program Files\x").
        _pt_e=${_pt_e#\"}
        _pt_e=${_pt_e%\"}
        if _pt_expand "$_pt_e"; then
            _pt_try_dir "$_pt_r" "" && return 0
        fi
    done
    return 1
}

# The user and machine PATH from the registry. Slow (two reg.exe runs): only
# when everything else failed. No /v switch: MSYS2 rewrites "/v" into "V:/".
_pt_uv_from_registry() {
    _pt_cr=$(printf '\r') || _pt_cr=
    for _pt_k in 'HKCU\Environment' 'HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'; do
        _pt_o=$(reg.exe query "$_pt_k" 2>/dev/null) || _pt_o=
        while IFS= read -r _pt_l; do
            _pt_l=${_pt_l%"$_pt_cr"}
            _pt_v=
            # "    Path    REG_EXPAND_SZ    C:\a;%USERPROFILE%\b" (4-space separators)
            case $_pt_l in
                '    '[Pp][Aa][Tt][Hh]'    REG_SZ    '*)
                    _pt_v=${_pt_l#*'    REG_SZ    '} ;;
                '    '[Pp][Aa][Tt][Hh]'    REG_EXPAND_SZ    '*)
                    _pt_v=${_pt_l#*'    REG_EXPAND_SZ    '} ;;
            esac
            if [ -n "$_pt_v" ]; then
                _pt_uv_in_list "$_pt_v" && return 0
            fi
        done <<EOF
$_pt_o
EOF
    done
    return 1
}

# Errors only set _pt_rc: the one exit at the end runs after the cleanup.
_pt_rc=
_pt_uv=
if [ -n "$_pt_other" ]; then
    printf '%s\n' "pyt: ${_pt_other%/}/.pytemplate/$_pt_entry is not yours (another user owns it, or it is at a drive root): not run. If you trust it, run ${_pt_other%/}/${_pt_entry%.py} yourself." >&2
    _pt_rc=2
elif [ -z "$_pt_root" ]; then
    printf '%s\n' "pyt: no .pytemplate/pyt.py next to this launcher, in $_pt_pwd or in any parent directory." \
        "To run pyt outside a project, install it: ./pyt install in a clone of the template (https://github.com/omardev29/py_template)." >&2
    _pt_rc=2
else
    if [ -n "${UV:-}" ]; then
        # uv exports its own path to everything `uv run` starts.
        _pt_slashes "$UV"
        _pt_try_uv "$_pt_r" || :
    fi
    if [ -z "$_pt_uv" ]; then
        _pt_t=$(command -v uv 2>/dev/null) || _pt_t=
        case $_pt_t in
            */* | *\\*)
                _pt_slashes "$_pt_t"
                _pt_try_uv "$_pt_r" || : ;;
        esac
    fi
    if [ -z "$_pt_uv" ]; then
        _pt_find_uv_dirs || :
    fi
    if [ -z "$_pt_uv" ] && [ -n "$_pt_win" ]; then
        _pt_uv_from_registry || :
    fi

    if [ -z "$_pt_uv" ]; then
        printf '%s\n' "pyt: uv not found (https://docs.astral.sh/uv/getting-started/installation/)." >&2
        if [ -t 0 ] && [ -t 2 ] && [ -z "${CI:-}" ]; then
            printf '%s' "Install it now with the official installer? [y/N] " >&2
            _pt_t=
            read -r _pt_t || :
            case $_pt_t in
                y | Y | yes | Yes | YES)
                    if [ -n "$_pt_win" ]; then
                        _pt_t=powershell.exe
                        if ! command -v powershell.exe >/dev/null 2>&1; then
                            _pt_slashes "${SYSTEMROOT:-${SystemRoot:-C:/Windows}}"
                            _pt_t=$_pt_r/System32/WindowsPowerShell/v1.0/powershell.exe
                        fi
                        MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "$_pt_t" -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' || :
                    elif command -v curl >/dev/null 2>&1; then
                        curl -LsSf https://astral.sh/uv/install.sh | sh || :
                    else
                        wget -qO- https://astral.sh/uv/install.sh | sh || :
                    fi
                    _pt_find_uv_dirs || : ;;
            esac
        fi
    fi
    if [ -z "$_pt_uv" ]; then
        if [ -n "$_pt_win" ]; then
            printf '%s\n' \
                "Install it with one of these, then open a new terminal:" \
                "  powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\"" \
                "  winget install --id=astral-sh.uv -e" \
                "  scoop install main/uv" >&2
        else
            printf '%s\n' \
                "Install it with one of these, then open a new terminal:" \
                "  curl -LsSf https://astral.sh/uv/install.sh | sh" \
                "  brew install uv" \
                "  pipx install uv" >&2
        fi
        _pt_rc=127
    fi
fi

# --- hand over -------------------------------------------------------------------

if [ -n "$_pt_rc" ]; then
    set -- "$_pt_rc"
else
    _pt_launcher='sh'
    if [ -n "${__RUBASH_SHELL_NAME:-}" ]; then
        _pt_launcher=sh:niubash
    elif [ -n "${ZSH_VERSION:-}" ]; then
        _pt_launcher=sh:zsh
    elif [ -n "${BASH_VERSION:-}" ]; then
        _pt_launcher=sh:bash
    fi
    if [ -f /usr/bin/msys-2.0.dll ]; then
        _pt_launcher=$_pt_launcher:msys
    elif [ -f /usr/bin/cygwin1.dll ] || [ -f /bin/cygwin1.dll ]; then
        _pt_launcher=$_pt_launcher:cygwin
    fi

    _pt_script=${_pt_root%/}/.pytemplate/$_pt_entry
    _pt_cwd=$_pt_pwd
    # The Python the runner starts on. While the project has an environment, the one uv reads
    # from .python-version (python.cpython, which that environment was made with), as always:
    # --python= with no value is no request, and only-managed keeps a system Python out (uv makes
    # its cached environment of the runner again when one built it). Else any CPython 3.11 or
    # newer that uv finds, a system one too (uv has none to download on Android or the BSDs); the
    # runner moves the commands that need python.cpython onto it itself. On Linux/macOS the
    # environment's python is a link to its base Python, which must exist too (-f follows it).
    _pt_py='>=3.11'
    _pt_pref=managed
    if [ -n "$_pt_win" ]; then
        if [ -f "${_pt_root%/}/.venv/Scripts/python.exe" ]; then
            _pt_py=
            _pt_pref=only-managed
        fi
    elif [ -f "${_pt_root%/}/.venv-wsl/bin/python" ] || [ -f "${_pt_root%/}/.venv/bin/python" ]; then
        _pt_py=
        _pt_pref=only-managed
    fi
    if [ -n "$_pt_win" ]; then
        _pt_winpath "$_pt_script"
        _pt_script=$_pt_r
        _pt_winpath "$_pt_cwd"
        _pt_cwd=$_pt_r
    fi
    PYTEMPLATE_CALLER_CWD=$_pt_cwd
    PYTEMPLATE_LAUNCHER=$_pt_launcher
    export PYTEMPLATE_CALLER_CWD PYTEMPLATE_LAUNCHER
    # The installed template runs in its global mode; a project's runner never does.
    if [ -n "$_pt_global" ]; then
        PYTEMPLATE_GLOBAL=1
        export PYTEMPLATE_GLOBAL
    else
        unset PYTEMPLATE_GLOBAL
    fi
    set -- "$_pt_uv" run --quiet "--python=$_pt_py" --python-preference "$_pt_pref" --script "$_pt_script" "$@"
fi

unset -f _pt_slashes _pt_backslashes _pt_drive _pt_winpath _pt_entry_in _pt_from_launcher \
    _pt_foreign _pt_installed _pt_try_uv _pt_try_dir _pt_find_uv_dirs _pt_expand _pt_uv_in_list \
    _pt_uv_from_registry
unset _pt_self _pt_r _pt_s _pt_p _pt_t _pt_d _pt_c _pt_n _pt_link _pt_pwd _pt_root _pt_other _pt_win \
    _pt_entry _pt_global _pt_exe _pt_uv _pt_h _pt_l _pt_f _pt_a _pt_g _pt_v _pt_rest _pt_e _pt_cr _pt_k \
    _pt_o _pt_launcher _pt_script _pt_cwd _pt_py _pt_pref _pt_rc
if [ "$#" -eq 1 ]; then
    # An error above (no project, no uv): $1 is its exit code.
    exit "$1"
fi
# The runner starts on the Python chosen above, in the caller's folder: a
# UV_PYTHON of the caller must not choose another one, a UV_MANAGED_PYTHON or
# UV_NO_MANAGED_PYTHON must not stop uv (it refuses them next to
# --python-preference), a PYTHONHOME or PYTHONPATH must not break it, a
# UV_WORKING_DIR must not move it (the runner's own tools never get them either).
if [ -n "${__RUBASH_SHELL_NAME:-}" ]; then
    # niubash runs this file inside the calling shell and `exec` only ends the
    # file: run uv, then drop the PYTEMPLATE_ exports so the session keeps no
    # stale copy. The session keeps its own values: uv reads an empty UV_PYTHON
    # as unset and false as an unset flag, Python an empty PYTHONHOME or
    # PYTHONPATH, and uv refuses an empty UV_WORKING_DIR (. is the caller's
    # folder) or flag.
    if UV_PYTHON='' UV_MANAGED_PYTHON=false UV_NO_MANAGED_PYTHON=false PYTHONHOME='' PYTHONPATH='' UV_WORKING_DIR=. "$@"; then
        set -- 0
    else
        set -- "$?"
    fi
    unset PYTEMPLATE_CALLER_CWD PYTEMPLATE_LAUNCHER PYTEMPLATE_GLOBAL
    exit "$1"
fi
unset UV_PYTHON UV_MANAGED_PYTHON UV_NO_MANAGED_PYTHON PYTHONHOME PYTHONPATH UV_WORKING_DIR
exec "$@"
