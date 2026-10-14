import subprocess
import tempfile
import unittest
from pathlib import Path

import gitlab_sync as gs


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, fname, content, msg):
    (Path(repo) / fname).write_text(content)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)


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

    def test_discover_and_filters(self):
        self.cfg.exclude = ["grp/other*"]
        self.assertEqual(gs.discover(self.cfg, []), [self.repo])
        self.assertEqual(gs.discover(self.cfg, ["nope"]), [])


if __name__ == "__main__":
    unittest.main()
