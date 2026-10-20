import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gitlab_sync as gs


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, fname, content, msg, who="t"):
    (Path(repo) / fname).write_text(content)
    git(repo, "add", "-A")
    git(repo, "-c", f"user.name={who}", "-c", f"user.email={who}@t", "commit", "-qm", msg)


class UrlTests(unittest.TestCase):
    cfg = gs.Config(root=Path("."), new_host="gitlab.example.com", old_host="git.example.com")

    def test_scp(self):
        self.assertEqual(gs.derive_old_url("git@gitlab.example.com:a/b/c.git", self.cfg),
                         "git@git.example.com:a/b/c.git")

    def test_https(self):
        self.assertEqual(gs.derive_old_url("https://gitlab.example.com/a/b.git", self.cfg),
                         "https://git.example.com/a/b.git")

    def test_ssh_url_with_user(self):
        self.assertEqual(gs.derive_old_url("ssh://git@gitlab.example.com/a/b.git", self.cfg),
                         "ssh://git@git.example.com/a/b.git")

    def test_wrong_host(self):
        with self.assertRaises(ValueError):
            gs.derive_old_url("https://other.com/a.git", self.cfg)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.old, self.new = t / "old.git", t / "new.git"
        seed = t / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        commit(seed, "a", "1", "c1")
        git(seed, "branch", "diverge")
        git(seed, "tag", "v1")
        for bare in (self.old, self.new):
            git(t, "clone", "-q", "--bare", str(seed), str(bare))
        # lokale Arbeitskopie vom "neuen" GitLab
        self.root = t / "work"
        (self.root / "grp").mkdir(parents=True)
        self.repo = self.root / "grp" / "repo"
        git(t, "clone", "-q", str(self.new), str(self.repo))
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@t")
        # Entwicklung im alten GitLab
        w = t / "oldwork"
        git(t, "clone", "-q", str(self.old), str(w))
        commit(w, "a", "2", "old main")
        commit(w, "b", "x", "old main 2")
        git(w, "checkout", "-q", "-b", "feature")
        commit(w, "f", "f", "feature")
        git(w, "checkout", "-q", "diverge")
        commit(w, "a", "old", "old diverge")  # konfligiert mit new
        git(w, "tag", "v2")
        git(w, "push", "-q", "origin", "--all")
        git(w, "push", "-q", "origin", "--tags")
        # Entwicklung im neuen GitLab (divergierender Branch)
        n = t / "newwork"
        git(t, "clone", "-q", str(self.new), str(n))
        git(n, "checkout", "-q", "diverge")
        commit(n, "a", "new", "new diverge")
        git(n, "push", "-q", "origin", "diverge")
        self.log = t / "sync.log"
        self.cfg = gs.Config(root=self.root, new_host="x", old_host="y", old_remote="git-old",
                             log_file=self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, dry):
        self.cfg.dry_run = dry
        log = gs.Log(self.log)
        stats = gs.Stats()
        ok = gs.sync_repo(self.repo, self.cfg, log, 1, stats, old_url=str(self.old))
        log.close()
        return ok, stats

    def heads(self, bare):
        return git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads")

    def test_dry_run_pushes_nothing_and_leaves_clean_state(self):
        before = git(self.new, "for-each-ref")
        ok, stats = self.run_sync(True)
        self.assertEqual(before, git(self.new, "for-each-ref"))
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertNotIn("feature", git(self.repo, "branch", "--list").split())
        self.assertFalse(ok)  # Konflikt auf 'diverge'
        self.assertEqual(len(stats.conflicts), 1)

    def test_live_sync(self):
        ok, stats = self.run_sync(False)
        self.assertFalse(ok)
        self.assertEqual(len(stats.conflicts), 1)
        self.assertEqual(stats.errors, [])
        # feature neu, main fast-forward, diverge unveraendert wegen Konflikt
        self.assertIn("feature", self.heads(self.new).split())
        self.assertEqual(git(self.new, "rev-parse", "main"), git(self.old, "rev-parse", "main"))
        self.assertEqual(git(self.new, "show", "diverge:a"), "new")
        self.assertIn("v2", git(self.new, "tag").split())
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        # zweiter Lauf: nichts mehr zu tun ausser dem Konflikt
        ok2, stats2 = self.run_sync(False)
        self.assertEqual(stats2.commits, 0)
        self.assertEqual(len(stats2.conflicts), 1)

    def test_merge_non_conflicting_divergence(self):
        t = Path(self.tmp.name)
        w = t / "w2"
        git(t, "clone", "-q", str(self.new), str(w))
        commit(w, "n", "n", "new main")
        git(w, "push", "-q", "origin", "main")
        self.run_sync(False)
        git(self.new, "show", "main:n")
        git(self.new, "show", "main:b")  # beide Seiten enthalten

    def test_exclude_branches(self):
        self.cfg.exclude_branches = ["feat*"]
        self.run_sync(False)
        self.assertNotIn("feature", self.heads(self.new).split())
        self.assertEqual(git(self.new, "rev-parse", "main"), git(self.old, "rev-parse", "main"))

    def test_up_to_date_skips_repo_entirely(self):
        self.cfg.api_precheck = gs.ApiPrecheck(enabled=True)
        before = git(self.new, "for-each-ref")
        with mock.patch.object(gs, "api_precheck", return_value=False):
            ok, stats = self.run_sync(False)
        self.assertTrue(ok)
        self.assertEqual(stats.repos_skipped, 1)
        self.assertEqual(before, git(self.new, "for-each-ref"))
        self.assertNotIn("git-old", git(self.repo, "remote").split())

    def test_api_error_falls_back_to_full_sync(self):
        self.cfg.api_precheck = gs.ApiPrecheck(enabled=True)
        with mock.patch.object(gs, "api_precheck", side_effect=gs.ApiError("boom")):
            ok, stats = self.run_sync(False)
        self.assertEqual(stats.repos_skipped, 0)
        self.assertIn("feature", self.heads(self.new).split())

    def test_has_news_runs_full_sync(self):
        self.cfg.api_precheck = gs.ApiPrecheck(enabled=True)
        with mock.patch.object(gs, "api_precheck", return_value=True):
            self.run_sync(False)
        self.assertIn("feature", self.heads(self.new).split())

    def _move_old_tag(self):
        t = Path(self.tmp.name)
        w = t / "tagmove"
        git(t, "clone", "-q", str(self.old), str(w))
        commit(w, "t", "t", "tag target")
        git(w, "tag", "-f", "v1")
        git(w, "push", "-q", "--force", "origin", "v1")
        return git(w, "rev-parse", "v1")

    def test_diverging_tag_not_overwritten_by_default(self):
        old_v1 = self._move_old_tag()
        new_v1 = git(self.new, "rev-parse", "v1")
        ok, stats = self.run_sync(False)
        self.assertEqual(git(self.new, "rev-parse", "v1"), new_v1)
        self.assertNotEqual(new_v1, old_v1)
        self.assertTrue(any("v1" in w for w in stats.warnings))

    def test_diverging_tag_overwritten(self):
        old_v1 = self._move_old_tag()
        self.cfg.overwrite_tags = True
        ok, stats = self.run_sync(False)
        self.assertEqual(git(self.new, "rev-parse", "v1"), old_v1)
        self.assertEqual(stats.tags_overwritten, 1)
        self.assertEqual(git(self.repo, "rev-parse", "v1"), old_v1)
        # zweiter Lauf: nichts mehr zu ueberschreiben
        ok, stats2 = self.run_sync(False)
        self.assertEqual(stats2.tags_overwritten, 0)

    def test_diverging_tag_dry_run(self):
        self._move_old_tag()
        new_v1 = git(self.new, "rev-parse", "v1")
        self.cfg.overwrite_tags = True
        self.run_sync(True)
        self.assertEqual(git(self.new, "rev-parse", "v1"), new_v1)

    # --- Jenkins-Rollback -------------------------------------------------------------
    def _push_new_main(self, *commits):
        """Commits (datei, inhalt, committer) auf main im neuen GitLab; Datei 'a' konfligiert mit old."""
        t = Path(self.tmp.name)
        w = t / f"jwork{len(list(t.glob('jwork*')))}"
        git(t, "clone", "-q", str(self.new), str(w))
        for fname, content, who in commits:
            commit(w, fname, content, f"{who} {fname}", who)
        git(w, "push", "-q", "origin", "main")

    def _enable_rollback(self, **kw):
        self.cfg.jenkins_rollback = gs.JenkinsRollback(enabled=True, committers=["jenkins*"], **kw)

    def _instructions(self, stats):
        path = gs.write_instructions(self.cfg, stats.rollbacks)
        return path, (path.read_text() if path else "")

    def _block(self, text, marker):
        """Inhalt des ```bash-Blocks direkt unter der Ueberschrift mit marker."""
        part = text.split(marker, 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]
        return part

    def test_jenkins_conflict_creates_instructions_and_changes_nothing(self):
        self._push_new_main(("a", "jenkins1", "jenkins"), ("b2", "x", "jenkins"))
        before = git(self.new, "rev-parse", "main")
        self._enable_rollback()
        ok, stats = self.run_sync(False)
        self.assertFalse(ok)
        self.assertEqual(git(self.new, "rev-parse", "main"), before)  # kein Force-Push
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)  # lokal unveraendert
        self.assertEqual(len(stats.rollbacks), 1)
        item = stats.rollbacks[0]
        self.assertEqual([c[1].split(" <")[0] for c in item.dropped], ["jenkins", "jenkins"])
        self.assertEqual(item.new_sha, before)
        path, text = self._instructions(stats)
        self.assertEqual(path.name, "jenkins-rollback.md")
        self.assertIn("protected_branches/main?allow_force_push=true", text)
        self.assertIn("protected_branches/main?allow_force_push=false", text)
        self.assertIn("--force-with-lease=refs/heads/main:" + before, text)
        self.assertIn("GITLAB_NEW_TOKEN", text)

    def test_generated_git_block_works_when_pasted(self):
        self._push_new_main(("a", "jenkins1", "jenkins"), ("b2", "x", "jenkins"))
        self._enable_rollback()
        ok, stats = self.run_sync(False)
        _, text = self._instructions(stats)
        block = self._block(text, "**B)")
        r = subprocess.run(["bash", "-c", block], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(git(self.new, "show", "main:a"), "2")  # Stand aus dem alten GitLab
        self.assertFalse(git(self.new, "ls-tree", "main", "b2"))  # Jenkins-Commit ist weg
        self.assertEqual(git(self.new, "rev-parse", "main"), git(self.repo, "rev-parse", "main"))

    def test_git_block_aborts_if_branch_moved_in_between(self):
        self._push_new_main(("a", "jenkins1", "jenkins"))
        self._enable_rollback()
        ok, stats = self.run_sync(False)
        _, text = self._instructions(stats)
        self._push_new_main(("late", "x", "alice"))  # jemand pusht nach der Erzeugung
        moved = git(self.new, "rev-parse", "main")
        r = subprocess.run(["bash", "-c", self._block(text, "**B)")], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(git(self.new, "rev-parse", "main"), moved)

    def test_no_instructions_if_human_commit_on_top(self):
        self._push_new_main(("a", "jenkins1", "jenkins"), ("h", "x", "alice"))
        self._enable_rollback()
        ok, stats = self.run_sync(False)
        self.assertFalse(ok)
        self.assertTrue(any("Branch main" in c for c in stats.conflicts))
        self.assertEqual(stats.rollbacks, [])
        self.assertIsNone(gs.write_instructions(self.cfg, stats.rollbacks))

    def test_no_instructions_if_human_commit_below_jenkins(self):
        self._push_new_main(("a", "alice-change", "alice"), ("j", "x", "jenkins"))
        self._enable_rollback()
        ok, stats = self.run_sync(False)
        self.assertTrue(any("Branch main" in c for c in stats.conflicts))
        self.assertEqual(stats.rollbacks, [])

    def test_instructions_only_on_configured_branches_and_when_enabled(self):
        self._push_new_main(("a", "jenkins1", "jenkins"))
        ok, stats = self.run_sync(False)  # nicht aktiviert
        self.assertEqual(stats.rollbacks, [])
        self._enable_rollback(branches=["release"])
        ok, stats = self.run_sync(False)
        self.assertEqual(stats.rollbacks, [])

    def test_instructions_max_commits(self):
        self._push_new_main(("a", "j1", "jenkins"), ("b2", "x", "jenkins"))
        self._enable_rollback(max_commits=1)
        ok, stats = self.run_sync(False)
        self.assertEqual(stats.rollbacks, [])

    def test_instructions_also_written_in_dry_run(self):
        self._push_new_main(("a", "jenkins1", "jenkins"))
        before = git(self.new, "rev-parse", "main")
        self._enable_rollback()
        ok, stats = self.run_sync(True)
        self.assertEqual(len(stats.rollbacks), 1)
        self.assertEqual(git(self.new, "rev-parse", "main"), before)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        _, text = self._instructions(stats)
        self.assertIn("Dry-Run", text)

    def test_committer_matches_email_and_case(self):
        self._push_new_main(("a", "j", "Jenkins"))
        self.cfg.jenkins_rollback = gs.JenkinsRollback(enabled=True, committers=["JENKINS@t"])
        ok, stats = self.run_sync(False)
        self.assertEqual(len(stats.rollbacks), 1)

    def test_discover_and_filters(self):
        self.cfg.exclude = ["grp/other*"]
        self.assertEqual(gs.discover(self.cfg, []), [self.repo])
        self.assertEqual(gs.discover(self.cfg, ["nope"]), [])


class ApiPrecheckTests(unittest.TestCase):
    URL = "git@gitlab.example.com:grp/sub/repo.git"

    def setUp(self):
        self.cfg = gs.Config(root=Path("."), new_host="gitlab.example.com",
                             old_host="git.example.com",
                             api_precheck=gs.ApiPrecheck(enabled=True))
        p = mock.patch.dict(os.environ, {"GITLAB_NEW_TOKEN": "n", "GITLAB_OLD_TOKEN": "o"})
        p.start()
        self.addCleanup(p.stop)

    def run_check(self, old_br, new_br, old_tags=(), new_tags=(), refs=None):
        """refs: sha -> Liste von Branch-Namen, die den Commit enthalten (None = unbekannt)."""
        def fake(host, token, path, query, pc):
            old = host == "git.example.com"
            self.assertIn("grp%2Fsub%2Frepo", path)
            if path.endswith("/branches"):
                data = old_br if old else new_br
                return 200, [{"name": n, "commit": {"id": s}} for n, s in data.items()], {}
            if path.endswith("/tags"):
                tags = old_tags if old else new_tags
                items = tags.items() if isinstance(tags, dict) else ((n, n) for n in tags)
                return 200, [{"name": n, "target": t} for n, t in items], {}
            sha = path.split("/commits/")[1].split("/")[0]
            if refs is None or sha not in refs:
                return 404, None, {}
            return 200, [{"type": "branch", "name": n} for n in refs[sha]], {}
        with mock.patch.object(gs, "_request", fake):
            return gs.api_precheck(self.URL, self.cfg)

    def test_project_path(self):
        self.assertEqual(gs.project_path(self.URL), "grp/sub/repo")
        self.assertEqual(gs.project_path("https://h/a/b.git"), "a/b")

    def test_identical_heads(self):
        self.assertFalse(self.run_check({"main": "a"}, {"main": "a", "other": "x"}))

    def test_branch_missing_in_new(self):
        self.assertTrue(self.run_check({"main": "a", "f": "b"}, {"main": "a"}))

    def test_old_head_already_contained_in_new(self):
        self.assertFalse(self.run_check({"main": "a"}, {"main": "b"}, refs={"a": ["main"]}))

    def test_old_head_unknown_in_new(self):
        self.assertTrue(self.run_check({"main": "a"}, {"main": "b"}))

    def test_old_head_only_on_other_branch(self):
        self.assertTrue(self.run_check({"main": "a"}, {"main": "b"}, refs={"a": ["other"]}))

    def test_new_tag_in_old(self):
        self.assertTrue(self.run_check({"main": "a"}, {"main": "a"}, ["v1"], []))
        self.assertFalse(self.run_check({"main": "a"}, {"main": "a"}, ["v1"], ["v1"]))

    def test_changed_tag_only_matters_with_overwrite(self):
        args = ({"main": "a"}, {"main": "a"}, {"v1": "x"}, {"v1": "y"})
        self.assertFalse(self.run_check(*args))
        self.cfg.overwrite_tags = True
        self.assertTrue(self.run_check(*args))
        self.assertFalse(self.run_check({"main": "a"}, {"main": "a"}, {"v1": "x"}, {"v1": "x"}))

    def test_excluded_branch_ignored(self):
        self.cfg.exclude_branches = ["wip/*"]
        self.assertFalse(self.run_check({"main": "a", "wip/x": "b"}, {"main": "a"}))

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(gs.ApiError):
                gs.api_precheck(self.URL, self.cfg)


if __name__ == "__main__":
    unittest.main()
