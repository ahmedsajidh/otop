#!/bin/sh
# Build otop_<version>_<arch>.deb with plain dpkg-deb -- no debhelper, no
# build system, no network. otop is pure Python, so "building" is really just
# laying the files out in the right places.
#
#   ./packaging/build-deb.sh                # -> otop_1.0.0_all.deb
#   ./packaging/build-deb.sh --arch amd64   # -> otop_1.0.0_amd64.deb
#
# Install with:  sudo apt install ./otop_1.0.0_all.deb

set -eu

ARCH=all
while [ $# -gt 0 ]; do
    case "$1" in
        --arch) ARCH="$2"; shift 2 ;;
        --arch=*) ARCH="${1#--arch=}"; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/otop/__init__.py")
[ -n "$VERSION" ] || { echo "cannot determine version" >&2; exit 1; }

BUILD="$ROOT/packaging/build"
STAGE="$BUILD/otop_${VERSION}_${ARCH}"
DEB="$ROOT/otop_${VERSION}_${ARCH}.deb"

echo "otop $VERSION ($ARCH)"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/lib/python3/dist-packages/otop" \
         "$STAGE/etc/otop" \
         "$STAGE/usr/share/doc/otop" \
         "$STAGE/usr/share/man/man1"

# --- application -----------------------------------------------------------
cp "$ROOT"/src/otop/*.py "$STAGE/usr/lib/python3/dist-packages/otop/"

cat > "$STAGE/usr/bin/otop" <<'LAUNCHER'
#!/usr/bin/python3
# otop -- htop/btop for Odoo
import sys

from otop.main import main

if __name__ == "__main__":
    sys.exit(main())
LAUNCHER
chmod 755 "$STAGE/usr/bin/otop"

# --- configuration (a dpkg conffile: local edits survive upgrades) ----------
cp "$ROOT/config/otop.yaml" "$STAGE/etc/otop/config.yaml"
chmod 644 "$STAGE/etc/otop/config.yaml"
echo "/etc/otop/config.yaml" > "$STAGE/DEBIAN/conffiles"

# --- documentation ---------------------------------------------------------
cp "$ROOT/README.md" "$STAGE/usr/share/doc/otop/README.md"
cp "$ROOT/config/otop.yaml" "$STAGE/usr/share/doc/otop/config.example.yaml"
cp "$HERE/copyright" "$STAGE/usr/share/doc/otop/copyright"
# native package (no debian revision in the version) -> changelog.gz
gzip -9nc "$HERE/changelog.Debian" > "$STAGE/usr/share/doc/otop/changelog.gz"
gzip -9nc "$HERE/otop.1" > "$STAGE/usr/share/man/man1/otop.1.gz"

# --- control ---------------------------------------------------------------
SIZE=$(du -ks "$STAGE" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@SIZE@/$SIZE/" \
    "$HERE/control.in" > "$STAGE/DEBIAN/control"

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr/lib" "$STAGE/usr/share" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/otop"

if dpkg-deb --help 2>&1 | grep -q -- --root-owner-group; then
    dpkg-deb --root-owner-group --build "$STAGE" "$DEB" >/dev/null
else
    fakeroot dpkg-deb --build "$STAGE" "$DEB" >/dev/null
fi

echo "built $DEB"
dpkg-deb --info "$DEB" | sed -n '2,12p'
echo
echo "install with:  sudo apt install $DEB"
