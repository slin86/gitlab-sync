# Ablauf von gitlab-sync

Die Diagramme sind Mermaid (werden in GitLab/GitHub und den meisten Markdown-Vorschauen direkt gerendert).
Als Bild: [Gesamtablauf](ablauf-gesamt.png) und [Ablauf pro Branch](ablauf-branch.png).

## 1. Gesamtablauf (pro Lauf und pro Repo)

```mermaid
flowchart TD
    start(["Start: Config laden, Repos unter root suchen<br/>(.git, include / exclude / --only)"]) --> nextrepo{"Naechstes Repo?"}
    nextrepo -- "nein" --> finish(["Zusammenfassung, Sammeldateien,<br/>Performance-Auswertung, Exit-Code"])
    nextrepo -- "ja" --> url["origin-URL lesen<br/>alte URL = Host ersetzt"]
    url --> hostok{"origin-Host =<br/>new_host?"}
    hostok -- "nein" --> err1["Fehler protokollieren,<br/>Repo ueberspringen"] --> nextrepo
    hostok -- "ja" --> pre{"api_precheck<br/>aktiv?"}

    pre -- "ja" --> api["API: Branches und Tags beider<br/>Instanzen vergleichen"]
    api --> news{"Altes GitLab hat Neues<br/>oder Pruefung nicht moeglich?"}
    news -- "nichts Neues" --> skip["Repo ueberspringen<br/>(kein fetch, kein Checkout)"] --> nextrepo
    news -- "Neues / Fehler" --> prep
    pre -- "nein" --> prep

    prep["Lokale Aenderungen verwerfen<br/>fetch origin, Remote git-old anlegen<br/>fetch git-old"] --> branches[["Jeden Branch aus git-old<br/>synchronisieren (Diagramm 2)"]]
    branches --> tags["Tags: neue pushen<br/>abweichende ueberschreiben (overwrite_tags)<br/>oder nur warnen"]
    tags --> restore["Urspruenglich ausgecheckten<br/>Branch wiederherstellen"] --> nextrepo
```

## 2. Ablauf pro Branch

```mermaid
flowchart TD
    b(["Branch aus git-old"]) --> excl{"in exclude_branches?"}
    excl -- "ja" --> done
    excl -- "nein" --> inorigin{"Branch existiert<br/>in origin?"}

    inorigin -- "nein" --> create["Branch lokal anlegen"] --> push
    inorigin -- "ja" --> uptodate{"git-old-Stand schon<br/>in origin enthalten?"}
    uptodate -- "ja" --> done
    uptodate -- "nein" --> localok{"Lokaler Branch hat<br/>unveroeffentlichte Commits?"}
    localok -- "ja" --> warn1["Warnung, Branch ueberspringen"] --> done
    localok -- "nein" --> ff{"Fast-Forward<br/>moeglich?"}
    ff -- "ja" --> push
    ff -- "nein" --> merge["git merge git-old/Branch"]
    merge --> conflict{"Konflikt?"}
    conflict -- "nein" --> push
    conflict -- "ja" --> abort["merge --abort, Branch zuruecksetzen<br/>Eintrag in branch_konflikts.txt"]
    abort --> jenkins{"jenkins_rollback aktiv, oberste<br/>Commits von Jenkins und<br/>Merge danach moeglich?"}
    jenkins -- "ja" --> jfile["Anleitung in jenkins-rollback.md<br/>(API + git, kein Eingriff)"] --> done
    jenkins -- "nein" --> done

    push{"Push nach origin<br/>(Dry-Run: kein Push)"} -- "ok" --> done
    push -- "geschuetzter Branch" --> prot["Link in protected_branches.md"] --> done
    push -- "anderer Fehler" --> err2["Fehler protokollieren"] --> done

    done(["weiter mit dem naechsten Branch"])
```
