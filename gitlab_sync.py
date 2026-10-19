#!/usr/bin/env python3
"""Synchronisiert lokal ausgecheckte Repos von einem alten GitLab (git.example.com)
in das neue GitLab (gitlab.example.com).

# --> Offen: Repos, die in git erstellt wurden, aber noch nicht in giutlab

Pro Repo:
  1. fetch vom neuen GitLab (origin)
  2. altes GitLab als zusaetzlichen Remote anlegen (falls nicht vorhanden)
  3. fuer jeden Branch des alten GitLab:
       - fehlt im neuen  -> Branch anlegen
       - existiert       -> Fast-Forward, sonst Merge (bei Konflikt: abbrechen, loggen)
       - Push ins neue GitLab (im Dry-Run entfaellt nur der Push)
  4. neue Tags uebertragen (bestehende, abweichende Tags werden nie ueberschrieben)

Siehe README.md und config.example.yaml.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml

TAG_NS = "refs/sync-legacy-tags"


# --------------------------------------------------------------------------- config
@dataclass
class ApiPrecheck:
    enabled: bool = False
    new_token_env: str = "GITLAB_NEW_TOKEN"
    old_token_env: str = "GITLAB_OLD_TOKEN"
    scheme: str = "https"
    timeout: int = 15


@dataclass
class Config:
    root: Path
    new_host: str
    old_host: str
    new_remote: str = "origin"
    old_remote: str = "git-old"
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    exclude_branches: List[str] = field(default_factory=list)
    overwrite_tags: bool = False
    api_precheck: ApiPrecheck = field(default_factory=ApiPrecheck)
    log_file: Path = Path("gitlab-sync.log")
    dry_run: bool = False
    restore_branch: bool = True
    git_timeout: int = 600


def load_config(path: Path) -> Config:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    missing = [k for k in ("root", "new_host", "old_host") if not raw.get(k)]
    if missing:
        raise SystemExit(f"Config {path}: fehlende Pflichtfelder: {', '.join(missing)}")
    base = path.resolve().parent

    def p(value: str) -> Path:
        pp = Path(os.path.expandvars(os.path.expanduser(str(value))))
        return pp if pp.is_absolute() else base / pp

    return Config(
        root=p(raw["root"]),
        new_host=raw["new_host"],
        old_host=raw["old_host"],
        new_remote=raw.get("new_remote", "origin"),
        old_remote=raw.get("old_remote", "git-old"),
        include=list(raw.get("include") or []),
        exclude=list(raw.get("exclude") or []),
        exclude_branches=list(raw.get("exclude_branches") or []),
        overwrite_tags=bool(raw.get("overwrite_tags", False)),
        api_precheck=ApiPrecheck(**(raw.get("api_precheck") or {})),
        log_file=p(raw.get("log_file", "gitlab-sync.log")),
        dry_run=bool(raw.get("dry_run", False)),
        restore_branch=bool(raw.get("restore_branch", True)),
        git_timeout=int(raw.get("git_timeout", 600)),
    )


# --------------------------------------------------------------------------- logging
class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def line(self, depth: int, text: str) -> None:
        indent = "    " * depth + ("-> " if depth else "")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for part in text.splitlines() or [""]:
            msg = f"{indent}{part}"
            self._fh.write(f"{stamp} | {msg}\n")
            print(msg, flush=True)
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


@dataclass
class Stats:
    repos: int = 0
    repos_ok: int = 0
    commits: int = 0
    branches_new: int = 0
    branches_updated: int = 0
    branches_current: int = 0
    repos_skipped: int = 0
    tags_pushed: int = 0
    tags_overwritten: int = 0
    conflicts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- git
class GitError(Exception):
    pass


class Git:
    def __init__(self, cwd: Path, timeout: int):
        self.cwd = cwd
        self.timeout = timeout
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")

    def run(self, *args: str, check: bool = True, input: Optional[str] = None) -> str:
        try:
            r = subprocess.run(
                ["git", *args], cwd=self.cwd, env=self.env, input=input,
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]}: Timeout nach {self.timeout}s")
        if check and r.returncode != 0:
            msg = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")
            raise GitError(f"git {' '.join(args)}: {msg}")
        return r.stdout.strip() if r.returncode == 0 else ""

    def ok(self, *args: str) -> bool:
        try:
            subprocess.run(["git", *args], cwd=self.cwd, env=self.env, check=True,
                           capture_output=True, timeout=self.timeout)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    def try_out(self, *args: str) -> Optional[str]:
        out = self.run(*args, check=False)
        return out or None

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def count(self, rev_range: str) -> int:
        return int(self.run("rev-list", "--count", rev_range))


# --------------------------------------------------------------------------- urls
_SCP = re.compile(r"^(?P<user>[^@/:]+@)?(?P<host>[^:/]+):(?P<path>(?!//).*)$")


def derive_old_url(new_url: str, cfg: Config) -> str:
    """Ersetzt den Host der origin-URL durch den alten Host; Pfad bleibt gleich."""
    if "://" in new_url:
        s = urlsplit(new_url)
        userinfo = s.netloc.rpartition("@")[0]
        host = s.hostname or ""
        if host.lower() != cfg.new_host.lower():
            raise ValueError(f"origin-Host '{host}' ist nicht '{cfg.new_host}'")
        netloc = f"{userinfo}@{cfg.old_host}" if userinfo else cfg.old_host
        return urlunsplit((s.scheme, netloc, s.path, s.query, s.fragment))
    m = _SCP.match(new_url)
    if not m:
        raise ValueError(f"URL nicht erkannt: {new_url}")
    if m["host"].lower() != cfg.new_host.lower():
        raise ValueError(f"origin-Host '{m['host']}' ist nicht '{cfg.new_host}'")
    return f"{m['user'] or ''}{cfg.old_host}:{m['path']}"


# --------------------------------------------------------------------------- api precheck
class ApiError(Exception):
    pass


def project_path(url: str) -> str:
    """Projektpfad (group/sub/repo) aus einer Git-URL."""
    if "://" in url:
        path = urlsplit(url).path
    else:
        m = _SCP.match(url)
        if not m:
            raise ValueError(f"URL nicht erkannt: {url}")
        path = m["path"]
    path = path.strip("/")
    return path[:-4] if path.endswith(".git") else path


def _request(host: str, token: str, path: str, query: Dict[str, object],
             pc: ApiPrecheck):
    """GET gegen die GitLab-API v4. Gibt (status, json, header) zurueck; 404 ist kein Fehler."""
    url = f"{pc.scheme}://{host}/api/v4{path}?{urlencode(query)}"
    req = Request(url, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=pc.timeout) as resp:
            return resp.status, json.load(resp), resp.headers
    except HTTPError as e:
        if e.code == 404:
            return 404, None, e.headers
        raise ApiError(f"{host}{path}: HTTP {e.code}")
    except (URLError, OSError, ValueError) as e:
        raise ApiError(f"{host}{path}: {e}")


def _paged(host: str, token: str, path: str, pc: ApiPrecheck) -> Optional[list]:
    """Alle Seiten einer Listen-Abfrage; None bei 404."""
    out: list = []
    page = 1
    while page:
        status, data, hdr = _request(host, token, path, {"per_page": 100, "page": page}, pc)
        if status == 404:
            return None
        out.extend(data)
        nxt = hdr.get("X-Next-Page") if hdr else ""
        page = int(nxt) if nxt else 0
    return out


def api_precheck(new_url: str, cfg: Config) -> bool:
    """Prueft per API, ob das alte GitLab Branches/Tags/Commits hat, die im neuen fehlen.

    True = es gibt Neues (vollen Sync ausfuehren), False = nichts Neues.
    Wirft ApiError, wenn die Pruefung nicht moeglich ist (dann voller Sync).
    """
    pc = cfg.api_precheck
    new_token, old_token = os.environ.get(pc.new_token_env), os.environ.get(pc.old_token_env)
    if not new_token or not old_token:
        raise ApiError(f"Token fehlt (Umgebungsvariablen {pc.new_token_env} / {pc.old_token_env})")
    proj = quote(project_path(new_url), safe="")
    base = f"/projects/{proj}/repository"

    old_br = _paged(cfg.old_host, old_token, f"{base}/branches", pc)
    new_br = _paged(cfg.new_host, new_token, f"{base}/branches", pc)
    if old_br is None:
        raise ApiError(f"Projekt in {cfg.old_host} nicht gefunden")
    if new_br is None:
        raise ApiError(f"Projekt in {cfg.new_host} nicht gefunden")
    new_heads = {b["name"]: b["commit"]["id"] for b in new_br}

    for b in old_br:
        name, sha = b["name"], b["commit"]["id"]
        if any(fnmatch.fnmatchcase(name, pat) for pat in cfg.exclude_branches):
            continue
        if name not in new_heads:
            return True
        if new_heads[name] == sha:
            continue
        # Heads unterscheiden sich: ist der alte Head schon im neuen Branch enthalten?
        refs = _paged(cfg.new_host, new_token, f"{base}/commits/{sha}/refs", pc)
        if refs is None:
            return True  # Commit im neuen GitLab unbekannt
        if not any(r["type"] == "branch" and r["name"] == name for r in refs):
            return True

    def target(t: dict) -> str:
        return t.get("target") or t["commit"]["id"]

    old_tags = _paged(cfg.old_host, old_token, f"{base}/tags", pc) or []
    new_tags = {t["name"]: target(t) for t in _paged(cfg.new_host, new_token, f"{base}/tags", pc) or []}
    for t in old_tags:
        if t["name"] not in new_tags:
            return True
        if cfg.overwrite_tags and new_tags[t["name"]] != target(t):
            return True
    return False


# --------------------------------------------------------------------------- discovery
def discover(cfg: Config, only: List[str]) -> List[Path]:
    repos: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(cfg.root):
        dirnames.sort()
        if ".git" in dirnames or ".git" in filenames:
            dirnames[:] = []
            repos.append(Path(dirpath))
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    out = []
    for r in repos:
        rel = r.relative_to(cfg.root).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in cfg.exclude):
            continue
        if cfg.include and not any(fnmatch.fnmatch(rel, pat) for pat in cfg.include):
            continue
        if only and not any(fnmatch.fnmatch(rel, pat) for pat in only):
            continue
        out.append(r)
    return out


# --------------------------------------------------------------------------- sync
def sync_repo(path: Path, cfg: Config, log: Log, d: int, stats: Stats,
              old_url: Optional[str] = None) -> bool:
    """Synchronisiert ein Repo. Gibt True zurueck, wenn kein Fehler/Konflikt auftrat."""
    git = Git(path, cfg.git_timeout)
    new, old = cfg.new_remote, cfg.old_remote
    name = path.name
    problems_before = len(stats.errors) + len(stats.conflicts)
    created_local: List[str] = []
    orig_branch = git.try_out("symbolic-ref", "--short", "-q", "HEAD")
    orig_sha = git.try_out("rev-parse", "--verify", "-q", "HEAD")
    skipped = False

    def err(depth: int, msg: str) -> None:
        log.line(depth, f"[FEHLER] {msg}")
        stats.errors.append(f"{path}: {msg}")

    try:
        new_url = git.try_out("remote", "get-url", new)
        if not new_url:
            err(d + 1, f"Remote '{new}' nicht vorhanden - Repo uebersprungen")
            return False
        try:
            expected = old_url or derive_old_url(new_url, cfg)
        except ValueError as e:
            err(d + 1, f"{e} - Repo uebersprungen")
            return False

        if cfg.api_precheck.enabled:
            try:
                has_news = api_precheck(new_url, cfg)
            except (ApiError, ValueError) as e:
                log.line(d + 1, f"[WARN] API-Vorabpruefung nicht moeglich ({e}) - voller Sync")
                has_news = True
            if not has_news:
                log.line(d + 1, f"API-Vorabpruefung: nichts Neues in {cfg.old_host} - uebersprungen")
                stats.repos_skipped += 1
                skipped = True
                return True
            log.line(d + 1, f"API-Vorabpruefung: Aenderungen in {cfg.old_host} vorhanden")

        if git.run("status", "--porcelain", check=False):
            log.line(d + 1, "[WARN] lokale Aenderungen vorhanden - werden verworfen")
            git.run("reset", "--hard", check=False)
            git.run("clean", "-fdq", check=False)

        log.line(d + 1, f"Update {new} ({new_url})")
        git.run("fetch", new, "--prune")
        git.run("fetch", new, "--tags", "--force")

        current = git.try_out("remote", "get-url", old)
        if current is None:
            log.line(d + 1, f"Fuege {old} hinzu: {expected}")
            git.run("remote", "add", old, expected)
        elif current != expected:
            log.line(d + 1, f"[WARN] {old} zeigte auf {current} - setze auf {expected}")
            git.run("remote", "set-url", old, expected)
        else:
            log.line(d + 1, f"Remote {old} vorhanden ({expected})")

        log.line(d + 1, f"Update {old}")
        git.run("fetch", old, "--prune", "--no-tags")

        old_branches = [b for b in git.run(
            "for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{old}/").splitlines()
            if b and b != "HEAD"]
        new_branches = set(git.run(
            "for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{new}/").splitlines())
        only_new = new_branches - set(old_branches) - {"HEAD"}
        if not old_branches:
            log.line(d + 1, f"Keine Branches in {old} - nichts zu syncen")
        if only_new:
            log.line(d + 1, f"{len(only_new)} Branch(es) nur in {new} (bleiben unveraendert)")

        for b in old_branches:
            if any(fnmatch.fnmatchcase(b, pat) for pat in cfg.exclude_branches):
                log.line(d + 2, f"Branch {b}: uebersprungen (exclude_branches)")
                continue
            try:
                _sync_branch(git, cfg, log, d + 1, stats, b, created_local, path)
            except GitError as e:
                err(d + 2, f"Branch {b}: {e}")

        try:
            _sync_tags(git, cfg, log, d + 1, stats, path)
        except GitError as e:
            err(d + 2, f"Tags: {e}")
    except GitError as e:
        err(d + 1, str(e))
    finally:
        if not skipped:
            _restore(git, cfg, orig_branch, orig_sha, created_local)
    return len(stats.errors) + len(stats.conflicts) == problems_before


def _sync_branch(git: Git, cfg: Config, log: Log, d: int, stats: Stats, b: str,
                 created_local: List[str], repo: Path) -> None:
    new, old = cfg.new_remote, cfg.old_remote
    new_ref, old_ref = f"refs/remotes/{new}/{b}", f"refs/remotes/{old}/{b}"
    old_sha = git.run("rev-parse", old_ref)
    new_sha = git.try_out("rev-parse", "--verify", "-q", new_ref)
    dry = cfg.dry_run

    def push_branch() -> bool:
        if dry:
            log.line(d + 2, f"[DRY-RUN] Push von {b} nach {new} uebersprungen")
            return True
        try:
            git.run("push", "-u", new, f"refs/heads/{b}:refs/heads/{b}")
            log.line(d + 2, f"Push {b} -> {new} OK")
            return True
        except GitError as e:
            log.line(d + 2, f"[FEHLER] Push {b}: {e}")
            stats.errors.append(f"{repo}: Push {b}: {e}")
            return False

    if new_sha is None:
        n = git.count(old_ref)
        log.line(d, f"Branch {b}: neu in {new} ({n} Commits)")
        if not dry:
            git.run("checkout", "-f", "-B", b, old_ref, "--no-track")
            created_local.append(b)
        if push_branch():
            if not dry:
                created_local.remove(b)
            stats.branches_new += 1
            stats.commits += n
        return

    if new_sha == old_sha or git.is_ancestor(old_sha, new_sha):
        log.line(d, f"Branch {b}: bereits aktuell")
        stats.branches_current += 1
        return

    n = git.count(f"{new_ref}..{old_ref}")
    local = git.try_out("rev-parse", "--verify", "-q", f"refs/heads/{b}")
    if local and local != new_sha and not git.is_ancestor(local, new_sha):
        msg = f"lokaler Branch {b} hat Commits, die nicht in {new} sind - uebersprungen"
        log.line(d, f"[WARN] {msg}")
        stats.warnings.append(f"{repo}: {msg}")
        return

    log.line(d, f"Checkout Branch {b}")
    git.run("checkout", "-f", "-B", b, new_ref)
    if git.is_ancestor(new_sha, old_sha):
        mode = "Fast-Forward"
        git.run("merge", "--ff-only", old_ref)
    else:
        mode = "Merge"
        try:
            git.run("merge", "--no-edit", "-m", f"Sync {old} -> {new}: {b}", old_ref)
        except GitError as e:
            git.run("merge", "--abort", check=False)
            git.run("checkout", "-f", "-B", b, new_ref)
            msg = f"Branch {b}: KONFLIKT beim Merge von {old}/{b} ({n} Commits) - uebersprungen"
            log.line(d, f"[KONFLIKT] {msg}")
            stats.conflicts.append(f"{repo}: {msg}")
            return
    log.line(d, f"Synce {n} Commits {old} -> {new} ({mode})")
    ok = push_branch()
    if ok:
        stats.branches_updated += 1
        stats.commits += n
    if dry or not ok:
        git.run("checkout", "-f", "-B", b, new_ref)  # lokalen Stand zuruecksetzen


def _sync_tags(git: Git, cfg: Config, log: Log, d: int, stats: Stats, repo: Path) -> None:
    new, old = cfg.new_remote, cfg.old_remote
    git.run("fetch", old, "--no-tags", "--prune", f"+refs/tags/*:{TAG_NS}/*")
    try:
        lines = git.run("for-each-ref", "--format=%(refname) %(objectname)", f"{TAG_NS}/").splitlines()
        to_push: Dict[str, str] = {}   # Tag -> Ziel-SHA (aus dem alten GitLab)
        previous: Dict[str, Optional[str]] = {}  # Tag -> bisheriger lokaler/neuer Stand
        for ln in lines:
            ref, sha = ln.split()
            tag = ref[len(TAG_NS) + 1:]
            local = git.try_out("rev-parse", "--verify", "-q", f"refs/tags/{tag}")
            if local == sha:
                continue
            if local is not None and not cfg.overwrite_tags:
                msg = f"Tag {tag} unterscheidet sich zwischen {old} und {new} - nicht ueberschrieben"
                log.line(d, f"[WARN] {msg}")
                stats.warnings.append(f"{repo}: {msg}")
                continue
            to_push[tag] = sha
            previous[tag] = local
        if not to_push:
            log.line(d, "Tags: nichts zu syncen")
            return
        changed = sorted(t for t in to_push if previous[t] is not None)
        created = sorted(t for t in to_push if previous[t] is None)
        log.line(d, f"Tags: {len(created)} neue, {len(changed)} abweichende Tags {old} -> {new}")
        for t in changed:
            log.line(d + 1, f"Tag {t}: ueberschreibe {previous[t][:10]} -> {to_push[t][:10]}"
                            + (" [DRY-RUN]" if cfg.dry_run else ""))
        if cfg.dry_run:
            log.line(d + 1, f"[DRY-RUN] Push von {len(to_push)} Tags uebersprungen: " + ", ".join(sorted(to_push)))
            return
        for tag, sha in to_push.items():
            git.run("update-ref", f"refs/tags/{tag}", sha)
        tags = sorted(to_push)
        for i in range(0, len(tags), 200):
            chunk = tags[i:i + 200]
            try:
                # '+' erzwingt das Ueberschreiben abweichender Tags; neue Tags sind davon unberuehrt
                git.run("push", new, *[f"+refs/tags/{t}:refs/tags/{t}" if previous[t] else
                                       f"refs/tags/{t}:refs/tags/{t}" for t in chunk])
                stats.tags_pushed += sum(1 for t in chunk if previous[t] is None)
                stats.tags_overwritten += sum(1 for t in chunk if previous[t] is not None)
            except GitError as e:
                for t in chunk:  # lokalen Stand auf den Zustand vor dem Lauf zuruecksetzen
                    if previous[t] is None:
                        git.run("update-ref", "-d", f"refs/tags/{t}", check=False)
                    else:
                        git.run("update-ref", f"refs/tags/{t}", previous[t], check=False)
                raise e
        log.line(d + 1, f"Push von {len(tags)} Tags OK ({len(changed)} ueberschrieben)")
    finally:
        refs = git.run("for-each-ref", "--format=delete %(refname)", f"{TAG_NS}/", check=False)
        if refs:
            git.run("update-ref", "--stdin", input=refs + "\n", check=False)


def _restore(git: Git, cfg: Config, branch: Optional[str], sha: Optional[str],
             created_local: List[str]) -> None:
    try:
        if cfg.restore_branch:
            if branch:
                git.run("checkout", "-f", branch, check=False)
            elif sha:
                git.run("checkout", "-f", "--detach", sha, check=False)
        for b in created_local:  # lokale Branches, deren Push fehlschlug
            git.run("branch", "-D", b, check=False)
    except Exception:
        pass


# --------------------------------------------------------------------------- main
def run(cfg: Config, only: List[str]) -> int:
    log = Log(cfg.log_file)
    stats = Stats()
    started = datetime.now()
    try:
        mode = "DRY-RUN (kein Push)" if cfg.dry_run else "LIVE"
        log.line(0, f"=== gitlab-sync Start {started:%Y-%m-%d %H:%M:%S} - {mode} ===")
        if not cfg.root.is_dir():
            log.line(0, f"[FEHLER] Root-Verzeichnis nicht gefunden: {cfg.root}")
            return 2
        repos = discover(cfg, only)
        log.line(0, f"Synce Ordner {cfg.root} ({len(repos)} Repos)")
        stack: List[str] = []
        for repo in repos:
            parts = repo.relative_to(cfg.root).parts
            dirs = list(parts[:-1])
            common = 0
            while common < min(len(stack), len(dirs)) and stack[common] == dirs[common]:
                common += 1
            for i in range(common, len(dirs)):
                log.line(i + 1, f"Synce Ordner {dirs[i]}")
            stack = dirs
            depth = len(dirs) + 1
            log.line(depth, f"Repo {repo.name}")
            stats.repos += 1
            if sync_repo(repo, cfg, log, depth, stats):
                stats.repos_ok += 1
        log.line(0, "=== Zusammenfassung ===")
        log.line(0, f"Repos: {stats.repos} ({stats.repos_ok} ohne Probleme, "
                    f"{stats.repos_skipped} per API-Vorabpruefung uebersprungen)")
        log.line(0, f"Branches: {stats.branches_new} neu, {stats.branches_updated} aktualisiert, "
                    f"{stats.branches_current} aktuell; Commits gesynct: {stats.commits}; "
                    f"Tags: {stats.tags_pushed} neu, {stats.tags_overwritten} ueberschrieben")
        for title, items in (("Konflikte", stats.conflicts), ("Fehler", stats.errors),
                             ("Warnungen", stats.warnings)):
            if items:
                log.line(0, f"{title} ({len(items)}):")
                for it in items:
                    log.line(1, it)
        log.line(0, f"Dauer: {datetime.now() - started}")
        return 1 if (stats.errors or stats.conflicts) else 0
    finally:
        log.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default="config.yaml", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="alles ausser Push durchfuehren")
    ap.add_argument("--only", action="append", default=[], metavar="GLOB",
                    help="nur Repos, deren relativer Pfad passt (mehrfach moeglich)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True
    return run(cfg, args.only)


if __name__ == "__main__":
    sys.exit(main())
