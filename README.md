# KaOS personal package repo

PKGBUILDs in `packages/`, built in GitHub Actions inside a KaOS container and
published as a pacman repository on a GitHub Release.

## Use the repo on KaOS

Replace `<repo>` with this GitHub repository's name (it is also the pacman
repo name) and add to `/etc/pacman.conf`:

```
[<repo>]
SigLevel = Optional TrustAll
Server = https://github.com/n3ptune-plan3t/<repo>/releases/download/x86_64
```

Then `sudo pacman -Syu` as usual. The GitHub repo must be **public**.

## One-time setup

1. Create the GitHub repo and push this folder to `main`.
2. Settings -> Secrets and variables -> Actions -> **Variables** -> new
   variable `KAOS_MIRROR`: the `Server = ...` value from
   `/etc/pacman.d/mirrorlist` on KaOS, keeping `$repo`
   (e.g. `https://HOST/PATH/$repo`).
3. Settings -> Actions -> General -> Workflow permissions: **Read and write**.
4. Push, or run **build-repo** by hand. On the very first run it builds the
   builder image itself (`bootstrap-kaos-image`), then builds `brave`.
5. If pulling the image fails with a permission error: github.com -> your
   profile -> Packages -> `kaos-builder` -> make it public (or link it to
   this repo).

No KaOS mirror handy, or the `mirror` method fails? Run **bootstrap-kaos-image**
manually with `method = iso` and a direct KaOS ISO URL.

## Workflows

| workflow | when | what |
|---|---|---|
| `build-repo` | push to `main` touching `packages/**`; manual | builds changed packages (manual: names or `all`), one at a time, each published right after it builds |
| `bootstrap-kaos-image` | weekly (Sun 03:00 UTC); manual; automatically if the image is missing | rebuilds `ghcr.io/<owner>/kaos-builder:latest` and prunes old image versions |
| `check-updates` | daily; manual | compares each PKGBUILD with upstream, keeps one issue per outdated package |

## Adding a package

1. `packages/<name>/PKGBUILD` (+ any support files).
2. Optional: `packages/build-order.txt`, one folder name per line, when your
   own packages depend on each other (listed ones build first, in that order).
3. Optional: an entry in `scripts/update-check.json` if upstream can't be
   worked out from the PKGBUILD (see the header of `scripts/check-updates.py`).
4. Push. Check dependency names against the KaOS repos first with
   `python3 tools/kaos_pkgbuild_report.py packages/<name>` on a KaOS machine.

## Updating a package

An `[<name>] update available` issue appears when upstream moves. Edit
`pkgver` (reset `pkgrel=1`), run `updpkgsums`, push. The build and publish
happen by themselves and the issue closes on the next daily check.

## Notes

- Packages are unsigned (`SigLevel = Optional TrustAll`).
- GitHub release assets max out at 2 GiB each; GitHub rewrites some file-name
  characters (`:` in epochs becomes `.`), so avoid `epoch` in this repo.
- `-git`/`-svn`/`-hg` packages always rebuild when you run the build for them
  and are ignored by the update checker.
- Pushing a new `pkgrel` or `pkgver` is what triggers a rebuild; unchanged
  versions already on the release are skipped.
