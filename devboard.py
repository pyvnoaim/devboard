#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["textual>=0.80"]
# ///
"""devboard - what did I leave unfinished. A Textual dashboard for a folder of git repos."""
import itertools, json, os, shutil, subprocess, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

VERSION = "v0.1.0"
ROOT = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else Path.home() / "dev"

# Text runs in four tiers, loudest first. Chrome (labels, keys, axis) sits at SUBTLE: quiet,
# but never below what a laptop panel at half brightness can still read.
TEXT, FG, DIM, SUBTLE, FAINT = "#e6edf3", "#c9d1d9", "#8b949e", "#626b77", "#3a404a"
BLUE, CYAN, AMBER, RED, GREEN = "#6b8cff", "#56c2d6", "#e3b341", "#f85149", "#3fb950"

# A repo's name dims as it goes quiet, so the board reads as a decay gradient top to bottom.
# Thresholds in days; last entry is the floor for anything older.
FADE = ((2, "#d6dde6"), (7, "#9aa5b4"), (30, "#6b7280"), (None, "#4a515b"))
GUTTER = "#343a44"   # the left bar on a repo with nothing to answer for

# A one-line-tall cell has no room for bar HEIGHT: partial blocks bottom-align and smear
# into neighbouring rows. Encode magnitude as colour intensity on a fixed glyph instead.
HEAT = ["#2b3039", "#31455f", "#3d5f8f", "#4f7bc4", "#6b8cff"]   # [0] = a silent day, must survive the cursor row
CELL, IDLE = "▪", "·"
MAX_DAYS, MIN_DAYS = 364, 7  # the strip shows as much history as the window is wide, up to a year
GAP = 2              # space between the board's columns
FLAGS_W, AGE_W = 7, 5   # FLAGS_W is the floor; the column grows to the longest row's badges
# The strip absorbs the width, so the board can span the terminal without opening a canyon
# between the columns. MAX_W is only a sanity stop for absurdly wide windows.
MAX_W, LOG_W = 300, 90
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
REVEAL_STEP = 0.03   # seconds between rows during the staggered reveal
RELOAD_EVERY = 15.0
CI_EVERY = 120.0     # gh is a network call per repo; the git rescan is local and can run far more often
CI_FAIL = {"failure", "timed_out", "startup_failure"}


def parse_counts(out):
    """`git rev-list --left-right --count @{u}...HEAD` -> (behind, ahead). Empty = no upstream."""
    parts = out.split()
    return (int(parts[0]), int(parts[1])) if len(parts) == 2 else (0, 0)


def parse_ci(out):
    """`gh run list --json workflowName,status,conclusion` -> did any workflow's latest finished run fail.
       Per workflow, so a green deploy run can't hide a red ci run that landed just before it."""
    seen = set()
    for run in json.loads(out or "[]"):
        if run["status"] != "completed" or run["workflowName"] in seen:
            continue
        seen.add(run["workflowName"])
        if run["conclusion"] in CI_FAIL:
            return True
    return False


def fmt_flags(dirty, behind, ahead, has_upstream, ci_failed=False):
    """Badges, and only badges. A repo with nothing wrong says nothing — the quiet is the signal."""
    t = Text()
    for text, style, on in ((f"●{dirty}", AMBER, dirty), (f"↑{ahead}", BLUE, ahead),
                            (f"↓{behind}", CYAN, behind), ("✗ci", RED, ci_failed),
                            ("local", DIM, not has_upstream)):
        if on:
            t.append(" " if t else "")
            t.append(text, style=style)
    return t


def fmt_age(ts, now=None):
    """epoch -> compact age: 42s, 9m, 21h, 3d, 5w, 7mo, 2y."""
    if not ts:
        return "—"
    s = max(0, (now if now is not None else time.time()) - ts)
    for div, suffix in ((60, "s"), (60, "m"), (24, "h"), (7, "d"), (4.35, "w"), (12, "mo")):
        if s < div:
            return f"{int(s)}{suffix}"
        s /= div
    return f"{int(s)}y"


def fade(ts, now=None):
    days = (max(0, (now if now is not None else time.time()) - ts)) / 86400 if ts else 1e9
    return next(c for limit, c in FADE if limit is None or days < limit)


def heat_index(c, hi):
    """Bucket one day's commit count into a HEAT shade. Any activity is at least shade 1."""
    if c == 0:
        return 0
    if hi <= 1:
        return len(HEAT) - 1
    return max(1, round(c / hi * (len(HEAT) - 1)))


def heat_strip(counts):
    """Weeks separated by a gap, so the strip reads as a calendar rather than a bar of noise."""
    hi = max(counts) if counts else 0
    t = Text()
    for i, c in enumerate(counts):
        if i and (len(counts) - i) % 7 == 0:
            t.append(" ")
        t.append(CELL if c else IDLE, style=HEAT[heat_index(c, hi)] if c else HEAT[0])
    return t


def month_axis(n, today=None):
    """Month names over an n-day strip, each sitting on the column of its month's 1st.
       Mirrors heat_strip's week gaps, so the label lands on the very cell it names."""
    today = today or date.today()
    out, col, free = [" "] * strip_width(n), 0, 0
    for i in range(n):
        if i and (n - i) % 7 == 0:
            col += 1
        day = today - timedelta(days=n - 1 - i)
        name = day.strftime("%b")
        if day.day == 1 and col >= free and col + len(name) <= len(out):
            out[col:col + len(name)] = name
            free = col + len(name) + 1
        col += 1
    return "".join(out)


def strip_width(days):
    return days + (days - 1) // 7 if days else 0


def fit_days(width):
    """Widest whole WEEKS whose strip fits `width` — a ragged part-week reads as a glitch."""
    return min(MAX_DAYS, max(MIN_DAYS, (width + 1) // 8 * 7))   # w weeks take 8w - 1 columns


def commit_days(path, days=MAX_DAYS):
    out = git(path, "log", f"--since={days}.days", "--format=%cd", "--date=short")
    seen = Counter(out.splitlines())
    today = date.today()
    return [seen.get((today - timedelta(days=i)).isoformat(), 0) for i in range(days - 1, -1, -1)]


def editor_cmd(env=None):
    """GUI editor, so opening a repo never fights the TUI for the terminal."""
    env = os.environ if env is None else env
    return (env.get("DEVBOARD_EDITOR") or shutil.which("code") or shutil.which("cursor")
            or shutil.which("zed") or ("open" if sys.platform == "darwin" else "xdg-open"))


def short(p):
    """/Users/x/dev -> ~/dev"""
    s, home = str(p), str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True).stdout.strip()


_ci = {}   # (path, branch) -> (checked_at, failed)


def ci_failed(path, branch):
    """Latest CI on this branch failed. No gh, not logged in, not on GitHub: quietly False."""
    hit = _ci.get((path, branch))
    if hit and time.time() - hit[0] < CI_EVERY:
        return hit[1]
    failed = False
    if shutil.which("gh") and branch != "HEAD":
        try:
            r = subprocess.run(["gh", "run", "list", "-L", "20", "-b", branch,
                                "--json", "workflowName,status,conclusion"],
                               cwd=path, capture_output=True, text=True, timeout=10)
            failed = r.returncode == 0 and parse_ci(r.stdout)
        except (subprocess.TimeoutExpired, ValueError, KeyError):
            pass
    _ci[(path, branch)] = (time.time(), failed)
    return failed


def repos(root):
    """Repos in root, plus repos one level down in plain folders (e.g. dev/org/repo)."""
    # ponytail: one level deep only, recurse if someone nests deeper
    for p in root.iterdir():
        if not p.is_dir() or p.name.startswith("."):
            continue
        if (p / ".git").exists():
            yield p
        else:
            try:
                yield from (c for c in p.iterdir() if c.is_dir() and (c / ".git").exists())
            except OSError:
                pass


def scan_one(root, d):
    counts = git(d, "rev-list", "--left-right", "--count", "@{u}...HEAD")
    behind, ahead = parse_counts(counts)
    ts = git(d, "log", "-1", "--format=%ct")
    branch = git(d, "rev-parse", "--abbrev-ref", "HEAD") or "?"
    upstream = git(d, "rev-parse", "--abbrev-ref", "@{u}") if counts else ""
    return {
        "name": str(d.relative_to(root)), "path": d, "branch": branch,
        "dirty": len([l for l in git(d, "status", "--porcelain").splitlines() if l]),
        "behind": behind, "ahead": ahead, "upstream": upstream or bool(counts),
        "ts": int(ts) if ts.isdigit() else 0,
        "days": commit_days(d),
        "ci": bool(counts) and ci_failed(d, branch),
    }


def scan(root):
    # ~0.9s per gh call, plus ~70ms of git per repo: in series 21 repos took 18s. In parallel
    # the board waits on the slowest single repo.
    with ThreadPoolExecutor(16) as pool:
        rows = list(pool.map(lambda d: scan_one(root, d), repos(root)))
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


class Devboard(App):
    CSS = """
    Screen { background: #1a1d23; }
    #body { width: 100%; height: 100%; background: #1a1d23; }
    #head { width: 100%; padding: 1 2; border-bottom: solid #22262e; }
    /* the board's column header; the strip's slot carries month names, making it a timeline */
    #cols { width: 100%; padding: 1 2 0 2; }
    /* one column, sized to a line this builds itself: the table does no layout, so the
       strip can be told exactly how much room is left over */
    DataTable {
        width: 100%; height: auto; padding: 0 2 1 2;
        background: transparent; color: #c9d1d9; border: none;
        /* stable, or the scrollbar appearing steals a column mid-render and the line I built
           to exactly fill the width wraps */
        scrollbar-size: 1 1; scrollbar-gutter: stable;
    }
    /* background only: setting a colour here would override each cell's own style and
       flatten the heat strip on the selected row */
    DataTable > .datatable--cursor { background: #232a36; text-style: none; }
    DataTable > .datatable--hover { background: transparent; }
    /* stretched and tinted: the leftover height reads as the lower panel, not as a gap */
    #detail { width: 100%; height: 1fr; padding: 1 2 0 2;
              background: #1e2128; border-top: solid #22262e; }
    /* docked, so leftover height falls below the content instead of between the panes */
    #keys { width: 100%; padding: 0 2; border-top: solid #22262e; dock: bottom;
            text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS = [("q", "quit", "quit"), ("r", "reload", "reload"), ("o", "open_editor", "open"),
                ("enter", "open_editor", "open")]
    rows = []            # the first Resize lands before on_mount, so this has to exist early

    def compose(self) -> ComposeResult:
        keys = Text()
        for k, label in (("↑↓", "select"), ("enter", "open in editor"), ("r", "reload"), ("q", "quit")):
            keys.append("    " if keys.plain else "")
            keys.append(f" {k} ", style=f"{FG} on #2a303a"); keys.append(f" {label}", style=SUBTLE)
        with Vertical(id="body"):
            yield Static("", id="head")
            yield Static("", id="cols")
            # cursor_foreground_priority defaults to "css", which paints the cursor's
            # foreground over the cell's own colours and flattens the heat strip
            yield DataTable(id="t", cursor_type="row", zebra_stripes=False, show_cursor=True,
                            show_header=False, cell_padding=0,
                            cursor_foreground_priority="renderable")
            yield Static("", id="detail")
            yield Static(keys, id="keys")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_column("board")
        self.rows, self.queue, self.reveal, self.days, self.keep = [], [], None, MIN_DAYS, 0
        self.spin = None
        # auto-reload updates in place; only mount and an explicit `r` replay the reveal
        self.set_interval(RELOAD_EVERY, lambda: self.load_rows(animate=False))
        self.action_reload()

    def action_reload(self) -> None:
        if self.spin is None:
            frames = itertools.cycle(SPIN)
            tick = lambda: self.head(Text(f"{next(frames)} scanning {short(ROOT)}", style=DIM))
            tick()
            self.spin = self.set_interval(0.08, tick)
        self.load_rows()

    def on_resize(self) -> None:
        if self.rows:                    # the horizon is a function of width; no git needed
            self.fill(animate=False)
            self.show_detail()

    def board_width(self, widget) -> int:
        return min(MAX_W, widget.content_size.width or self.size.width - 4)

    # head 4 + column header 2 + keys 2; the detail card adds border, padding and 4 lines of
    # its own heading before the first commit row
    CHROME, CARD = 8, 6

    def detail_rows(self) -> int:
        """Whatever is left after the chrome and the board (+1: its bottom padding). The panel
           fills itself with history rather than leaving tinted emptiness."""
        return self.size.height - self.CHROME - len(self.rows) - 1 - self.CARD

    def head(self, status: Text) -> None:
        """Brand hard left, status hard right, on one line as wide as the board."""
        t = Text("▌ ", style=BLUE)
        t.append("devboard", style=f"bold {TEXT}"); t.append(f"  {VERSION}", style=FAINT)
        width = self.board_width(self.query_one("#head"))
        t.append(" " * max(1, width - len(t.plain) - len(status.plain))); t.append_text(status)
        t.truncate(width)            # a narrow terminal drops the status rather than wrapping
        self.query_one("#head", Static).update(t)

    def reveal_one(self) -> None:
        if self.reveal is None:          # a tick already queued when the timer was stopped
            return
        table = self.query_one(DataTable)
        if not self.queue:
            self.reveal.stop(); self.reveal = None
            if 0 < self.keep < table.row_count:
                table.move_cursor(row=self.keep)
            self.show_detail()
            return
        table.add_row(self.row_line(self.queue.pop(0)))

    def action_open_editor(self) -> None:
        if not self.rows:
            return
        row = self.rows[self.query_one(DataTable).cursor_row]
        subprocess.Popen([editor_cmd(), str(row["path"])],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.notify(f"opening {row['name']}", timeout=2)

    def on_data_table_row_selected(self) -> None:
        self.action_open_editor()    # the table eats enter before the app binding sees it

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self.reveal is None:          # the reveal moves the cursor per row; one git call is enough
            self.show_detail(event.cursor_row)

    def show_detail(self, index=None) -> None:
        """The dead space under a short list, spent on what the row can only hint at: the
           repo's sync state in words, what is uncommitted here, and what the last commits were."""
        panel = self.query_one("#detail", Static)
        table = self.query_one(DataTable)
        index = table.cursor_row if index is None else index
        if not self.rows or not 0 <= index < len(self.rows):
            return
        r = self.rows[index]
        n = self.detail_rows()
        panel.display = n >= 2       # a short terminal gives the space back to the board
        if not panel.display:
            return
        width = self.board_width(panel) - 2          # indented under the title, past the bar
        log_w = min(LOG_W, (width - GAP * 2) // 2)
        # ponytail: two git calls per cursor move, measured 10-40ms on this ~/dev. Move them to
        # an exclusive thread worker if a repo ever makes scrolling the board stutter.
        files = git(r["path"], "status", "--porcelain").splitlines()
        # a clean repo has no second column, so the history runs on into it rather than
        # leaving half the panel blank
        log = git(r["path"], "log", f"-{n if files else n * 2}", "--abbrev=7",
                  "--format=%h\x1f%s\x1f%ct").splitlines()
        right_w = log_w if not files else width - GAP * 2 - log_w

        head = Text("▌ ", style=BLUE)
        head.append(short(r["path"]), style=f"bold {TEXT}")
        head.append(f"   {r['branch']}", style=DIM if r["branch"] in ("main", "master") else BLUE)
        if isinstance(r["upstream"], str):
            head.append(f" → {r['upstream']}", style=SUBTLE)

        # every badge from the row, said in words
        recent = sum(r["days"][-self.days:])
        facts = Text("  ")
        for text, style, on in (
                (f"{r['dirty']} uncommitted", AMBER, r["dirty"]),
                ("working tree clean", SUBTLE, not r["dirty"]),
                (f"{r['ahead']} to push", BLUE, r["ahead"]),
                (f"{r['behind']} to pull", CYAN, r["behind"]),
                ("no upstream", DIM, not r["upstream"]),
                ("ci failing", RED, r["ci"]),
                (f"{recent} commit{'s' * (recent != 1)} in {self.days}d", SUBTLE, True),
                (f"last {fmt_age(r['ts'])} ago", SUBTLE, r["ts"])):
            if on:
                facts.append("   ·   " if len(facts) > 2 else "", style=FAINT)
                facts.append(text, style=style)

        def label(text, count=None):
            t = Text(text, style=f"bold {SUBTLE}")
            if count is not None:
                t.append(f"  {count}", style=FAINT)
            return t

        labels = Text("  "); labels.append_text(label("RECENT COMMITS"))
        if files:
            labels.truncate(2 + log_w + GAP * 2, pad=True)
            labels.append_text(label("WORKING TREE", len(files)))

        def commit(i, w):
            t = Text()
            if i < len(log):
                sha, _, rest = log[i].partition("\x1f")
                subject, _, ts = rest.rpartition("\x1f")
                age = fmt_age(int(ts)) if ts.isdigit() else ""
                t.append(f"{sha}  ", style=HEAT[3])
                body = Text(subject, style=FG if i == 0 else DIM)
                body.truncate(max(0, w - len(t.plain) - AGE_W - 1), overflow="ellipsis", pad=True)
                t.append_text(body); t.append(age.rjust(AGE_W + 1), style=SUBTLE)
            t.truncate(w, pad=True)
            return t

        tree = {"M": AMBER, "A": GREEN, "D": RED, "R": BLUE, "?": SUBTLE}
        body = [head, facts, Text(), labels]
        for i in range(n):
            line = Text("  ")
            line.append_text(commit(i, log_w))
            line.append(" " * GAP * 2)
            if files and i < len(files):
                code, _, name = files[i].strip().partition(" ")
                line.append(f"{code:<3}", style=tree.get(code[:1], DIM))
                right = Text(name.strip(), style=DIM)
                right.truncate(max(0, right_w - 3), overflow="ellipsis")
                line.append_text(right)
            elif not files:
                line.append_text(commit(n + i, right_w))
            body.append(line)
        for line in body:
            line.truncate(width + 2)     # nothing here may wrap; the pane's height is fixed
        panel.update(Text("\n").join(body))

    def measure(self, width):
        """lane | flags | strip | age. The strip takes what's left, rounded down to whole weeks —
           and the board then shrinks to that, so no slack opens up between the columns."""
        lane = 2 + max(len(r["name"]) + (0 if r["branch"] in ("main", "master") else len(r["branch"]) + 2)
                       for r in self.rows)
        flags = max([FLAGS_W] + [len(self.flags(r).plain) for r in self.rows])
        room = max(0, width - lane - flags - AGE_W - 3 * GAP)
        days = fit_days(room)
        return lane, flags, (strip_width(days) if room >= MIN_DAYS else 0), days

    @staticmethod
    def flags(r) -> Text:
        return fmt_flags(r["dirty"], r["behind"], r["ahead"], r["upstream"], r["ci"])

    def row_line(self, r) -> Text:
        # the bar carries the worst thing about the repo; colour is the whole status column
        bar = RED if r["ci"] else CYAN if r["behind"] else AMBER if r["dirty"] else BLUE if r["ahead"] else GUTTER
        t = Text("▌ ", style=bar)
        org, _, leaf = r["name"].rpartition("/")
        if org:                                      # the group recedes; the repo is the name
            t.append(f"{org}/", style=FAINT if fade(r["ts"]) == FADE[-1][1] else SUBTLE)
        t.append(leaf, style=fade(r["ts"]))
        if r["branch"] not in ("main", "master"):   # naming "main" ten times says nothing
            t.append(f"  {r['branch']}", style=BLUE)
        t.append(" " * (self.lane_w - len(t.plain) + GAP))
        flags = self.flags(r)
        t.append_text(flags); t.append(" " * (self.flags_w - len(flags.plain) + GAP))
        if self.strip_w:
            strip = heat_strip(r["days"][-self.days:])
            t.append(" " * (self.strip_w - len(strip.plain)))   # today hugs the right edge
            t.append_text(strip)
            t.append(" " * GAP)
        t.append(fmt_age(r["ts"]).rjust(AGE_W), style=fade(r["ts"]))
        return t

    def fill(self, animate: bool = True) -> None:
        table = self.query_one(DataTable)
        if self.reveal is not None:      # a reload mid-reveal cancels the old one
            self.reveal.stop(); self.reveal = None
        table.clear()
        if not self.rows:
            return
        self.lane_w, self.flags_w, self.strip_w, self.days = self.measure(
            min(MAX_W, table.scrollable_content_region.width or self.size.width - 4))
        # the list keeps only the height it uses, so the detail pane sits right under it
        table.styles.height = max(3, min(len(self.rows) + 1, self.size.height - self.CHROME))
        self.query_one("#cols", Static).update(self.column_header())
        if animate:
            self.queue = list(self.rows)
            self.reveal = self.set_interval(REVEAL_STEP, self.reveal_one)
        else:
            for r in self.rows:
                table.add_row(self.row_line(r))
            if 0 < self.keep < len(self.rows):
                table.move_cursor(row=self.keep)
            self.show_detail()
        status = Text(short(ROOT), style=DIM)
        # count bright, word quiet: the eye lands on the numbers first
        for key, word, style in (("dirty", "dirty", AMBER), ("ahead", "unpushed", BLUE),
                                 ("behind", "behind", CYAN), ("ci", "ci failing", RED)):
            count = sum(1 for r in self.rows if r[key])
            if count:
                status.append("     "); status.append(str(count), style=f"bold {style}")
                status.append(f" {word}", style=SUBTLE)
        status.append("     "); status.append(str(len(self.rows)), style=f"bold {FG}")
        status.append(" repos", style=SUBTLE)
        self.head(status)

    def column_header(self) -> Text:
        """Labels over lane and badges, month names over the strip, so it reads as a timeline."""
        t = Text(" " * 2)
        t.append("REPOSITORY".ljust(self.lane_w - 2 + GAP), style=f"bold {SUBTLE}")
        t.append("STATUS".ljust(self.flags_w + GAP), style=f"bold {SUBTLE}")
        if self.strip_w:
            axis = month_axis(self.days)
            t.append(axis.rjust(self.strip_w) + " " * GAP, style=SUBTLE)
        t.append("LAST".rjust(AGE_W), style=f"bold {SUBTLE}")
        return t

    @work(thread=True, exclusive=True)
    def load_rows(self, animate: bool = True) -> None:
        """Off the event loop: the intro line, the keys and the reveal all stay live while
           git runs. exclusive, so held `r` or a tick landing mid-scan cancels the old one."""
        rows = scan(ROOT)
        self.call_from_thread(self.apply_rows, rows, animate)

    def apply_rows(self, rows, animate: bool) -> None:
        if self.spin is not None:
            self.spin.stop(); self.spin = None
        self.keep = self.query_one(DataTable).cursor_row   # a reload must not yank the cursor to row 0
        self.rows = rows
        self.fill(animate)


def selftest():
    assert parse_counts("3\t4") == (3, 4) and parse_counts("") == (0, 0)
    assert fmt_flags(0, 0, 0, True).plain == ""          # a clean synced repo says nothing
    assert fmt_flags(0, 0, 0, False).plain == "local"
    assert fmt_flags(3, 4, 2, True).plain == "●3 ↑2 ↓4"  # dirty, then ahead, then behind
    assert fmt_flags(1, 0, 0, True, True).plain == "●1 ✗ci"
    run = lambda w, c, s="completed": {"workflowName": w, "status": s, "conclusion": c}
    assert parse_ci("") is False and parse_ci("[]") is False
    assert parse_ci(json.dumps([run("ci", "failure")])) is True
    assert parse_ci(json.dumps([run("ci", "success"), run("ci", "failure")])) is False  # newest wins
    assert parse_ci(json.dumps([run("pages", "success"), run("ci", "failure")])) is True  # per workflow
    assert parse_ci(json.dumps([run("ci", "", "in_progress"), run("ci", "failure")])) is True  # last finished
    assert parse_ci(json.dumps([run("ci", "cancelled")])) is False
    assert fmt_age(0) == "—"                     # ts 0 is the "no commits" sentinel, not the epoch
    assert fmt_age(1, now=1) == "0s"
    assert fmt_age(1, now=1 + 21 * 3600) == "21h"
    assert fmt_age(1, now=1 + 150 * 86400) == "4mo"
    assert fmt_age(1, now=1 + 900 * 86400) == "2y"
    assert fade(1, now=1 + 86400) == FADE[0][1]
    assert fade(1, now=1 + 40 * 86400) == FADE[-1][1] and fade(0) == FADE[-1][1]
    assert fade(10, now=0) == FADE[0][1]         # a commit "in the future" still reads as fresh
    home = str(Path.home())
    assert short(Path(home) / "dev") == "~/dev" and short(Path(home)) == "~"
    assert short(Path("/opt/other")) == "/opt/other"
    assert short(Path(home + "x/dev")) == home + "x/dev"   # prefix match must not false-positive
    assert editor_cmd({"DEVBOARD_EDITOR": "mate"}) == "mate"   # env override wins
    assert editor_cmd({}) != ""                                # always falls back to something
    assert heat_index(0, 5) == 0 and heat_index(5, 5) == len(HEAT) - 1   # silent vs peak day
    assert heat_index(1, 10) == 1        # a 1-commit day never rounds away to "silent"
    assert heat_index(1, 1) == len(HEAT) - 1                             # lone commit reads as peak
    assert len(heat_strip([1] * 14).plain) == strip_width(14) == 15      # 14 days + one week gap
    assert heat_strip([0] * 14).plain == IDLE * 7 + " " + IDLE * 7       # gap sits on the week line
    assert strip_width(0) == 0 and strip_width(7) == 7
    assert all(strip_width(fit_days(w)) <= w and strip_width(fit_days(w) + 7) > w   # widest that fits
               for w in range(MIN_DAYS, strip_width(MAX_DAYS)))
    assert fit_days(3) == MIN_DAYS and fit_days(1000) == MAX_DAYS        # clamped both ends
    assert all(fit_days(w) % 7 == 0 for w in range(1, 200))              # always whole weeks
    assert heat_strip([]).plain == ""
    axis = month_axis(14, today=date(2026, 3, 4))   # strip runs Feb 19 .. Mar 4, Mar 1 is day 10
    assert len(axis) == strip_width(14) and axis.index("Mar") == 11   # day 10 + one week gap
    assert month_axis(14, today=date(2026, 3, 1)).strip() == ""      # no room: Mar 1 is the last cell
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        for d in ("a/.git", "org/b/.git", "org/notes", ".hidden/c/.git", "x/y/z/.git"):
            (t / d).mkdir(parents=True)
        (t / "file").touch()
        assert sorted(str(p.relative_to(t)) for p in repos(t)) == ["a", "org/b"]   # one level, no dotdirs
    print("ok")


def smoke(size=(120, 40), timeout=30.0):
    """Boot the real app headless and check the layout the pure functions can't reach: that the
       rows the table actually holds are one width, that the strip fills the board, and that the
       panel agrees with the room it has. Point it at a folder of repos: `devboard ~/work --smoke`."""
    import asyncio

    async def run():
        app = Devboard()
        async with app.run_test(size=size) as pilot:
            table = app.query_one(DataTable)
            head = lambda: app.query_one("#head", Static).render().plain
            assert "scanning" in head() and head()[head().index("scanning") - 2] in SPIN, "no spinner while scanning"
            deadline = time.time() + timeout      # scan() is a thread worker, and git scales with repos
            while time.time() < deadline and (not app.rows or table.row_count < len(app.rows)):
                await pilot.pause(); await asyncio.sleep(0.05)
            assert app.rows, f"scan found no git repos under {ROOT} after {timeout:.0f}s"
            assert table.row_count == len(app.rows), "board did not finish revealing"
            assert app.spin is None and "scanning" not in head(), "spinner outlived the scan"

            board = min(MAX_W, table.scrollable_content_region.width)   # what refresh() measured: no scrollbar gutter
            want = app.lane_w + app.strip_w + app.flags_w + AGE_W + GAP * (3 if app.strip_w else 2)
            for i in range(table.row_count):
                got = len(table.get_row_at(i)[0].plain)
                assert got == want, f"row {i} renders {got} wide, the layout says {want}"
            assert want <= board, f"the board overflows its {board} columns"
            assert board - want < 8, f"{board - want} columns of slack: the strip stopped filling"

            await pilot.press("down")
            await pilot.pause()
            panel = app.query_one("#detail", Static)
            assert panel.display == (app.detail_rows() >= 2), "panel disagrees with the room it has"
            if panel.display:
                assert short(app.rows[table.cursor_row]["path"]) in panel.render().plain
    asyncio.run(run())
    print("ok")


def main():
    if "--selftest" not in sys.argv and not ROOT.is_dir():
        sys.exit(f"devboard: {ROOT} is not a directory")
    if "--selftest" in sys.argv:
        selftest()
    elif "--smoke" in sys.argv:
        smoke()
    else:
        Devboard().run()


if __name__ == "__main__":
    main()
