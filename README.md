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

## Ablaufdiagramm

Gesamtablauf und Ablauf pro Branch als Flussdiagramm: [docs/ablauf.md](docs/ablauf.md) (Mermaid, wird in
GitLab/GitHub direkt gerendert; als Bild: [Gesamtablauf](docs/ablauf-gesamt.png),
[pro Branch](docs/ablauf-branch.png)). Wer die Diagramme in `docs/ablauf.md` ändert, muss die Bilder neu
rendern (z.B. mit `mmdc` aus `@mermaid-js/mermaid-cli`).

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

Das Tool löscht nie einen Branch oder Tag und pusht keinen Branch mit Force (Force-Push nur für abweichende Tags bei `overwrite_tags`; Jenkins-Rollbacks nur als Anleitung, siehe unten). Branches, die nur im neuen GitLab
existieren, bleiben unverändert. Ein lokaler Branch mit unveröffentlichten Commits wird übersprungen (Warnung).

## Jenkins-Rollback: Anleitung statt Eingriff (optional)

Baut Jenkins im neuen GitLab bei Pushes auf `main` und erzeugt dabei eigene Commits/Tags, kann der Merge
aus dem alten GitLab daran scheitern. Mit `jenkins_rollback.enabled: true` prüft das Tool bei einem
**Merge-Konflikt** auf den Branches aus `jenkins_rollback.branches` (Default `main`), ob ein reiner
Jenkins-Rollback helfen würde:

1. Die obersten Commits (First-Parent-Kette) von `main` im neuen GitLab werden gedanklich einzeln entfernt,
   **solange ihr Committer Jenkins ist** (Committer, nicht Autor; `committers` sind fnmatch-Globs auf
   Name bzw. E-Mail, Groß-/Kleinschreibung egal, z.B. `["jenkins*"]`). Nach jedem Schritt wird ein
   Probe-Merge versucht (nur lokal).
2. Das Tool ändert **nichts**: der Branch bleibt lokal und im neuen GitLab unverändert, der Konflikt wird
   normal protokolliert. Stattdessen schreibt es die Datei `instructions_file` (Default
   `jenkins-rollback.md`, neben der Logdatei; wird bei jedem Lauf mit Anleitungen überschrieben).
3. Ist ein Commit darunter nicht von Jenkins oder werden mehr als `max_commits` (Default 50) gebraucht,
   gibt es keine Anleitung (normaler Konflikt, manuell klären).

Pro Repo enthält die Datei drei Blöcke zum Kopieren:

- **A)** `curl`-Befehle gegen die GitLab-API: Schutz anzeigen und `allow_force_push=true` auf dem
  geschützten Branch setzen (`PATCH /projects/:id/protected_branches/:name`, ab GitLab 15.6).
- **B)** git-Befehle, mit `&&` verkettet (bricht beim ersten Fehler ab): fetch, Branch auf `origin/main`,
  `reset --hard` auf den Stand vor den Jenkins-Commits, Merge von `git-old/main`, danach
  `push --force-with-lease=refs/heads/main:<erwarteter SHA>`. Hat jemand dazwischen gepusht, lehnt der
  Push ab.
- **C)** `allow_force_push=false`: Schutz wieder herstellen.

Die API-Blöcke brauchen ein Token des neuen GitLab mit Scope `api` und Rolle Maintainer (Variablenname
wie `api_precheck.new_token_env`, Default `GITLAB_NEW_TOKEN`). Tags auf den entfernten Commits bleiben
bestehen und sind in der Anleitung aufgelistet. Die Datei entsteht auch im Dry-Run.

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

Das Protokoll läuft über [loguru](https://github.com/Delgan/loguru): eingerückte Textdatei (`log_file`,
wird angehängt, Format `Zeit | Level | Meldung`) plus Konsole (nur die eingerückte Meldung). Level:
`[FEHLER]` → ERROR, `[WARN]`/`[KONFLIKT]` → WARNING, sonst INFO. Auf der Konsole sind Fehler rot und
Warnungen/Konflikte gelb; Farben gibt es nur im Terminal (nicht bei Pipe oder Umleitung), `NO_COLOR=1`
schaltet sie ab, `FORCE_COLOR=1` erzwingt sie. Die Logdatei enthält nie Farbcodes. Am Ende steht eine Zusammenfassung mit
Konflikten, Fehlern und Warnungen:

    Synce Ordner /work/gitlab (12 Repos)
        -> Synce Ordner team-a
            -> Repo repo-x
                -> Update origin (git@gitlab.example.com:team-a/repo-x.git)
                -> Fuege git-old hinzu: git@git.example.com:team-a/repo-x.git
                -> Checkout Branch main
                -> Synce 123 Commits git-old -> origin (Fast-Forward)

## Performance-Tracking

Jeder git-Befehl und jeder API-Request wird gemessen (Kategorie, Dauer, Repo; nie Argumente oder URLs, da
diese Zugangsdaten enthalten können). Kategorien: `git fetch origin`, `git fetch git-old`, `git push origin`,
`git merge` usw. sowie `api <host> branches|tags|commit-refs`. So sieht man, ob das alte oder das neue GitLab
bzw. die API bremst.

- **Pro Repo** im Protokoll: `Dauer 12.3s (Netzwerk 11.8s)`.
- **Am Ende des Laufs** eine Auswertung: Gesamtdauer und Netzwerkanteil, Zeit pro Befehlsart (Anzahl, Summe,
  Schnitt, Maximum), die `perf_top` langsamsten Repos mit Aufschlüsselung (fetch neu/alt, push, API) und alle
  Einzelbefehle ab `slow_command_seconds` (Default 30).
- Einzelbefehle ab dieser Schwelle werden sofort als `[WARN] Langsamer Befehl: git fetch git-old 42.1s (team-a/repo-x)`
  gemeldet, damit man beim laufenden Sync sieht, wo es hakt. `0` schaltet das ab.
- **`performance.csv`** (`perf_file`, leer = aus): eine Zeile pro Repo und Lauf, angehängt, Spalten
  `run_start, repo, status (ok|problem|skipped-api), total_s, network_s, fetch_new_s, fetch_old_s, push_s,
  api_s, git_other_s`. Gut für Excel oder zum Vergleich mehrerer Läufe (z.B. mit und ohne `api_precheck`).

## Sammeldateien

Alle liegen neben der `log_file` (Pfad jeweils in der Config änderbar), werden angehängt und enthalten
keine Duplikate bei gleichem Stand. Sie entstehen auch im Dry-Run, soweit der Anlass ohne Push erkennbar ist.

- **`branch_konflikts.txt`** (`conflicts_file`): Branches, die nicht gemerged werden konnten. Eine Zeile pro
  Branch: `Zeit | Repo | Branch | git-old=<SHA> | origin=<SHA> | N Commits`, ggf. mit dem Zusatz
  `Jenkins-Rollback-Anleitung`.
- **`protected_branches.md`** (`protected_branches_file`): Repos, bei denen der Push mit
  `GitLab: You are not allowed to push code to protected branches on this project.` abgelehnt wurde
  (der Maintainer darf nicht in den Branch pushen). Eine Zeile pro Repo und Branch mit Link direkt zur
  Seite „Protected branches“ (Settings → Repository) des Projekts im neuen GitLab. Dort beim Branch unter
  „Allowed to push and merge“ die Rolle Maintainer erlauben und das Tool erneut laufen lassen. Der Seitenpfad
  (`protected_branches_page`, Default `/-/settings/repository#js-protected-branches-settings`) kann je nach
  GitLab-Version abweichen. Im Dry-Run gibt es keinen Push und damit keine Einträge.
- **`jenkins-rollback.md`**: siehe oben.

## Tests

    python -m unittest -v
