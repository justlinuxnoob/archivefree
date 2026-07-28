#!/usr/bin/env bash
# Run ArchiveFree straight from the source tree, without installing it.
#
# On distributions that ship the GTK 4 and libadwaita libraries but not their
# GObject-introspection typelibs, drop the typelibs into ./.devtypelib and this
# script will pick them up.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -d "$here/.devtypelib" ]]; then
    export GI_TYPELIB_PATH="$here/.devtypelib:${GI_TYPELIB_PATH:-}"
fi

export PYTHONPATH="$here:${PYTHONPATH:-}"
export ARCHIVEFREE_DATA_DIR="$here/data"
exec python3 -m archivefree "$@"
