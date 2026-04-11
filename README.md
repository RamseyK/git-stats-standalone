# GitStats

A Python script that analyzes a Git repository and generates a self-contained HTML statistics report viewable offline in any browser.

## Primary Metrics

- **Activity** — commit heatmap, lines-of-code history, and hourly punchcard
- **Impact** — weighted leaderboard ranking authors and teams by commit volume, lines changed, and active tenure
- **Authors** — sortable contributor table with team badges, filterable by component or team
- **Teams** — per-team stats, member lists, and top components
- **Releases** — per-release breakdown of commits by author and team
- **Components** — churn chart for directories that contain a `make.py`, `pyproject.toml`, or `setup.py`

## Requirements

Python 3.8+

## Usage

```
python gitstats.py -s <repo-path> -o <output.html> [-c <config.json>]
```

| Flag | Required | Description |
|------|----------|-------------|
| `-s`, `--source` | Yes | Path to the Git repository to analyze |
| `-o`, `--output` | Yes | Path for the generated HTML file |
| `-c`, `--config` | No | Path to a config JSON file (default: `./config.json`) |

**Example:**

```bash
python gitstats.py -s ~/projects/myrepo -c config.json -o report.html
```

Open `report.html` in a browser. No server required.

If no config file is found, all authors are grouped under a single "Community" team and no aliases are applied.

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
  "impact_line_cap_percentile": 95
}
```

### General

| Key | Default | Description |
|-----|---------|-------------|
| `release_tag_prefix` | `""` | Only tags whose name starts with this prefix appear in the Releases tab. For example, `"v"` includes `v1.0`, `v2.3.1` but excludes `nightly-20240101`. An empty string includes all tags. |
| `max_release_tags` | `20` | Maximum number of release tags to display, taken from the most recent. Set to `0` to show all tags with no limit. |

### Teams

`teams` is an object where each key is a team name and the value is an object with:

| Key | Required | Description |
|-----|----------|-------------|
| `members` | Yes | List of git author names and/or email addresses belonging to this team. Names are matched exactly; emails are matched case-insensitively. A contributor can be listed by both name and email — the first match wins. |
| `color` | No | Hex color for this team's badges and charts (e.g. `"#3b82f6"`). If omitted, a color is assigned automatically from a built-in palette. |

Authors not listed in any team are grouped into a built-in **Community** team shown in slate gray.

### Aliases

`aliases` merges multiple git identities into a single canonical name. This is useful when a contributor has committed under different names or email addresses over time.

```json
"aliases": {
  "Canonical Name": ["old name", "old@email.com", "otheremail@example.com"]
}
```

Each key is the canonical display name. Each value is a list of alternate names or email addresses that should be treated as the same person. Emails in the alias list are matched case-insensitively.

### Impact score noise filtering

The impact score's **Lines Changed** metric is filtered before scoring to prevent meaningless bulk operations (reformats, mass moves, revert pairs) from inflating contributor rankings.

| Key | Default | Description |
|-----|---------|-------------|
| `impact_use_net_lines` | `true` | When `true`, each commit contributes `\|adds − dels\|` instead of `adds + dels`. A reformatting commit that deletes and re-adds 10,000 lines scores near zero rather than 20,000 gross lines. Set to `false` to use raw gross lines. |
| `impact_wash_window_days` | `7` | Groups each author's commits into non-overlapping N-day time buckets. If a bucket's raw gross lines exceed `impact_wash_min_gross` and at least 67% of the changes cancel out (adds ≈ dels), the bucket scores only its net `\|adds − dels\|`. This catches the two-commit revert pattern: mass delete on Monday, mass re-add on Friday. Set to `0` to disable. |
| `impact_wash_min_gross` | `200` | Minimum gross lines in a time bucket to trigger wash-window detection. Prevents small balanced day-to-day edits from being mistakenly zeroed out. |
| `impact_line_cap_percentile` | `95` | Caps each commit's effective line contribution at this percentile of all commits in the repository. Prevents a one-time bulk import from dominating the lines metric. Set to `0` to disable the cap. |

The filtering details used to generate any given report are displayed on the **Impact** tab under "Lines noise filtering," including the exact configured values.

### Impact score weights

The overall impact score formula is:

```
score = (commits / max_commits)         × 40
      + (effective_lines / max_lines)   × 40
      + (tenure_days / max_tenure)      × 20
```

Each metric is normalized against the top performer in that dimension, so the highest scorer in each category always contributes the full weight for that category. To change the weights, edit the class constants at the top of `gitstats.py`:

```python
IMPACT_W_COMMITS = 40   # commit count
IMPACT_W_LINES   = 40   # effective lines changed
IMPACT_W_TENURE  = 20   # active tenure in days
```

The three values must sum to 100.
