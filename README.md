# GitStats

A Python script that analyzes a Git repository and generates a self-contained HTML statistics report viewable offline in any browser.

## Primary Metrics

- **Summary** — project age, lines of code, weekly cadence, release count, commit velocity trend, monthly commit activity chart, bus factor, and hourly punchcard
- **Impact** — weighted leaderboard ranking authors and teams by commit volume, lines changed, and active tenure
- **Authors** — sortable contributor table with team badges, filterable by component or team
- **Teams** — per-team stats, member lists, and top components
- **Releases** — per-release breakdown of commits by author and team
- **Components** — churn chart for directories that contain a component marker file (configurable; defaults to `make.py`, `pyproject.toml`, `setup.py`, `Makefile`, `meta.yaml`)

## Requirements

Python 3.11+

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

```bash
python gitstats.py -s ~/projects/myrepo -c config.json -o report.html \
  -support ~/projects/myrepo-recipes \
  -support ~/projects/myrepo-tools
```

On success, `tailwind.js` and `chart.js` are copied from the externals directory into the same directory as the output HTML so the report works fully offline or from a self-hosted webserver.

If no config file is found, all authors are grouped under a single "Community" team and no aliases are applied.

## Support Repositories

Some projects spread their work across multiple Git repositories — a main repo and separate repos for recipes, tools, tests, or packaging. The `-support` flag folds those additional histories into a single unified report. It may be repeated for any number of additional repositories.

### What merges across all repositories

| Data | Behavior |
|------|----------|
| Author commit counts | Combined — each commit in any repo is credited to the resolved canonical author |
| Author line stats | Combined — additions and deletions accumulate across all repos |
| Team attribution | Combined — the same config teams and aliases apply to all repos |
| Hourly punchcard | Combined |
| Monthly commit activity | Combined |
| Impact scoring | Computed over the combined author and team data |

### What stays main-repository-only

| Data | Reason |
|------|--------|
| Components tab | Each repo gets its own component chart card (main repo first, support repos below) |
| Release tags | Tag names and release ranges are specific to the main repo's version history |
| File count | Reflects the current tracked files in the main repo |
| Repo age | Derived from the oldest and newest commit in the main repo |

The Components tab shows a separate churn chart for each repository. The main repo card appears first; each support repo card is labeled **Support Repository**. Clicking any bar on any card filters the Authors table to contributors who touched that component.

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
  "monthly_top_authors": 3,
  "bus_factor_threshold": 0.5,

  "component_markers": ["pyproject.toml", "Cargo.toml", "CMakeLists.txt"],
  "loc_extensions": [".py", ".cc", ".c", ".cpp", ".h", ".hpp", ".rs"]
}
```

### General

| Key | Default | Description |
|-----|---------|-------------|
| `release_tag_prefix` | `""` | Only tags whose name starts with this prefix appear in the Releases tab. `"v"` includes `v1.0` but excludes `nightly-20240101`. An empty string includes all tags. |
| `max_release_tags` | `20` | Maximum number of release tags to display, taken from the most recent. `0` shows all tags. |
| `summary_velocity_days` | `[30, 90]` | Day windows shown as velocity cards on the Summary tab. Each entry produces one card comparing commits in the last N days against the prior N days. |
| `monthly_top_authors` | `3` | Number of top contributors listed in the monthly commit activity chart tooltip. Set to `0` to show only the total commit count. |
| `bus_factor_threshold` | `0.5` | Fraction of total commits (0–1) used to compute the bus factor — the fewest contributors whose combined commits reach this fraction. |
| `component_markers` | *(see below)* | Filenames that identify a component root. Any directory directly containing one of these files becomes a component in the churn chart. Replaces the default set entirely. |
| `loc_extensions` | *(see below)* | File extensions (with leading dot) counted toward the **Lines of Code** tile. Matched case-insensitively. Replaces the default set entirely. |

#### Component markers

A component is any directory whose immediate contents include a marker file. The built-in defaults are:

```
make.py  pyproject.toml  setup.py  Makefile  meta.yaml
```

Add build-system-specific markers (`"Cargo.toml"`, `"CMakeLists.txt"`, `"go.mod"`, `"BUILD"`) or remove any that appear in too many directories and would fragment the chart into trivial components.

#### Lines of code extensions

Only files whose extension matches an entry in `loc_extensions` are counted toward the **Lines of Code** tile. The built-in defaults are:

```
.py  .cc  .c  .cpp  .h  .hpp  .rs
```

Add extensions for languages not in the default set (e.g. `".ts"`, `".go"`, `".java"`, `".swift"`). Extensions must include the leading dot.

### Teams

Defining teams unlocks a dedicated **Teams tab** and enriches every other tab with team context:

- **Impact tab** — team rankings alongside the author leaderboard, with click-to-filter
- **Authors tab** — each contributor row shows their team badge
- **Releases tab** — each release card shows a per-team commit and impact breakdown
- **Components tab** — team ownership breakdown per component

Without teams, all authors are grouped under a single **Community** label and the Teams tab is not shown.

`teams` is an object where each key is a team name and the value is an object with:

| Key | Required | Description |
|-----|----------|-------------|
| `members` | Yes | List of git author names and/or email addresses. Names are matched exactly; emails are matched case-insensitively. |
| `color` | No | Hex color for this team's badges and charts. If omitted, a color is assigned from a built-in palette. |

Authors not listed in any team are grouped into a built-in **Community** team shown in slate gray.

#### Time-ranged membership

Members can belong to a team for a specific date range, useful when contributors switch teams over time:

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

Each commit is credited to whichever team the author belonged to **at the time of that commit**.

### Aliases

`aliases` merges multiple git identities into a single canonical name:

```json
"aliases": {
  "Canonical Name": ["old name", "old@email.com", "otheremail@example.com"]
}
```

Each key is the canonical display name. Each value is a list of alternate names or email addresses matched case-insensitively.

## Impact Score

The impact score is a single number in the range 0–100 that answers: *who did the most meaningful, sustained work in this repository?* It balances three signals so that no single dimension can dominate the ranking:

```
score = (commits / max_commits)         × 40
      + (effective_lines / max_lines)   × 40
      + (tenure_days / max_tenure)      × 20
```

Each metric is normalized against the top performer in that dimension.

- **Commits (40%)** — rewards consistent, incremental contribution.
- **Effective lines (40%)** — volume of real code changed, after noise filtering. Reformats and reverts score near zero.
- **Tenure (20%)** — days between first and last commit. A burst of 500 commits over one week scores lower than 500 commits spread over three years.

### Noise filtering

Raw line counts from `git log --numstat` overcount meaningless work. The effective lines metric applies a three-step pipeline before scoring.

**Step 1 — Net lines** (`impact_use_net_lines`, default `true`): Each commit contributes `|adds − dels|` rather than `adds + dels`. A reformatting pass that deletes and re-adds 10,000 lines scores near zero.

**Step 2 — Percentile cap** (`impact_line_cap_percentile`, default `95`): Each commit's contribution is capped at the 95th percentile of all commits. A one-time bulk import won't overshadow years of regular work.

**Step 3 — Wash-window detection** (`impact_wash_window_days`, default `7`): Catches the two-commit revert pattern Step 1 misses. Commits are grouped into N-day buckets per author; if a bucket's gross lines exceed `impact_wash_min_gross` and at least 67% of the changes cancel out, the bucket scores its raw net instead.

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
- **Raise `IMPACT_W_LINES`, lower `IMPACT_W_COMMITS`** for greenfield codebases where volume of real code matters more than commit discipline.
- **Raise `IMPACT_W_TENURE`** to weight long-term contributors more heavily relative to recent high-activity newcomers.
- **Set `impact_use_net_lines: false`** if gross volume is genuinely meaningful (e.g., large automated test generation).
- **Lower `impact_line_cap_percentile`** (e.g., `80`) for tighter outlier control; `0` to disable.
- **Lower `impact_wash_window_days`** (e.g., `3`) for high-frequency repos where revert pairs happen within days.
