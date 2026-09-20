# devboard

What did I leave unfinished. A terminal dashboard for a folder of git repos.

```
▌ devboard v0.1.0                                    ~/dev · 10 repos · 4 dirty · 175d → now
──────────────────────────────────────────────────────────────────────────────────────────
  ▌ kova                ▪▪▪▪▪▪▪ ▪▪▪▪·▪· ··▪▪··· ▪·▪▪▪·▪ ▪▪▪▪·▪·                        21h
  ▌ cue           ●6    ······· ·▪▪▪▪▪▪ ▪··▪▪·· ······▪ ▪▪▪▪·▪▪                         4d
  ▌ kovapresets   ↓2    ·▪··▪▪▪ ······· ▪······ ······· ·······                        1mo
──────────────────────────────────────────────────────────────────────────────────────────
▌ ~/dev/cue  main                         uncommitted 6
recent                                    M   CHANGELOG.md
d1c2280  the page says a mail is a ticket  ??  COMMERCIAL-LICENSE.md
```

Repos sort by last commit, freshest first.

## Run

```sh
./devboard.py                # scans ~/dev
./devboard.py ~/work         # or any folder of repos
./devboard.py --selftest     # the pure functions: ages, flags, the strip's geometry
./devboard.py --smoke        # boots it headless and checks the layout holds together
```

Single file, [uv](https://docs.astral.sh/uv/) shebang — it installs Textual on first run. Put it
on `PATH` with a symlink: `ln -s "$PWD/devboard.py" ~/.local/bin/devboard`.

## Keys

| key | does |
| --- | --- |
| `↑` `↓` | select a repo (the lower panel follows) |
| `enter` / `o` | open it in your editor |
| `r` | rescan now (it also rescans every 15s) |
| `q` | quit |

Editor is `$DEVBOARD_EDITOR`, else the first of `code`, `cursor`, `zed`, `open` it finds — a GUI
one, so opening a repo never fights the TUI for the terminal.

## Reading a row

- **Left bar** — the worst thing about the repo: red behind the remote, amber dirty, blue
  unpushed, grey nothing to answer for.
- **Name** — dims as the repo goes quiet: bright under 2 days, then a week, a month, older.
- **Badges** — only when something needs attention: `●3` uncommitted, `↑2` unpushed, `↓2`
  behind, `local` no upstream. A clean synced repo says nothing.
- **Strip** — one cell per day, grouped into weeks, today at the right edge. Brighter blue is
  more commits that day, scaled against that repo's own busiest day in view.
- **Age** — time since the last commit.

The strip shows as much history as the window is wide, in whole weeks, up to a year: widen the
terminal and you see further back. The header says how far (`175d → now`).

## Lower panel

Recent commits for the selected repo, and its uncommitted files when it has any — when it
doesn't, the history runs on into the second column. The panel hides itself on a terminal too
short to hold it and gives the space back to the board.
