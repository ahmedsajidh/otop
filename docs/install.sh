#!/bin/sh
# Add the otop apt repository, then:  sudo apt install otop
#   curl -fsSL https://ahmedsajidh.github.io/otop/install.sh | sudo sh
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run this as root (sudo)" >&2; exit 1; }

install -d -m 0755 /usr/share/keyrings
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "https://ahmedsajidh.github.io/otop/otop-archive-keyring.gpg" -o /usr/share/keyrings/otop-archive-keyring.gpg
else
    wget -qO /usr/share/keyrings/otop-archive-keyring.gpg "https://ahmedsajidh.github.io/otop/otop-archive-keyring.gpg"
fi
chmod 0644 /usr/share/keyrings/otop-archive-keyring.gpg
install -d -m 0755 /etc/apt/sources.list.d
cat > /etc/apt/sources.list.d/otop.sources <<'SOURCES'
Types: deb
URIs: https://ahmedsajidh.github.io/otop
Suites: stable
Components: main
Signed-By: /usr/share/keyrings/otop-archive-keyring.gpg
SOURCES
apt-get update
echo
echo "otop repository added. Install it with:  sudo apt install otop"
