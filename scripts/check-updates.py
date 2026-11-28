#!/usr/bin/env python3
"""
check-updates.py - compare each packages/<name>/PKGBUILD with its upstream.

  python3 scripts/check-updates.py                    # print a report
  python3 scripts/check-updates.py --sync-issues      # also manage GitHub issues

Per package it works out how upstream is tracked (first match wins):
- "release"  config release = true (or github_release = "owner/repo"): latest
               non-prerelease release from the forge's API. Works for GitHub,
               GitLab (gitlab.com and self-hosted), Codeberg / Forgejo / Gitea.
               Needed when tags mix channels, e.g. Brave.
- "page"     config scrape = {url, regex}: highest version captured by the
               regex (group 1) on any web page, for upstreams with no forge
- "commit"   source pinned with #commit=<sha> or a 40-hex variable: compared
               with the HEAD of the default (or configured) branch
- "pypi"     source on files.pythonhosted.org (or config pypi)
- "crates"   source on crates.io (or config crates)
- "npm"      source on registry.npmjs.org (or config npm)
- "tag"      anything else with a git-hosted URL: newest stable tag from
               `git ls-remote --tags` (works on every forge, incl. sourcehut,
               bitbucket and any self-hosted git server)

Forges are recognised by host: github.com, gitlab.com, invent.kde.org,
opencode.net, gitlab.gnome.org, gitlab.freedesktop.org, salsa.debian.org,
codeberg.org, gitea.com, git.sr.ht, bitbucket.org and any host whose first
label is gitlab*/gitea*/forgejo*. Anything else self-hosted: set "repo" and
"forge" ("gitlab" | "gitea" | "github" | "git") in update-check.json.
GitLab nested groups (group/subgroup/project) are supported.
Optional tokens (only to avoid rate limits / reach private repos):
GH_TOKEN, GITLAB_TOKEN, CODEBERG_TOKEN (or FORGEJO_TOKEN / GITEA_TOKEN).
VCS packages (-git, -svn, -hg, ...) track HEAD by design and are skipped.

Every issue also carries the sha256 of the NEW source archives (downloaded and
hashed by the checker) as a ready-to-paste sha256sums=(...) array, so the
PKGBUILD can be updated by hand without running updpkgsums.

--sync-issues keeps exactly ONE issue per outdated package (label
"upstream-updates", marker <!-- pkg:NAME -->): opened when a package falls
behind, edited if upstream moves again, closed when the PKGBUILD catches up
or the package is removed. It needs the `gh` CLI and GH_TOKEN.

scripts/update-check.json overrides, per package folder name:
  skip (bool), reason (why skipped), repo (url), forge, branch, tag_prefix,
  release (true: use the forge releases API of "repo"/the detected repo),
  github_release ("owner/repo", shortcut for release on GitHub),
  pypi / crates / npm (project name),
  scrape ({"url": ..., "regex": "...(\\d+\\.\\d+)..."}),
  current_var (name of the PKGBUILD variable that holds the real upstream
               version, e.g. "_pkgver", when pkgver is a munged copy of it)

The PKGBUILD version is resolved statically (no PKGBUILD code is executed):
$var, ${var}, and the bash parameter expansions ${v//a/b} ${v/a/b} ${v/#a/b}
${v/%a/b} ${v#p} ${v##p} ${v%p} ${v%%p} ${v:off:len} ${v^} ${v^^} ${v,}
${v,,} ${v:-d} ${v:=d} ${v:+alt} ${#v}. If a version still cannot be
resolved, the package is reported as an error/skipped, never as "outdated",
and an already-open issue for it is left alone.

  python3 scripts/check-updates.py --self-test        # run the built-in tests
  python3 scripts/check-updates.py --only nvtop       # check just some packages
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LABEL = "upstream-updates"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PRE_RE = re.compile(r"(?i)(^|[._\-])(rc|alpha|beta|pre|dev|snapshot|nightly|test|preview)\d*($|[._\-])")
VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr", "-nightly")
UNRESOLVED = re.compile(r"[$`]")   # leftover shell syntax in a "resolved" value

# host -> forge type ("github" | "gitlab" | "gitea" | "git" = plain git, tags only)
KNOWN_FORGES = {
    "github.com": "github",
    "gitlab.com": "gitlab", "invent.kde.org": "gitlab", "opencode.net": "gitlab",
    "gitlab.gnome.org": "gitlab", "gitlab.freedesktop.org": "gitlab",
    "salsa.debian.org": "gitlab", "framagit.org": "gitlab",
    "gitlab.archlinux.org": "gitlab", "gitlab.alpinelinux.org": "gitlab",
    "gitlab.kitware.com": "gitlab", "gitlab.xfce.org": "gitlab",
    "codeberg.org": "gitea", "gitea.com": "gitea",
    "git.sr.ht": "git", "bitbucket.org": "git",
}
FORGE_TYPES = ("github", "gitlab", "gitea", "git")
# path pieces that end the "owner/repo" part of a forge URL
PATH_STOP = re.compile(r"/(?:-|archive|repository|raw|blob|tree|commits?|releases|tags?|issues|"
                       r"merge_requests|pulls?|wikis?|src|download)(?:/|$)")


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


class Unresolved(Exception):
    """a ${...} construct that cannot be evaluated statically"""


def _match_brace(s, i):
    """index of the } closing a ${ whose body starts at i (nesting aware), or -1"""
    depth = 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s.startswith("${", i):
            depth += 1
            i += 2
            continue
        if s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _glob_re(pat):
    """bash pattern (* ? [...] \\x) -> compiled regex"""
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if c == "\\" and i + 1 < len(pat):
            out.append(re.escape(pat[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[":
            j = pat.find("]", i + 2)
            if j == -1:
                out.append(r"\[")
            else:
                body = pat[i + 1:j]
                neg = body[:1] in ("!", "^")
                if neg:
                    body = body[1:]
                out.append("[" + ("^" if neg else "") + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    try:
        return re.compile("".join(out), re.S)
    except re.error as e:
        raise Unresolved(str(e))


def _strip(val, pat, front, longest):
    """${v#p} ${v##p} (front) / ${v%p} ${v%%p} (back)"""
    rx = _glob_re(pat)
    idx = range(len(val) + 1)
    if front:
        for n in (reversed(idx) if longest else idx):
            if rx.fullmatch(val[:n]):
                return val[n:]
    else:
        for n in (idx if longest else reversed(idx)):
            if rx.fullmatch(val[n:]):
                return val[:n]
    return val


def _subst(val, pat, rep, mode):
    """${v/p/r} (first) ${v//p/r} (all) ${v/#p/r} (prefix) ${v/%p/r} (suffix)"""
    if pat == "":
        return val
    rx = _glob_re(pat)

    def longest_at(i):
        for j in range(len(val), i - 1, -1):
            if rx.fullmatch(val, i, j):
                return j
        return None

    if mode == "prefix":
        j = longest_at(0)
        return rep + val[j:] if j is not None else val
    if mode == "suffix":
        for i in range(len(val) + 1):
            if rx.fullmatch(val, i, len(val)):
                return val[:i] + rep
        return val
    out, i = [], 0
    while i < len(val):
        j = longest_at(i)
        if j is not None and j > i:
            out.append(rep)
            i = j
            if mode == "first":
                out.append(val[i:])
                return "".join(out)
        else:
            out.append(val[i])
            i += 1
    return "".join(out)


def _split_slash(rest):
    """'pat/rep' -> (pat, rep), splitting at the first unescaped /"""
    i = 0
    while i < len(rest):
        if rest[i] == "\\":
            i += 2
            continue
        if rest[i] == "/":
            return rest[:i], rest[i + 1:].replace("\\/", "/")
        i += 1
    return rest, ""


def _param(inner, scalars, depth):
    """evaluate the body of one ${...}; anything unsupported is returned verbatim"""
    raw = "${" + inner + "}"

    def sub(t):
        return expand(t, scalars, depth + 1)

    def value(name):
        v = sub(scalars[name])
        if "$" in v:
            raise Unresolved(name)
        return v

    try:
        m = re.fullmatch(r"#([A-Za-z_]\w*)", inner)          # ${#var}
        if m:
            return str(len(value(m.group(1)))) if m.group(1) in scalars else raw
        m = re.match(r"([A-Za-z_]\w*)(.*)$", inner, re.S)
        if not m:
            return raw
        name, op = m.groups()
        isset = name in scalars
        if op == "":
            return sub(scalars[name]) if isset else raw

        # defaults: ${v:-d} ${v:=d} ${v:+alt} ${v-d} ${v=d} ${v+alt}
        if op[:2] in (":-", ":=", ":+") or op[0] in "-=+":
            colon = op[0] == ":"
            kind, arg = (op[1], op[2:]) if colon else (op[0], op[1:])
            val = sub(scalars[name]) if isset else ""
            empty = (not isset) or (colon and val == "")
            arg = sub(arg)
            if kind in "-=":
                return arg if empty else val
            return "" if empty else arg

        if not isset:
            raise Unresolved(name)
        val = value(name)

        if op.startswith(":"):                                # ${v:off} ${v:off:len}
            try:
                nums = [int(x.strip().strip("()")) for x in sub(op[1:]).split(":")]
            except ValueError:
                raise Unresolved(op)
            if len(nums) > 2:
                raise Unresolved(op)
            off = nums[0]
            if off < 0:
                off = max(len(val) + off, 0)
            if len(nums) == 1:
                return val[off:]
            end = off + nums[1] if nums[1] >= 0 else len(val) + nums[1]
            return val[off:end] if end > off else ""
        if op == "^^":
            return val.upper()
        if op == "^":
            return val[:1].upper() + val[1:]
        if op == ",,":
            return val.lower()
        if op == ",":
            return val[:1].lower() + val[1:]
        for pre, mode in (("//", "all"), ("/#", "prefix"), ("/%", "suffix"), ("/", "first")):
            if op.startswith(pre):
                pat, rep = _split_slash(op[len(pre):])
                return _subst(val, sub(pat), sub(rep), mode)
        for pre, front, longest in (("##", True, True), ("#", True, False),
                                    ("%%", False, True), ("%", False, False)):
            if op.startswith(pre):
                return _strip(val, sub(op[len(pre):]), front, longest)
        raise Unresolved(op)
    except Unresolved:
        return raw


def expand(s, scalars, depth=0):
    """resolve $var / ${var} / ${var<bash parameter expansion>} using the
    PKGBUILD's own scalars; names that are unknown are left as written"""
    if depth > 8:                       # self-referencing variables
        return s
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "$" and s.startswith("{", i + 1):
            j = _match_brace(s, i + 2)
            if j == -1:
                out.append(s[i:])
                break
            out.append(_param(s[i + 2:j], scalars, depth))
            i = j + 1
            continue
        if c == "$":
            m = re.match(r"[A-Za-z_]\w*", s[i + 1:])
            if m:
                name = m.group(0)
                out.append(expand(scalars[name], scalars, depth + 1) if name in scalars else "$" + name)
                i += 1 + len(name)
                continue
        out.append(c)
        i += 1
    return "".join(out)


def self_test():
    s = {"_v": "0.4.0-devel3", "pkgver": "${_v//-/_}", "a": "ble-${_v}-2", "p": "1.2.3", "x": "$x"}
    cases = [
        ("${pkgver}", "0.4.0_devel3"), ("$a", "ble-0.4.0-devel3-2"),
        ("${p%.*}", "1.2"), ("${p%%.*}", "1"), ("${p#*.}", "2.3"), ("${p##*.}", "3"),
        ("${p/./_}", "1_2.3"), ("${p//./_}", "1_2_3"), ("${p:0:3}", "1.2"), ("${p: -1}", "3"),
        ("${_v^^}", "0.4.0-DEVEL3"), ("${_v,,}", "0.4.0-devel3"), ("${missing:-x}", "x"),
        ("${p:+yes}", "yes"), ("${#p}", "5"), ("${_v/#0/9}", "9.4.0-devel3"),
        ("${_v/%3/9}", "0.4.0-devel9"), ("${p//${p%%.*}/X}", "X.2.3"),
        ("${_v//[0-9]/N}", "N.N.N-develN"),
        ("$missing", "$missing"), ("${p@U}", "${p@U}"), ("$x", "$x"),
    ]
    bad = [(i, exp, expand(i, s)) for i, exp in cases if expand(i, s) != exp]
    repo_cases = [
        ("https://github.com/Syllo/nvtop/archive/refs/tags/3.4.0.tar.gz", None, "https://github.com/Syllo/nvtop"),
        ("https://www.opencode.net/trialuser/qt6ct/-/archive/0.11/qt6ct-0.11.tar.bz2", None,
         "https://www.opencode.net/trialuser/qt6ct"),
        ("https://gitlab.com/a/b/c/-/archive/v1/c-v1.tar.gz", None, "https://gitlab.com/a/b/c"),
        ("https://gitlab.com/a/b/c/-/releases/v1", None, "https://gitlab.com/a/b/c"),
        ("https://gitlab.com/a/b/repository/archive.tar.gz?ref=v1", None, "https://gitlab.com/a/b"),
        ("https://invent.kde.org/utilities/kate.git", None, "https://invent.kde.org/utilities/kate"),
        ("https://codeberg.org/u/r/archive/v1.tar.gz", None, "https://codeberg.org/u/r"),
        ("https://codeberg.org/u/r/releases/download/v1/x.zip", None, "https://codeberg.org/u/r"),
        ("https://gitlab.example.org/g/p/-/tags/1", None, "https://gitlab.example.org/g/p"),
        ("https://git.example.org/u/r/archive/1.tar.gz", None, None),
        ("https://git.example.org/u/r/archive/1.tar.gz", "gitea", "https://git.example.org/u/r"),
        ("https://example.com/foo.tar.gz", None, None),
    ]
    bad += [(f"repo_from_url({u!r}, {f!r})", exp, repo_from_url(u, f))
            for u, f, exp in repo_cases if repo_from_url(u, f) != exp]
    kind_cases = [("github.com", "github"), ("www.opencode.net", "gitlab"), ("codeberg.org", "gitea"),
                  ("gitlab.foo.net", "gitlab"), ("forgejo.foo.net", "gitea"), ("git.sr.ht", "git"),
                  ("example.com", None)]
    bad += [(f"forge_kind({h!r})", exp, forge_kind(h)) for h, exp in kind_cases if forge_kind(h) != exp]
    for i, exp, got in bad:
        print(f"FAIL {i!r}: expected {exp!r}, got {got!r}")
    total = len(cases) + len(repo_cases) + len(kind_cases)
    print(f"self-test: {total - len(bad)}/{total} passed")
    return not bad


# -------------------------------------------------------------- upstream info
def clean_source(src):
    src = re.sub(r"^[^:]+::", "", src)      # name::url
    return src[4:] if src.startswith("git+") else src


def forge_kind(host, override=None):
    """forge type for a host name, or None when it is not a known forge"""
    if override:
        return override if override in FORGE_TYPES else None
    h = (host or "").lower()
    h = h[4:] if h.startswith("www.") else h
    if h in KNOWN_FORGES:
        return KNOWN_FORGES[h]
    first = h.split(".")[0]
    for prefix, kind in (("gitlab", "gitlab"), ("gitea", "gitea"), ("forgejo", "gitea"), ("codeberg", "gitea")):
        if first == prefix or first.startswith(prefix + "-"):
            return kind
    return None


def repo_from_url(url, forge=None):
    """https://host/owner/repo (GitLab: host/group/sub/project) out of any
    archive / release / clone URL on a known forge, else None"""
    m = re.match(r"https?://([^/\s#?]+)/([^#?\s]+)", url)
    if not m:
        return None
    host, path = m.group(1), "/" + m.group(2)
    kind = forge_kind(host, forge)
    if not kind:
        return None
    path = PATH_STOP.split(path, 1)[0] if kind == "gitlab" else path
    segs = [x for x in path.split("/") if x]
    if kind != "gitlab":
        segs = segs[:2]
    if len(segs) < 2:
        return None
    if segs[-1].endswith(".git"):
        segs[-1] = segs[-1][:-4]
    return f"https://{host}/" + "/".join(segs)


def split_repo(repo):
    u = urllib.parse.urlparse(repo)
    return u.netloc, u.path.strip("/")


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


def _open(url, accept):
    headers = {"User-Agent": "kaos-repo-update-checker", "Accept": accept}
    env = os.environ.get
    if "api.github.com" in url:
        tok = env("GH_TOKEN") or env("GITHUB_TOKEN")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
    elif "/api/v4/" in url:
        if env("GITLAB_TOKEN"):
            headers["PRIVATE-TOKEN"] = env("GITLAB_TOKEN")
    elif "/api/v1/" in url:
        tok = env("CODEBERG_TOKEN") or env("FORGEJO_TOKEN") or env("GITEA_TOKEN")
        if tok:
            headers["Authorization"] = f"token {tok}"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code in (401, 403, 429):
            hint = " (rate limited? set GH_TOKEN / GITLAB_TOKEN / CODEBERG_TOKEN)"
        raise RuntimeError(f"HTTP {e.code} for {url}{hint}") from e


def http_json(url):
    with _open(url, "application/json") as r:
        return json.load(r)


def http_text(url):
    with _open(url, "text/html,text/plain,*/*") as r:
        return r.read(4_000_000).decode("utf-8", "replace")


def forge_latest_release(repo, kind):
    """tag name of the newest published, non-prerelease release of a repo"""
    host, path = split_repo(repo)
    if kind == "github":
        return http_json(f"https://api.github.com/repos/{path}/releases/latest")["tag_name"]
    if kind == "gitlab":
        pid = urllib.parse.quote(path, safe="")
        for r in http_json(f"https://{host}/api/v4/projects/{pid}/releases?per_page=30"):
            if not r.get("upcoming_release") and not PRE_RE.search(r["tag_name"]):
                return r["tag_name"]
        raise RuntimeError("no stable releases on GitLab (project may only use tags)")
    if kind == "gitea":
        for r in http_json(f"https://{host}/api/v1/repos/{path}/releases?limit=30&draft=false"):
            if not r.get("draft") and not r.get("prerelease"):
                return r["tag_name"]
        raise RuntimeError("no stable releases on Gitea/Forgejo (project may only use tags)")
    raise RuntimeError(f"no releases API for {host}; set \"forge\" in update-check.json or drop \"release\"")


def scrape_latest(spec):
    """highest stable version matched by spec['regex'] (group 1) on spec['url']"""
    rx = re.compile(spec["regex"])
    found = []
    for m in rx.finditer(http_text(spec["url"])):
        v = m.group(1) if rx.groups else m.group(0)
        if v and not PRE_RE.search(v):
            found.append(v)
    if not found:
        raise RuntimeError("scrape regex matched nothing")
    return max(found, key=vkey)


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
        current = expand(scalars.get(o.get("current_var") or "pkgver", ""), scalars)
        srcs = [expand(clean_source(s), scalars) for s in sources]
        res["current"] = current

        # Never call a package outdated on a version we could not read: report
        # why instead (an existing issue is left untouched).  A pinned-commit
        # package does not need pkgver, so that path ignores this.
        bad = None
        if not current:
            bad = {"status": "skipped",
                   "note": "no static pkgver (set by pkgver() or $(...)); set \"current_var\" "
                           "or \"skip\" in scripts/update-check.json"}
        elif UNRESOLVED.search(current):
            bad = {"status": "error",
                   "note": f"version '{current}' cannot be resolved statically; set \"current_var\" "
                           "(e.g. \"_pkgver\") or \"skip\" in scripts/update-check.json"}

        # where does upstream live?  explicit config first, then the sources / url=
        forge_hint = o.get("forge")
        repo = o.get("repo")
        if not repo and o.get("github_release"):
            repo = f"https://github.com/{o['github_release']}"
        if not repo:
            for s in srcs + [expand(scalars.get("url", ""), scalars)]:
                repo = repo_from_url(s, forge_hint)
                if repo:
                    break
        kind = forge_kind(split_repo(repo)[0], forge_hint) if repo else None
        prefixes_cfg = [o["tag_prefix"]] if "tag_prefix" in o else None

        if o.get("release") or o.get("github_release"):
            # 1. forge releases API (GitHub / GitLab / Codeberg / Forgejo / Gitea)
            if bad:
                return {**res, **bad}
            if not repo:
                raise RuntimeError("no repo found for release lookup; set \"repo\" in update-check.json")
            tag = forge_latest_release(repo, kind)
            res.update(kind="release", source=repo,
                       latest=strip_prefix(tag, (prefixes_cfg or [""]) + ["v", "V"]))
        elif o.get("scrape"):
            # 2. any web page
            if bad:
                return {**res, **bad}
            res.update(kind="page", source=o["scrape"]["url"], latest=scrape_latest(o["scrape"]))
        else:
            # 3. pinned commit
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
            if bad and not (pinned and repo):
                return {**res, **bad}
            if pinned and repo:
                head = ls_remote_head(repo, o.get("branch"))
                res.update(kind="commit", source=repo, current=pinned[:12], latest=head[:12],
                           current_full=pinned, latest_full=head, outdated=pinned != head)
                return res
            pypi, crates, npm = o.get("pypi"), o.get("crates"), o.get("npm")
            for s in srcs:
                m = re.search(r"pythonhosted\.org/packages/source/./([^/]+)/", s)
                if m and not pypi:
                    pypi = m.group(1)
                m = re.search(r"crates\.io/(?:api/v1/)?crates/([^/]+)/", s)
                if m and not crates:
                    crates = m.group(1)
                m = re.search(r"registry\.npmjs\.org/(@[^/]+/[^/]+|[^/@][^/]*)/-/", s)
                if m and not npm:
                    npm = m.group(1)
            # 4. package registries
            if pypi:
                latest = http_json(f"https://pypi.org/pypi/{pypi}/json")["info"]["version"]
                res.update(kind="pypi", source=f"https://pypi.org/project/{pypi}/", latest=latest)
            elif crates:
                latest = http_json(f"https://crates.io/api/v1/crates/{crates}")["crate"]["max_stable_version"]
                res.update(kind="crates", source=f"https://crates.io/crates/{crates}", latest=latest)
            elif npm:
                latest = http_json(f"https://registry.npmjs.org/{npm.replace('/', '%2f')}/latest")["version"]
                res.update(kind="npm", source=f"https://www.npmjs.com/package/{npm}", latest=latest)
            elif repo:
                # 5. newest stable tag, via plain git: works on any forge
                prefixes = prefixes_cfg or ["v", "V", f"{name}-", f"{repo.rsplit('/', 1)[-1]}-", ""]
                res.update(kind="tag", source=repo, latest=latest_stable_tag(repo, prefixes))
            else:
                return {**res, "status": "skipped",
                        "note": "no forge / registry source found; add \"repo\" (and \"forge\" if self-hosted), "
                                "\"release\" or \"scrape\" to update-check.json"}
        res["outdated"] = bool(current) and is_newer(res["latest"], current)
    except Exception as e:  # noqa
        res.update(status="error", note=str(e)[:300])
    return res


# ------------------------------------------------------------------ checksums
SUMS_OK = "<!-- sha256:ok -->"


def parse_sums(path):
    """entries of every sha256sums / sha256sums_x86_64 array, in file order
    (same order parse_pkgbuild() uses for source / source_x86_64)"""
    text = strip_comments(Path(path).read_text(errors="replace"))
    sums = []
    for m in re.finditer(r"^[ \t]*sha256sums(?:_x86_64)?(\+?)=\(", text, re.M):
        sums += words(grab_array(text, m.end()))
    return sums


def source_parts(src):
    """(local file name, url, kind) of one expanded source entry;
    kind is remote (downloadable), vcs (git+..., makepkg uses SKIP) or local"""
    m = re.match(r"^([^:/]+)::(.*)$", src)
    fname, url = (m.group(1), m.group(2)) if m else (None, src)
    url = url.split("#", 1)[0]
    if re.match(r"^(git|svn|hg|bzr)(\+|://)", url) or url.endswith(".git"):
        kind = "vcs"
    elif re.match(r"^(https?|ftp)://", url):
        kind = "remote"
    else:
        kind = "local"
    if not fname:
        fname = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]) or url
    return fname, url, kind

def sha256_url(url, tries=3):
    """stream-download url, return (sha256 hex, size in bytes)"""
    last = None
    for _ in range(tries):
        try:
            h, n = hashlib.sha256(), 0
            req = urllib.request.Request(url, headers={"User-Agent": "kaos-repo-update-checker"})
            with urllib.request.urlopen(req, timeout=120) as r:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    n += len(chunk)
            return h.hexdigest(), n
        except Exception as e:  # noqa
            last = e
    raise RuntimeError(str(last)[:200])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_source_sums(r, root, cfg):
    """sha256 of every source the PKGBUILD will have at the NEW version.
    Sources are re-expanded with the new version substituted for the old one
    (pinned-commit packages: new sha for old sha); remote archives are
    downloaded and hashed; git+ sources stay SKIP; local files keep the
    checksum already in the PKGBUILD.  Returns a list of dicts."""
    folder = r["package"]
    o = cfg.get(folder, {})
    pkgfile = Path(root) / folder / "PKGBUILD"
    scalars, sources = parse_pkgbuild(pkgfile)
    old_sums = parse_sums(pkgfile)
    scalars.setdefault("pkgname", scalars.get("pkgname", folder))

    if r["kind"] == "commit":
        srcs = [expand(x, scalars).replace(r["current_full"], r["latest_full"]) for x in sources]
    else:
        s2 = dict(scalars)
        current = expand(scalars.get(o.get("current_var") or "pkgver", ""), scalars)
        # every variable that holds the current version moves to the new one,
        # so sources written with $pkgver, $_ver, ... all follow
        for k, v in scalars.items():
            if k not in ("pkgrel", "epoch") and current and expand(v, scalars) == current:
                s2[k] = r["latest"]
        s2[o.get("current_var") or "pkgver"] = r["latest"]
        srcs = [expand(x, s2) for x in sources]

    parts = [source_parts(x) for x in srcs]
    aligned = len(old_sums) == len(srcs)
    out = [{"file": f, "url": u, "kind": k, "sum": None, "size": None, "error": None}
           for f, u, k in parts]

    def work(i):
        e = out[i]
        if e["kind"] == "vcs":
            e["sum"] = "SKIP"
        elif e["kind"] == "local":
            local = pkgfile.parent / e["file"]
            if aligned:
                e["sum"] = old_sums[i]
            elif local.is_file():
                e["sum"] = sha256_file(local)
            else:
                e["sum"] = "SKIP"
        else:
            if "$" in e["url"]:
                e["error"] = "url still has unresolved variables"
                return
            try:
                e["sum"], e["size"] = sha256_url(e["url"])
            except Exception as ex:  # noqa
                e["error"] = str(ex)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, range(len(out))))
    return out


def sums_section(entries):
    """markdown for the issue body: paste-ready array + per-file table"""
    if not entries:
        return ""
    vals = [e["sum"] or "DOWNLOAD-FAILED" for e in entries]
    arr = "sha256sums=(" + ("\n" + " " * 12).join(f"'{v}'" for v in vals) + ")"
    lines = ["", "### New source checksums (sha256)", "",
             "Replace the `sha256sums` array in the PKGBUILD with:", "",
             "```bash", arr, "```", "",
             "| file | sha256 | size |", "|---|---|---|"]
    for e in entries:
        size = f"{e['size'] / 1048576:.1f} MiB" if e["size"] else ""
        cell = f"`{e['sum']}`" if e["sum"] else f"⚠️ {e['error']}"
        lines.append(f"| `{e['file']}` | {cell} | {size} |")
    lines += ["", "`SKIP` = git source or an unchanged local file; its checksum is not affected by the update."]
    failed = [e for e in entries if e["error"]]
    if failed:
        lines += ["", "⚠️ Some downloads failed; they are retried on the next run "
                      "(or run `updpkgsums`)."]
    else:
        lines += ["", SUMS_OK]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- issues
def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()}")
    return r.stdout


def issue_title(r):
    return f"[{r['package']}] update available: {r['current']} → {r['latest']}"


def issue_body(r, sums=""):
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
        + "reset `pkgrel=1`), paste the checksums below into `sha256sums` "
        "(or refresh them with `updpkgsums`), and push. "
        "The push builds and publishes the package automatically.\n"
        + sums
        + "\n_Opened by the update checker; it edits or closes this issue by itself._\n"
    )


def sync_issues(results, dry=False, root="packages", cfg=None):
    cfg = cfg or {}
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
        title = issue_title(r)
        it = by_pkg.get(pkg)
        if dry:
            body = (it or {}).get("body") or issue_body(r)      # no downloads in a dry run
        elif it and it["title"] == title and SUMS_OK in (it.get("body") or ""):
            body = it["body"]                                   # already hashed for this version
        else:
            try:
                body = issue_body(r, sums_section(new_source_sums(r, root, cfg)))
            except Exception as e:  # noqa
                log.append(f"sums   {pkg}: {e}")
                body = issue_body(r)
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
    ap.add_argument("--self-test", action="store_true", help="test the PKGBUILD version resolver and exit")
    ap.add_argument("--only", action="append", metavar="NAME", help="only check these package folders (repeatable)")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(0 if self_test() else 1)

    cfg = {}
    if Path(a.config).is_file():
        cfg = {k: v for k, v in json.loads(Path(a.config).read_text()).items() if not k.startswith("_")}
    root = Path(a.packages_dir)
    folders = sorted(p.parent.name for p in root.glob("*/PKGBUILD"))
    if a.only:
        folders = [f for f in folders if f in a.only]
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
        log = sync_issues(results, dry=a.dry_run, root=a.packages_dir, cfg=cfg)
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
