# gitlab_sync

Moves ongoing work from an old GitLab instance (git.example.com) into local checkouts of the new
instance (gitlab.example.com) and pushes it there. Both instances started from the same copy, so users,
projects, commits and merge requests exist on both. Development continues on the old one until the
switch, and this tool keeps the new one up to date.

## Setup

Python 3.9 or newer.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml
```

Git authentication uses the existing credentials of the machine (SSH keys or a credential helper). The
tool sets `GIT_TERMINAL_PROMPT=0`, so it never waits for a password prompt.

## Usage

```bash
gitlab_sync -c config.yaml                      # live run
gitlab_sync -c config.yaml --dry-run            # everything except pushing
gitlab_sync --only "team-a/*" --only "team-b/repo"
python -m gitlab_sync -c config.yaml            # same without the console script
```

Exit codes: 0 when everything was fine, 1 when a repo had errors or conflicts, 2 when the config is
invalid or the root directory does not exist.

## What happens per repo

The flow is drawn in [docs/flow.md](docs/flow.md) ([overall](docs/flow_overall.png),
[per branch](docs/flow_branch.png)).

1. Optional API precheck. If the old instance has nothing the new one is missing, the repo is skipped
   without any fetch or checkout.
2. Local changes are discarded, origin is fetched and the remote of the old instance is added if it is
   missing. Its URL is the origin URL with the host replaced.
3. Every branch of the old instance:
   * missing on the new instance: created and pushed
   * already contained in the new branch: nothing to do
   * otherwise fast forward if possible, else a merge commit; on a conflict the merge is aborted, the
     branch stays as it is and the conflict is recorded
   * in a dry run everything happens locally and is reset afterwards; only the push is skipped
4. Tags that are missing on the new instance are pushed. Tags with a different target are overwritten
   when `overwrite_tags` is set, otherwise only reported.
5. The previously checked out branch is checked out again.

The tool never deletes a branch or tag and never force pushes a branch. Branches that only exist on the
new instance are left alone. A local branch with unpublished commits is skipped with a warning.

## Output

The run log is written with loguru to `log_file` (appended, `time | level | message`) and to the
console (indented message only). On the console errors are red and warnings and conflicts yellow.
Colors are only used on a terminal; `NO_COLOR=1` disables them and `FORCE_COLOR=1` forces them.

```
Syncing folder /work/gitlab (12 repos)
    -> Syncing folder team-a
        -> Repo repo-x
            -> Fetching origin (git@gitlab.example.com:team-a/repo-x.git)
            -> Adding remote git-old: git@git.example.com:team-a/repo-x.git
            -> Checking out branch main
            -> Syncing 123 commits from git-old to origin (fast forward)
```

The following files are written next to the log file. Their names can be changed in the config. The
lists are appended to and skip entries that are already present.

* `branch_conflicts.txt`: branches that could not be merged, one line per branch and state:
  `time | repo | branch | git-old=<sha> | origin=<sha> | N commits`.
* `protected_branches.md`: repos whose push was rejected with "You are not allowed to push code to
  protected branches", with a link to the protected branches settings of the project. Allow the
  Maintainer role to push and merge there and run the sync again. The page path is configurable in
  `protected_branches_page` because it depends on the GitLab version.
* `jenkins_rollback.md`: manual rollback steps, see below. Replaced on every run that has any.
* `performance.csv`: timings per repo, see below.

## API precheck

With `api_precheck.enabled` the tool asks the API of both instances before touching a repo:

* one request per instance for all branches with their head commits (100 per page)
* for a branch whose head differs, one request whether the new branch already contains the old head
* only if all branches match, one request per instance for the tags

The repo is skipped when nothing is missing on the new instance. Tokens with scope `read_api` are read
from `GITLAB_NEW_TOKEN` and `GITLAB_OLD_TOKEN`. When the check is not possible the repo gets a full sync
and the log contains a warning.

## Jenkins rollback instructions

Jenkins builds on pushes to main on the new instance and creates its own commits and tags, which can
block the merge from the old instance. With `jenkins_rollback.enabled` a merge conflict on one of the
configured branches is analysed locally: the newest commits of the new branch are dropped one by one
as long as their committer matches `committers`, and a merge is tried after each step. The tool does not
change anything. When such a rollback would solve the conflict it writes three blocks per repo to
`jenkins_rollback.md`:

* A: API calls that show the branch protection and allow force push on the branch
* B: git commands chained with `&&` that reset the branch, merge and push with
  `--force-with-lease`, so the push fails if someone pushed in the meantime
* C: an API call that disallows force push again

The API blocks need a token with scope `api` and the Maintainer role. A non Jenkins commit in the way or
more than `max_commits` commits means no instructions, only the normal conflict.

## Performance tracking

Every git command and API request is timed by category (`git fetch origin`, `git fetch git-old`,
`git push origin`, `api <host> branches` and so on). Arguments and URLs are never stored because they
can contain credentials. Each repo logs its duration and network share, single commands slower than
`slow_command_seconds` are logged as warnings, and the run ends with a report of the time per command
type, the slowest repos and the slowest commands. `performance.csv` gets one row per repo and run with
the columns `run_start, repo, status, total_s, network_s, fetch_new_s, fetch_old_s, push_s, api_s,
git_other_s`.

## Development

```bash
pytest
ruff check src tests
ruff format src tests
```

```
src/gitlab_sync/
  cli.py          command line entry point
  config.py       configuration model and YAML loader
  runner.py       walks all repos and writes summary, reports and timings
  sync/           sync of one repo: repo.py, branch.py, tags.py, jenkins.py
  precheck.py     API precheck
  gitlab_api.py   minimal GitLab API client
  git.py          git command wrapper
  reports.py      conflict, protected branch and Jenkins rollback files
  summary.py      summary and performance report in the log
  perf.py         timing collection and CSV
  log.py          indented log on top of loguru
  urls.py         remote URL parsing
  discovery.py    finds repos below root
```

## Limitations

* Repos that were created on the old instance but do not exist on the new one yet are not handled.
* The diagrams in `docs/flow.md` are the source; the PNG and SVG files have to be rendered again after a
  change, for example with `mmdc` from `@mermaid-js/mermaid-cli`.
