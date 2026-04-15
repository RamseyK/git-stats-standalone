# Git Stats Standalone

A Python script that analyzes a Git repository and generates a self-contained HTML statistics report viewable offline in any browser. The most important design principal of this project: All computed statistics and scoring **must** serve a purpose to help highlight the actual impact of contributors (and optionally their teams) make on a codebase. All metrics added must assist in measuring true impact, effort, and expertise of contributors to a codebase (based on git history).

## Primary Metrics

- **Summary** — project age, lines of code, weekly cadence, release count, commit velocity trend, monthly commit activity chart, bus factor (commits and PR merges), and hourly punchcard
- **Impact** — weighted leaderboard ranking authors and teams by commit volume, lines changed, active tenure, PR merges, and (optionally) issues addressed; each row shows all active dimensions inline
- **Authors** — sortable contributor table with team badges, filterable by component or team; optional Issues column when `issue_tag_prefixes` is configured
- **Teams** — per-team stats, member lists, and top components
- **Releases** — per-release breakdown of commits by author and team, with referenced issue count when `issue_tag_prefixes` is configured
- **Components** — churn chart for directories that contain a component marker file (configurable; defaults to `make.py`, `pyproject.toml`, `setup.py`, `Makefile`, `meta.yaml`)

## Requirements

* Python 3.11+

## Usage

```
python gitstats.py -s <repo-path> -o <output.html> [-c <config.json>] [-externals <dir>] [-support <repo-path>] ...
```

| Flag | Required | Description |
|------|----------|-------------|
| `-s`, `--source` | Yes | Path to the primary Git repository to analyze |
| `-o`, `--output` | Yes | Path for the generated HTML file |
| `-c`, `--config` | No | Path to a config JSON file. If omitted, all settings use built-in defaults (no teams, no aliases). |
| `-externals`, `--externals` | No | Path to the directory containing `tailwind.js` and `chart.js` (default: `./externals`) |
| `-support`, `--support` | No | Path to an additional Git repository whose commits contribute to the combined stats. Repeatable for multiple support repositories. |

```bash
python gitstats.py -s ~/projects/myrepo -c config.json -o report.html \
  -support ~/projects/myrepo-recipes \
  -support ~/projects/myrepo-tools
```

On success, `tailwind.js` and `chart.js` are copied from the externals directory into the same directory as the output HTML so the report works fully offline or from a self-hosted webserver.

Running without `-c` is fully supported. All settings use built-in defaults: every author is shown under a single **Community** label, no aliases are resolved, and all impact weights and noise-reduction settings use the values documented in [Impact Score](#impact-score).

## Configuration

All configuration lives in a single JSON file. Every key is optional.

```json
{
  "release_tag_prefix": "v",
  "max_release_tags": 50,
  "max_authors_per_tag": 20,
  "max_teams_per_tag": 10,
  "primary_branch": "main",

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

  "issue_tag_prefixes": ["PROJ", "BUG"],
  "ignore_commits": [],

  "impact_w_commits": 30,
  "impact_w_lines": 30,
  "impact_w_tenure": 15,
  "impact_w_merges": 25,
  "impact_w_issues": 0,

  "merge_heuristics": ["Pull request #", "Merge remote-tracking branch", "Merge branch"],
  "merge_exclude_primary_branch": true,

  "impact_use_net_lines": true,
  "impact_wash_window_days": 7,
  "impact_wash_min_gross": 200,
  "impact_line_cap_percentile": 95,

  "summary_velocity_days": [30, 90],
  "monthly_top_authors": 3,
  "bus_factor_threshold": 0.5,

  "component_markers": ["pyproject.toml", "Cargo.toml", "CMakeLists.txt"],
  "loc_extensions": [".py", ".cc", ".c", ".cpp", ".h", ".hpp", ".rs", ".cs"]
}
```

### General

| Key | Default | Description |
|-----|---------|-------------|
| `release_tag_prefix` | `""` | Only tags whose name starts with this prefix appear in the Releases tab. `"v"` includes `v1.0` but excludes `nightly-20240101`. An empty string includes all tags. |
| `max_release_tags` | `20` | Maximum number of release tags to display, taken from the most recent. `0` shows all tags. |
| `max_authors_per_tag` | `20` | Maximum number of authors shown in the per-release breakdown on the Releases tab. |
| `max_teams_per_tag` | `10` | Maximum number of teams shown in the per-release breakdown on the Releases tab. |
| `primary_branch` | `"develop"` | Name of the primary branch. Pull requests merged into this branch are counted toward each committer's PR Merges impact dimension. |
| `issue_tag_prefixes` | `[]` | List of issue tracker prefixes (e.g. `["PROJ", "BUG"]`). Commit subjects are scanned for patterns like `PROJ-1234`. When set, the Authors tab gains an Issues column and sort button, each release card shows its referenced issue count, and the `impact_w_issues` dimension becomes available. Each unique issue is counted once per author even if referenced multiple times. |
| `summary_velocity_days` | `[30, 90]` | Day windows shown as velocity cards on the Summary tab. Each entry produces one card comparing commits in the last N days against the prior N days. |
| `monthly_top_authors` | `3` | Number of top contributors listed in the monthly commit activity chart tooltip. Set to `0` to show only the total commit count. |
| `bus_factor_threshold` | `0.5` | Fraction (0–1) used to compute both bus factors — the fewest contributors whose combined commits (or PR merges) reach this fraction. The PR Merges bus factor is hidden when `impact_w_merges` is `0`. |
| `merge_heuristics` | *(see below)* | Substrings matched case-insensitively against commit subjects to detect squash/rebase merges. Replaces the built-in subject patterns entirely when present. |
| `merge_exclude_primary_branch` | `true` | When `true`, subjects matching `"Merge branch '<primary>'"` are excluded from the subject heuristics — they indicate a sync commit pulling the primary branch into a feature branch rather than a PR landing. Set to `false` to credit those commits. |
| `ignore_commits` | `[]` | List of commit hashes to exclude from all analysis — commit counts, line stats, PR merge credits, activity heatmap, and per-release attribution. Full 40-character SHA1 hashes or unique short prefixes (7+ characters recommended) are accepted. Useful for excluding bulk imports, automated commits, or history-rewriting artifacts. |
| `component_markers` | *(see below)* | Filenames that identify a component root. Any directory directly containing one of these files becomes a component in the churn chart. Replaces the default set entirely. |
| `loc_extensions` | *(see below)* | File extensions (with leading dot) counted toward the **Lines of Code** tile. Matched case-insensitively. Replaces the default set entirely. |

#### Merge heuristics

In addition to true merge commits (two or more parents), GitStats detects squash and rebase merges by matching commit subjects against heuristic patterns. When `merge_heuristics` is absent the built-in patterns are used:

- Subject contains `Pull request #`
- Subject starts with `Merge remote-tracking branch`
- Subject starts with `Merge branch`

When `merge_exclude_primary_branch` is `true` (the default), subjects that match `"Merge branch '<primary>'"` are excluded from the `Merge branch` pattern — they indicate a sync commit pulling the primary branch into a feature branch, not a PR landing. If another configured pattern also matches the same subject, the exclusion does not apply. Set `merge_exclude_primary_branch` to `false` to credit these commits.

When `merge_heuristics` is present in config it **replaces** the built-in subject patterns entirely. Each string is matched as a case-insensitive substring of the commit subject. The `merge_exclude_primary_branch` exclusion continues to apply.

```json
"merge_heuristics": ["Pull request #", "Merge remote-tracking branch"]
```

Set to an empty array `[]` to rely solely on true merge commits (two-parent merges); all subject-based detection is disabled.

**Line count exclusion:** any commit identified as a merge — true or heuristic — has its line additions and deletions excluded from all author and team metrics. Component churn is still tracked. See [Noise filtering — Step 0](#noise-filtering) for the full rationale.

#### Component markers

A component is any directory whose immediate contents include a marker file. The built-in defaults are:

```
make.py  pyproject.toml  setup.py  Makefile  meta.yaml  Cargo.toml  CMakeLists.txt
```

Add build-system-specific markers (`"go.mod"`, `"BUILD"`) or remove any that appear in too many directories and would fragment the chart into trivial components.

#### Lines of code extensions

Only files whose extension matches an entry in `loc_extensions` are counted toward the **Lines of Code** tile. The built-in defaults are:

```
.py  .cc  .c  .cpp  .h  .hpp  .rs, .cs
```

Add extensions for languages not in the default set (e.g. `".ts"`, `".go"`, `".java"`, `".swift"`). Extensions must include the leading dot.

### Teams

Defining teams unlocks a dedicated **Teams tab** and enriches every other tab with team context:

- **Impact tab** — team rankings alongside the author leaderboard, with click-to-filter
- **Authors tab** — each contributor row shows their team badge
- **Releases tab** — each release card shows a per-team commit and impact breakdown
- **Components tab** — team ownership breakdown per component

The Teams tab is not shown if teams are not specified in the configuration.

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

#### Impact attribution and membership tenure

A team's impact score reflects only the work performed by its members **during their active membership**. Commits, lines changed, and PR merges made outside a member's `from`/`to` window are credited to whichever team (or **Community**) they belonged to at that time — they never contribute to the current team's score.

- An author with **no `from` or `to` date** is considered a permanent member of the team for the entire repository history. All of their commits count toward that team.
- An author with a **`from` date only** contributes to the team from that date onward through the present.
- An author with a **`to` date** no longer contributes to the team after that date. Their work during their active period is still counted.
- An author who **switches teams** has their contributions split: commits before the switch count for the old team, commits after count for the new team.

The **Teams tab** reflects this split visually: each team card shows a **Members** section (authors whose membership is currently active) and, when applicable, a **Previous Members** section (authors who have contributed historically but whose membership has since ended).

### Aliases

`aliases` merges multiple git identities into a single canonical name:

```json
"aliases": {
  "Canonical Name": ["old name", "old@email.com", "otheremail@example.com"]
}
```

Each key is the canonical display name. Each value is a list of alternate names or email addresses matched case-insensitively.

## Impact Score

The impact score is a single number in the range 0–100 that answers: *who did the most meaningful, sustained work in this repository?* It balances up to five signals so that no single dimension can dominate the ranking:

```
raw   = (commits / max_commits)       × impact_w_commits
      + (effective_lines / max_lines) × impact_w_lines
      + (tenure_days / max_tenure)    × impact_w_tenure
      + (pr_merges / max_merges)      × impact_w_merges
      + (issues / max_issues)         × impact_w_issues   (when > 0)

score = raw × (100 / sum_of_active_weights)
```

Each metric is normalized against the top performer in that dimension. The final rescaling step keeps scores in the 0–100 range even when one or more dimensions are disabled (see [Tuning](#tuning)).

- **Commits** — rewards consistent, incremental contribution.
- **Effective lines** — volume of real code changed, after noise filtering. Reformats and reverts score near zero.
- **Tenure** — days between first and last commit. A burst of 500 commits over one week scores lower than 500 commits spread over three years.
- **PR Merges** — number of pull requests merged into the primary branch, credited to the committer. Includes true merge commits (detected via both the first-parent spine and any true merge commits that explicitly name the primary branch as the target in their subject line) and squash/rebase merges detected by commit message heuristics.
- **Issues Addressed** — count of unique issue tags (e.g. `PROJ-1234`) found in commit subjects, deduplicated per author. Requires `issue_tag_prefixes` in config. Disabled (`0` weight) by default.

The Impact tab's author and team leaderboard rows show all active dimensions inline — commits, lines, tenure, and (when enabled) PR merges and issues addressed.

### Noise filtering

Raw line counts from `git log --numstat` overcount meaningless work. The effective lines metric applies a four-step pipeline before scoring.

**Step 0 — Merge commit exclusion** (always on): Any commit identified as a merge — true (two or more parents) or heuristic (matched by `merge_heuristics`) — has its line additions and deletions excluded entirely from all author and team metrics before any other filtering takes place. This applies to add/del totals, the lines dimension of the impact score, and the LOC history chart.

A merge commit's diff is not original work. It is one of three things:

- **Conflict resolution** — lines produced mechanically by reconciling two diverging histories, not written by the author. A large conflict can add thousands of lines that belong to the commits being merged, not to the person resolving the conflict.
- **A duplicate of already-counted work** — in a `--no-ff` merge the parent commits already contain the changes; counting the merge commit's diff would double-count every line that came through a feature branch.
- **A sync commit** — when a developer pulls the primary branch into their feature branch (`Merge branch 'main' into feature/x`), the diff represents other people's commits being incorporated. Attributing those lines to the feature branch author misrepresents who did the work.

All three cases would inflate the lines dimension for authors who merge frequently, rewarding integration activity instead of original contribution. Excluding merge lines ensures the metric reflects code that the author actually wrote.

Component churn is still tracked for merge commits — file activity is real regardless of authorship credit, and the Components tab is meant to show where the codebase is changing, not to rank individual contributions.

See [Merge heuristics](#merge-heuristics) for details on how merges are detected and how to customize the patterns.

**Step 1 — Net lines** (`impact_use_net_lines`, default `true`): Each commit contributes `|adds − dels|` rather than `adds + dels`. A reformatting pass that deletes and re-adds 10,000 lines scores near zero.

**Step 2 — Percentile cap** (`impact_line_cap_percentile`, default `95`): Each commit's contribution is capped at the 95th percentile of all commits. A one-time bulk import won't overshadow years of regular work.

**Step 3 — Wash-window detection** (`impact_wash_window_days`, default `7`): Catches the two-commit revert pattern Step 1 misses. Commits are grouped into N-day buckets per author; if a bucket's gross lines exceed `impact_wash_min_gross` and at least 67% of the changes cancel out, the bucket scores its raw net instead.

The exact configured values and computed cap are displayed on the **Impact** tab under "Lines noise filtering."

| Key | Default | Description |
|-----|---------|-------------|
| `impact_w_commits` | `30` | Weight applied to the commit count dimension. Set to `0` to exclude commits from scoring. |
| `impact_w_lines` | `30` | Weight applied to the effective lines changed dimension. Set to `0` to exclude lines from scoring. |
| `impact_w_tenure` | `15` | Weight applied to active tenure in days. Set to `0` to exclude tenure from scoring. |
| `impact_w_merges` | `25` | Weight applied to the PR merge count dimension. Set to `0` to exclude merges from scoring. |
| `impact_w_issues` | `0` | Weight applied to unique issues addressed. Set to a non-zero value (and rebalance other weights) to include this dimension. Requires `issue_tag_prefixes`. |
| `impact_use_net_lines` | `true` | `true` → `\|adds − dels\|` per commit; `false` → raw `adds + dels`. |
| `impact_wash_window_days` | `7` | Size of the wash-window in days. Set to `0` to disable. |
| `impact_wash_min_gross` | `200` | Minimum gross lines in a bucket to trigger wash detection. |
| `impact_line_cap_percentile` | `95` | Per-commit line cap percentile. Set to `0` to disable. |

### PR merge detection

The PR Merges dimension counts merges into `primary_branch` using two strategies: true merge commits (two or more parents) and heuristic detection of squash/rebase merges by commit subject — see [Merge heuristics](#merge-heuristics) for the built-in patterns and how to configure them.

The committer (`git log %cn/%ce`) receives credit — the author of the merged branch does not. Credit accumulates across all repositories (main and support repos).

### Tuning

Weights are configured via `config.json` using the `impact_w_*` keys (see the table above). **All active weights must sum to exactly 100** — if they do not, the program exits with an error. When a weight is `0` that dimension is excluded from scoring entirely and its card is hidden from the Impact tab; the remaining active dimensions must still sum to 100.

The same weights can also be changed permanently by editing the class constants at the top of `gitstats.py`; the config file always takes precedence over the class defaults when a key is present.

- **Raise `impact_w_commits`, lower `impact_w_lines`** for projects that value steady incremental work over large code drops.
- **Raise `impact_w_lines`, lower `impact_w_commits`** for greenfield codebases where volume of real code matters more than commit discipline.
- **Raise `impact_w_tenure`** to weight long-term contributors more heavily relative to recent high-activity newcomers.
- **Raise `impact_w_merges`** to heavily reward reviewers and integrators who merge many PRs.
- **Set `impact_w_merges: 0`** for repositories that do not use a PR workflow or whose primary branch has no merge commits.
- **Set `impact_w_issues` to a non-zero value** (e.g. `10`) and configure `issue_tag_prefixes` to reward contributors who resolve tracked bugs and features. Rebalance the other weights so all active dimensions still sum to 100.
- **Set `impact_use_net_lines: false`** if gross volume is genuinely meaningful (e.g., large automated test generation).
- **Lower `impact_line_cap_percentile`** (e.g., `80`) for tighter outlier control; `0` to disable.
- **Lower `impact_wash_window_days`** (e.g., `3`) for high-frequency repos where revert pairs happen within days.

## Support Repositories

Some projects spread their work across multiple Git repositories — a main repo and separate repos for recipes, tools, tests, or packaging. The `-support` flag folds those additional histories into a single unified report. It may be repeated for any number of additional repositories.

### Metrics computed across all repositories (main and support git repositories)

| Data | Behavior |
|------|----------|
| Author commit counts | Combined — each commit in any repo is credited to the resolved canonical author |
| Author line stats | Combined — additions and deletions accumulate across all repos |
| Team attribution | Combined — the same config teams and aliases apply to all repos |
| Hourly punchcard | Combined |
| Monthly commit activity | Combined |
| Impact scoring | Computed over the combined author and team data |

### Metrics computed for main repository only

| Data | Reason |
|------|--------|
| Components tab | Each repo gets its own component chart card (main repo first, support repos below) |
| Release tags | Tag names and release ranges are specific to the main repo's version history |
| File count | Reflects the current tracked files in the main repo |
| Repo age | Derived from the oldest and newest commit in the main repo |

The Components tab shows a separate churn chart for each repository. The main repo card appears first; each support repo card is labeled **Support Repository**. Clicking any bar on any card filters the Authors table to contributors who touched that component.

## Potential Shortfalls

### Tenure rewards clock-time, not contribution density

The Active Tenure dimension is computed as the number of days between an author's first and last commit in the repository. This means an author who made a single commit on day 1 and a single commit on day 1000 receives 999 days of tenure — identical to a contributor who made hundreds of commits across that same span. A dormant author who returns years later with a minor fix receives a significant tenure boost that can rank them above steady, recent contributors.

**This is a deliberate design choice for individual authors.** Tenure is intended to reward sustained, long-term engagement with a project. A contributor who has been present across many years of a repository's history represents a form of institutional knowledge and staying power that pure commit counts do not capture. Whether a dormant period reflects disengagement or appropriate prioritization is context-dependent and outside what a commit log can determine.

**For teams, dormancy is structurally unlikely by design.** A well-defined team is expected to maintain a diversity of active members such that the team as a whole rarely goes dormant — individual members may be less active at any given time, but the team's collective tenure reflects real, ongoing presence in the codebase. A team whose tenure span is largely empty (e.g., one historical member from years ago and no recent activity) is likely a team that no longer exists in a meaningful sense and probably should not be defined in the configuration, or should use time-ranged membership entries to reflect its actual active period.

If tenure distortion is a concern for a specific repository, it can be reduced or eliminated by lowering `impact_w_tenure` or setting it to `0`.
