# gitlab_sync flow

The diagrams are Mermaid and render directly in GitLab, GitHub and most Markdown previews.
Rendered images: [overall flow](flow_overall.png) and [flow per branch](flow_branch.png).

## 1. Overall flow per run and per repo

```mermaid
flowchart TD
    start(["Start: load config, find repos below root<br/>(.git, include, exclude, --only)"]) --> repos["Audit: compare project lists<br/>(missing repos, archive status)"]
    repos --> nextrepo{"Next repo?"}
    nextrepo -- "no" --> finish(["Summary, findings.md, report files,<br/>performance report, exit code"])
    nextrepo -- "yes" --> url["Read origin URL<br/>old URL = host replaced"]
    url --> hostok{"origin host is<br/>new_host?"}
    hostok -- "no" --> err1["Log error,<br/>skip repo"] --> nextrepo
    hostok -- "yes" --> audit["Audit: push permissions,<br/>schedules, feature flags, environments<br/>(read only)"]
    audit --> pre{"api_precheck<br/>enabled?"}

    pre -- "yes" --> api["API: compare branches and tags<br/>of both instances"]
    api --> news{"Old instance has something new<br/>or the check failed?"}
    news -- "nothing new" --> skip["Skip repo<br/>(no fetch, no checkout)"] --> nextrepo
    news -- "new or failed" --> prep
    pre -- "no" --> prep

    prep["Discard local changes<br/>fetch origin, add remote git-old<br/>fetch git-old"] --> branches[["Sync every branch of git-old<br/>(diagram 2)"]]
    branches --> tags["Tags: push new ones<br/>overwrite changed ones (overwrite_tags)<br/>or only report them"]
    tags --> restore["Check out the original<br/>branch again"] --> nextrepo
```

## 2. Flow per branch

```mermaid
flowchart TD
    b(["Branch of git-old"]) --> excl{"Matches<br/>exclude_branches?"}
    excl -- "yes" --> done
    excl -- "no" --> inorigin{"Branch exists<br/>on origin?"}

    inorigin -- "no" --> create["Create branch locally"] --> push
    inorigin -- "yes" --> uptodate{"git-old state already<br/>contained in origin?"}
    uptodate -- "yes" --> done
    uptodate -- "no" --> localok{"Local branch has<br/>unpublished commits?"}
    localok -- "yes" --> warn1["Warning, skip branch"] --> done
    localok -- "no" --> ff{"Fast forward<br/>possible?"}
    ff -- "yes" --> push
    ff -- "no" --> merge["git merge git-old/branch"]
    merge --> conflict{"Conflict?"}
    conflict -- "no" --> push
    conflict -- "yes" --> abort["merge --abort, reset branch<br/>entry in branch_conflicts.txt"]
    abort --> jenkins{"jenkins_rollback enabled,<br/>newest commits from Jenkins<br/>and merge works without them?"}
    jenkins -- "yes" --> jfile["Instructions in jenkins_rollback.md<br/>(API and git, no change by the tool)"] --> done
    jenkins -- "no" --> done

    push{"Push to origin<br/>(dry run: no push)"} -- "ok" --> done
    push -- "protected branch" --> prot["Link in protected_branches.md"] --> done
    push -- "other error" --> err2["Log error"] --> done

    done(["Continue with the next branch"])
```
