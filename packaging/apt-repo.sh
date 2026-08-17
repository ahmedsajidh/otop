#!/bin/sh
# Build a signed APT repository out of otop .deb files, so that servers can do
#
#     sudo apt install otop
#
# The output is a plain directory of static files -- publish it anywhere that
# serves HTTP (GitHub Pages, nginx, S3...). By default it is written to docs/,
# which is what GitHub Pages serves when the repository is configured with
# "Deploy from a branch: main /docs".
#
#   ./packaging/apt-repo.sh                       # sign with your existing key
#   ./packaging/apt-repo.sh --generate-key        # create a signing key first
#   ./packaging/apt-repo.sh --unsigned            # no GPG (see the warning)
#   ./packaging/apt-repo.sh --url https://example.com/otop --output /srv/apt
#
# Requirements: dpkg-dev (dpkg-scanpackages), gzip, coreutils, gpg (unless
# --unsigned). No reprepro, aptly or apt-utils needed.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")

OUTPUT="$ROOT/docs"
DIST=stable
COMPONENT=main
ARCHES="all amd64 arm64"
URL=""
KEY=""
SIGN=yes
GENERATE=no
KEYRING_NAME=otop-archive-keyring.gpg
DEBS=""

usage() { sed -n '2,20p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --output|-o) OUTPUT="$2"; shift 2 ;;
        --dist) DIST="$2"; shift 2 ;;
        --component) COMPONENT="$2"; shift 2 ;;
        --url) URL="$2"; shift 2 ;;
        --sign) KEY="$2"; SIGN=yes; shift 2 ;;
        --unsigned) SIGN=no; shift ;;
        --generate-key) GENERATE=yes; shift ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option: $1" >&2; usage 2 ;;
        *) DEBS="$DEBS $1"; shift ;;
    esac
done

command -v dpkg-scanpackages >/dev/null 2>&1 || {
    echo "dpkg-scanpackages not found -- apt install dpkg-dev" >&2; exit 1; }

# Default URL from the git remote, so the printed instructions are usable as-is.
if [ -z "$URL" ]; then
    REMOTE=$(git -C "$ROOT" remote get-url origin 2>/dev/null || true)
    case "$REMOTE" in
        *github.com[:/]*)
            SLUG=$(echo "$REMOTE" | sed -e 's|.*github.com[:/]||' -e 's|\.git$||')
            USER=${SLUG%%/*}; REPO=${SLUG##*/}
            URL="https://$USER.github.io/$REPO"
            ;;
        *) URL="https://example.invalid/otop" ;;
    esac
fi

# Packages to publish: everything given on the command line, else every .deb
# next to the project root.
if [ -z "$DEBS" ]; then
    DEBS=$(ls "$ROOT"/*.deb 2>/dev/null || true)
fi
[ -n "$DEBS" ] || { echo "no .deb files -- run packaging/build-deb.sh first" >&2; exit 1; }

# --- signing key -----------------------------------------------------------
if [ "$SIGN" = yes ]; then
    command -v gpg >/dev/null 2>&1 || {
        echo "gpg not found -- apt install gnupg, or use --unsigned" >&2; exit 1; }
    if [ "$GENERATE" = yes ] && [ -z "$KEY" ]; then
        echo "generating a repository signing key (no passphrase, for automation)"
        gpg --batch --quiet --gen-key <<EOF
%no-protection
Key-Type: eddsa
Key-Curve: ed25519
Key-Usage: sign
Name-Real: otop repository signing key
Name-Email: otop@localhost
Expire-Date: 0
%commit
EOF
    fi
    if [ -z "$KEY" ]; then
        KEY=$(gpg --list-secret-keys --with-colons 2>/dev/null \
              | awk -F: '/^sec:/ {print $5; exit}')
    fi
    if [ -z "$KEY" ]; then
        cat >&2 <<'EOF'
No GPG secret key found. Either:
  * create one for this repository:  ./packaging/apt-repo.sh --generate-key
  * use an existing key:             ./packaging/apt-repo.sh --sign <KEYID>
  * or publish without signatures:   ./packaging/apt-repo.sh --unsigned
EOF
        exit 1
    fi
fi

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/otop/__init__.py")
echo "otop apt repository"
echo "  version   $VERSION"
echo "  output    $OUTPUT"
echo "  url       $URL"
echo "  signing   $([ "$SIGN" = yes ] && echo "$KEY" || echo 'DISABLED (trusted=yes)')"

# --- lay out the pool ------------------------------------------------------
# Publishing several builds of the same package+version (otop is built both as
# Architecture: all and, optionally, amd64) would show up twice in apt-cache
# policy, so keep one per package+version and prefer the portable "all" build.
rm -rf "$OUTPUT/dists" "$OUTPUT/pool"
mkdir -p "$OUTPUT/pool/$COMPONENT"
SEEN=""
for deb in $DEBS; do
    name=$(dpkg-deb --field "$deb" Package)
    version=$(dpkg-deb --field "$deb" Version)
    arch=$(dpkg-deb --field "$deb" Architecture)
    key="$name=$version"
    case " $SEEN " in
        *" $key "*)
            if [ "$arch" = all ]; then
                rm -f "$OUTPUT/pool/$COMPONENT/$name"_*.deb
                cp "$deb" "$OUTPUT/pool/$COMPONENT/"
                echo "  using    $(basename "$deb") (replaces the arch-specific build)"
            else
                echo "  skipping $(basename "$deb") ($key already published)"
            fi
            continue
            ;;
    esac
    SEEN="$SEEN $key"
    cp "$deb" "$OUTPUT/pool/$COMPONENT/"
    echo "  adding   $(basename "$deb")"
done

cd "$OUTPUT"
PACKAGES=$(mktemp)
dpkg-scanpackages --multiversion pool 2>/dev/null > "$PACKAGES"
[ -s "$PACKAGES" ] || { echo "dpkg-scanpackages produced nothing" >&2; exit 1; }

# The same index is published for every architecture: otop is Architecture: all,
# and older apt versions only look at binary-<arch> for the host architecture.
for arch in $ARCHES; do
    mkdir -p "dists/$DIST/$COMPONENT/binary-$arch"
    cp "$PACKAGES" "dists/$DIST/$COMPONENT/binary-$arch/Packages"
    gzip -9nc "$PACKAGES" > "dists/$DIST/$COMPONENT/binary-$arch/Packages.gz"
done
rm -f "$PACKAGES"

# --- Release ---------------------------------------------------------------
cd "$OUTPUT/dists/$DIST"
INDEXES=$(find "$COMPONENT" -type f | sort)
{
    echo "Origin: otop"
    echo "Label: otop"
    echo "Suite: $DIST"
    echo "Codename: $DIST"
    echo "Version: $VERSION"
    echo "Architectures: $ARCHES"
    echo "Components: $COMPONENT"
    echo "Description: otop -- htop/btop for Odoo"
    echo "Date: $(date -uR)"
    echo "MD5Sum:"
    for index in $INDEXES; do
        printf " %s %16s %s\n" "$(md5sum "$index" | cut -d' ' -f1)" \
               "$(wc -c < "$index")" "$index"
    done
    echo "SHA256:"
    for index in $INDEXES; do
        printf " %s %16s %s\n" "$(sha256sum "$index" | cut -d' ' -f1)" \
               "$(wc -c < "$index")" "$index"
    done
} > Release

rm -f Release.gpg InRelease
if [ "$SIGN" = yes ]; then
    gpg --batch --yes --default-key "$KEY" --armor --detach-sign \
        --output Release.gpg Release
    gpg --batch --yes --default-key "$KEY" --clearsign --output InRelease Release
    gpg --batch --yes --export "$KEY" > "$OUTPUT/$KEYRING_NAME"
    gpg --batch --yes --armor --export "$KEY" > "$OUTPUT/otop-archive-keyring.asc"
fi

# --- client helpers --------------------------------------------------------
cd "$OUTPUT"
if [ "$SIGN" = yes ]; then
    SOURCES_BODY="Types: deb
URIs: $URL
Suites: $DIST
Components: $COMPONENT
Signed-By: /usr/share/keyrings/$KEYRING_NAME"
else
    SOURCES_BODY="Types: deb
URIs: $URL
Suites: $DIST
Components: $COMPONENT
Trusted: yes"
fi
printf '%s\n' "$SOURCES_BODY" > otop.sources

cat > install.sh <<CLIENT
#!/bin/sh
# Add the otop apt repository, then:  sudo apt install otop
#   curl -fsSL $URL/install.sh | sudo sh
set -eu
[ "\$(id -u)" -eq 0 ] || { echo "run this as root (sudo)" >&2; exit 1; }

CLIENT
if [ "$SIGN" = yes ]; then
    cat >> install.sh <<CLIENT
install -d -m 0755 /usr/share/keyrings
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL/$KEYRING_NAME" -o /usr/share/keyrings/$KEYRING_NAME
else
    wget -qO /usr/share/keyrings/$KEYRING_NAME "$URL/$KEYRING_NAME"
fi
chmod 0644 /usr/share/keyrings/$KEYRING_NAME
CLIENT
fi
cat >> install.sh <<CLIENT
install -d -m 0755 /etc/apt/sources.list.d
cat > /etc/apt/sources.list.d/otop.sources <<'SOURCES'
$SOURCES_BODY
SOURCES
apt-get update
echo
echo "otop repository added. Install it with:  sudo apt install otop"
CLIENT
chmod 0755 install.sh

cat > index.html <<HTML
<!doctype html>
<meta charset="utf-8">
<title>otop apt repository</title>
<style>
 body{font:15px/1.6 system-ui,sans-serif;max-width:46rem;margin:3rem auto;padding:0 1rem}
 pre{background:#111;color:#eee;padding:.9rem;border-radius:6px;overflow-x:auto}
 code{font-family:ui-monospace,monospace}
</style>
<h1>otop</h1>
<p><em>htop/btop for Odoo</em> &mdash; version $VERSION.</p>
<h2>Install</h2>
<pre>curl -fsSL $URL/install.sh | sudo sh
sudo apt install otop</pre>
<h2>Or add the repository by hand</h2>
<pre>$( [ "$SIGN" = yes ] && printf 'sudo curl -fsSL %s/%s -o /usr/share/keyrings/%s\n' "$URL" "$KEYRING_NAME" "$KEYRING_NAME")sudo curl -fsSL $URL/otop.sources -o /etc/apt/sources.list.d/otop.sources
sudo apt update &amp;&amp; sudo apt install otop</pre>
<h2>Upgrades</h2>
<pre>sudo apt update &amp;&amp; sudo apt upgrade otop</pre>
<p><a href="https://github.com/ahmedsajidh/otop">Source code</a></p>
HTML

echo
echo "repository written to $OUTPUT"
find "$OUTPUT" -type f | sed "s|^$OUTPUT|  .|" | sort
cat <<NEXT

Publish it (GitHub Pages: Settings -> Pages -> Deploy from a branch: main /docs):

  git add docs && git commit -m "apt repository for otop $VERSION" && git push

Then on any server:

  curl -fsSL $URL/install.sh | sudo sh
  sudo apt install otop
NEXT
