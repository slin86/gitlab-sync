# gitlab-sync

Holt Änderungen aus dem alten GitLab (`git.example.com`) in die lokal ausgecheckten Repos des
neuen GitLab (`gitlab.example.com`) und pusht sie dorthin.

## Setup

    pip install -r requirements.txt
    cp config.example.yaml config.yaml   # anpassen

Authentifizierung läuft über die vorhandenen git-Credentials (SSH-Keys / Credential-Helper);
das Tool setzt `GIT_TERMINAL_PROMPT=0`, hängt also nie an einer Passwortabfrage.

## Aufruf

    python gitlab_sync.py -c config.yaml              # live
    python gitlab_sync.py -c config.yaml --dry-run    # alles ausser Push
    python gitlab_sync.py --only "team-a/*" --only "team-b/repo"   # nur ausgewählte Repos

Exit-Code 0 = alles ok, 1 = Fehler oder Konflikte aufgetreten, 2 = Root-Verzeichnis fehlt.

## Ablauf pro Repo

1. lokale Änderungen verwerfen (`reset --hard`, `clean -fd`), `fetch` von `origin`
2. Remote `git-old` anlegen: URL = origin-URL mit ersetztem Host (SSH-scp, `ssh://` und `https://`)
3. pro Branch des alten GitLab:
   - fehlt im neuen: Branch anlegen und pushen
   - bereits enthalten: nichts tun
   - sonst Fast-Forward, falls möglich, andernfalls Merge-Commit; bei Konflikt `merge --abort`,
     Eintrag `[KONFLIKT]` im Protokoll, weiter mit dem nächsten Branch
   - Push nach `origin` (Dry-Run: Merge wird lokal getestet und danach zurückgesetzt, nur der Push entfällt)
4. neue Tags übertragen; Tags mit gleichem Namen, aber anderem Commit werden mit `overwrite_tags: true`
   im neuen GitLab überschrieben (alt → neu, Force-Push nur für diese Tags), sonst nur Warnung
5. ursprünglich ausgecheckten Branch wiederherstellen (`restore_branch`)

Es wird nie ein Branch oder Tag gelöscht und kein Branch force-gepusht (Force-Push nur für abweichende Tags bei `overwrite_tags`). Branches, die nur im neuen GitLab
existieren, bleiben unverändert. Ein lokaler Branch mit unveröffentlichten Commits wird übersprungen (Warnung).

## Jenkins-Rollback (optional)

Baut Jenkins im neuen GitLab bei Pushes auf `main` und erzeugt dabei eigene Commits/Tags, kann der Merge
aus dem alten GitLab daran scheitern. Mit `jenkins_rollback.enabled: true` gilt bei einem **Merge-Konflikt**
auf den Branches aus `jenkins_rollback.branches` (Default `main`):

1. Die obersten Commits (First-Parent-Kette) von `main` im neuen GitLab werden einzeln zurückgedreht,
   **solange ihr Committer Jenkins ist**. Maßgeblich ist der Committer, nicht der Autor; `committers` sind
   fnmatch-Globs (Groß-/Kleinschreibung egal) auf Committer-Name oder -E-Mail, z.B. `["jenkins*"]`.
2. Nach jedem Schritt wird der Merge erneut versucht. Klappt er, wird das Ergebnis mit
   `git push --force-with-lease` (erwarteter Stand = der gerade geholte) nach `origin` geschrieben.
3. Ist ein Commit darunter nicht von Jenkins, oder werden mehr als `max_commits` (Default 50) Commits
   gebraucht, wird nichts zurückgedreht: normaler `[KONFLIKT]`, der Branch bleibt unverändert.

Tags auf zurückgedrehten Commits werden nicht gelöscht (nur Hinweis im Protokoll). Jeder Rollback steht
als Warnung in der Zusammenfassung, inklusive der verworfenen Commit-Hashes. Im Dry-Run wird der Rollback
nur lokal durchgespielt und anschließend zurückgesetzt; es wird nichts gepusht.

## API-Vorabpruefung (optional)

Mit `api_precheck.enabled: true` fragt das Tool vor jedem fetch per GitLab-API (v4) Branches und Tags
beider Instanzen ab und ueberspringt das Repo komplett, wenn das alte GitLab nichts Neues hat
(kein fetch, kein Checkout, kein `git-old`-Remote). Als "neu" gilt:

- ein Branch (ausser `exclude_branches`) oder Tag, der im neuen GitLab fehlt (bei `overwrite_tags` auch ein Tag mit anderem Ziel), oder
- ein Branch-Head, der im neuen GitLab unbekannt oder nicht im gleichnamigen Branch enthalten ist.

Tokens (Scope `read_api`) werden aus den Umgebungsvariablen `GITLAB_NEW_TOKEN` und
`GITLAB_OLD_TOKEN` gelesen (Namen in der Config aenderbar). Ist die Pruefung nicht moeglich
(Token fehlt, Netzwerk-/API-Fehler, Projekt nicht gefunden), laeuft der volle Sync wie bisher.

## Protokoll

Eingerückte Textdatei (`log_file`, wird angehängt) plus Konsole, am Ende eine Zusammenfassung mit
Konflikten, Fehlern und Warnungen:

    Synce Ordner /work/gitlab (12 Repos)
        -> Synce Ordner team-a
            -> Repo repo-x
                -> Update origin (git@gitlab.example.com:team-a/repo-x.git)
                -> Fuege git-old hinzu: git@git.example.com:team-a/repo-x.git
                -> Checkout Branch main
                -> Synce 123 Commits git-old -> origin (Fast-Forward)

## Tests

    python -m unittest -v
