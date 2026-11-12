#!/usr/bin/env python3
"""
kaos_pkgbuild_report.py - inspect a PKGBUILD for porting to KaOS.

Run it ON KaOS (it asks your local pacman which names exist), in the folder
with the PKGBUILD:

    python3 kaos_pkgbuild_report.py                 # ./PKGBUILD
    python3 kaos_pkgbuild_report.py path/to/dir -o report.md

Then paste report.md back to Claude. The PKGBUILD is parsed as text only,
never executed (AUR scripts are untrusted).

What it reports:
  - package info, sources, checksum sanity, detected build system
  - every depends/makedepends/checkdepends/optdepends entry, checked against
    your enabled KaOS repos (in repo / provided by / MISSING) plus likely
    rename candidates for missing ones
  - KaOS-specific gotchas found in the script (sbin, qmake, python, Qt/KF naming)
  - environment (distro, pacman version, enabled repos)
  - the PKGBUILD itself (omit with --no-pkgbuild)
"""
import argparse
import difflib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

DEP_KEYS = ("depends", "makedepends", "checkdepends", "optdepends")
OTHER_ARRAYS = ("provides", "conflicts", "replaces", "source", "arch", "license")
SCALARS = ("pkgbase", "pkgname", "pkgver", "pkgrel", "epoch", "pkgdesc", "url", "install")

BUILD_HINTS = [
    ("cmake", r"\bcmake\b"),
    ("meson", r"\bmeson\b|\barch-meson\b"),
    ("qmake", r"\bqmake\w*\b"),
    ("cargo/rust", r"\bcargo\b"),
    ("go", r"\bgo (build|install)\b"),
    ("python", r"setup\.py|\bpip\b|python\d? -m (build|installer)"),
    ("autotools", r"\./configure|\bautoreconf\b"),
    ("zig", r"\bzig build\b"),
    ("node", r"\bnpm\b|\byarn\b|\bpnpm\b"),
    ("ninja", r"\bninja\b"),
    ("make", r"\bmake\b"),
]


# ---------------------------------------------------------------- parsing
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


def split_words(body):
    try:
        return shlex.split(body, posix=True)
    except ValueError:
        return body.split()


def parse_pkgbuild(raw):
    text = strip_comments(raw)
    arrays, scalars = {}, {}
    for m in re.finditer(r"^[ \t]*(\w+)(\+?)=\(", text, re.M):
        key = m.group(1)
        if key in DEP_KEYS + OTHER_ARRAYS or key.endswith("sums"):
            arrays.setdefault(key, []).extend(split_words(grab_array(text, m.end())))
    for key in SCALARS:
        m = re.search(rf"^{key}=(.*)$", text, re.M)
        if m:
            words = split_words(m.group(1))
            scalars[key] = words[0] if words else ""
    # pkgname may be an array
    if "pkgname" in arrays and "pkgname" not in scalars:
        scalars["pkgname"] = arrays["pkgname"][0]
    return arrays, scalars, text


def expand(s, scalars):
    def sub(m):
        return scalars.get(m.group(1) or m.group(2), m.group(0))
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, s)


def dep_name(entry, optional=False):
    if optional:
        entry = entry.split(":")[0]
    return re.split(r"[<>=]", entry.strip())[0].strip()


# ---------------------------------------------------------------- pacman
def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:  # noqa
        return 127, "", str(e)


def field(out, name):
    m = re.search(rf"^{name}\s*:\s*(.+)$", out, re.M)
    return m.group(1).strip() if m else ""


def check_dep(name):
    info = {"status": "missing", "repo": "", "version": "", "provider": "",
            "installed": run(["pacman", "-Q", name])[0] == 0}
    rc, out, _ = run(["pacman", "-Si", name])
    if rc == 0:
        info.update(status="in-repo", repo=field(out, "Repository"),
                    version=field(out, "Version"))
        return info
    rc, out, _ = run(["pacman", "-Sp", "--print-format", "%n", "--noconfirm", name])
    if rc == 0 and out:
        info.update(status="provided", provider=out.splitlines()[0])
    return info


def candidates(name, all_names):
    variants = {name, re.sub(r"[-_]?(git|svn|hg|bin)$", "", name),
                re.sub(r"\d+$", "", name), name + "6", name + "-python3"}
    if name.startswith("lib"):
        variants.add(name[3:])
    if name.startswith("python-"):
        rest = name[len("python-"):]
        variants.update({"python3-" + rest, rest + "-python3", rest})
    if re.match(r"qt[56]-", name):
        variants.add(re.sub(r"^qt[56]-", "", name))
        variants.add(re.sub(r"^qt5-", "qt6-", name))
    names = set(all_names)
    hits = [v for v in variants if v in names and v != name]
    hits += difflib.get_close_matches(name, all_names, n=5, cutoff=0.7)
    stem = re.sub(r"(\d+|-git|-bin)$", "", name).replace("lib", "", 1)
    if len(stem) >= 4:
        hits += sorted(n for n in all_names if stem in n)[:5]
    seen, out = set(), []
    for h in hits:
        if h not in seen and h != name:
            seen.add(h)
            out.append(h)
    return out[:6]


# ---------------------------------------------------------------- lint
def lint(raw):
    findings = []
    for n, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if re.search(r"sbin\w*=(/usr/)?bin|--sbindir=/usr/bin|SBINDIR=(/usr/)?bin", s):
            findings.append((n, s, "KaOS keeps /sbin and /usr/sbin; remove sbin=/usr/bin style settings"))
        if re.search(r"\bqmake\b", s) and "/usr/lib/qt" not in s and "depends" not in s:
            findings.append((n, s, "KaOS Qt versions are separate; use /usr/lib/qt5/bin/qmake (or the qt6 path)"))
        if re.search(r"(^|[\s'\"(])python([\s'\"<>=)]|$)", s) and "python3" not in s:
            findings.append((n, s, "bare 'python': KaOS uses python3"))
        if re.search(r"\bpyqt[56]?\b|python-pyqt|pyqt[56]-", s):
            findings.append((n, s, "KaOS PyQt names are pyqt5-python3 / pyqt6-python3"))
        if re.search(r"\b(kf5|KF5)\b|-DQT_MAJOR_VERSION|Qt5|Qt6", s):
            findings.append((n, s, "check Qt/KF major version; KaOS Qt6-era KDE Frameworks packages end in 6"))
        if re.search(r"/usr/lib/modules|/usr/lib/(firmware|kernel)|vmlinuz", s):
            findings.append((n, s, "kernel/modules live in /lib on KaOS, not /usr/lib with symlinks"))
        if re.search(r"archlinux|\bmakepkg-template\b|\barch-meson\b", s, re.I):
            findings.append((n, s, "Arch-specific helper/tool, may not exist on KaOS"))
    return findings


# ---------------------------------------------------------------- report
def build_report(path, no_pkgbuild, offline):
    raw = path.read_text(errors="replace")
    arrays, scalars, text = parse_pkgbuild(raw)
    sc = {k: v for k, v in scalars.items()}
    sc.setdefault("pkgname", "")

    have_pacman = bool(shutil.which("pacman")) and not offline
    all_names = []
    env = {"python": platform.python_version(), "machine": platform.machine()}
    try:
        osr = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text().splitlines() if "=" in l)
        env["os"] = osr.get("PRETTY_NAME", osr.get("NAME", "")).strip('"')
    except OSError:
        env["os"] = "unknown"
    if have_pacman:
        rc, out, _ = run(["pacman", "--version"])
        m = re.search(r"Pacman v[\w.\-]+", out)
        env["pacman"] = m.group(0) if m else "unknown"
        rc, out, _ = run(["pacman-conf", "--repo-list"])
        env["repos"] = out.split() if rc == 0 else []
        rc, out, _ = run(["pacman", "-Slq"])
        all_names = sorted(set(out.split())) if rc == 0 else []
        env["repo_package_count"] = len(all_names)

    # dependencies
    deps = {}
    for key in DEP_KEYS:
        for entry in arrays.get(key, []):
            entry = expand(entry, sc)
            name = dep_name(entry, key == "optdepends")
            if not name:
                continue
            d = deps.setdefault(name, {"name": name, "kinds": [], "raw": []})
            if key not in d["kinds"]:
                d["kinds"].append(key)
            d["raw"].append(entry)
    for name, d in deps.items():
        if have_pacman:
            d.update(check_dep(name))
            d["candidates"] = candidates(name, all_names) if d["status"] == "missing" else []
        else:
            d.update(status="unchecked", candidates=[])

    # sources / sums
    sources = [expand(s, sc) for s in arrays.get("source", [])]
    sums = {k: v for k, v in arrays.items() if k.endswith("sums")}
    sum_note = []
    for k, v in sums.items():
        sum_note.append(f"{k}: {len(v)} entries, {v.count('SKIP')} SKIP")
    if sources and not sums:
        sum_note.append("no checksum array found")
    for k, v in sums.items():
        if len(v) != len(sources):
            sum_note.append(f"WARNING {k} count ({len(v)}) != source count ({len(sources)})")

    # build system
    code = text
    build = [n for n, rx in BUILD_HINTS if re.search(rx, code)]

    data = {
        "file": str(path),
        "info": {k: scalars.get(k, "") for k in SCALARS},
        "arch": arrays.get("arch", []),
        "license": arrays.get("license", []),
        "build_systems": build,
        "sources": sources,
        "checksums": sum_note,
        "provides": arrays.get("provides", []),
        "conflicts": arrays.get("conflicts", []),
        "dependencies": list(deps.values()),
        "lint": [{"line": n, "text": t, "issue": m} for n, t, m in lint(raw)],
        "extra_files": sorted(p.name for p in path.parent.iterdir() if p.name != path.name and p.is_file()),
        "environment": env,
    }

    # markdown
    L = []
    L.append("# KaOS PKGBUILD report\n")
    i = data["info"]
    L.append("## Package")
    for k in ("pkgname", "pkgver", "pkgrel", "epoch", "pkgdesc", "url"):
        if i.get(k):
            L.append(f"- {k}: {i[k]}")
    L.append(f"- arch: {', '.join(data['arch']) or '?'}   license: {', '.join(data['license']) or '?'}")
    L.append(f"- build system(s) detected: {', '.join(build) or 'unknown'}")
    if data["provides"]:
        L.append(f"- provides: {', '.join(data['provides'])}")
    if data["conflicts"]:
        L.append(f"- conflicts: {', '.join(data['conflicts'])}")
    L.append("\n## Sources")
    L += [f"- {s}" for s in sources] or ["- none"]
    L += [f"- {s}" for s in sum_note]
    if data["extra_files"]:
        L.append(f"- other files in dir: {', '.join(data['extra_files'])}")

    L.append("\n## Dependencies")
    if not deps:
        L.append("none found")
    else:
        L.append("| name | type | status | detail |")
        L.append("|---|---|---|---|")
        for d in sorted(deps.values(), key=lambda x: (x["status"] != "missing", x["name"])):
            detail = ""
            if d["status"] == "in-repo":
                detail = f"{d['repo']} {d['version']}"
            elif d["status"] == "provided":
                detail = f"provided by {d['provider']}"
            elif d["status"] == "missing":
                detail = "candidates: " + (", ".join(d["candidates"]) or "none found")
            if d.get("installed"):
                detail += " (installed)"
            L.append(f"| {d['name']} | {'/'.join(d['kinds'])} | {d['status'].upper()} | {detail} |")
        missing = [d["name"] for d in deps.values() if d["status"] == "missing"]
        L.append(f"\nMissing: {', '.join(missing) if missing else 'none'}")

    L.append("\n## KaOS gotchas found in the script")
    L += [f"- line {x['line']}: `{x['text']}`\n  -> {x['issue']}" for x in data["lint"]] or ["- none detected"]

    L.append("\n## Environment")
    L.append(f"- os: {env.get('os')}   machine: {env.get('machine')}")
    if have_pacman:
        L.append(f"- {env.get('pacman')}; repos: {', '.join(env.get('repos', [])) or '?'}; "
                 f"{env.get('repo_package_count', 0)} packages in sync DBs")
        if env.get("repo_package_count", 0) == 0:
            L.append("- WARNING: sync DB empty, run `sudo pacman -Sy` and re-run")
        if "kaos" not in env.get("os", "").lower():
            L.append("- WARNING: this does not look like KaOS, so the repo results above may not apply")
    else:
        L.append("- pacman not available or --offline: dependency status is UNCHECKED")

    if not no_pkgbuild:
        L.append("\n## PKGBUILD\n```bash\n" + raw.rstrip() + "\n```")
    return "\n".join(L) + "\n", data


def main():
    ap = argparse.ArgumentParser(description="Report on a PKGBUILD for porting to KaOS")
    ap.add_argument("path", nargs="?", default=".", help="PKGBUILD file or its directory")
    ap.add_argument("-o", "--output", help="write report to this file as well as stdout")
    ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    ap.add_argument("--no-pkgbuild", action="store_true", help="do not append the PKGBUILD text")
    ap.add_argument("--offline", action="store_true", help="skip pacman checks")
    a = ap.parse_args()

    p = Path(a.path)
    if p.is_dir():
        p = p / "PKGBUILD"
    if not p.is_file():
        sys.exit(f"error: {p} not found")

    md, data = build_report(p, a.no_pkgbuild, a.offline)
    out = json.dumps(data, indent=2) if a.json else md
    print(out)
    if a.output:
        Path(a.output).write_text(out)
        print(f"\n[written to {a.output}]", file=sys.stderr)


if __name__ == "__main__":
    main()
