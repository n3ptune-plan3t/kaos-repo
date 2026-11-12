#!/usr/bin/env python3
"""
check-updates.py - compare each packages/<name>/PKGBUILD with its upstream.

  python3 scripts/check-updates.py                    # print a report
  python3 scripts/check-updates.py --sync-issues      # also manage GitHub issues

Per package it works out how upstream is tracked:
  - "release"  config github_release = "owner/repo": latest non-prerelease
               GitHub release (needed when tags mix channels, e.g. Brave)
  - "commit"   source pinned with #commit=<sha> or a 40-hex variable: compared
               with the HEAD of the default (or configured) branch
  - "pypi"     source on files.pythonhosted.org
  - "tag"      anything else with a git-hosted URL: newest stable tag from
               `git ls-remote --tags`
VCS packages (-git, -svn, -hg, ...) track HEAD by design and are skipped.

--sync-issues keeps exactly ONE issue per outdated package (label
"upstream-updates", marker <!-- pkg:NAME -->): opened when a package falls
behind, edited if upstream moves again, closed when the PKGBUILD catches up
or the package is removed. It needs the `gh` CLI and GH_TOKEN.

scripts/update-check.json overrides, per package folder name:
  skip (bool), repo (url), branch, tag_prefix, github_release ("owner/repo"),
  pypi (project name), reason (why skipped)
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LABEL = "upstream-updates"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PRE_RE = re.compile(r"(?i)(^|[._\-])(rc|alpha|beta|pre|dev|snapshot|nightly|test|preview)\d*($|[._\-])")
VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr", "-nightly")


# ------------------------------------------------------------------ parsing
def strip_comments(text):
    out = []
    for line in text.splitlines():
        q, res = None, []
        for i, c in enumerate(line):
            if q:
                if c == q:
                    q = None
            elif c in "'\"":
                q = c
            elif c == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            res.append(c)
        out.append("".join(res))
    return "\n".join(out)


def grab_array(text, start):
    depth, q, i = 1, None, start
    while i < len(text) and depth:
        c = text[i]
        if q:
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return text[start:i - 1]


def words(body):
    try:
        return shlex.split(body, posix=True)
    except ValueError:
        return body.split()


def parse_pkgbuild(path):
    text = strip_comments(Path(path).read_text(errors="replace"))
    scalars, sources = {}, []
    for m in re.finditer(r"^(\w+)=(?!\()(.*)$", text, re.M):
        key, val = m.group(1), m.group(2).strip()
        if "$(" in val or key in scalars:
            continue
        w = words(val)
        if w:
            scalars[key] = w[0]
    for m in re.finditer(r"^[ \t]*source(?:_x86_64)?(\+?)=\(", text, re.M):
        sources += words(grab_array(text, m.end()))
    if "pkgname" not in scalars:
        m = re.search(r"^pkgname=\(([^)]*)\)", text, re.M)
        if m and words(m.group(1)):
            scalars["pkgname"] = words(m.group(1))[0]
    return scalars, sources


def expand(s, scalars):
    for _ in range(6):
        new = re.sub(r"\$\{(\w+)\}|\$(\w+)",
                     lambda m: scalars.get(m.group(1) or m.group(2), m.group(0)), s)
        if new == s:
            break
        s = new
    return s


# -------------------------------------------------------------- upstream info
def clean_source(src):
    src = re.sub(r"^[^:]+::", "", src)      # name::url
    return src[4:] if src.startswith("git+") else src


def repo_from_url(url):
    m = re.match(r"(https?://(?:github\.com|gitlab\.com|codeberg\.org|bitbucket\.org|git\.sr\.ht|invent\.kde\.org)"
                 r"/[^/#?]+/[^/#?]+?)(?:\.git)?(?:[/#?]|$)", url)
    return m.group(1) if m else None


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def vkey(v):
    return [(0, int(p)) if p.isdigit() else (-1, p.lower()) for p in re.findall(r"\d+|[A-Za-z]+", v)]


def is_newer(latest, current):
    return vkey(latest) > vkey(current)


def ls_remote_tags(repo):
    r = run(["git", "ls-remote", "--tags", "--refs", repo])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "git ls-remote failed")
    return [ln.split("refs/tags/", 1)[1] for ln in r.stdout.splitlines() if "refs/tags/" in ln]


def ls_remote_head(repo, branch):
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    r = run(["git", "ls-remote", repo, ref])
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(r.stderr.strip() or f"no {ref} on {repo}")
    return r.stdout.split()[0]


def http_json(url):
    headers = {"User-Agent": "kaos-repo-update-checker", "Accept": "application/json"}
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        hint = " (rate limited? set GH_TOKEN)" if e.code in (401, 403) and "api.github.com" in url else ""
        raise RuntimeError(f"HTTP {e.code} for {url}{hint}") from e


def strip_prefix(tag, prefixes):
    for p in prefixes:
        if p and tag.startswith(p):
            return tag[len(p):]
    return tag


def latest_stable_tag(repo, prefixes):
    best = None
    for tag in ls_remote_tags(repo):
        ver = strip_prefix(tag, prefixes)
        if not re.match(r"^\d", ver) or PRE_RE.search(ver):
            continue
        if best is None or vkey(ver) > vkey(best[1]):
            best = (tag, ver)
    if not best:
        raise RuntimeError("no stable version tags found")
    return best[1]


# ------------------------------------------------------------------- checking
def check_package(folder, root, cfg):
    o = cfg.get(folder, {})
    res = {"package": folder, "status": "ok", "outdated": False, "current": "", "latest": "",
           "kind": "", "source": "", "note": ""}
    pkgfile = Path(root) / folder / "PKGBUILD"
    try:
        scalars, sources = parse_pkgbuild(pkgfile)
        name = scalars.get("pkgname", folder)
        if o.get("skip"):
            return {**res, "status": "skipped", "note": o.get("reason", "skipped in update-check.json")}
        if name.endswith(VCS_SUFFIXES):
            return {**res, "status": "skipped", "note": "VCS package, tracks upstream HEAD"}

        scalars.setdefault("pkgname", name)
        current = expand(scalars.get("pkgver", ""), scalars)
        srcs = [expand(clean_source(s), scalars) for s in sources]
        res["current"] = current

        # 1. configured GitHub release channel
        if o.get("github_release"):
            slug = o["github_release"]
            tag = http_json(f"https://api.github.com/repos/{slug}/releases/latest")["tag_name"]
            res.update(kind="release", source=f"https://github.com/{slug}",
                       latest=strip_prefix(tag, [o.get("tag_prefix", "v"), "v", "V"]))
        else:
            repo = o.get("repo")
            if not repo:
                for s in srcs + [expand(scalars.get("url", ""), scalars)]:
                    repo = repo_from_url(s)
                    if repo:
                        break
            # 2. pinned commit
            pinned = None
            for s in srcs:
                m = re.search(r"#commit=([0-9a-f]{40})", s)
                if m:
                    pinned = m.group(1)
            if not pinned:
                for k, v in scalars.items():
                    v = expand(v, scalars)
                    if HEX40.match(v) and any(v in s for s in srcs):
                        pinned = v
            pypi = o.get("pypi")
            if not pypi:
                for s in srcs:
                    m = re.search(r"pythonhosted\.org/packages/source/./([^/]+)/", s)
                    if m:
                        pypi = m.group(1)
            if pinned and repo:
                head = ls_remote_head(repo, o.get("branch"))
                res.update(kind="commit", source=repo, current=pinned[:12], latest=head[:12],
                           outdated=pinned != head)
                return res
            if pypi:
                latest = http_json(f"https://pypi.org/pypi/{pypi}/json")["info"]["version"]
                res.update(kind="pypi", source=f"https://pypi.org/project/{pypi}/", latest=latest)
            elif repo:
                prefixes = [o["tag_prefix"]] if "tag_prefix" in o else ["v", "V", f"{name}-", f"{repo.rsplit('/', 1)[-1]}-", ""]
                res.update(kind="tag", source=repo, latest=latest_stable_tag(repo, prefixes))
            else:
                return {**res, "status": "skipped",
                        "note": "no git-hosted or PyPI source found; add 'repo' to update-check.json"}
        res["outdated"] = bool(current) and is_newer(res["latest"], current)
    except Exception as e:  # noqa
        res.update(status="error", note=str(e)[:300])
    return res


# --------------------------------------------------------------------- issues
def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()}")
    return r.stdout


def issue_title(r):
    return f"[{r['package']}] update available: {r['current']} → {r['latest']}"


def issue_body(r):
    what = "commit" if r["kind"] == "commit" else "version"
    return (
        f"<!-- pkg:{r['package']} -->\n"
        f"**{r['package']}** is behind upstream.\n\n"
        f"| | |\n|---|---|\n"
        f"| In PKGBUILD ({what}) | `{r['current']}` |\n"
        f"| Upstream | `{r['latest']}` |\n"
        f"| Source | {r['source']} |\n\n"
        f"To update, edit `packages/{r['package']}/PKGBUILD`"
        + (" (pinned commit variable, " if r["kind"] == "commit" else " (`pkgver`, ")
        + "reset `pkgrel=1`), refresh checksums with `updpkgsums` (or `makepkg -g`), "
        "and push. The push builds and publishes the package automatically.\n\n"
        "_Opened by the update checker; it edits or closes this issue by itself._\n"
    )


def sync_issues(results, dry=False):
    log = []
    if not dry:
        try:
            gh("label", "create", LABEL, "--color", "1d76db",
               "--description", "Upstream has a newer version", "--force")
        except RuntimeError as e:
            log.append(f"label: {e}")
    open_issues = json.loads(gh("issue", "list", "--label", LABEL, "--state", "open",
                                "--limit", "300", "--json", "number,title,body") or "[]")
    by_pkg = {}
    for it in open_issues:
        m = re.search(r"<!-- pkg:([^ ]+) -->", it.get("body") or "")
        if m:
            by_pkg.setdefault(m.group(1), it)
    state = {r["package"]: r for r in results}

    for pkg, r in state.items():
        if not r["outdated"]:
            continue
        title, body = issue_title(r), issue_body(r)
        it = by_pkg.get(pkg)
        if it is None:
            log.append(f"open   #{pkg}: {title}")
            if not dry:
                gh("issue", "create", "--title", title, "--label", LABEL, "--body", body)
        elif it["title"] != title or (it.get("body") or "").strip() != body.strip():
            log.append(f"edit   #{it['number']} {pkg}: {title}")
            if not dry:
                gh("issue", "edit", str(it["number"]), "--title", title, "--body", body)
        else:
            log.append(f"keep   #{it['number']} {pkg}")

    for pkg, it in by_pkg.items():
        r = state.get(pkg)
        if r is None or (r["status"] == "ok" and not r["outdated"]):
            why = "package removed" if r is None else f"PKGBUILD is now at {r['current']}"
            log.append(f"close  #{it['number']} {pkg}: {why}")
            if not dry:
                gh("issue", "close", str(it["number"]), "--comment", f"Resolved: {why}.")
        # errors/skips leave an existing issue untouched
    return log


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packages-dir", default="packages")
    ap.add_argument("--config", default="scripts/update-check.json")
    ap.add_argument("--json", help="write results as JSON")
    ap.add_argument("--markdown", help="write a markdown summary")
    ap.add_argument("--sync-issues", action="store_true", help="open/edit/close one issue per package")
    ap.add_argument("--dry-run", action="store_true", help="with --sync-issues: only print what would happen")
    a = ap.parse_args()

    cfg = {}
    if Path(a.config).is_file():
        cfg = {k: v for k, v in json.loads(Path(a.config).read_text()).items() if not k.startswith("_")}
    root = Path(a.packages_dir)
    folders = sorted(p.parent.name for p in root.glob("*/PKGBUILD"))
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda f: check_package(f, root, cfg), folders))

    outdated = [r for r in results if r["outdated"]]
    errors = [r for r in results if r["status"] == "error"]
    lines = [f"# Upstream check: {len(outdated)} outdated of {len(results)} packages\n"]
    if outdated:
        lines += ["| package | PKGBUILD | upstream | how | source |", "|---|---|---|---|---|"]
        lines += [f"| {r['package']} | `{r['current']}` | `{r['latest']}` | {r['kind']} | {r['source']} |" for r in outdated]
    if errors:
        lines += ["", "## Errors"] + [f"- {r['package']}: {r['note']}" for r in errors]
    skipped = [r for r in results if r["status"] == "skipped"]
    if skipped:
        lines += ["", "## Skipped"] + [f"- {r['package']}: {r['note']}" for r in skipped]

    log = []
    if a.sync_issues:
        log = sync_issues(results, dry=a.dry_run)
        lines += ["", "## Issues"] + [f"- {x}" for x in log] if log else []
    md = "\n".join(lines) + "\n"
    print(md)
    if a.markdown:
        Path(a.markdown).write_text(md)
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2))
    # check errors do not fail the run; a broken upstream lookup should not hide the others


if __name__ == "__main__":
    main()
