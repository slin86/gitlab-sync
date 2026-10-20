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
import csv
import fnmatch
import os
import re
import json
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml
from loguru import logger

TAG_NS = "refs/sync-legacy-tags"
PROTECTED_MSG = "not allowed to push code to protected branches"


# --------------------------------------------------------------------------- config
@dataclass
class ApiPrecheck:
    enabled: bool = False
    new_token_env: str = "GITLAB_NEW_TOKEN"
    old_token_env: str = "GITLAB_OLD_TOKEN"
    scheme: str = "https"
    timeout: int = 15


@dataclass
class JenkinsRollback:
    enabled: bool = False
    committers: List[str] = field(default_factory=list)   # fnmatch-Globs auf Name bzw. E-Mail
    branches: List[str] = field(default_factory=lambda: ["main"])
    max_commits: int = 50
    instructions_file: str = "jenkins-rollback.md"


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
    jenkins_rollback: JenkinsRollback = field(default_factory=JenkinsRollback)
    log_file: Path = Path("gitlab-sync.log")
    conflicts_file: str = "branch_konflikts.txt"
    protected_branches_file: str = "protected_branches.md"
    protected_branches_page: str = "/-/settings/repository#js-protected-branches-settings"
    perf_file: str = "performance.csv"        # leer = keine CSV
    perf_top: int = 10                          # Anzahl langsamste Repos in der Auswertung
    slow_command_seconds: float = 30.0          # Einzelbefehle ab dieser Dauer werden gemeldet (0 = aus)
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
        jenkins_rollback=JenkinsRollback(**(raw.get("jenkins_rollback") or {})),
        log_file=p(raw.get("log_file", "gitlab-sync.log")),
        conflicts_file=str(raw.get("conflicts_file", "branch_konflikts.txt")),
        protected_branches_file=str(raw.get("protected_branches_file", "protected_branches.md")),
        protected_branches_page=str(raw.get("protected_branches_page",
                                            "/-/settings/repository#js-protected-branches-settings")),
        perf_file=str(raw.get("perf_file", "performance.csv") or ""),
        perf_top=int(raw.get("perf_top", 10)),
        slow_command_seconds=float(raw.get("slow_command_seconds", 30)),
        dry_run=bool(raw.get("dry_run", False)),
        restore_branch=bool(raw.get("restore_branch", True)),
        git_timeout=int(raw.get("git_timeout", 600)),
    )


# --------------------------------------------------------------------------- logging
def _console_format(record) -> str:
    """Konsole: nur die eingerueckte Meldung, Fehler rot, Warnungen/Konflikte gelb."""
    color = {"ERROR": "red", "CRITICAL": "red", "WARNING": "yellow"}.get(record["level"].name)
    return (f"<{color}>{{message}}</{color}>\n") if color else "{message}\n"


class Log:
    """Eingerueckte Protokollzeilen ueber loguru (Datei + Konsole).

    Der Level ergibt sich aus dem Praefix der Zeile: [FEHLER] -> ERROR, [WARN]/[KONFLIKT] -> WARNING,
    sonst INFO. Die Datei enthaelt Zeitstempel und Level; die Konsole nur die eingerueckte Meldung,
    nach Level eingefaerbt. colorize=None: NO_COLOR schaltet Farben ab, FORCE_COLOR erzwingt sie, sonst
    automatisch nur bei Terminal (nicht bei Pipe/Umleitung).
    """
    FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}"

    def __init__(self, path: Path, console: bool = True, colorize: Optional[bool] = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ids = [logger.add(path, format=self.FILE_FORMAT, colorize=False, encoding="utf-8")]
        if console:
            if colorize is None:
                if os.environ.get("NO_COLOR"):
                    colorize = False
                elif os.environ.get("FORCE_COLOR"):
                    colorize = True
            self._ids.append(logger.add(sys.stdout, format=_console_format, colorize=colorize))

    def line(self, depth: int, text: str) -> None:
        indent = "    " * depth + ("-> " if depth else "")
        if text.startswith("[FEHLER]"):
            level = "ERROR"
        elif text.startswith(("[WARN]", "[KONFLIKT]")):
            level = "WARNING"
        else:
            level = "INFO"
        for part in text.splitlines() or [""]:
            logger.log(level, f"{indent}{part}")

    def close(self) -> None:
        for i in self._ids:
            logger.remove(i)
        self._ids = []


@dataclass
class RollbackItem:
    """Manuell auszufuehrender Jenkins-Rollback (Anleitung), kein automatischer Eingriff."""
    repo: Path
    branch: str
    new_url: str
    new_sha: str             # aktueller Stand von branch im neuen GitLab (Lease)
    target_sha: str          # Stand vor den Jenkins-Commits
    dropped: List[Tuple[str, str, str]]   # (sha, committer, betreff), neueste zuerst
    tags: List[str]


@dataclass
class BranchConflict:
    """Branch, der nicht automatisch gemerged werden konnte."""
    repo: Path
    branch: str
    old_sha: str
    new_sha: str
    commits: int             # Commits aus dem alten GitLab, die im neuen fehlen
    jenkins: bool            # Jenkins-Rollback-Anleitung wurde erzeugt


@dataclass
class ProtectedPush:
    """Push auf einen geschuetzten Branch im neuen GitLab wurde abgelehnt."""
    repo: Path
    branch: str
    new_url: str


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
    rollbacks: List["RollbackItem"] = field(default_factory=list)
    branch_conflicts: List["BranchConflict"] = field(default_factory=list)
    protected_pushes: List["ProtectedPush"] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- performance
def fmt_secs(secs: float) -> str:
    if secs >= 3600:
        return f"{int(secs // 3600)}h{int(secs % 3600 // 60):02d}m"
    if secs >= 60:
        return f"{int(secs // 60)}m{int(secs % 60):02d}s"
    return f"{secs:.1f}s"


class Perf:
    """Misst die Dauer jedes git-Befehls und jedes API-Requests, global und pro Repo.

    Kategorien: `git <befehl>` (bei fetch/push mit Remote-Name, z.B. `git fetch origin`) und
    `api <host> <branches|tags|commit-refs>`. Es werden nie Argumente oder URLs gespeichert
    (koennten Zugangsdaten enthalten), nur Kategorie, Dauer und Repo.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self, slow: float = 30.0) -> None:
        self.slow = slow
        self.cat: Dict[str, List[float]] = {}                  # Kategorie -> [Anzahl, Summe, Max]
        self.repos: Dict[str, Dict[str, List[float]]] = {}     # Repo -> Kategorie -> [...]
        self.repo_total: Dict[str, float] = {}
        self.repo_status: Dict[str, str] = {}
        self.slow_calls: List[Tuple[float, str, Optional[str]]] = []
        self.repo: Optional[str] = None                        # aktuell bearbeitetes Repo

    def record(self, category: str, secs: float) -> None:
        targets = [self.cat]
        if self.repo is not None:
            targets.append(self.repos.setdefault(self.repo, {}))
        for t in targets:
            e = t.setdefault(category, [0, 0.0, 0.0])
            e[0] += 1
            e[1] += secs
            e[2] = max(e[2], secs)
        if self.slow > 0 and secs >= self.slow:
            self.slow_calls.append((secs, category, self.repo))
            logger.warning(f"[WARN] Langsamer Befehl: {category} {fmt_secs(secs)}"
                           + (f" ({self.repo})" if self.repo else ""))

    @contextmanager
    def timed(self, category: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record(category, time.perf_counter() - t0)

    @staticmethod
    def is_network(category: str) -> bool:
        return category.startswith(("api ", "git fetch ", "git push ", "git ls-remote"))

    def network_secs(self, cats: Dict[str, List[float]]) -> float:
        return sum(v[1] for k, v in cats.items() if self.is_network(k))


PERF = Perf()


def _git_category(args: Tuple[str, ...]) -> str:
    """`git fetch origin`, `git push git-old`, sonst `git <befehl>`."""
    if args[0] in ("fetch", "push", "ls-remote", "clone"):
        for a in args[1:]:
            if not a.startswith("-"):
                return f"git {args[0]} {a}"
    return f"git {args[0]}"


def perf_group_secs(cfg: Config, cats: Dict[str, List[float]]) -> Dict[str, float]:
    g = {"fetch_new_s": 0.0, "fetch_old_s": 0.0, "push_s": 0.0, "api_s": 0.0, "git_other_s": 0.0}
    for k, v in cats.items():
        if k == f"git fetch {cfg.new_remote}":
            g["fetch_new_s"] += v[1]
        elif k == f"git fetch {cfg.old_remote}":
            g["fetch_old_s"] += v[1]
        elif k.startswith("git push "):
            g["push_s"] += v[1]
        elif k.startswith("api "):
            g["api_s"] += v[1]
        else:
            g["git_other_s"] += v[1]
    return g


def log_perf_report(cfg: Config, log: Log, total: float) -> None:
    p = PERF
    all_secs = sum(v[1] for v in p.cat.values())
    net = p.network_secs(p.cat)
    log.line(0, "=== Performance ===")
    log.line(0, f"Gesamtdauer {fmt_secs(total)}; Zeit in git/API {fmt_secs(all_secs)}, davon Netzwerk "
                f"(fetch/push/API) {fmt_secs(net)}" + (f" = {net / all_secs:.0%}" if all_secs else ""))
    if p.cat:
        log.line(0, "Zeit pro Befehlsart (nach Summe):")
        for k, (n, tot, mx) in sorted(p.cat.items(), key=lambda kv: -kv[1][1]):
            log.line(1, f"{k:<34} n={int(n):<5} Summe={fmt_secs(tot):<8} "
                        f"Schnitt={fmt_secs(tot / n):<7} Max={fmt_secs(mx)}")
    if p.repo_total and cfg.perf_top > 0:
        log.line(0, f"Langsamste Repos (Top {min(cfg.perf_top, len(p.repo_total))}):")
        for repo, tot in sorted(p.repo_total.items(), key=lambda kv: -kv[1])[:cfg.perf_top]:
            cats = p.repos.get(repo, {})
            g = perf_group_secs(cfg, cats)
            log.line(1, f"{repo}: {fmt_secs(tot)} (Netzwerk {fmt_secs(p.network_secs(cats))}: "
                        f"fetch {cfg.new_remote} {fmt_secs(g['fetch_new_s'])}, "
                        f"fetch {cfg.old_remote} {fmt_secs(g['fetch_old_s'])}, "
                        f"push {fmt_secs(g['push_s'])}, API {fmt_secs(g['api_s'])})")
    if p.slow_calls:
        log.line(0, f"Einzelbefehle ab {fmt_secs(cfg.slow_command_seconds)} ({len(p.slow_calls)}):")
        for secs, cat, repo in sorted(p.slow_calls, reverse=True)[:20]:
            log.line(1, f"{fmt_secs(secs)}  {cat}  {repo or ''}")


def append_perf_csv(cfg: Config, started: datetime) -> Optional[Path]:
    """Haengt eine Zeile pro Repo an (Spalten fuer Excel/Auswertung); leer konfiguriert = aus."""
    if not cfg.perf_file or not PERF.repo_total:
        return None
    path = Path(cfg.perf_file)
    if not path.is_absolute():
        path = cfg.log_file.parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    cols = ["run_start", "repo", "status", "total_s", "network_s", "fetch_new_s", "fetch_old_s",
            "push_s", "api_s", "git_other_s"]
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(cols)
        for repo, tot in PERF.repo_total.items():
            cats = PERF.repos.get(repo, {})
            g = perf_group_secs(cfg, cats)
            w.writerow([f"{started:%Y-%m-%d %H:%M:%S}", repo, PERF.repo_status.get(repo, ""),
                        f"{tot:.2f}", f"{PERF.network_secs(cats):.2f}"]
                       + [f"{g[c]:.2f}" for c in cols[5:]])
    return path


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
            with PERF.timed(_git_category(args)):
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
            with PERF.timed(_git_category(args)):
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
    kind = "tags" if path.endswith("/tags") else "branches" if path.endswith("/branches") else "commit-refs"
    try:
        with PERF.timed(f"api {host} {kind}"):
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
            if PROTECTED_MSG in str(e):
                stats.protected_pushes.append(ProtectedPush(repo, b, git.run("remote", "get-url", new)))
                log.line(d + 2, "Hinweis: Branch ist im neuen GitLab geschuetzt - Link steht in "
                                f"{cfg.protected_branches_file}")
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
        except GitError:
            git.run("merge", "--abort", check=False)
            git.run("checkout", "-f", "-B", b, new_ref)
            found = _find_jenkins_rollback(git, cfg, log, d, b, new_ref, old_ref)
            git.run("checkout", "-f", "-B", b, new_ref)  # nichts am Branch aendern
            stats.branch_conflicts.append(BranchConflict(repo, b, old_sha, new_sha, n, bool(found)))
            msg = f"Branch {b}: KONFLIKT beim Merge von {old}/{b} ({n} Commits) - uebersprungen"
            if found:
                dropped, target, tags = found
                stats.rollbacks.append(RollbackItem(
                    repo, b, git.run("remote", "get-url", new), new_sha, target, dropped, tags))
                msg += (f"; {len(dropped)} Jenkins-Commits blockieren den Merge - Anleitung fuer "
                        f"manuellen Rollback wird nach {cfg.jenkins_rollback.instructions_file} geschrieben")
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


def _is_jenkins(git: Git, sha: str, patterns: List[str]) -> bool:
    name, _, email = git.run("show", "-s", "--format=%cn%x00%ce", sha).partition("\0")
    return any(fnmatch.fnmatchcase(v.lower(), pat.lower())
               for pat in patterns for v in (name.strip(), email.strip()))


def _find_jenkins_rollback(git: Git, cfg: Config, log: Log, d: int, b: str, new_ref: str,
                           old_ref: str) -> Optional[Tuple[List[Tuple[str, str, str]], str, List[str]]]:
    """Ermittelt, ob ein Merge-Konflikt auf b an Jenkins-Commits im neuen GitLab liegt.

    Die obersten Commits (First-Parent-Kette) von new_ref werden gedanklich einzeln entfernt, solange
    der Committer Jenkins ist, bis der Merge von old_ref klappt (Probe-Merge, nur lokal). Es wird nichts
    veraendert: der Aufrufer setzt den lokalen Branch zurueck und nichts wird gepusht.

    Rueckgabe: (verworfene Commits [(sha, committer, betreff)], Ziel-SHA, Tags darauf) oder None,
    wenn kein reiner Jenkins-Rollback moeglich ist.
    """
    rb = cfg.jenkins_rollback
    if not rb.enabled or b not in rb.branches or not rb.committers:
        return None
    base = git.try_out("merge-base", new_ref, old_ref)
    if base is None:
        return None
    dropped: List[Tuple[str, str, str]] = []
    for sha in git.run("rev-list", "--first-parent", f"{base}..{new_ref}").splitlines():
        who = git.run("show", "-s", "--format=%cn <%ce>", sha)
        if not _is_jenkins(git, sha, rb.committers):
            log.line(d + 1, f"Kein Jenkins-Rollback moeglich: Commit {sha[:10]} ({who}) ist nicht von Jenkins")
            return None
        if len(dropped) >= rb.max_commits:
            log.line(d + 1, f"Kein Jenkins-Rollback moeglich: mehr als {rb.max_commits} Jenkins-Commits")
            return None
        dropped.append((sha, who, git.run("show", "-s", "--format=%s", sha)))
        target = git.run("rev-parse", f"{sha}^")
        git.run("checkout", "-f", "-B", b, target)
        try:
            git.run("merge", "--no-edit", "-m", f"Sync {cfg.old_remote} -> {cfg.new_remote}: {b}", old_ref)
        except GitError:
            git.run("merge", "--abort", check=False)
            continue
        tags = sorted({t for c, _, _ in dropped for t in git.run("tag", "--points-at", c).splitlines()})
        return dropped, target, tags
    return None


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


# --------------------------------------------------------------------------- instructions
def _api_base(cfg: Config, item: RollbackItem) -> str:
    try:
        proj = quote(project_path(item.new_url), safe="")
    except ValueError:
        proj = "GRUPPE%2FPROJEKT"
    return f"{cfg.api_precheck.scheme}://{cfg.new_host}/api/v4/projects/{proj}/protected_branches"


def render_instructions(cfg: Config, items: List[RollbackItem]) -> str:
    tok = cfg.api_precheck.new_token_env
    out = [
        "# Jenkins-Rollback: manuelle Schritte",
        "",
        f"Erzeugt von gitlab-sync am {datetime.now():%Y-%m-%d %H:%M:%S}"
        + (" (Dry-Run, es wurde nichts veraendert)" if cfg.dry_run else "") + ".",
        "",
        f"In diesen Repos scheitert der Merge aus {cfg.old_host} an Commits von Jenkins im neuen GitLab.",
        "Pro Repo: Bloecke der Reihe nach kopieren und im Terminal einfuegen (A, B, C).",
        "",
        "Voraussetzungen:",
        f"- `{tok}` ist ein Token des neuen GitLab mit Scope `api` und mindestens Rolle Maintainer,",
        "  z.B. `export " + tok + "=...` (nicht in die Shell-History schreiben).",
        "- Die SHAs gelten fuer den Stand zum Zeitpunkt der Erzeugung. Hat jemand inzwischen auf den Branch",
        "  gepusht, bricht der Force-Push durch `--force-with-lease` ab; dann gitlab-sync neu laufen lassen.",
        "- Block B setzt den Branch hart zurueck und loescht die Jenkins-Commits im neuen GitLab. Jenkins",
        "  baut nach dem Push neu. Tags auf den entfernten Commits bleiben bestehen.",
        "- Block A: `PATCH` auf protected_branches gibt es ab GitLab 15.6. Bei aelteren Versionen den",
        "  Schutz stattdessen per `DELETE` entfernen und mit `POST ...?name=<branch>&allow_force_push=true`",
        "  neu anlegen (Zugriffslevel vorher per `GET` notieren). `404` bei A/C: Branch ist nicht geschuetzt,",
        "  dann den API-Schritt ueberspringen.",
        "",
    ]
    for it in items:
        rel = it.repo.relative_to(cfg.root).as_posix() if cfg.root in it.repo.parents else str(it.repo)
        br = quote(it.branch, safe="")
        url = f"{_api_base(cfg, it)}/{br}"
        q = shlex.quote
        out += [
            f"## {rel} - Branch `{it.branch}`", "",
            f"Jenkins-Commits auf {cfg.new_remote}/{it.branch} (neueste zuerst):", "",
        ]
        out += [f"- `{sha[:10]}` {subj} ({who})" for sha, who, subj in it.dropped]
        if it.tags:
            out += ["", "Tags auf diesen Commits (bleiben bestehen): " + ", ".join(f"`{t}`" for t in it.tags)]
        out += [
            "", "**A) Force-Push auf dem Branch erlauben (API)**", "",
            "```bash",
            f': "${{{tok}:?bitte {tok} setzen}}"',
            f'curl -sS -H "PRIVATE-TOKEN: ${tok}" {q(url)}   # aktuellen Schutz anzeigen',
            f'curl -sS -w "\\nHTTP %{{http_code}}\\n" -X PATCH -H "PRIVATE-TOKEN: ${tok}" {q(url + "?allow_force_push=true")}',
            "```", "",
            "**B) Rollback, Merge und Force-Push (git)**", "",
            "```bash",
            f"cd {q(str(it.repo))} \\",
            f"  && git fetch {q(cfg.new_remote)} \\",
            f"  && git fetch {q(cfg.old_remote)} \\",
            f"  && git checkout -f -B {q(it.branch)} {q(cfg.new_remote + '/' + it.branch)} \\",
            f"  && git reset --hard {it.target_sha} \\",
            f"  && git merge --no-edit {q(cfg.old_remote + '/' + it.branch)} \\",
            f"  && git push --force-with-lease={q('refs/heads/' + it.branch + ':' + it.new_sha)} "
            f"{q(cfg.new_remote)} {q(it.branch)}",
            "```", "",
            "**C) Force-Push wieder verbieten (API)**", "",
            "```bash",
            f'curl -sS -w "\\nHTTP %{{http_code}}\\n" -X PATCH -H "PRIVATE-TOKEN: ${tok}" {q(url + "?allow_force_push=false")}',
            "```", "",
        ]
    return "\n".join(out)


def write_instructions(cfg: Config, items: List[RollbackItem]) -> Optional[Path]:
    """Schreibt die gesammelten Anleitungen (ueberschreibt eine alte Datei); None, wenn keine noetig."""
    if not items:
        return None
    path = Path(cfg.jenkins_rollback.instructions_file)
    if not path.is_absolute():
        path = cfg.log_file.parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_instructions(cfg, items), encoding="utf-8")
    return path


def append_branch_conflicts(cfg: Config, items: List[BranchConflict]) -> Optional[Path]:
    """Haengt Branches, die nicht gemerged werden konnten, an die Konfliktdatei an.

    Eine Zeile pro Branch. Ein Eintrag mit gleichem Repo, Branch und gleichen Staenden steht schon
    in der Datei, wenn derselbe Konflikt bei einem frueheren Lauf erfasst wurde; er wird nicht doppelt
    angehaengt. Gibt den Pfad zurueck, None wenn es nichts zu schreiben gab.
    """
    if not items:
        return None
    path = Path(cfg.conflicts_file)
    if not path.is_absolute():
        path = cfg.log_file.parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    known = set()
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            known.add(ln.partition(" | ")[2])
    new_lines = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for it in items:
        rel = it.repo.relative_to(cfg.root).as_posix() if cfg.root in it.repo.parents else str(it.repo)
        entry = (f"{rel} | {it.branch} | {cfg.old_remote}={it.old_sha[:10]} | "
                 f"{cfg.new_remote}={it.new_sha[:10]} | {it.commits} Commits"
                 + (" | Jenkins-Rollback-Anleitung" if it.jenkins else ""))
        if entry not in known:
            known.add(entry)
            new_lines.append(f"{stamp} | {entry}")
    if new_lines:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(new_lines) + "\n")
    return path


def protected_branches_url(cfg: Config, new_url: str) -> str:
    """Link zur Seite 'Geschuetzte Branches' (Settings > Repository) des Projekts im neuen GitLab."""
    return (f"{cfg.api_precheck.scheme}://{cfg.new_host}/{project_path(new_url)}"
            f"{cfg.protected_branches_page}")


def append_protected_branches(cfg: Config, items: List[ProtectedPush]) -> Optional[Path]:
    """Haengt Repos, in die nicht auf einen geschuetzten Branch gepusht werden durfte, als Linkliste an.

    Eine Zeile pro Repo und Branch (`- [pfad](link) - Branch main`), ohne Duplikate bei erneutem Lauf.
    Der Link fuehrt direkt zu den geschuetzten Branches; dort muss die Rolle Maintainer unter
    'Allowed to push and merge' eingetragen werden. Gibt den Pfad zurueck, None wenn nichts zu schreiben war.
    """
    if not items:
        return None
    path = Path(cfg.protected_branches_file)
    if not path.is_absolute():
        path = cfg.log_file.parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    known = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
    lines = []
    if not known:
        lines += [
            "# Repos mit geschuetztem Branch im neuen GitLab",
            "",
            "Push abgelehnt (\"You are not allowed to push code to protected branches\"). Link oeffnen, unter",
            "\"Protected branches\" beim Branch in \"Allowed to push and merge\" die Rolle Maintainer erlauben,",
            "danach gitlab-sync erneut laufen lassen. Erledigte Zeilen koennen geloescht werden.",
            "",
        ]
    for it in items:
        rel = it.repo.relative_to(cfg.root).as_posix() if cfg.root in it.repo.parents else str(it.repo)
        try:
            link = protected_branches_url(cfg, it.new_url)
        except ValueError:
            link = it.new_url  # kein URL-Format erkannt: Remote-URL unveraendert angeben
        line = f"- [{rel}]({link}) - Branch {it.branch}"
        if line not in known:
            known.add(line)
            lines.append(line)
    if lines:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------- main
def run(cfg: Config, only: List[str]) -> int:
    log = Log(cfg.log_file)
    stats = Stats()
    started = datetime.now()
    t_start = time.perf_counter()
    PERF.reset(cfg.slow_command_seconds)
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
            rel = repo.relative_to(cfg.root).as_posix()
            PERF.repo = rel
            skipped_before = stats.repos_skipped
            t_repo = time.perf_counter()
            ok = sync_repo(repo, cfg, log, depth, stats)
            elapsed = time.perf_counter() - t_repo
            PERF.repo_total[rel] = elapsed
            PERF.repo_status[rel] = ("skipped-api" if stats.repos_skipped > skipped_before
                                     else "ok" if ok else "problem")
            log.line(depth + 1, f"Dauer {fmt_secs(elapsed)} (Netzwerk "
                                f"{fmt_secs(PERF.network_secs(PERF.repos.get(rel, {})))})")
            PERF.repo = None
            if ok:
                stats.repos_ok += 1
        log.line(0, "=== Zusammenfassung ===")
        log.line(0, f"Repos: {stats.repos} ({stats.repos_ok} ohne Probleme, "
                    f"{stats.repos_skipped} per API-Vorabpruefung uebersprungen)")
        log.line(0, f"Branches: {stats.branches_new} neu, {stats.branches_updated} aktualisiert, "
                    f"{stats.branches_current} aktuell; Commits gesynct: {stats.commits}; "
                    f"Tags: {stats.tags_pushed} neu, {stats.tags_overwritten} ueberschrieben")
        conflicts_path = append_branch_conflicts(cfg, stats.branch_conflicts)
        if conflicts_path:
            log.line(0, f"Branch-Konflikte: {len(stats.branch_conflicts)} Branch(es) in {conflicts_path}")
        protected_path = append_protected_branches(cfg, stats.protected_pushes)
        if protected_path:
            log.line(0, f"Geschuetzte Branches: {len(stats.protected_pushes)} Push(es) abgelehnt, "
                        f"Links in {protected_path}")
        written = write_instructions(cfg, stats.rollbacks)
        if written:
            log.line(0, f"Jenkins-Rollback: Anleitung fuer {len(stats.rollbacks)} Branch(es) in {written}")
        for title, items in (("Konflikte", stats.conflicts), ("Fehler", stats.errors),
                             ("Warnungen", stats.warnings)):
            if items:
                log.line(0, f"{title} ({len(items)}):")
                for it in items:
                    log.line(1, it)
        total = time.perf_counter() - t_start
        log_perf_report(cfg, log, total)
        perf_path = append_perf_csv(cfg, started)
        if perf_path:
            log.line(0, f"Performance je Repo in {perf_path}")
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
    logger.remove()  # loguru-Standardhandler (stderr) entfernen; Log richtet Datei und Konsole ein
    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True
    return run(cfg, args.only)


if __name__ == "__main__":
    sys.exit(main())
