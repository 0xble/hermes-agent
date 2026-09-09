#!/usr/bin/env bash
# Both supported Linux architectures use checksum-pinned upstream artifacts.
set -euo pipefail
version=15.1.0
case "$(uname -m)" in
  x86_64)
    target=x86_64-unknown-linux-musl
    checksum=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599
    ;;
  aarch64)
    target=aarch64-unknown-linux-gnu
    checksum=2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e
    ;;
  *) printf 'Unsupported Linux architecture\n' >&2; exit 1 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
archive="ripgrep-${version}-${target}"
curl -fsSL --retry 3 --retry-delay 5 \
  "https://github.com/BurntSushi/ripgrep/releases/download/${version}/${archive}.tar.gz" \
  -o "$tmp/rg.tar.gz"
printf '%s  %s\n' "$checksum" "$tmp/rg.tar.gz" | sha256sum -c -
tar -xzf "$tmp/rg.tar.gz" -C "$tmp"
sudo install -m 755 "$tmp/$archive/rg" /usr/local/bin/rg
rg --version
