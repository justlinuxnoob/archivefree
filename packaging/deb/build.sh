#!/usr/bin/env bash
#
# Build a .deb for ArchiveFree.
#
# Deliberately built with plain dpkg-deb rather than debhelper: the package is
# pure Python and architecture-independent, so there is nothing to compile, and
# this way the only build dependency is dpkg itself. That keeps CI fast and lets
# anyone reproduce the package on a machine with no dev tooling installed.
#
# Usage:  packaging/deb/build.sh [output-directory]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
outdir="${1:-$root/dist}"

version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/archivefree/_version.py")"
appid="io.github.justlinuxnoob.ArchiveFree"
pkgname="archivefree"

if [[ -z "$version" ]]; then
    echo "error: could not read the version from archivefree/_version.py" >&2
    exit 1
fi

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

echo "Building $pkgname $version"

# --- payload ---------------------------------------------------------------
install -d "$staging/usr/share/$pkgname"
cp -r "$root/archivefree" "$staging/usr/share/$pkgname/"
find "$staging/usr/share/$pkgname" -name '__pycache__' -type d -exec rm -rf {} +
find "$staging/usr/share/$pkgname" -name '*.py[co]' -delete

install -d "$staging/usr/bin"
cat > "$staging/usr/bin/$pkgname" <<'LAUNCHER'
#!/bin/sh
# ArchiveFree launcher.
#
# -m is required, not a path: archivefree/__main__.py uses relative imports,
# which only resolve when Python loads it as part of the package.
# -P keeps the current directory off sys.path.
PYTHONPATH="/usr/share/archivefree${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -P -m archivefree "$@"
LAUNCHER
chmod 755 "$staging/usr/bin/$pkgname"

# --- desktop integration ---------------------------------------------------
install -d "$staging/usr/share/applications"
install -m 644 "$root/data/$appid.desktop" "$staging/usr/share/applications/"

install -d "$staging/usr/share/metainfo"
install -m 644 "$root/data/$appid.metainfo.xml" "$staging/usr/share/metainfo/"

install -d "$staging/usr/share/icons/hicolor/scalable/apps"
install -m 644 "$root/data/icons/hicolor/scalable/apps/$appid.svg" \
    "$staging/usr/share/icons/hicolor/scalable/apps/"

# Right-click menus for the file managers that read declarative files.
install -d "$staging/usr/share/nemo/actions"
install -m 644 "$root"/data/integration/*.nemo_action "$staging/usr/share/nemo/actions/"

install -d "$staging/usr/share/kio/servicemenus"
install -m 755 "$root/data/integration/archivefree-servicemenu.desktop" \
    "$staging/usr/share/kio/servicemenus/archivefree.desktop"

install -d "$staging/usr/share/doc/$pkgname"
install -m 644 "$root/README.md" "$staging/usr/share/doc/$pkgname/"
gzip -9n "$staging/usr/share/doc/$pkgname/README.md"
install -m 644 "$root/LICENSE" "$staging/usr/share/doc/$pkgname/copyright"

# --- control ---------------------------------------------------------------
installed_size="$(du -ks "$staging" | cut -f1)"

install -d "$staging/DEBIAN"
cat > "$staging/DEBIAN/control" <<CONTROL
Package: $pkgname
Version: $version
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.11),
         python3-gi (>= 3.42),
         gir1.2-gtk-4.0 (>= 4.10),
         gir1.2-adw-1 (>= 1.4),
         gir1.2-glib-2.0
Recommends: 7zip | p7zip-full
Suggests: unrar, zstd, lz4
Maintainer: The ArchiveFree contributors <noreply@github.com>
Installed-Size: $installed_size
Homepage: https://github.com/justlinuxnoob/archivefree
Description: Look inside archives before unpacking them
 ArchiveFree opens zip, 7z, rar and tar files in a window so you can see what
 is inside before anything is written to disk. Browse the folders, check file
 sizes, preview a document, then extract everything or just what you need.
 .
 It never silently overwrites your files, handles password-protected and
 multi-part archives, and keeps the window responsive while it works.
 .
 No adverts, no telemetry, no accounts.
CONTROL

cat > "$staging/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true
    fi
fi
exit 0
POSTINST
chmod 755 "$staging/DEBIAN/postinst"

cat > "$staging/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true
    fi
fi
exit 0
POSTRM
chmod 755 "$staging/DEBIAN/postrm"

# Ownership must be root:root inside the package even though we build as a
# normal user; --root-owner-group handles that without fakeroot.
mkdir -p "$outdir"
deb="$outdir/${pkgname}_${version}_all.deb"
dpkg-deb --root-owner-group --build "$staging" "$deb" >/dev/null

echo "Built $deb"
if command -v lintian >/dev/null 2>&1; then
    lintian --no-tag-display-limit "$deb" || true
fi
