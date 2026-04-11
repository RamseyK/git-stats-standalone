# GitStats

A Python script that analyzes a Git repository and generates a self-contained HTML statistics report viewable offline in any browser.

## Primary Metrics

- **Summary** — project age, net lines, weekly cadence, release count, commit velocity trend, bus factor, and hourly punchcard
- **Impact** — weighted leaderboard ranking authors and teams by commit volume, lines changed, and active tenure
- **Authors** — sortable contributor table with team badges, filterable by component or team
- **Teams** — per-team stats, member lists, and top components
- **Releases** — per-release breakdown of commits by author and team
- **Components** — churn chart for directories that contain a component marker file (configurable; defaults to `make.py`, `pyproject.toml`, `setup.py`, `Makefile`, `meta.yaml`)

## Requirements

Python 3.8+

## Usage

```
python gitstats.py -s <repo-path> -o <output.html> [-c <config.json>] [-externals <dir>] [-support <repo-path>] ...
```

| Flag | Required | Description |
|------|----------|-------------|
| `-s`, `--source` | Yes | Path to the primary Git repository to analyze |
| `-o`, `--output` | Yes | Path for the generated HTML file |
| `-c`, `--config` | No | Path to a config JSON file (default: `./config.json`) |
| `-externals`, `--externals` | No | Path to the directory containing `tailwind.js` and `chart.js` (default: `./externals`) |
| `-support`, `--support` | No | Path to an additional Git repository whose commits contribute to the combined stats. Repeatable for multiple support repositories. |

**Example:**

```bash
python gitstats.py -s ~/projects/myrepo -c config.json -o report.html
```

**Example with support repositories:**

```bash
python gitstats.py -s ~/projects/myrepo -c config.json -o report.html \
  -support ~/projects/myrepo-recipes \
  -support ~/projects/myrepo-tools
```

On success, `tailwind.js` and `chart.js` are copied from the externals directory into the same directory as the output HTML so the report works fully offline or from a self-hosted webserver.

If no config file is found, all authors are grouped under a single "Community" team and no aliases are applied.

## Support Repositories

Some projects spread their work across multiple Git repositories — a main repo and separate repos for recipes, tools, tests, or packaging. Support repositories let you fold those additional histories into a single unified report.

```bash
python gitstats.py -s ~/projects/myrepo -c config.json -o report.html \
  -support ~/projects/myrepo-recipes
```

The `-support` flag may be repeated to include any number of additional repositories. Support repos may be submodules checked out separately or entirely unrelated directories.

### What merges across all repositories

| Data | Behavior |
|------|----------|
| Author commit counts | Combined — each commit in any repo is credited to the resolved canonical author |
| Author line stats | Combined — additions and deletions accumulate across all repos |
| Team attribution | Combined — the same config teams and aliases apply to all repos |
| Activity heatmap and punchcard | Combined |
| Impact scoring | Computed over the combined author and team data |

### What stays main-repository-only

| Data | Reason |
|------|--------|
| LOC history chart | A per-file running total only makes sense within a single repo's history; the chart notes this |
| Components tab | Each repo gets its own component chart card (main repo first, support repos below) |
| Release tags | Tag names and release ranges are specific to the main repo's version history |
| File count | Reflects the current tracked files in the main repo |
| Repo age | Derived from the oldest and newest commit in the main repo |

### Per-repo component cards

The Components tab shows a separate churn chart for each repository. The main repo card appears first; each support repo card is labeled **Support Repository** so it is clearly distinguished. Clicking any bar on any card filters the Authors table to contributors who touched that component.

## Configuration

All configuration lives in a single JSON file. Every key is optional.

```json
{
  "release_tag_prefix": "v",
  "max_release_tags": 50,

  "teams": {
    "Core": {
      "color": "#3b82f6",
      "members": [
        "Jane Smith",
        "jane@example.com",
        "jsmith@work.com"
      ]
    },
    "Build & Packaging": {
      "members": [
        "Carlos Rivera",
        "carlos@example.com"
      ]
    }
  },

  "aliases": {
    "Jane Smith": [
      "jane@example.com",
      "jsmith@work.com",
      "jane.smith@oldcompany.com"
    ]
  },

  "impact_use_net_lines": true,
  "impact_wash_window_days": 7,
  "impact_wash_min_gross": 200,
  "impact_line_cap_percentile": 95,

  "summary_velocity_days": [30, 90],
  "bus_factor_threshold": 0.5
}
```

### General

| Key | Default | Description |
|-----|---------|-------------|
| `release_tag_prefix` | `""` | Only tags whose name starts with this prefix appear in the Releases tab. For example, `"v"` includes `v1.0`, `v2.3.1` but excludes `nightly-20240101`. An empty string includes all tags. |
| `max_release_tags` | `20` | Maximum number of release tags to display, taken from the most recent. Set to `0` to show all tags with no limit. |
| `component_markers` | *(see below)* | List of filenames that identify a component boundary. Any directory directly containing one of these files becomes a component root in the Components tab churn chart. When omitted, the built-in default set is used. |
| `summary_velocity_days` | `[30, 90]` | List of day windows shown as commit velocity cards on the Summary tab. Each entry produces one card comparing commits in the last N days against the prior N days. Uses the combined commit history across all repositories. |
| `bus_factor_threshold` | `0.5` | Fraction of total commits (0–1) used to compute the bus factor. The bus factor is the fewest contributors whose combined commits reach this fraction. For example, `0.5` answers "how many people own 50% of the codebase?" |

#### Component markers

A **component** is any directory whose immediate contents include a marker file. The built-in default markers are:

```
make.py  pyproject.toml  setup.py  Makefile  meta.yaml
```

Override this list in config to match the conventions of the repos you analyse:

```json
{
  "component_markers": ["pyproject.toml", "Cargo.toml", "CMakeLists.txt"]
}
```

**When to customise:**

- **Add markers** (`"Cargo.toml"`, `"CMakeLists.txt"`, `"BUILD"`, `"BUCK"`, `"go.mod"`) to detect component boundaries specific to your build system.
- **Remove markers** (e.g. drop `"Makefile"`) when a file is present in almost every directory — leaving it in would fragment the chart into dozens of trivial components rather than meaningful project modules.
- The config list **replaces** the default set entirely, so include every marker you want.

### Teams

Defining teams unlocks a dedicated **Teams tab** in the report (hidden when no teams are configured) and enriches every other tab with team context:

- **Impact tab** — team rankings alongside the author leaderboard, with click-to-filter so you can isolate any team's contributors
- **Authors tab** — each contributor row shows their team badge
- **Releases tab** — each release card shows a per-team commit and impact breakdown
- **Components tab** — team ownership breakdown per component

Without teams, all authors are grouped under a single **Community** label and the Teams tab is not shown.

`teams` is an object where each key is a team name and the value is an object with:

| Key | Required | Description |
|-----|----------|-------------|
| `members` | Yes | List of git author names and/or email addresses belonging to this team. Names are matched exactly; emails are matched case-insensitively. A contributor can be listed by both name and email — the first match wins. |
| `color` | No | Hex color for this team's badges and charts (e.g. `"#3b82f6"`). If omitted, a color is assigned automatically from a built-in palette. |

Authors not listed in any team are grouped into a built-in **Community** team shown in slate gray.

#### Time-ranged membership

Members can belong to a team for a specific date range, useful when contributors switch teams over time. Replace the plain string entry with an object:

```json
"members": [
  "Always Member",
  {"name": "Jane Smith", "from": "2021-01-01", "to": "2022-12-31"},
  {"name": "bob@example.com", "from": "2023-06-01"}
]
```

| Field | Description |
|-------|-------------|
| `name` | Git author name or email address |
| `from` | Start date (`YYYY-MM-DD`). Omit to mean "since the beginning of the repo". |
| `to` | End date (`YYYY-MM-DD`), inclusive. Omit to mean "through the present". |

Each commit is credited to whichever team the author belonged to **at the time of that commit**. This ensures team impact scores reflect actual historical membership rather than a contributor's current team assignment.

### Aliases

`aliases` merges multiple git identities into a single canonical name. This is useful when a contributor has committed under different names or email addresses over time.

```json
"aliases": {
  "Canonical Name": ["old name", "old@email.com", "otheremail@example.com"]
}
```

Each key is the canonical display name. Each value is a list of alternate names or email addresses that should be treated as the same person. Emails in the alias list are matched case-insensitively.

## Impact Score

The impact score is a single number in the range 0–100 that answers: *who did the most meaningful, sustained work in this repository?* It balances three signals so that no single dimension can dominate the ranking:

```
score = (commits / max_commits)         × 40
      + (effective_lines / max_lines)   × 40
      + (tenure_days / max_tenure)      × 20
```

Each metric is normalized against the top performer in that dimension, so the leader in any one category always contributes the full weight for that category and everyone else is proportional.

- **Commits (40%)** — rewards consistent, incremental contribution. Hard to inflate without corresponding lines.
- **Effective lines (40%)** — volume of real code changed, after noise filtering. Reformats and reverts score near zero.
- **Tenure (20%)** — days between first and last commit. A burst of 500 commits over one week scores lower than 500 commits spread over three years.

### Noise filtering

Raw line counts from `git log --numstat` overcount meaningless work. The effective lines metric applies a three-step pipeline before scoring.

**Step 1 — Net lines** (`impact_use_net_lines`, default `true`): Each commit contributes `|adds − dels|` rather than `adds + dels`. A reformatting pass that deletes and re-adds 10,000 lines scores near zero.

**Step 2 — Percentile cap** (`impact_line_cap_percentile`, default `95`): Each commit's effective contribution is capped at the 95th percentile of all commits. A one-time bulk import won't overshadow years of regular work.

**Step 3 — Wash-window detection** (`impact_wash_window_days`, default `7`): Catches the two-commit revert pattern Step 1 misses — a large delete on Monday and re-add on Friday each have `|adds − dels|` near zero individually, but together represent no net change. Commits are grouped into N-day buckets per author; if a bucket's gross lines exceed `impact_wash_min_gross` and at least 67% of the changes cancel out, the bucket scores its raw net instead. `impact_wash_min_gross` (default `200`) prevents small balanced edits from being mistakenly zeroed out.

The exact configured values and computed cap are displayed on the **Impact** tab under "Lines noise filtering."

| Key | Default | Description |
|-----|---------|-------------|
| `impact_use_net_lines` | `true` | `true` → `\|adds − dels\|` per commit; `false` → raw `adds + dels`. |
| `impact_wash_window_days` | `7` | Size of the wash-window in days. Set to `0` to disable. |
| `impact_wash_min_gross` | `200` | Minimum gross lines in a bucket to trigger wash detection. |
| `impact_line_cap_percentile` | `95` | Per-commit line cap percentile. Set to `0` to disable. |

### Tuning

The defaults suit most open-source projects. To change the metric weights, edit the class constants at the top of `gitstats.py` (must sum to 100):

```python
IMPACT_W_COMMITS = 40   # commit count
IMPACT_W_LINES   = 40   # effective lines changed
IMPACT_W_TENURE  = 20   # active tenure in days
```

- **Raise `IMPACT_W_COMMITS`, lower `IMPACT_W_LINES`** for projects that value steady incremental work over large code drops.
- **Raise `IMPACT_W_LINES`, lower `IMPACT_W_COMMITS`** for greenfield or research codebases where volume of real code matters more than commit discipline.
- **Raise `IMPACT_W_TENURE`** to weight long-term contributors more heavily relative to recent high-activity newcomers.
- **Set `impact_use_net_lines: false`** if gross volume is genuinely meaningful (e.g., large automated test generation).
- **Lower `impact_line_cap_percentile`** (e.g., `80`) for tighter outlier control; `0` to disable.
- **Lower `impact_wash_window_days`** (e.g., `3`) for high-frequency repos where revert pairs happen within days.
