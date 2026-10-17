#!/bin/sh
# pytemplate-installer: installs the `pyt` command in one line, without keeping a clone:
#
#   curl -LsSf https://raw.githubusercontent.com/omardev29/py_template/main/install.sh | sh
#
# It clones the template (its newest commit only) into a temporary folder, runs the clone's
# `./pyt install` (the template is copied into your data folder and the launchers into uv's tool
# bin folder: README, "Install pyt"), then deletes the clone. Run it again to update pyt; `pyt
# uninstall` removes it. Needs git and uv. PYT_REPO and PYT_REF pick another repository or
# branch (a fork, a tag). Windows: install.ps1.

_pt_say() {
  printf '%s\n' "pyt installer: $*" >&2
}

_pt_main() {
  _pt_repo=${PYT_REPO:-https://github.com/omardev29/py_template.git}
  _pt_ref=${PYT_REF:-main}
  if ! command -v git >/dev/null 2>&1; then
    _pt_say "git is needed: install it (your package manager, or https://git-scm.com/downloads), then run this again"
    return 3
  fi
  _pt_tmp=$(mktemp -d 2>/dev/null || mktemp -d -t pyt-install) || {
    _pt_say "cannot make a temporary folder"
    return 1
  }
  trap 'rm -rf "$_pt_tmp"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  _pt_say "cloning $_pt_repo ($_pt_ref)"
  if ! git clone --quiet --depth 1 --branch "$_pt_ref" -- "$_pt_repo" "$_pt_tmp/py_template" </dev/null; then
    _pt_say "git could not clone $_pt_repo ($_pt_ref)"
    return 1
  fi
  # The install record names where pyt came from: the clone is gone once this ends
  PYTEMPLATE_INSTALL_SOURCE="$_pt_repo ($_pt_ref)" sh "$_pt_tmp/py_template/pyt" install </dev/null
  _pt_rc=$?
  if [ "$_pt_rc" -eq 127 ]; then
    _pt_say "uv is needed (how to install it is above): install it, then run this again"
  fi
  return "$_pt_rc"
}

# The whole script is read before anything runs (curl | sh hands it over a pipe)
_pt_main
exit $?
