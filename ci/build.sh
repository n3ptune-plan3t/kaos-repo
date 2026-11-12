#!/usr/bin/env bash
# Builds ONE package inside the KaOS builder container (run as root, from the
# repo checkout, by build-repo.yml via `docker run`).
#
# Inputs:
#   packages/$PKG/PKGBUILD
#   existing/assets.txt             names already on the release
#   existing/<repo>.db.tar.gz       the currently published db (if any)
#
# Outputs, in out/:
#   *.pkg.tar.*                     packages built now (empty if up to date)
#   <repo>.db / <repo>.db.tar.gz    updated database (only if built)
#   stale.txt                       older release assets to delete afterwards
#
# Env:
#   PKG        package folder name under packages/      (required)
#   REPO_NAME  pacman repo / db name                    (required)
#   TAG        release tag hosting the repo             (default x86_64)
#   FORCE      "true" rebuilds even if published        (default false)
#   MAKEPKG_FLAGS  extra makepkg flags, e.g. --skippgpcheck
set -euo pipefail

PKG="${PKG:?PKG is required}"
REPO_NAME="${REPO_NAME:?REPO_NAME is required}"
TAG="${TAG:-x86_64}"
FORCE="${FORCE:-false}"
EXISTING="$PWD/existing"
OUT="$PWD/out"
SRC="$PWD/packages/$PKG"
PUBLISHED="$EXISTING/assets.txt"
BUILD=/home/builder/build

mkdir -p "$OUT" "$EXISTING"; touch "$PUBLISHED"
: > "$OUT/stale.txt"
[[ -f "$SRC/PKGBUILD" ]] || { echo "::error::$SRC/PKGBUILD not found"; exit 1; }

# warn when the builder image is old (it is rebuilt weekly)
if [[ -f /etc/kaos-builder-image ]]; then
  built="$(cat /etc/kaos-builder-image)"
  age=$(( ( $(date +%s) - $(date -d "$built" +%s) ) / 86400 )) || age=0
  (( age > 10 )) && echo "::warning::builder image is $age days old (built $built)"
fi

# If a db is already published, add the repo to this container's pacman so
# dependencies on your own packages resolve (and -Syu sees them).
if grep -qxF "$REPO_NAME.db" "$PUBLISHED"; then
  cat >> /etc/pacman.conf <<EOF

[$REPO_NAME]
SigLevel = Optional TrustAll
Server = https://github.com/${GITHUB_REPOSITORY}/releases/download/${TAG}
EOF
fi

echo "::group::Update build container"
pacman -Syu --noconfirm --needed base-devel git sudo
echo "::endgroup::"

command -v useradd >/dev/null || pacman -S --noconfirm --needed shadow
id builder &>/dev/null || useradd -m builder
echo 'builder ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/builder
chmod 0440 /etc/sudoers.d/builder
rm -rf "$BUILD"; mkdir -p "$BUILD"
work="$BUILD/$PKG"
cp -a "$SRC" "$work"; chown -R builder:builder "$BUILD"

pkgname_of() {  # foo-1.2-1-x86_64.pkg.tar.zst -> foo
  sed -E 's/-[^-]+-[^-]+-[^-]+\.pkg\.tar\..*$//' <<<"$1"
}
is_pkgfile() { [[ "$1" == *.pkg.tar.* && "$1" != *.sig ]]; }

mapfile -t want < <(runuser -u builder -- bash -c "cd '$work' && makepkg --packagelist" 2>/dev/null | xargs -r -n1 basename)
echo "expected files: ${want[*]:-unknown}"

# skip when everything is already published (-git/-svn/-hg compute pkgver at build time)
if [[ "$FORCE" != "true" && "$PKG" != *-git && "$PKG" != *-svn && "$PKG" != *-hg && ${#want[@]} -gt 0 ]]; then
  all_there=true
  for f in "${want[@]}"; do grep -qxF "$f" "$PUBLISHED" || { all_there=false; break; }; done
  if $all_there; then echo "up to date, nothing to do"; exit 0; fi
fi

runuser -u builder -- bash -c "cd '$work' && makepkg -sf --noconfirm --needed ${MAKEPKG_FLAGS:-}"

mapfile -t new < <(cd "$work" && ls -1 ./*.pkg.tar.* 2>/dev/null | grep -v '\.sig$' | xargs -r -n1 basename)
(( ${#new[@]} )) || { echo "::error::makepkg produced no package"; exit 1; }

for f in "${new[@]}"; do
  cp -f "$work/$f" "$OUT/$f"
  name="$(pkgname_of "$f")"
  # older versions of this package already on the release become stale
  while read -r a; do
    if [[ -n "$a" && "$a" != "$f" ]] && is_pkgfile "$a" && [[ "$(pkgname_of "$a")" == "$name" ]]; then
      echo "$a" >> "$OUT/stale.txt"
    fi
  done < "$PUBLISHED"
done
sort -u -o "$OUT/stale.txt" "$OUT/stale.txt"

# database: start from the published one, add what was built
cd "$OUT"
[[ -f "$EXISTING/$REPO_NAME.db.tar.gz" ]] && cp "$EXISTING/$REPO_NAME.db.tar.gz" .
repo-add "$REPO_NAME.db.tar.gz" "${new[@]}"
# release assets are plain files: replace symlinks with real copies
for kind in db files; do
  if [[ -e "$REPO_NAME.$kind.tar.gz" ]]; then
    rm -f "$REPO_NAME.$kind"; cp "$REPO_NAME.$kind.tar.gz" "$REPO_NAME.$kind"
  fi
done
rm -f ./*.old
ls -la "$OUT"
