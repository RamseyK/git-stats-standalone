# /// script
# requires-python = ">=3.11"
# dependencies = [
# ]
# ///

import html
import os
import shutil
import subprocess
import sys
import json
import time
import datetime
import argparse
from collections import Counter, defaultdict

# Default palette cycled through when a team has no explicit "color" in config.
# Supports up to 12 teams; the 13th wraps back to the first color.
# The fallback "Community" team is always assigned slate (#94a3b8) separately.
TEAM_COLORS = [
    '#3b82f6', '#10b981', '#f59e0b', '#ef4444',
    '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16',
    '#f97316', '#14b8a6', '#a855f7', '#6366f1',
]


class GitStats:
    """
    Analyzes a Git repository and generates a self-contained HTML report with:
      - Summary tab: project overview, commit velocity, bus factor, punchcard
      - Impact leaderboard (authors + teams) with a weighted score
      - Sortable contributors table with team badges
      - Team cards showing stats, ownership, members
      - Release/tag breakdown by author and team
      - Component churn chart (click to filter by component)

    Config file format:
    {
      "release_tag_prefix": "v",
      "max_release_tags": 50,
      "teams": {
        "Team Name": {
          "color": "#3b82f6",
          "members": [
            "always member name",        // plain string → no time bounds
            "always@email.com",
            {"name": "Jane Smith", "from": "2021-01-01", "to": "2022-12-31"},
            {"name": "bob@example.com", "from": "2023-06-01"}  // no end date
          ]
        }
      },
      "aliases": {
        "Canonical Name": ["alias", "old@email.com", ...]
      },

      // Impact score noise-reduction options (all optional, defaults shown):
      "impact_use_net_lines": true,
        // true  → each commit contributes abs(adds - dels) to the lines metric.
        //         A reformatting commit that deletes and re-adds 10,000 lines
        //         scores near zero instead of 20,000 gross lines.
        // false → use raw adds + dels (original behavior).

      "impact_wash_window_days": 7,
        // Group each author's commits into non-overlapping N-day buckets.
        // If a bucket's raw gross lines exceed impact_wash_min_gross AND
        // at least 40% of the changes cancel out (e.g., mass delete on Monday,
        // mass re-add on Friday), the bucket scores only its net |adds - dels|.
        // Set to 0 to disable this check entirely.

      "impact_wash_min_gross": 200,
        // Minimum gross lines in a time bucket to trigger wash detection.
        // Prevents small day-to-day edits from being mistakenly zeroed out.

      "impact_line_cap_percentile": 95
        // Cap each commit's effective line contribution at this percentile
        // of all commits in the repo. A one-time 500,000-line import won't
        // drown out years of regular work. Set to 0 to disable.
    }

    Members in "teams" are matched against git author names and emails.
    "color" is optional; teams without one are assigned a color from the palette.
    "aliases" merges multiple identities into a single canonical name.
    Authors not assigned to any team appear under "Community".
    """

    # ── Impact score weights ─────────────────────────────────────────────────────
    # Each metric is normalized to the max value across all authors/teams (0–1),
    # then multiplied by its weight.  The raw sum is then rescaled by
    # 100 / (sum of non-zero weights) so the final score always lies in 0–100
    # regardless of how many dimensions are active.
    #
    # Set any weight to 0 to remove that dimension from scoring entirely.
    # The remaining active weights are automatically renormalized to fill 0–100.
    IMPACT_W_COMMITS = 30   # commit count          — set to 0 to disable
    IMPACT_W_LINES   = 30   # effective lines changed — set to 0 to disable
    IMPACT_W_TENURE  = 15   # active tenure in days  — set to 0 to disable
    IMPACT_W_MERGES  = 25   # PR merges into primary branch — set to 0 to disable

    # ── Component marker filenames ───────────────────────────────────────────────
    # A directory that directly contains one of these files is treated as a
    # component root for the Components tab churn chart.  This set is the
    # built-in default; it can be replaced entirely per-run via the
    # "component_markers" key in config.json (see README for details).
    #
    # Tweak the default or use the config key to match the conventions of the
    # repos you analyse:
    #   - Add marker files specific to your build system (e.g. 'CMakeLists.txt',
    #     'BUILD', 'BUCK', 'Cargo.toml') to pick up more component boundaries.
    #   - Remove entries that are too common in your repo (e.g. 'Makefile' in a
    #     repo where every subdirectory has one) to avoid over-fragmenting the chart.
    COMPONENT_MARKERS = {'make.py', 'pyproject.toml', 'setup.py', 'Makefile', 'meta.yaml', 'Cargo.toml', 'CMakeLists.txt'}

    # ── Source file extensions counted toward "Lines of Code" ───────────────────
    # Only files whose extension (lowercased) is in this set contribute to the
    # total_repo_lines metric shown on the Summary tab.  The config key
    # "loc_extensions" replaces this set entirely — include every extension you
    # want counted.  Extensions must include the leading dot (e.g. ".py").
    LOC_EXTENSIONS = {'.py', '.cc', '.c', '.cpp', '.h', '.hpp', '.rs', '.cs'}

    # ── Shared constants ─────────────────────────────────────────────────────────
    _SECS_PER_DAY       = 86400          # seconds in one day
    _DEFAULT_TEAM       = 'Community'    # fallback team for unassigned authors
    _DEFAULT_TEAM_COLOR = '#94a3b8'      # slate — always shown for the fallback team

    def __init__(self, repo_path, config_file=None, support_paths=None):
        self.repo_path = repo_path
        # Additional git repositories whose commit histories contribute to the
        # combined author/team/activity stats.  Components, LOC history, file
        # counts, and release tags are always taken from the main repo only.
        self.support_paths = list(support_paths or [])
        config = self._load_config(config_file)

        # Build alias lookup: any name/email variant → canonical author name.
        # Email keys are stored lowercased so matching is case-insensitive.
        # Config format: {"Canonical Name": ["alias", "old@email.com", ...]}
        aliases = config.get('aliases', {})
        self.alias_to_canonical = {
            (alias.lower() if '@' in alias else alias): canon
            for canon, als in aliases.items() for alias in als
        }
        # Preserved for HTML output: canonical name → list of alias strings.
        self.canonical_to_aliases = {canon: list(als) for canon, als in aliases.items()}
        # Build team membership and color lookups from config.
        #
        # Each member entry may be a plain string (always on the team) or a dict
        # with optional "from" / "to" date fields for time-bounded membership:
        #
        #   "members": [
        #     "Always Member",                          # always on this team
        #     "always@email.com",
        #     {"name": "Jane Smith", "from": "2021-01-01", "to": "2022-12-31"},
        #     {"name": "bob@example.com", "from": "2023-06-01"}  # no end date
        #   ]
        #
        # Membership ranges are stored as (team, from_ts, to_ts) tuples keyed by
        # lowercased email or exact author name.  from_ts=0 / to_ts=inf for
        # open-ended entries.  _get_team() picks the first matching range.
        teams_config = config.get('teams', {})
        # True only when at least one team is explicitly defined in config.
        # Used to hide the Teams tab when running without team configuration.
        self.has_teams = bool(teams_config)
        # Retained so generate_report() can seed zero-commit teams into the display.
        self.teams_config = teams_config
        self.team_colors = {}      # team name  → hex color string
        # key (lowercased email or author name) → list of (team, from_ts, to_ts)
        self.author_to_team_ranges = defaultdict(list)

        def _date_to_ts(date_str):
            """Convert a 'YYYY-MM-DD' string to a Unix timestamp (midnight local time).
            Returns None when date_str is absent or empty."""
            if not date_str:
                return None
            return int(datetime.datetime.strptime(date_str, '%Y-%m-%d').timestamp())

        for i, (team, value) in enumerate(teams_config.items()):
            self.team_colors[team] = value.get('color') or TEAM_COLORS[i % len(TEAM_COLORS)]
            for m in value.get('members', []):
                if isinstance(m, str):
                    # Plain string — membership has no time bounds.
                    key = m.lower() if '@' in m else m
                    self.author_to_team_ranges[key].append((team, 0, float('inf')))
                elif isinstance(m, dict):
                    # Dict entry — parse optional "from" / "to" date bounds.
                    name = m.get('name', '')
                    if not name:
                        continue
                    key = name.lower() if '@' in name else name
                    from_ts = _date_to_ts(m.get('from')) or 0
                    # Add 86399 so the "to" date is inclusive through end of day.
                    to_raw = _date_to_ts(m.get('to'))
                    to_ts  = (to_raw + 86399) if to_raw is not None else float('inf')
                    self.author_to_team_ranges[key].append((team, from_ts, to_ts))
        self.team_colors[self._DEFAULT_TEAM] = self._DEFAULT_TEAM_COLOR  # slate — always last / fallback

        # Only git tags whose name begins with this prefix are shown in the
        # Releases tab. An empty string (the default) means all tags are included.
        self.release_tag_prefix = config.get('release_tag_prefix', '')

        # Cap on how many tags to show in the Releases tab.
        # Set to 0 in config to display all matching tags with no limit.
        self.max_release_tags = int(config.get('max_release_tags', 20))

        # Component marker filenames — overridable via "component_markers" in config.
        # When present, the config list replaces the class default entirely, giving
        # full control over which directories are detected as component boundaries.
        cfg_markers = config.get('component_markers')
        self.component_markers = set(cfg_markers) if cfg_markers is not None else self.COMPONENT_MARKERS

        # Source file extensions counted toward "Lines of Code" on the Summary tab.
        # Overridable via "loc_extensions" in config.  The config list replaces the
        # default set entirely; include every extension you want counted.
        # Extensions must include the leading dot (e.g. ".py", ".rs").
        cfg_loc_ext = config.get('loc_extensions')
        self.loc_extensions = (
            {e.lower() for e in cfg_loc_ext} if cfg_loc_ext is not None
            else self.LOC_EXTENSIONS
        )

        # ── Summary tab configuration ─────────────────────────────────────────
        # Day windows for commit velocity cards.  Each entry produces one card
        # comparing commits in the last N days vs the prior N days.  The default
        # [30, 90] shows a 30-day and a 90-day view side by side.  Uses the
        # combined heatmap (main repo + all support repos).
        self.summary_velocity_days = list(config.get('summary_velocity_days', [30, 90]))

        # Number of top authors shown in the monthly chart tooltip.
        self.monthly_top_n = int(config.get('monthly_top_authors', 3))

        # Fraction of total commits that defines bus-factor ownership (0–1).
        # The bus factor is the minimum number of contributors whose combined
        # commits reach this fraction.  For example, 0.5 = the fewest people
        # whose commits add up to 50% of the total.  Default: 0.5.
        self.bus_factor_threshold = float(config.get('bus_factor_threshold', 0.5))

        # ── Impact score noise-reduction options ──────────────────────────────
        # These filter out commits that inflate the lines metric without
        # representing real work (reformats, mass moves, revert pairs).
        # See the class docstring for a full explanation of each option.

        # Replace adds+dels with abs(adds-dels) per commit so that a reformat
        # that deletes and re-adds the same lines scores near zero.
        self.use_net_lines = bool(config.get('impact_use_net_lines', True))

        # Width (in days) of the time buckets used for wash-window detection.
        # 0 disables the check. When enabled, a bucket whose gross changes are
        # large and whose adds/dels are roughly balanced is replaced by its net.
        self.wash_window_days = int(config.get('impact_wash_window_days', 7))

        # Minimum gross lines in a bucket to trigger wash-window detection.
        # Keeps small, balanced daily edits from being incorrectly zeroed out.
        self.wash_min_gross = int(config.get('impact_wash_min_gross', 200))

        # Winsorization percentile: cap each commit's effective lines at this
        # percentile of all commits, preventing one-time bulk imports from
        # dominating the lines metric. 0 disables the cap.
        self.line_cap_percentile = int(config.get('impact_line_cap_percentile', 95))

        # Primary branch name used to detect PR merges.  Only merge commits
        # (or heuristic squash/rebase merges) whose target is this branch are
        # counted in the merges dimension of the impact score.
        self.primary_branch = config.get('primary_branch', 'develop')

        # Heuristic patterns for detecting squash/rebase merges by commit subject.
        # Each string is matched as a case-insensitive substring of the subject.
        # None means use the built-in defaults (pull request #, merge remote-tracking
        # branch, merge branch) which include primary-branch exclusion logic.
        raw_h = config.get('merge_heuristics', None)
        self.merge_heuristics = (
            None if raw_h is None
            else [s.lower() for s in raw_h]
        )

        # ── Impact score weights (config-overridable) ─────────────────────────
        # Each key maps to the corresponding class-level default.  Set a weight
        # to 0 to remove that dimension from scoring entirely; the remaining
        # active weights are renormalized so scores still span 0–100.
        # Negative values are clamped to 0.
        def _w(key, default):
            """Read a weight from config, clamping negative values to 0."""
            return max(0, int(config.get(key, default)))

        self.IMPACT_W_COMMITS = _w('impact_w_commits', self.IMPACT_W_COMMITS)
        self.IMPACT_W_LINES   = _w('impact_w_lines',   self.IMPACT_W_LINES)
        self.IMPACT_W_TENURE  = _w('impact_w_tenure',  self.IMPACT_W_TENURE)
        self.IMPACT_W_MERGES  = _w('impact_w_merges',  self.IMPACT_W_MERGES)

        # Validate that all impact weights sum to exactly 100.
        _total_w = self.IMPACT_W_COMMITS + self.IMPACT_W_LINES + self.IMPACT_W_TENURE + self.IMPACT_W_MERGES
        if _total_w != 100:
            raise ValueError(
                f"Impact score weights must sum to 100, but got {_total_w} "
                f"(commits={self.IMPACT_W_COMMITS}, lines={self.IMPACT_W_LINES}, "
                f"tenure={self.IMPACT_W_TENURE}, merges={self.IMPACT_W_MERGES}). "
                f"Adjust the impact_w_* values in config.json so they sum to 100."
            )

        # Maximum number of authors/teams shown per release tag on the Releases tab.
        self.max_authors_per_tag = int(config.get('max_authors_per_tag', 20))
        self.max_teams_per_tag   = int(config.get('max_teams_per_tag', 10))

        # Central data store populated by collect() and consumed by generate_report().
        self.data = {
            'project_name': os.path.basename(os.path.abspath(repo_path)),
            'analysis_date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            'general': {'total_commits': 0, 'total_files': 0, 'total_lines': 0,
                        'total_repo_lines': 0, 'age_days': 0},
            'activity': {'hour': Counter(), 'weekday': Counter(), 'heatmap': Counter()},
            'monthly_author_commits': defaultdict(Counter),  # 'YYYY-MM' → {author: count}
            'authors': {},   # canonical name → {commits, add, del, first, last, team, impact}
            'teams': {},     # team name      → {commits, add, del, members, first, last, impact}
            'component_contributions': defaultdict(lambda: Counter()),  # component path → {author: commit_count}
            'team_components': defaultdict(lambda: Counter()),           # team name      → {component path: churn lines}
            'files': Counter(),      # file extension → count
            'components': Counter(), # main repo: component path → total churn lines
            'tags': [],              # list of release dicts built in step 3 of collect()
            'loc_history': [],       # running net LOC per file-change event, oldest-first after reversal
            # One entry per support repo, populated during collect() phase 2b.
            # Each entry: {name, components, component_contributions, team_components}
            'support_repos': [],
        }

    # ------------------------------------------------------------------ helpers

    def _load_config(self, path):
        """Return parsed JSON config, or an empty dict if the file is absent or unspecified."""
        if not path or not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    def _get_component(self, path, dirs):
        """Map a repo-relative file path to its component directory.

        A component is any directory that directly contains make.py,
        pyproject.toml, setup.py, or Makefile (discovered during collect()).
        `dirs` must be pre-sorted longest-first so the most specific (deepest)
        ancestor wins when directories are nested.

        Returns the component directory string, '(root)' for top-level
        marker files, or None if the file is not inside any component.
        """
        for comp in dirs:
            if comp == '':
                # Marker file is at the repo root (dirname of 'pyproject.toml' == '')
                return '(root)'
            if path == comp or path.startswith(comp + '/'):
                return comp
        return None  # file is not inside any recognized component

    def _get_author(self, name, email):
        """Resolve a raw git author name/email to a canonical name via the alias map.

        Lookup order: email (lowercased) → display name → original name as-is.
        """
        return self.alias_to_canonical.get(email.lower(), self.alias_to_canonical.get(name, name))

    def _author_lookup_keys(self, author: str, email: str = '') -> tuple:
        """Return all lookup keys for a canonical author name.

        Combines the commit-time email (if any), the canonical name, and all
        configured alias strings so that team membership can be resolved
        regardless of which identity a contributor used in any given commit.
        Alias strings that contain '@' are lowercased (they are email keys);
        plain-name aliases are kept as-is, matching how author_to_team_ranges
        is populated in __init__.
        """
        keys = []
        if email:
            keys.append(email.lower())
        keys.append(author)
        for alias in self.canonical_to_aliases.get(author, []):
            keys.append(alias.lower() if '@' in alias else alias)
        return tuple(keys)

    def _get_team(self, author, email, ts=0):
        """Return the team name for a given canonical author name, email, and commit timestamp.

        Checks the email (lowercased) first, then the author display name, then all
        configured aliases. For each key, iterates through its (team, from_ts, to_ts)
        ranges and returns the first team whose range contains ts. Falls back to
        _DEFAULT_TEAM when no range matches — including when an author has left all
        configured teams.

        ts defaults to 0 so callers that don't have a timestamp (e.g. tests) get a
        sensible result for unbounded entries.
        """
        for key in self._author_lookup_keys(author, email):
            for team, from_ts, to_ts in self.author_to_team_ranges.get(key, []):
                if from_ts <= ts <= to_ts:
                    return team
        return self._DEFAULT_TEAM

    def _run_git(self, args, repo=None):
        """Run a git subcommand and return its stdout as a string.

        Runs inside `repo` when provided, otherwise inside self.repo_path.
        stderr is suppressed. Raises subprocess.CalledProcessError on non-zero exit.
        """
        return subprocess.check_output(
            ['git', '-C', repo or self.repo_path] + args,
            stderr=subprocess.DEVNULL
        ).decode('utf-8', 'ignore')

    # ------------------------------------------------------------------ collect

    def _collect_commits(self, repo_path, component_dirs, components,
                         component_contributions, team_components, record_loc=False):
        """Process git log --numstat for one repository.

        Reads every commit in `repo_path` and accumulates data into the shared
        author/team/activity structures in self.data.  Per-repo component stats
        are written into the caller-supplied Counter/defaultdict arguments so
        the main repo and each support repo maintain separate component records.

        Args:
            repo_path:               Path to the git repository to analyse.
            component_dirs:          Sorted (longest-first) list of component
                                     root directories for this repo, as returned
                                     by the Phase-1 ls-files scan.
            components:              Counter()  — component path → churn lines.
            component_contributions: defaultdict(Counter) — component → {author: n}.
            team_components:         defaultdict(Counter) — team → {component: churn}.
            record_loc:              When True, append running net LOC to
                                     self.data['loc_history'] (main repo only).

        Returns:
            (all_ts, running_loc) — list of commit timestamps and the final net
            LOC value, used by the caller to set age_days and total_lines on the
            main repo.
        """
        log_data = self._run_git(
            ['log', '--numstat', '--pretty=format:COMMIT|%at|%an|%ae'], repo_path
        )

        # State carried across lines within the same commit
        current_author = current_team = None
        running_loc = 0
        all_ts = []

        # Per-commit line accumulators flushed on each new COMMIT header and
        # after the loop.  _compute_impact() uses these to filter out noise.
        current_commit_ts = 0
        current_commit_adds = 0
        current_commit_dels = 0

        for line in log_data.splitlines():
            if line.startswith('COMMIT|'):
                # ── Commit header ─────────────────────────────────────────────
                # Flush the previous commit's accumulated line counts.
                # Store the team alongside the line stats so _compute_impact()
                # can credit effective lines to the right team per-commit.
                if current_author is not None:
                    self.data['authors'][current_author]['commit_lines'].append(
                        (current_commit_ts, current_commit_adds, current_commit_dels, current_team))
                current_commit_adds = 0
                current_commit_dels = 0

                _, ts_str, name, email = line.split('|', 3)
                ts = int(ts_str)
                current_commit_ts = ts
                dt = datetime.datetime.fromtimestamp(ts)
                author = self._get_author(name, email)
                team = self._get_team(author, email, ts)
                all_ts.append(ts)
                current_author = author
                current_team = team
                # Track the most-recently-seen email for each canonical author so
                # we can resolve email-keyed team membership entries later.


                # Initialize author record on first encounter.
                # commit_lines stores (ts, adds, dels, team) per commit —
                # used by _compute_impact() to filter noise before scoring.
                if author not in self.data['authors']:
                    self.data['authors'][author] = {
                        'commits': 0, 'add': 0, 'del': 0,
                        'first': ts, 'last': ts, 'team': team,
                        'commit_lines': [], 'merges': 0,
                    }
                au = self.data['authors'][author]
                au['commits'] += 1
                au['last'] = max(au['last'], ts)
                au['first'] = min(au['first'], ts)
                # Per-team commit counts — used by the bus factor bar to show a
                # proportional colour split when an author has spanned multiple teams.
                tc = au.setdefault('team_commits', {})
                tc[team] = tc.get(team, 0) + 1

                # Initialize team record on first encounter
                if team not in self.data['teams']:
                    self.data['teams'][team] = {
                        'commits': 0, 'add': 0, 'del': 0,
                        'members': set(), 'first': ts, 'last': ts,
                    }
                tm = self.data['teams'][team]
                tm['commits'] += 1
                tm['last'] = max(tm['last'], ts)
                tm['first'] = min(tm['first'], ts)
                tm['members'].add(author)

                # Activity counters used for the heatmap and punchcard charts
                self.data['general']['total_commits'] += 1
                self.data['activity']['hour'][dt.hour] += 1
                self.data['activity']['weekday'][dt.weekday()] += 1
                self.data['activity']['heatmap'][dt.strftime('%Y-%m-%d')] += 1
                self.data['monthly_author_commits'][dt.strftime('%Y-%m')][author] += 1

            elif '\t' in line and current_author:
                # ── Per-file stat line ────────────────────────────────────────
                try:
                    parts = line.split('\t', 2)
                    # Binary files show '-' instead of a numeric count
                    a = int(parts[0]) if parts[0] and parts[0] != '-' else 0
                    d = int(parts[1]) if parts[1] and parts[1] != '-' else 0
                    path = parts[2] if len(parts) > 2 else ''

                    self.data['authors'][current_author]['add'] += a
                    self.data['authors'][current_author]['del'] += d
                    self.data['teams'][current_team]['add'] += a
                    self.data['teams'][current_team]['del'] += d

                    # Accumulate into the current commit bucket for impact scoring.
                    current_commit_adds += a
                    current_commit_dels += d

                    if record_loc:
                        # Track running net LOC for the LOC history chart.
                        # Reversed to chronological order in collect() after the loop.
                        running_loc += (a - d)
                        self.data['loc_history'].append(running_loc)

                    # Attribute churn to this repo's component (if any)
                    component = self._get_component(path, component_dirs)
                    if component is not None:
                        components[component] += (a + d)
                        component_contributions[component][current_author] += 1
                        team_components[current_team][component] += (a + d)
                except (ValueError, IndexError):
                    continue

        # Flush the final commit's line stats (the loop only flushes on the *next*
        # COMMIT header, so the last commit in the log would otherwise be lost).
        if current_author is not None:
            self.data['authors'][current_author]['commit_lines'].append(
                (current_commit_ts, current_commit_adds, current_commit_dels, current_team))

        return all_ts, running_loc

    def _collect_merges(self, repo_path):
        """Credit committers for PR merges detected in repo_path.

        Walks the first-parent history of self.primary_branch and identifies
        merge commits by two criteria:

          True merge  — the commit has two or more parents (git merge).
          Heuristic   — squash/rebase merges detected by commit subject:
            • "Pull request #..."
            • Starts with "Merge remote-tracking branch"
            • Starts with "Merge branch" but does NOT merge the primary
              branch itself back in (i.e., not "Merge branch 'develop'").

        The committer (not the author) is credited because they are the
        person who pressed the merge button.  If the committer is not
        already in self.data['authors'] (e.g. a bot that never authored
        a code commit), a stub entry is created so the merge count survives
        into the impact score.

        Silently returns when the primary branch does not exist in repo_path.
        """
        primary = self.primary_branch
        # Bail out silently if the branch doesn't exist in this repo.
        check = subprocess.run(
            ['git', '-C', repo_path, 'rev-parse', '--verify', primary],
            capture_output=True,
        )
        if check.returncode != 0:
            return

        result = subprocess.run(
            ['git', '-C', repo_path, 'log', primary, '--first-parent',
             '--format=MERGE|%P|%ce|%cn|%ct|%ae|%s'],
            capture_output=True, text=True, errors='replace',
        )

        pb_lower = primary.lower()
        for line in result.stdout.splitlines():
            if not line.startswith('MERGE|'):
                continue
            parts = line.split('|', 6)
            if len(parts) < 7:
                continue
            _, parents_str, c_email, c_name, ts_str, a_email, subject = parts
            parents = parents_str.split()

            is_true_merge = len(parents) >= 2

            s = subject.lower().strip()
            if self.merge_heuristics is None:
                # Built-in defaults with primary-branch exclusion for "merge branch".
                is_subject_heuristic = (
                    'pull request #' in s
                    or s.startswith('merge remote-tracking branch')
                    or (
                        s.startswith('merge branch')
                        and not s.startswith(f"merge branch '{pb_lower}'")
                        and not s.startswith(f'merge branch "{pb_lower}"')
                        and not s.startswith(f'merge branch {pb_lower} ')
                        and s != f'merge branch {pb_lower}'
                    )
                )
                # A non-merge commit where committer ≠ author typically indicates
                # someone edited the commit message before merging (squash/rebase
                # with a custom message).  Subject heuristics miss these since
                # the edited message may not contain a PR pattern.
                is_committer_merge = (
                    not is_true_merge
                    and c_email.lower() != a_email.strip().lower()
                )
                is_heuristic = is_subject_heuristic or is_committer_merge
            else:
                # User-supplied patterns: each is a case-insensitive substring match.
                is_heuristic = any(h in s for h in self.merge_heuristics)

            if not (is_true_merge or is_heuristic):
                continue

            ts = int(ts_str) if ts_str.strip().isdigit() else 0
            author = self._get_author(c_name, c_email)
            if author not in self.data['authors']:
                team = self._get_team(author, c_email, ts)
                self.data['authors'][author] = {
                    'commits': 0, 'add': 0, 'del': 0,
                    'first': ts, 'last': ts, 'team': team,
                    'commit_lines': [], 'merges': 0,
                    'team_commits': {},
                }
            au = self.data['authors'][author]
            au['merges'] = au.get('merges', 0) + 1
            # Store per-merge timestamps so team attribution can use the team
            # the committer belonged to *at the time of each merge*.
            au.setdefault('merge_timestamps', []).append((ts, c_email.strip()))

    def collect(self):
        """Populate self.data by running git commands against all repositories.

        Three phases:
          1. Main repo file inventory + component discovery (git ls-files).
             File counts, LOC history, age, and release tags are always taken
             from the main repo only.
          2. Commit history for the main repo then each support repo in order
             (git log --numstat).  Author, team, and activity stats accumulate
             across all repos; component data is tracked separately per repo so
             each gets its own chart card in the output.
          3. Release tag breakdown from the main repo only (git tag + git log).

        Must be called before generate_report().
        """
        print(f"Analyzing {self.data['project_name']}...")

        # ── Phase 1: main repo file inventory + component discovery ──────────
        # git ls-files lists every tracked file. We use it to:
        #   a) count files by extension for the general stats
        #   b) find component roots (dirs that contain a marker file)
        #   c) count current lines of code — binary newline count restricted to
        #      files whose extension is in self.loc_extensions (configurable)
        ls_files = self._run_git(['ls-files']).splitlines()
        self.data['general']['total_files'] = len(ls_files)
        component_dirs_set = set()
        _repo_lines = 0
        for f in ls_files:
            ext = os.path.splitext(f)[1].lower() or 'source'
            self.data['files'][ext] += 1
            if os.path.basename(f) in self.component_markers:
                component_dirs_set.add(os.path.dirname(f))
            if ext in self.loc_extensions:
                try:
                    with open(os.path.join(self.repo_path, f), 'rb') as _fh:
                        _repo_lines += _fh.read().count(b'\n')
                except OSError:
                    pass
        self.data['general']['total_repo_lines'] = _repo_lines
        # Longest paths first so _get_component() matches the deepest ancestor.
        main_component_dirs = sorted(component_dirs_set, key=len, reverse=True)

        # ── Phase 2a: main repo commit history ───────────────────────────────
        # --numstat emits one "COMMIT|..." header per commit followed by
        # tab-separated (added, deleted, filepath) lines for each changed file.
        # record_loc=True so the LOC history chart is populated for the main repo.
        main_components      = Counter()
        main_comp_contrib    = defaultdict(lambda: Counter())
        main_team_comps      = defaultdict(lambda: Counter())

        main_all_ts, main_running_loc = self._collect_commits(
            self.repo_path, main_component_dirs,
            main_components, main_comp_contrib, main_team_comps,
            record_loc=True,
        )

        self.data['components']              = main_components
        self.data['component_contributions'] = main_comp_contrib
        self.data['team_components']         = main_team_comps

        # Age and total LOC derive from the main repo only.
        if main_all_ts:
            self.data['general']['age_days']         = (max(main_all_ts) - min(main_all_ts)) // self._SECS_PER_DAY
            self.data['general']['first_commit_ts']  = min(main_all_ts)
            self.data['general']['last_commit_ts']   = max(main_all_ts)
        self.data['general']['total_lines'] = main_running_loc

        # git log walks newest-first, so reverse to get chronological order.
        self.data['loc_history'].reverse()
        # For large repos, decimating to ≤2000 points keeps the chart responsive
        # without meaningfully reducing visual fidelity.
        if len(self.data['loc_history']) > 2000:
            step = len(self.data['loc_history']) // 2000
            self.data['loc_history'] = self.data['loc_history'][::step]

        # ── Phase 2b: support repo commit histories ───────────────────────────
        # Author/team/activity stats accumulate; each support repo gets its own
        # component record appended to self.data['support_repos'].
        for support_path in self.support_paths:
            support_name = os.path.basename(os.path.abspath(support_path))
            print(f"  + {support_name} (support repository)...")

            support_ls = self._run_git(['ls-files'], support_path).splitlines()
            sup_dirs_set = set()
            for f in support_ls:
                if os.path.basename(f) in self.component_markers:
                    sup_dirs_set.add(os.path.dirname(f))
            support_component_dirs = sorted(sup_dirs_set, key=len, reverse=True)

            sup_components   = Counter()
            sup_comp_contrib = defaultdict(lambda: Counter())
            sup_team_comps   = defaultdict(lambda: Counter())

            self._collect_commits(
                support_path, support_component_dirs,
                sup_components, sup_comp_contrib, sup_team_comps,
                record_loc=False,
            )

            self.data['support_repos'].append({
                'name': support_name,
                'components': sup_components,
                'component_contributions': sup_comp_contrib,
                'team_components': sup_team_comps,
            })

        # ── Phase 2c: PR merge counts (main repo + all support repos) ────────
        # Committers on merge commits into the primary branch are credited with
        # a merge count used in the impact score merges dimension.
        self._collect_merges(self.repo_path)
        for support_path in self.support_paths:
            self._collect_merges(support_path)

        # ── Phase 3: release tag breakdown (main repo only) ──────────────────
        # Tags are sorted newest-first. For each tag we attribute the commits
        # between it and the previous tag (tag_range) to authors and teams.
        # The oldest tag uses just its name as the range (all commits up to it).
        try:
            tags = self._run_git(['tag', '--sort=-creatordate']).splitlines()
        except subprocess.CalledProcessError:
            tags = []

        if self.release_tag_prefix:
            tags = [t for t in tags if t.startswith(self.release_tag_prefix)]

        # Record the full count before capping so the Summary tab can show the
        # true total even when display is limited by max_release_tags.
        self.data['general']['total_tags'] = len(tags)

        # 0 means no limit; any positive value caps the list
        if self.max_release_tags:
            tags = tags[:self.max_release_tags]

        for i, tag in enumerate(tags):
            # Range: commits between the next-older tag and this one.
            # For the oldest tag in the list, include all its commits.
            tag_range = tag if i == len(tags) - 1 else f"{tags[i + 1]}..{tag}"
            try:
                # Include --numstat so per-author line stats are available for
                # per-release impact scoring alongside commit counts and tenure.
                tag_log = self._run_git(['log', tag_range, '--numstat',
                                         '--pretty=format:COMMIT|%at|%an|%ae'])
            except subprocess.CalledProcessError:
                continue

            # Accumulate per-author and per-team data: commits, line stats,
            # first/last commit timestamp.
            tag_authors  = {}    # name → {commits, add, del, first_ts, last_ts}
            tag_teams    = defaultdict(lambda: {
                'commits': 0, 'add': 0, 'del': 0,
                'first_ts': float('inf'), 'last_ts': 0,
            })
            current_author = None
            current_team   = None

            for line in tag_log.splitlines():
                if line.startswith('COMMIT|'):
                    parts = line.split('|', 3)
                    if len(parts) < 4:
                        continue
                    _, ts_str, name, email = parts
                    ts    = int(ts_str) if ts_str.isdigit() else 0
                    canon = self._get_author(name, email)
                    team  = self._get_team(canon, email, ts)
                    current_author = canon
                    current_team   = team
                    if canon not in tag_authors:
                        tag_authors[canon] = {
                            'commits': 0, 'add': 0, 'del': 0,
                            'first_ts': ts, 'last_ts': ts,
                        }
                    a = tag_authors[canon]
                    a['commits']  += 1
                    a['first_ts']  = min(a['first_ts'], ts)
                    a['last_ts']   = max(a['last_ts'],  ts)
                    tt = tag_teams[team]
                    tt['commits']  += 1
                    tt['first_ts']  = min(tt['first_ts'], ts)
                    tt['last_ts']   = max(tt['last_ts'],  ts)
                elif current_author and '\t' in line:
                    parts = line.split('\t', 2)
                    if len(parts) >= 2:
                        try:
                            add = int(parts[0]) if parts[0] != '-' else 0
                            dl  = int(parts[1]) if parts[1] != '-' else 0
                            tag_authors[current_author]['add'] += add
                            tag_authors[current_author]['del'] += dl
                            tag_teams[current_team]['add'] += add
                            tag_teams[current_team]['del'] += dl
                        except ValueError:
                            pass

            if tag_authors:
                ranked       = self._compute_tag_impacts(tag_authors)
                team_impacts = self._compute_tag_team_impacts(tag_teams)
                top_teams    = sorted(tag_teams.keys(),
                                      key=lambda t: tag_teams[t]['commits'],
                                      reverse=True)[:self.max_teams_per_tag]
                self.data['tags'].append({
                    'name':         tag,
                    'count':        sum(a['commits'] for a in tag_authors.values()),
                    'authors':      ranked[:self.max_authors_per_tag],
                    'top_teams':    [(t, tag_teams[t]['commits']) for t in top_teams],
                    'team_impacts': team_impacts,
                })

        self._compute_impact()
        print(f"   → {self.data['general']['total_commits']:,} commits · "
              f"{len(self.data['authors'])} authors · "
              f"{len(self.data['teams'])} teams")

    # ------------------------------------------------------------------ impact

    @staticmethod
    def _wash_bucket_score(bucket: dict, wash_min: float, cap_val: float) -> float:
        """Return the effective line score for one wash-window bucket.

        If the bucket is large and adds/dels are roughly balanced (wash condition),
        the bucket contributes only its net |adds - dels| capped at cap_val.
        Otherwise the pre-computed effective sum (already capped per commit) is used.

        Args:
            bucket:   Dict with keys 'eff' (float), 'raw_a' (int), 'raw_d' (int).
            wash_min: Minimum gross lines in the bucket to trigger wash detection.
            cap_val:  Per-commit line cap applied to the net on the wash path.
        """
        gross = bucket['raw_a'] + bucket['raw_d']
        net   = abs(bucket['raw_a'] - bucket['raw_d'])
        if gross > wash_min and gross > 0 and min(bucket['raw_a'], bucket['raw_d']) / gross > 0.4:
            return min(net, cap_val)
        return bucket['eff']

    def _compute_impact(self):
        """Compute and store impact scores for every author and team.

        Score formula (produces a value in the range 0–100):
            raw   = (commits / max_commits)      * IMPACT_W_COMMITS   (if wc > 0)
                  + (effective_lines / max_eff)  * IMPACT_W_LINES     (if wl > 0)
                  + (tenure_days / max_tenure)   * IMPACT_W_TENURE    (if wt > 0)
                  + (merges / max_merges)         * IMPACT_W_MERGES   (if wm > 0)
            score = raw * (100 / sum_of_active_weights)

        Setting any weight to 0 removes that dimension from scoring entirely.
        The remaining active weights are automatically renormalized so that the
        top performer still reaches 100.  Setting all weights to 0 yields 0 for
        every author and team.

        "effective_lines" is derived from the per-commit line stats collected
        during collect() via a three-step noise-reduction pipeline:

          Step 1 — Net lines per commit (use_net_lines, default on):
            Each commit contributes abs(adds - dels) instead of adds + dels.
            A reformatting commit that deletes and re-adds 10,000 lines scores
            near zero rather than 20,000.

          Step 2 — Winsorization (line_cap_percentile, default 95):
            Per-commit effective values are capped at the given percentile of
            all commits in the repo. One 500,000-line import won't overshadow
            years of regular contributions.

          Step 3 — Wash-window detection (wash_window_days, default 7):
            Commits are grouped into non-overlapping N-day buckets. If a
            bucket's raw gross lines exceed wash_min_gross AND at least 40%
            of changes cancel out (min(adds,dels)/gross > 0.4), the bucket's
            contribution is replaced by its raw net |adds - dels|. This catches
            the two-commit revert pattern: mass-delete on Monday, mass-re-add
            on Wednesday.

        Team effective_lines = sum of their members' author-level effective lines
        (consistent with per-author filtering). Authors and teams are otherwise
        normalized against their own group maxima.

        Results are written back into the author/team dicts as 'impact'.
        commit_lines and internal scratch fields are removed before returning.
        """
        wc = self.IMPACT_W_COMMITS
        wl = self.IMPACT_W_LINES
        wt = self.IMPACT_W_TENURE
        wm = self.IMPACT_W_MERGES

        # Scale factor that renormalizes scores to 0–100 when one or more
        # dimensions are disabled (weight = 0).  When all four weights are
        # active (the default), total_w = 100 and scale = 1.0 exactly.
        total_w = (wc if wc > 0 else 0) + (wl if wl > 0 else 0) + \
                  (wt if wt > 0 else 0) + (wm if wm > 0 else 0)
        scale = (100.0 / total_w) if total_w > 0 else 0.0

        use_net   = self.use_net_lines
        wash_days = self.wash_window_days
        wash_min  = self.wash_min_gross
        cap_pct   = self.line_cap_percentile

        authors = self.data['authors']

        # ── Step 1: per-commit effective lines ───────────────────────────────
        # Compute abs(adds - dels) or adds + dels for each commit depending on
        # the use_net_lines setting. Stored as a parallel list (_eff) per author.
        # commit_lines entries are (ts, adds, dels, team) — team is ignored here.
        for a in authors.values():
            a['_eff'] = [
                abs(adds - dels) if use_net else (adds + dels)
                for _, adds, dels, _team in a.get('commit_lines', [])
            ]

        # ── Step 2: winsorization ─────────────────────────────────────────────
        # Find the cap value from the cross-author distribution, then apply it.
        if cap_pct > 0:
            all_eff = sorted(v for a in authors.values() for v in a['_eff'])
            if all_eff:
                cap_idx = min(int(len(all_eff) * cap_pct / 100), len(all_eff) - 1)
                cap_val = all_eff[cap_idx]
            else:
                cap_val = float('inf')
        else:
            cap_val = float('inf')

        for a in authors.values():
            a['_eff'] = [min(v, cap_val) for v in a['_eff']]

        # ── Step 3: wash-window bucketing ─────────────────────────────────────
        def effective_lines(author):
            """Return total effective lines for an author after wash detection.

            Without wash_days, this is simply sum(_eff). With it, each time
            bucket that looks like a revert (large gross, balanced adds/dels)
            is replaced by its raw net to avoid crediting the reversal.
            """
            commits = author.get('commit_lines', [])
            eff     = author.get('_eff', [])
            if not commits:
                return 0
            if not wash_days:
                return sum(eff)

            window_secs = wash_days * self._SECS_PER_DAY
            # Each bucket accumulates per-commit effective lines AND the raw
            # adds/dels needed to detect the wash condition.
            buckets = defaultdict(lambda: {'eff': 0, 'raw_a': 0, 'raw_d': 0})
            for (ts, raw_a, raw_d, _team), e in zip(commits, eff):
                b = ts // window_secs
                buckets[b]['eff']   += e
                buckets[b]['raw_a'] += raw_a
                buckets[b]['raw_d'] += raw_d

            return sum(self._wash_bucket_score(b, wash_min, cap_val) for b in buckets.values())

        # ── Score authors ─────────────────────────────────────────────────────
        eff_map = {}   # author name → effective lines (used for team rollup too)
        if authors:
            eff_map = {name: effective_lines(a) for name, a in authors.items()}
            # Only compute the max for each dimension when that weight is active.
            # Skipping the computation avoids a misleading or divide-by-zero
            # situation when a dimension is intentionally disabled.
            max_c = (max(a['commits'] for a in authors.values()) or 1) if wc > 0 else 1
            max_k = (max(eff_map.values()) or 1)                         if wl > 0 else 1
            max_d = (max((a['last'] - a['first']) // self._SECS_PER_DAY
                         for a in authors.values()) or 1)                if wt > 0 else 1
            max_m = (max(a.get('merges', 0) for a in authors.values()) or 1) if wm > 0 else 1
            for name, a in authors.items():
                days = (a['last'] - a['first']) // self._SECS_PER_DAY
                raw = (
                    ((a['commits']       / max_c) * wc if wc > 0 else 0.0) +
                    ((eff_map[name]      / max_k) * wl if wl > 0 else 0.0) +
                    ((days               / max_d) * wt if wt > 0 else 0.0) +
                    ((a.get('merges', 0) / max_m) * wm if wm > 0 else 0.0)
                )
                a['impact']   = round(raw * scale, 1)
                a['eff_lines'] = int(eff_map[name])

        # ── Score teams ───────────────────────────────────────────────────────
        # Team effective lines apply the same three-step noise-reduction pipeline
        # as author scoring, but bucket by (time-window, team) so each commit is
        # credited to the team the author belonged to *at that commit's timestamp*.
        # This correctly handles time-ranged membership: Alice's commits before she
        # joined Core go to Community; commits after go to Core.
        teams = self.data['teams']
        if teams:
            team_eff = defaultdict(float)
            for a in authors.values():
                commits = a.get('commit_lines', [])
                eff     = a.get('_eff', [])
                if not commits:
                    continue
                if not wash_days:
                    # Wash detection disabled: sum per-commit _eff by commit-time team.
                    for (_, _ra, _rd, team), e in zip(commits, eff):
                        team_eff[team] += e
                else:
                    window_secs = wash_days * self._SECS_PER_DAY
                    # Bucket by (time-window, team) — applies wash detection per team
                    # the same way effective_lines() does per author.
                    t_buckets = defaultdict(lambda: {'eff': 0.0, 'raw_a': 0, 'raw_d': 0})
                    for (ts, raw_a, raw_d, team), e in zip(commits, eff):
                        key = (ts // window_secs, team)
                        t_buckets[key]['eff']   += e
                        t_buckets[key]['raw_a'] += raw_a
                        t_buckets[key]['raw_d'] += raw_d
                    for (_, team), b in t_buckets.items():
                        team_eff[team] += self._wash_bucket_score(b, wash_min, cap_val)

            # Accumulate merges per team using per-merge timestamps so each
            # merge is credited to the team the author belonged to at that time.
            team_merges = defaultdict(int)
            for name, a in authors.items():
                for ts, email in a.get('merge_timestamps', []):
                    team_merges[self._get_team(name, email, ts)] += 1

            max_c = (max(t['commits'] for t in teams.values()) or 1) if wc > 0 else 1
            max_k = (max(team_eff.values()) or 1)                      if wl > 0 else 1
            max_d = (max((t['last'] - t['first']) // self._SECS_PER_DAY
                         for t in teams.values()) or 1)                if wt > 0 else 1
            max_m = ((max(team_merges.values()) if team_merges else 1)) if wm > 0 else 1
            for tname, t in teams.items():
                days = (t['last'] - t['first']) // self._SECS_PER_DAY
                raw = (
                    ((t['commits']           / max_c) * wc if wc > 0 else 0.0) +
                    ((team_eff[tname]        / max_k) * wl if wl > 0 else 0.0) +
                    ((days                   / max_d) * wt if wt > 0 else 0.0) +
                    ((team_merges[tname]     / max_m) * wm if wm > 0 else 0.0)
                )
                t['impact'] = round(raw * scale, 1)
                t['merges'] = team_merges[tname]

        # Clean up internal scratch fields — they must not appear in JSON output.
        for a in authors.values():
            a.pop('commit_lines', None)
            a.pop('_eff', None)
            a.pop('merge_timestamps', None)

    # ------------------------------------------------------------------ HTML helpers

    def _score_tag_entities(self, entities: dict, tenure_map: dict = None) -> dict:
        """Compute per-release impact scores for a dict of authors or teams.

        Both callers (_compute_tag_impacts and _compute_tag_team_impacts) share
        the same schema: {name: {'commits', 'add', 'del', 'first_ts', 'last_ts'}}.
        Uses the configured commits, lines, and tenure weights normalized within
        the release.  The merges dimension is excluded — it is not tracked per
        release range.

        Args:
            entities:    {name: {commits, add, del, first_ts, last_ts}}
            tenure_map:  Optional {name: tenure_days} supplying global tenure values.
                         When provided, a name's global tenure is used instead of the
                         release-range tenure derived from first_ts/last_ts.  Names
                         absent from tenure_map fall back to release-range tenure.

        Returns:
            {name: {'commits', 'eff_lines', 'tenure_days', 'impact'}}
        """
        wc = self.IMPACT_W_COMMITS
        wl = self.IMPACT_W_LINES
        wt = self.IMPACT_W_TENURE
        active_w = (wc if wc > 0 else 0) + (wl if wl > 0 else 0) + (wt if wt > 0 else 0)
        scale = (100.0 / active_w) if active_w > 0 else 0.0

        eff_map = {
            name: abs(e['add'] - e['del']) if self.use_net_lines else (e['add'] + e['del'])
            for name, e in entities.items()
        }

        def _tenure(name, e):
            if tenure_map and name in tenure_map:
                return tenure_map[name]
            return (e['last_ts'] - e['first_ts']) // self._SECS_PER_DAY

        tenure_values = {name: _tenure(name, e) for name, e in entities.items()}

        max_c = (max(e['commits'] for e in entities.values()) or 1) if wc > 0 else 1
        max_k = (max(eff_map.values()) or 1)                        if wl > 0 else 1
        max_d = (max(tenure_values.values()) or 1)                  if wt > 0 else 1

        results = {}
        for name, e in entities.items():
            tenure = tenure_values[name]
            raw = (
                ((e['commits']  / max_c) * wc if wc > 0 else 0.0) +
                ((eff_map[name] / max_k) * wl if wl > 0 else 0.0) +
                ((tenure        / max_d) * wt if wt > 0 else 0.0)
            )
            results[name] = {
                'commits':     e['commits'],
                'eff_lines':   int(eff_map[name]),
                'tenure_days': tenure,
                'impact':      round(raw * scale, 1),
            }
        return results

    def _compute_tag_impacts(self, tag_authors: dict) -> list:
        """Score per-release authors by impact and return a list sorted descending.

        Each entry has keys: 'name', 'commits', 'eff_lines', 'tenure_days', 'impact'.
        Tenure is taken from the author's full global history when available.
        Returns an empty list when tag_authors is empty.
        """
        if not tag_authors:
            return []
        tenure_map = {
            name: (self.data['authors'][name]['last'] - self.data['authors'][name]['first']) // self._SECS_PER_DAY
            for name in tag_authors
            if name in self.data['authors']
        }
        scored = self._score_tag_entities(tag_authors, tenure_map=tenure_map or None)
        results = [{'name': name, **data} for name, data in scored.items()]
        results.sort(key=lambda x: x['impact'], reverse=True)
        return results

    def _compute_tag_team_impacts(self, tag_teams: dict) -> dict:
        """Score per-release teams by impact and return a {team: data} dict.

        Each value has keys: 'commits', 'eff_lines', 'tenure_days', 'impact'.
        Tenure is taken from the team's full global history when available.
        Returns an empty dict when tag_teams is empty.
        """
        if not tag_teams:
            return {}
        tenure_map = {
            team: (self.data['teams'][team]['last'] - self.data['teams'][team]['first']) // self._SECS_PER_DAY
            for team in tag_teams
            if team in self.data['teams']
        }
        return self._score_tag_entities(tag_teams, tenure_map=tenure_map or None)

    def _render_tags_html(self):
        """Return an HTML string of release cards for the Releases tab.

        Each card shows the tag name, per-team commit badges (with impact score),
        and a grid of top authors with their commit counts for that release.
        Returns a placeholder card when no tags were collected.
        """
        if not self.data['tags']:
            return ('<div class="card text-center text-slate-400 font-bold py-16">'
                    'No tags found in this repository.</div>')
        parts = []
        _release_medals = ['🥇', '🥈', '🥉']
        _merges_excluded = self.IMPACT_W_MERGES > 0

        def _author_prefix(i):
            if i < 3:
                return f'<span class="mr-1">{_release_medals[i]}</span>{i + 1}. '
            return f'{i + 1}. '

        def _impact_tooltip(label, data):
            """Build an HTML-escaped title attribute for an author or team entry."""
            days   = data['tenure_days']
            plural = 's' if days != 1 else ''
            note   = '&#10;(merges not tracked per release)' if _merges_excluded else ''
            return (
                f'{html.escape(label)}&#10;'
                f'Commits: {data["commits"]:,}&#10;'
                f'Lines: {data["eff_lines"]:,}&#10;'
                f'Tenure: {days} day{plural}'
                f'{note}'
            )

        for t in self.data['tags']:
            author_items = ''.join([
                f'<div class="flex justify-between items-center text-sm bg-slate-50 p-2 rounded-xl '
                f'border border-transparent hover:border-slate-200 transition-all">'
                f'<span class="truncate font-bold text-slate-600 cursor-default"'
                f' title="{_impact_tooltip(a["name"], a)}">{_author_prefix(i)}{html.escape(a["name"])}</span>'
                f'<b class="text-blue-600 ml-2 shrink-0">⚡{a["impact"]}</b></div>'
                for i, a in enumerate(t['authors'])
            ])
            # Only render team badges when teams are explicitly configured.
            # With no teams, every commit maps to "Community", which adds no signal.
            if self.has_teams:
                team_impacts = t.get('team_impacts', {})
                team_badges = ''.join([
                    f'<span class="inline-flex items-center gap-1.5 text-[10px] px-2.5 py-1 rounded-lg font-black uppercase cursor-default"'
                    f' title="{_impact_tooltip(team, team_impacts[team]) if team in team_impacts else html.escape(team)}"'
                    f' style="background:{self.team_colors.get(team, self._DEFAULT_TEAM_COLOR)}18;'
                    f'color:{self.team_colors.get(team, self._DEFAULT_TEAM_COLOR)}">'
                    f'{team}'
                    f'<span class="opacity-60">·</span>'
                    f'<span>{count} commits</span>'
                    f'<span class="opacity-60">·</span>'
                    f'<span class="bg-white/60 rounded px-1 py-px">⚡ {team_impacts.get(team, {}).get("impact", 0)}</span>'
                    f'</span>'
                    for team, count in t['top_teams']
                ])
            else:
                team_badges = ''
            parts.append(f'''
            <div class="card group">
                <div class="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 mb-6 border-b border-slate-100 pb-6">
                    <div>
                        <span class="text-3xl font-black text-slate-900 group-hover:text-blue-600 transition-colors">{t["name"]}</span>
                        <p class="text-[10px] font-bold text-slate-400 uppercase tracking-widest mt-1">Release Contributors</p>
                        <div class="flex flex-wrap gap-1 mt-2">{team_badges}</div>
                    </div>
                    <div class="px-6 py-2 bg-slate-900 text-white rounded-2xl font-black text-sm shrink-0">{t["count"]} Commits</div>
                </div>
                <div class="grid grid-cols-2 lg:grid-cols-4 xl:grid-cols-5 gap-3">
                    {author_items}
                </div>
            </div>''')
        return '\n'.join(parts)

    # ------------------------------------------------------------------ report helpers

    def _build_component_section(self):
        """Build per-repo component chart data and the HTML card markup.

        Returns a 4-tuple:
            repo_charts          — list of {id, name, labels, values} dicts, one per repo.
            repo_charts_json     — JSON-serialized repo_charts for the <script> block.
            component_json       — JSON-serialized unified componentData mapping used by
                                   the click-to-filter Authors table.
            component_cards_html — HTML string with one chart card per repo.
        """
        repo_charts = []
        main_top = self.data['components'].most_common(30)
        repo_charts.append({
            'id':     'main',
            'name':   self.data['project_name'],
            'labels': [c[0] for c in main_top],
            'values': [c[1] for c in main_top],
        })
        for i, sr in enumerate(self.data['support_repos']):
            sr_top = sorted(sr['components'].items(), key=lambda x: x[1], reverse=True)[:30]
            repo_charts.append({
                'id':     f'support-{i}',
                'name':   sr['name'],
                'labels': [f"{sr['name']}:{c[0]}" for c in sr_top],
                'values': [c[1] for c in sr_top],
            })

        # Unified component→author mapping used by click-to-filter.
        # Main repo paths are bare; support repo paths carry the "reponame:" prefix
        # to avoid collisions when both repos contain a component at the same path.
        all_comp_contrib = {k: dict(v) for k, v in self.data['component_contributions'].items()}
        for sr in self.data['support_repos']:
            for path, authors in sr['component_contributions'].items():
                all_comp_contrib[f"{sr['name']}:{path}"] = dict(authors)

        has_support = bool(self.support_paths)
        cards = []
        for rc in repo_charts:
            cid     = rc['id']
            is_main = cid == 'main'
            margin  = '' if is_main else ' mt-6'
            if has_support and is_main:
                repo_label = (' <span class="text-xs font-bold text-slate-400 '
                              'uppercase tracking-widest ml-2">Main Repository</span>')
            elif not is_main:
                repo_label = (' <span class="text-xs font-bold text-slate-400 '
                              'uppercase tracking-widest ml-2">Support Repository</span>')
            else:
                repo_label = ''
            cards.append(
                f'        <div class="card{margin}" id="componentChartCard-{cid}"'
                f' style="display:flex;flex-direction:column">\n'
                f'            <div class="mb-6">\n'
                f'                <h3 class="text-xl font-black">'
                f'Component Churn \u2014 {rc["name"]}{repo_label}</h3>\n'
                f'                <p class="text-sm text-slate-400 font-medium mt-1">'
                f'Click any bar to filter contributors by component.</p>\n'
                f'            </div>\n'
                f'            <div style="position:relative;min-height:0"'
                f' id="componentChartWrapper-{cid}">\n'
                f'                <canvas id="componentChart-{cid}"></canvas>\n'
                f'            </div>\n'
                f'        </div>'
            )

        return (
            repo_charts,
            json.dumps(repo_charts),
            json.dumps(all_comp_contrib),
            '\n'.join(cards),
        )

    def _render_velocity_card(self, days: int, current: int, prior: int, delta) -> str:
        """Render a single commit-velocity card for the Summary tab.

        Args:
            days:    Window size in days.
            current: Commit count in the most-recent `days`-day window.
            prior:   Commit count in the preceding equal window.
            delta:   Percentage change (current vs prior), or None when prior == 0.
        """
        if delta is None:
            trend = '<span class="text-slate-400 text-sm font-medium">— no prior data</span>'
        elif delta > 0:
            trend = (f'<span class="text-emerald-600 text-sm font-bold">'
                     f'&#8593; +{delta}% vs prior {days}d</span>')
        elif delta < 0:
            trend = (f'<span class="text-red-500 text-sm font-bold">'
                     f'&#8595; {delta}% vs prior {days}d</span>')
        else:
            trend = (f'<span class="text-slate-500 text-sm font-bold">'
                     f'&rarr; 0% vs prior {days}d</span>')
        scope = ' (all repos)' if self.support_paths else ''
        return (
            f'        <div class="card">\n'
            f'            <div class="text-[10px] uppercase font-bold text-slate-400 mb-2 tracking-widest">Last {days} Days</div>\n'
            f'            <div class="text-4xl font-black text-slate-900 mb-1">{current:,}</div>\n'
            f'            <div class="text-sm text-slate-500 mb-3">commits{scope}</div>\n'
            f'            {trend}\n'
            f'            <div class="text-xs text-slate-400 mt-2">Prior {days}d: {prior:,}</div>\n'
            f'        </div>'
        )

    def _compute_bus_factor_entries(self, metric_key: str) -> tuple:
        """Return (entries, total) for the bus factor section identified by metric_key.

        Iterates through authors sorted by the given metric in descending order,
        accumulating entries until the combined metric reaches bus_factor_threshold
        of the total.

        Args:
            metric_key: 'commits' or 'merges'.

        Returns:
            entries: List of dicts with keys name, count, pct, team, and
                     (for commits) team_commits.
            total:   Total metric value across all authors; 0 when there is
                     no history for that metric.
        """
        authors = self.data['authors']
        getter  = (lambda a: a['commits']) if metric_key == 'commits' else (lambda a: a.get('merges', 0))
        sorted_authors = sorted(authors.items(), key=lambda x: getter(x[1]), reverse=True)
        total = sum(getter(a) for _, a in sorted_authors)
        if total == 0:
            return [], 0
        cutoff  = total * self.bus_factor_threshold
        running = 0
        entries = []
        for name, adata in sorted_authors:
            val = getter(adata)
            if metric_key == 'merges' and val == 0:
                break
            running += val
            entry = {
                'name':  name,
                'count': val,
                'pct':   round(val / total * 100, 1),
                'team':  adata.get('team', self._DEFAULT_TEAM),
            }
            if metric_key == 'commits':
                entry['team_commits'] = adata.get('team_commits', {})
            entries.append(entry)
            if running >= cutoff:
                break
        return entries, total

    def _render_bf_section(self, label: str, metric_key: str, entries: list, bus_pct: int) -> str:
        """Render one labeled bus-factor section (Commits or PR Merges).

        Produces the inner <div> that appears inside the two-column bus factor
        card layout.  Both sections share the same structure; only the label,
        the metric name in the description sentence, and the row data differ.

        Args:
            label:      Section heading shown above the content (e.g. 'Commits').
            metric_key: 'commits' or 'merges' — passed to _render_bf_rows and
                        used to build the description sentence.
            entries:    Output of _compute_bus_factor_entries for this metric.
            bus_pct:    Threshold as an integer percentage (e.g. 50 for 50%).
        """
        count       = len(entries)
        plural_s    = 's' if count != 1 else ''
        plural_verb = 'account' if count != 1 else 'accounts'
        dim_label   = 'commits' if metric_key == 'commits' else 'PR merges'
        rows        = self._render_bf_rows(entries, metric_key)
        return (
            f'            <div>\n'
            f'                <div class="text-[10px] uppercase font-bold text-slate-400 tracking-widest mb-4">{label}</div>\n'
            f'                <div class="flex items-start gap-6 flex-wrap">\n'
            f'                    <div class="shrink-0 text-center min-w-[4rem]">\n'
            f'                        <div class="text-5xl font-black text-slate-900">{count}</div>\n'
            f'                        <div class="text-[10px] uppercase font-bold text-slate-400 mt-1 tracking-widest">Bus Factor</div>\n'
            f'                    </div>\n'
            f'                    <div class="flex-1 min-w-0">\n'
            f'                        <p class="text-sm text-slate-600 font-medium mb-4">\n'
            f'                            <strong>{count} contributor{plural_s}</strong> {plural_verb} for\n'
            f'                            {bus_pct}% of all {dim_label}.\n'
            f'                        </p>\n'
            f'{rows}\n'
            f'                    </div>\n'
            f'                </div>\n'
            f'            </div>'
        )

    def _render_bf_rows(self, bf_entries: list, metric_key: str) -> str:
        """Render contributor rows for one bus-factor section.

        Args:
            bf_entries:  List of dicts with keys: name, count, pct, team,
                         and optionally team_commits (for segmented commit bars).
            metric_key:  'commits' or 'merges' — controls the label on the right.
        """
        rows = []
        metric_label = 'commit' if metric_key == 'commits' else 'merge'
        for ba in bf_entries:
            if metric_key == 'commits':
                # Build a segmented bar when the author has commits across multiple
                # teams; fall back to a solid bar when teams are not configured.
                team_commits = ba.get('team_commits', {})
                author_total = sum(team_commits.values()) or 1
                if self.has_teams and len(team_commits) > 1:
                    segments = sorted(team_commits.items(), key=lambda kv: kv[1], reverse=True)
                    seg_html = ''.join(
                        f'<div style="width:{tc / author_total * 100:.2f}%;'
                        f'background:{self.team_colors.get(t, self._DEFAULT_TEAM_COLOR)};flex-shrink:0"></div>'
                        for t, tc in segments
                    )
                    bar_inner = (
                        f'<div style="width:{min(ba["pct"], 100)}%;height:100%;display:flex">'
                        f'{seg_html}</div>'
                    )
                else:
                    color = self.team_colors.get(ba['team'], self._DEFAULT_TEAM_COLOR) if self.has_teams else '#3b82f6'
                    bar_inner = (
                        f'<div class="h-full rounded-full"'
                        f' style="width:{min(ba["pct"], 100)}%;background:{color}"></div>'
                    )
            else:
                # Merges: simple solid bar using the author's team color.
                color = self.team_colors.get(ba['team'], self._DEFAULT_TEAM_COLOR) if self.has_teams else '#3b82f6'
                bar_inner = (
                    f'<div class="h-full rounded-full"'
                    f' style="width:{min(ba["pct"], 100)}%;background:{color}"></div>'
                )
            plural_v = 's' if ba['count'] != 1 else ''
            rows.append(
                f'                <div class="flex items-center gap-3 py-2">\n'
                f'                    <span class="text-sm font-bold text-slate-700 w-36 truncate shrink-0">{ba["name"]}</span>\n'
                f'                    <div class="flex-1 h-2 bg-slate-100 rounded-full overflow-hidden">\n'
                f'                        {bar_inner}\n'
                f'                    </div>\n'
                f'                    <span class="text-sm font-mono font-bold text-slate-500 w-10 text-right shrink-0">{ba["pct"]}%</span>\n'
                f'                    <span class="text-xs text-slate-400 w-24 text-right shrink-0">{ba["count"]:,} {metric_label}{plural_v}</span>\n'
                f'                </div>'
            )
        return '\n'.join(rows)

    def _render_bus_factor_card(self,
                                commit_bf_authors: list,
                                merge_bf_authors: list,
                                total_merges: int,
                                bus_pct: int,
                                show_merges_section: bool = True) -> str:
        """Render the bus factor card for the Summary tab.

        When show_merges_section is True (default) the card contains two side-by-side
        sections — Commits and PR Merges.  When False only the Commits section is
        rendered and it occupies the full width of the card.

        Args:
            commit_bf_authors:   Entries for the commits section (see _compute_bus_factor_entries).
            merge_bf_authors:    Entries for the merges section.
            total_merges:        Total PR merges across all authors; 0 → no merge history.
            bus_pct:             Threshold as an integer percentage (e.g. 50 for 50%).
            show_merges_section: Show the PR Merges section.  Pass False when
                                 impact_w_merges == 0.
        """
        if not show_merges_section:
            # Single-section card — commits only, full width (no section label).
            commit_bus_count = len(commit_bf_authors)
            plural_s    = 's' if commit_bus_count != 1 else ''
            plural_verb = 'account' if commit_bus_count != 1 else 'accounts'
            commit_rows = self._render_bf_rows(commit_bf_authors, 'commits')
            commits_section = (
                f'            <div class="flex items-start gap-8 flex-wrap">\n'
                f'                <div class="shrink-0 text-center min-w-[5rem]">\n'
                f'                    <div class="text-5xl font-black text-slate-900">{commit_bus_count}</div>\n'
                f'                    <div class="text-[10px] uppercase font-bold text-slate-400 mt-1 tracking-widest">Bus Factor</div>\n'
                f'                </div>\n'
                f'                <div class="flex-1 min-w-0">\n'
                f'                    <p class="text-sm text-slate-600 font-medium mb-4">\n'
                f'                        <strong>{commit_bus_count} contributor{plural_s}</strong> {plural_verb} for\n'
                f'                        {bus_pct}% of all commits.\n'
                f'                    </p>\n'
                f'{commit_rows}\n'
                f'                </div>\n'
                f'            </div>'
            )
            return (
                f'    <div class="card">\n'
                f'{commits_section}\n'
                f'        <p class="text-xs text-slate-400 mt-4 border-t border-slate-100 pt-3">\n'
                f'            Threshold: {bus_pct}% \u00b7 configurable via\n'
                f'            <span class="font-mono">bus_factor_threshold</span> in config.json\n'
                f'        </p>\n'
                f'    </div>'
            )

        # ── Two-section card — Commits | PR Merges ───────────────────────────
        commits_section = self._render_bf_section('Commits', 'commits', commit_bf_authors, bus_pct)

        if total_merges == 0:
            merges_section = (
                f'            <div>\n'
                f'                <div class="text-[10px] uppercase font-bold text-slate-400 tracking-widest mb-4">PR Merges</div>\n'
                f'                <p class="text-sm text-slate-400 italic">No PR merge history detected.</p>\n'
                f'            </div>'
            )
        else:
            merges_section = self._render_bf_section('PR Merges', 'merges', merge_bf_authors, bus_pct)

        return (
            f'    <div class="card">\n'
            f'        <div class="grid grid-cols-1 md:grid-cols-2 gap-8">\n'
            f'{commits_section}\n'
            f'            <div class="hidden md:block w-px bg-slate-100 self-stretch"></div>\n'
            f'{merges_section}\n'
            f'        </div>\n'
            f'        <p class="text-xs text-slate-400 mt-4 border-t border-slate-100 pt-3">\n'
            f'            Threshold: {bus_pct}% \u00b7 configurable via\n'
            f'            <span class="font-mono">bus_factor_threshold</span> in config.json\n'
            f'        </p>\n'
            f'    </div>'
        )

    def _render_summary_tab(self, age_days: int, tcom: int, repo_lines: int) -> str:
        """Compute Summary tab values and render the full tab HTML.

        Pulls project metadata from self.data and self.support_paths; receives
        the few pre-computed scalars it needs as parameters to avoid redundant
        dict lookups.

        Args:
            age_days:   Lifetime of the main repo in days.
            tcom:       Total commit count across all repos.
            repo_lines: Current line count of tracked files in the main repo.
        """
        pname = self.data['project_name']
        adate = self.data['analysis_date']

        # First commit date
        first_ts = self.data['general'].get('first_commit_ts', 0)
        first_commit_date = (
            datetime.datetime.fromtimestamp(first_ts).strftime('%b %d, %Y')
            if first_ts else 'N/A'
        )

        # Lifetime average weekly cadence
        age_weeks  = age_days / 7
        avg_weekly = round(tcom / age_weeks, 1) if age_weeks > 0 else 0

        # Release count before any display cap
        total_tags_count = self.data['general'].get('total_tags', len(self.data['tags']))
        shown_tags_count = len(self.data['tags'])
        tags_note = (
            f'<div class="text-xs text-slate-400 mt-1">Showing {shown_tags_count} of {total_tags_count}</div>'
            if shown_tags_count < total_tags_count else
            '<div class="text-xs text-slate-400 mt-1">&nbsp;</div>'
        )

        # Support repo pills in the project header
        if self.support_paths:
            pills = ' '.join(
                f'<span class="text-[10px] px-2 py-0.5 rounded font-black uppercase '
                f'bg-slate-100 text-slate-500">'
                f'+ {os.path.basename(os.path.abspath(p))}</span>'
                for p in self.support_paths
            )
            support_repos_html = f'<div class="flex flex-wrap gap-1 mt-1.5">{pills}</div>'
        else:
            support_repos_html = ''

        # Commit velocity — last N days vs the prior N days, from the combined heatmap
        heatmap   = self.data['activity']['heatmap']
        today     = datetime.date.today()
        today_str = today.isoformat()
        velocity_cards = []
        for days in self.summary_velocity_days:
            cur_start  = (today - datetime.timedelta(days=days)).isoformat()
            prev_start = (today - datetime.timedelta(days=days * 2)).isoformat()
            current = sum(v for k, v in heatmap.items() if cur_start <= k <= today_str)
            prior   = sum(v for k, v in heatmap.items() if prev_start <= k < cur_start)
            delta   = round((current - prior) / prior * 100, 1) if prior else None
            velocity_cards.append(self._render_velocity_card(days, current, prior, delta))

        ncols     = min(len(velocity_cards), 4)
        vcols_cls = f'md:grid-cols-{ncols}' if ncols > 1 else ''
        velocity_html = (
            f'    <div class="grid grid-cols-1 {vcols_cls} gap-6">\n'
            + '\n'.join(velocity_cards)
            + '\n    </div>'
        )

        bus_pct = int(self.bus_factor_threshold * 100)

        commit_bf, _            = self._compute_bus_factor_entries('commits')
        merge_bf,  total_merges = self._compute_bus_factor_entries('merges')

        bus_factor_html = self._render_bus_factor_card(
            commit_bf, merge_bf,
            total_merges, bus_pct,
            show_merges_section=(self.IMPACT_W_MERGES > 0),
        )

        return f"""    <!-- ═══ SUMMARY ════════════════════════════════════════════════════════════ -->
    <div id="tab-summary" class="tab-content space-y-8">

        <!-- Project info + key stats -->
        <div class="card">
            <div class="flex items-start justify-between gap-4 flex-wrap mb-6 pb-6 border-b border-slate-100">
                <div>
                    <h2 class="text-2xl font-black text-slate-900">{pname}</h2>
                    {support_repos_html}
                    <p class="text-xs text-slate-400 font-medium mt-1">Analyzed {adate}</p>
                </div>
            </div>
            <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
                <div class="text-center p-4 bg-slate-50 rounded-2xl">
                    <div class="text-3xl font-black text-slate-900">{age_days:,}</div>
                    <div class="text-[10px] uppercase font-bold text-slate-400 mt-0.5 tracking-widest">Days Old</div>
                    <div class="text-xs text-slate-400 mt-1">Since {first_commit_date}</div>
                </div>
                <div class="text-center p-4 bg-slate-50 rounded-2xl">
                    <div class="text-3xl font-black text-blue-600">{repo_lines:,}</div>
                    <div class="text-[10px] uppercase font-bold text-slate-400 mt-0.5 tracking-widest">Lines of Code</div>
                    <div class="text-xs text-slate-400 mt-1">Main repository</div>
                </div>
                <div class="text-center p-4 bg-slate-50 rounded-2xl">
                    <div class="text-3xl font-black text-slate-900">{avg_weekly}</div>
                    <div class="text-[10px] uppercase font-bold text-slate-400 mt-0.5 tracking-widest">Commits / Week</div>
                    <div class="text-xs text-slate-400 mt-1">Lifetime average</div>
                </div>
                <div class="text-center p-4 bg-slate-50 rounded-2xl">
                    <div class="text-3xl font-black text-slate-900">{total_tags_count:,}</div>
                    <div class="text-[10px] uppercase font-bold text-slate-400 mt-0.5 tracking-widest">Releases</div>
                    {tags_note}
                </div>
            </div>
        </div>

{velocity_html}

        <!-- Monthly commit activity -->
        <div class="card" style="height:280px;display:flex;flex-direction:column">
            <h3 class="font-bold mb-1">Monthly Commit Activity</h3>
            <p class="text-xs text-slate-400 font-medium mb-3">Commits per calendar month — all repositories</p>
            <div style="flex:1;position:relative;min-height:0"><canvas id="monthlyChart"></canvas></div>
        </div>

{bus_factor_html}

        <!-- Hourly Punchcard -->
        <div class="card" style="height:420px;display:flex;flex-direction:column">
            <h3 class="font-bold mb-1">Hourly Punchcard</h3>
            <p class="text-xs text-slate-400 font-medium mb-3">Commits per hour of day over repository lifetime</p>
            <div style="flex:1;position:relative;min-height:0"><canvas id="hourChart"></canvas></div>
        </div>

    </div>"""

    # ------------------------------------------------------------------ report

    def generate_report(self, external_dir, output="index.html"):
        """Render all collected data into a self-contained HTML file.

        Serializes self.data to JSON and injects it directly into the HTML
        as JavaScript constants, so the output file has zero external data
        dependencies. externals/tailwind.js and externals/chart.js are copied
        next to the output file so the report works fully offline.
        """
        # Ensure every configured team with at least one member appears in the
        # Teams tab, even if no commits were attributed to it (e.g. a team added
        # to config before any of its members have pushed code).  Teams whose
        # config members list is empty are omitted — nothing meaningful to show.
        for team_name, team_val in self.teams_config.items():
            if not team_val.get('members'):
                continue
            if team_name not in self.data['teams']:
                self.data['teams'][team_name] = {
                    'commits': 0, 'add': 0, 'del': 0, 'merges': 0,
                    'members': set(), 'first': 0, 'last': 0,
                    'impact': 0,
                }

        # Split member sets into current vs. previous based on active team
        # membership as of now.  All alias keys (name + configured aliases/emails)
        # are checked so membership is determined solely by the from/to date ranges
        # in config, regardless of which identity the author used in any commit.
        now_ts = int(time.time())
        for team_name, t in self.data['teams'].items():
            current, previous = [], []
            for author in sorted(t['members']):
                # Check all lookup keys for this author at now_ts.
                is_current = any(
                    any(t_name == team_name and from_ts <= now_ts <= to_ts
                        for t_name, from_ts, to_ts in self.author_to_team_ranges.get(key, []))
                    for key in self._author_lookup_keys(author)
                )
                if is_current:
                    current.append(author)
                else:
                    previous.append(author)
            t['members']          = current
            t['previous_members'] = previous

        # ── JSON blobs injected into the <script> block ───────────────────────
        authors_json        = json.dumps(self.data['authors'])
        teams_json          = json.dumps(self.data['teams'])
        author_aliases_json = json.dumps(self.canonical_to_aliases)
        team_colors_json = json.dumps(self.team_colors)
        # team_components: top 8 components per team — used by the Teams tab cards.
        # Drawn from the main repo only; support repo component data appears in
        # the separate per-repo cards on the Components tab.
        team_component_json = json.dumps({k: dict(v.most_common(8)) for k, v in self.data['team_components'].items()})
        hour_data           = json.dumps([self.data['activity']['hour'][i] for i in range(24)])

        # Monthly commit counts — aggregate the daily heatmap (all repos combined)
        # into calendar months for the line chart on the Summary tab.
        monthly: Counter = Counter()
        for date_str, count in self.data['activity']['heatmap'].items():
            monthly[date_str[:7]] += count          # 'YYYY-MM-DD' → 'YYYY-MM'
        sorted_months       = sorted(monthly.keys())
        monthly_labels_json = json.dumps([
            datetime.datetime.strptime(m, '%Y-%m').strftime('%b %Y')
            for m in sorted_months
        ])
        monthly_counts_json = json.dumps([monthly[m] for m in sorted_months])
        monthly_keys_json   = json.dumps(sorted_months)
        monthly_top_authors_json = json.dumps({
            m: [{'name': n, 'commits': c}
                for n, c in self.data['monthly_author_commits'].get(m, Counter()).most_common(self.monthly_top_n)]
            for m in sorted_months
        })

        # ── Component section ─────────────────────────────────────────────────
        repo_charts, repo_charts_json, component_json, component_cards_html = (
            self._build_component_section()
        )

        pname  = self.data['project_name']
        adate  = self.data['analysis_date']
        tcom   = self.data['general']['total_commits']
        tlines = self.data['general']['total_lines']
        nauth  = len(self.data['authors'])
        nteams = len(self.data['teams'])
        age_days   = self.data['general'].get('age_days', 0)
        repo_lines = self.data['general'].get('total_repo_lines', 0)

        iw_commits = self.IMPACT_W_COMMITS
        iw_lines   = self.IMPACT_W_LINES
        iw_tenure  = self.IMPACT_W_TENURE
        iw_merges  = self.IMPACT_W_MERGES
        primary_branch = self.primary_branch

        # Build the weight cards and formula for the Impact explanation section.
        # Cards and formula terms are omitted when a weight is 0.
        _active_dims = []
        if iw_commits > 0:
            _active_dims.append(('Commit Volume', iw_commits,
                'Total number of commits authored. Rewards consistent contribution frequency over time.',
                f'score += (commits / max_commits) × {iw_commits}'))
        if iw_lines > 0:
            _active_dims.append(('Lines Changed', iw_lines,
                'Effective lines changed, filtered to remove noise. Reformats, mass moves, and revert pairs are discounted before scoring.',
                f'score += (effective_lines / max_lines) × {iw_lines}'))
        if iw_tenure > 0:
            _active_dims.append(('Active Tenure', iw_tenure,
                "Days between a contributor's first and last commit. Rewards long-term, sustained engagement.",
                f'score += (tenure_days / max_tenure) × {iw_tenure}'))
        if iw_merges > 0:
            _active_dims.append(('PR Merges', iw_merges,
                f'Number of pull requests merged into <span class="font-mono">{primary_branch}</span>. Credits the committer — the person who pressed the merge button. Includes true merges and squash/rebase merges detected by commit message.',
                f'score += (merges / max_merges) × {iw_merges}'))

        _ncards = len(_active_dims)
        _grid_cols = {1: 'grid-cols-1', 2: 'grid-cols-1 md:grid-cols-2',
                      3: 'grid-cols-1 md:grid-cols-3', 4: 'grid-cols-1 md:grid-cols-2 lg:grid-cols-4'}.get(_ncards, 'grid-cols-1 md:grid-cols-2')
        _weight_cards_html = '\n'.join(
            f'''                <div class="bg-slate-50 rounded-2xl p-5">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-sm font-black text-slate-700">{label}</span>
                        <span class="text-2xl font-black text-blue-600">{w}%</span>
                    </div>
                    <div class="impact-bar mb-3"><div class="impact-fill" style="width:{w}%"></div></div>
                    <p class="text-xs text-slate-500">{desc}</p>
                    <p class="text-[11px] text-slate-400 mt-2 font-mono">{formula}</p>
                </div>'''
            for label, w, desc, formula in _active_dims
        )
        _formula_terms = ' &nbsp;+&nbsp; '.join(
            {'Commit Volume': f'(commits / max_commits) × {iw_commits}',
             'Lines Changed': f'(effective_lines / max_lines) × {iw_lines}',
             'Active Tenure': f'(tenure_days / max_tenure) × {iw_tenure}',
             'PR Merges':     f'(merges / max_merges) × {iw_merges}'}[label]
            for label, w, desc, formula in _active_dims
        )
        _impact_subtitle_parts = []
        if iw_commits > 0: _impact_subtitle_parts.append(f'commits&nbsp;{iw_commits}%')
        if iw_lines   > 0: _impact_subtitle_parts.append(f'lines&nbsp;{iw_lines}%')
        if iw_tenure  > 0: _impact_subtitle_parts.append(f'tenure&nbsp;{iw_tenure}%')
        if iw_merges  > 0: _impact_subtitle_parts.append(f'merges&nbsp;{iw_merges}%')
        impact_subtitle = 'Impact = ' + '&nbsp;·&nbsp;'.join(_impact_subtitle_parts)

        # Teams tab visibility
        teams_tab_hidden = ' hidden' if not self.has_teams else ''
        # Impact tab: two-column layout only when teams are configured
        impact_grid_cls  = 'grid grid-cols-1 lg:grid-cols-2 gap-8' if self.has_teams else 'grid grid-cols-1 gap-8'
        has_teams_js     = json.dumps(self.has_teams)
        iw_commits_js    = json.dumps(self.IMPACT_W_COMMITS)
        iw_lines_js      = json.dumps(self.IMPACT_W_LINES)
        iw_tenure_js     = json.dumps(self.IMPACT_W_TENURE)
        iw_merges_js     = json.dumps(self.IMPACT_W_MERGES)
        tags_tab_html    = self._render_tags_html()

        # Noise-reduction settings — displayed in the impact explanation section
        # so viewers know exactly what filtering was applied to this report.
        cfg_use_net      = 'on' if self.use_net_lines else 'off'
        cfg_wash_days    = self.wash_window_days
        cfg_wash_min     = self.wash_min_gross
        cfg_cap_pct      = self.line_cap_percentile
        cfg_wash_status  = f'{cfg_wash_days}-day window, ≥{cfg_wash_min} gross lines' if cfg_wash_days else 'off'
        cfg_cap_status   = f'{cfg_cap_pct}th percentile' if cfg_cap_pct else 'off'

        # ── Per-tab HTML ──────────────────────────────────────────────────────
        summary_tab_html = self._render_summary_tab(age_days, tcom, repo_lines)

        html = f"""<!DOCTYPE html>
<html class="scroll-smooth">
<head>
    <meta charset="UTF-8">
    <script src="tailwind.js"></script>
    <script src="chart.js"></script>
    <title>GitStats: {pname}</title>
    <style>
        .card {{ background:white; padding:2rem; border-radius:1.5rem; border:1px solid #e2e8f0; box-shadow:0 1px 3px rgba(0,0,0,0.04); }}
        .tab-btn {{ padding:0.5rem 1.25rem; border-radius:0.75rem; font-weight:700; font-size:0.875rem;
                    transition:all 0.15s; color:#64748b; cursor:pointer; border:none; background:none; }}
        .tab-btn:hover {{ color:#0f172a; background:#f1f5f9; }}
        .tab-btn.active {{ background:#0f172a; color:white; box-shadow:0 4px 12px rgba(0,0,0,0.15); }}
        .sticky-header {{ position:sticky; top:0; z-index:50; }}
        .impact-bar {{ height:6px; border-radius:3px; background:#e2e8f0; overflow:hidden; }}
        .impact-fill {{ height:100%; border-radius:3px; background:linear-gradient(90deg,#3b82f6,#1d4ed8); }}
        /* Critical: own definition so tabs work without Tailwind CDN */
        .hidden {{ display:none !important; }}
    </style>
</head>
<body class="bg-[#f8fafc] text-slate-900 pb-24">

<!-- ═══ HEADER ═══════════════════════════════════════════════════════════════ -->
<header class="bg-white border-b border-slate-200 pt-10 pb-6 px-6 mb-8">
    <div class="max-w-7xl mx-auto flex flex-col md:flex-row justify-between items-center gap-6">
        <div>
            <div class="flex items-center gap-3 mb-1">
                <span class="bg-blue-600 text-white text-[10px] font-black px-2 py-0.5 rounded uppercase tracking-widest">Repo Stats</span>
            </div>
            <h2 class="text-2xl font-bold text-slate-800">{pname}</h2>
            <p class="text-xs font-bold text-slate-400 uppercase tracking-widest mt-1">Last Analysis: {adate}</p>
        </div>
        <div class="flex gap-6 flex-wrap justify-center">
            <div class="text-center px-4 border-r border-slate-100">
                <span class="block text-2xl font-black text-slate-900">{tcom:,}</span>
                <span class="text-[10px] uppercase font-bold text-slate-400">Commits</span>
            </div>
            <div class="text-center px-4 border-r border-slate-100">
                <span class="block text-2xl font-black text-blue-600">{tlines:,}</span>
                <span class="text-[10px] uppercase font-bold text-slate-400">Net Lines</span>
            </div>
            <div class="text-center px-4 border-r border-slate-100">
                <span class="block text-2xl font-black text-slate-900">{nauth}</span>
                <span class="text-[10px] uppercase font-bold text-slate-400">Contributors</span>
            </div>
            <div class="text-center px-4">
                <span class="block text-2xl font-black text-slate-900">{nteams}</span>
                <span class="text-[10px] uppercase font-bold text-slate-400">Teams</span>
            </div>
        </div>
    </div>
</header>

<div class="max-w-7xl mx-auto px-6">

    <!-- ═══ NAV ══════════════════════════════════════════════════════════════ -->
    <nav class="sticky-header mb-12">
        <div class="bg-white/70 backdrop-blur-xl p-1.5 rounded-2xl shadow-xl shadow-slate-200/50
                    flex gap-1 justify-center max-w-fit mx-auto border border-slate-200">
            <button onclick="showTab('summary',this)" class="tab-btn active">Summary</button>
            <button onclick="showTab('impact',this)"   class="tab-btn">Impact</button>
            <button onclick="showTab('authors',this)"  class="tab-btn">Authors</button>
            <button onclick="showTab('teams',this)"    class="tab-btn{teams_tab_hidden}">Teams</button>
            <button onclick="showTab('tags',this)"     class="tab-btn">Releases</button>
            <button onclick="showTab('components',this)"  class="tab-btn">Components</button>
        </div>
    </nav>

{summary_tab_html}

    <!-- ═══ IMPACT ═══════════════════════════════════════════════════════════ -->
    <div id="tab-impact" class="tab-content hidden space-y-8">
        <div class="{impact_grid_cls}">
            <div class="card">
                <div class="flex items-start justify-between mb-0.5">
                    <h3 class="text-xl font-black text-slate-900" id="impact-authors-title">Top Contributors</h3>
                    <button id="impact-clear-btn" onclick="clearImpactFilter()"
                            class="hidden text-[10px] font-black px-2.5 py-1 rounded-lg bg-slate-100 text-slate-500 hover:bg-slate-200 transition-all uppercase tracking-wide">
                        Clear Filter
                    </button>
                </div>
                <p class="text-xs text-slate-400 font-medium mb-6">{impact_subtitle}</p>
                <div id="impact-authors" class="space-y-1"></div>
            </div>
            <div class="card" id="impact-teams-card">
                <h3 class="text-xl font-black text-slate-900 mb-0.5">Team Rankings</h3>
                <p class="text-xs text-slate-400 font-medium mb-6">Click a team to filter contributors</p>
                <div id="impact-teams" class="space-y-1"></div>
            </div>
        </div>
        <div class="card" style="height:360px">
            <h3 class="text-xl font-black mb-4">Impact Score — Top 15 Contributors</h3>
            <canvas id="impactChart"></canvas>
        </div>

        <!-- Score methodology explanation -->
        <div class="card">
            <h3 class="text-xl font-black text-slate-900 mb-1">How the Impact Score Is Computed</h3>
            <p class="text-xs text-slate-400 font-medium mb-6">Scores range from 0 to 100. Each metric is normalized relative to the top performer, so the highest scorer in each dimension always contributes the full weight for that dimension.</p>
            <div class="grid {_grid_cols} gap-6 mb-6">
{_weight_cards_html}
            </div>
            <div class="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-800 mb-4">
                <span class="font-black">Formula: </span>
                Impact = {_formula_terms}
                <br><span class="text-blue-500 mt-1 block">All normalizations are computed independently for authors and for teams.</span>
            </div>
            <div class="bg-slate-50 border border-slate-200 rounded-xl p-4 text-xs text-slate-700">
                <span class="font-black text-slate-800">Lines noise filtering</span>
                <span class="text-slate-400 ml-2">(configured via config.json)</span>
                <ul class="mt-2 space-y-1 text-slate-500 leading-relaxed">
                    <li><span class="font-bold text-slate-600">Net lines per commit</span> <span class="font-mono bg-slate-200 text-slate-700 rounded px-1">{cfg_use_net}</span> — each commit scores <span class="font-mono">|adds − dels|</span> instead of <span class="font-mono">adds + dels</span>, so reformatting commits that delete and re-add the same code count near zero. Config key: <span class="font-mono">impact_use_net_lines</span>.</li>
                    <li><span class="font-bold text-slate-600">Winsorization</span> <span class="font-mono bg-slate-200 text-slate-700 rounded px-1">{cfg_cap_status}</span> — per-commit effective lines are capped at this percentile of all commits in the repo, preventing a single mass import from dominating the metric. Config key: <span class="font-mono">impact_line_cap_percentile</span>.</li>
                    <li><span class="font-bold text-slate-600">Wash-window detection</span> <span class="font-mono bg-slate-200 text-slate-700 rounded px-1">{cfg_wash_status}</span> — commits are grouped into time buckets; if a bucket's raw changes exceed the minimum and adds ≈ dels, the bucket scores only its net <span class="font-mono">|adds − dels|</span>, catching the pattern of a mass delete followed by a mass re-add. Config keys: <span class="font-mono">impact_wash_window_days</span>, <span class="font-mono">impact_wash_min_gross</span>.</li>
                </ul>
            </div>
        </div>
    </div>

    <!-- ═══ AUTHORS ═══════════════════════════════════════════════════════════ -->
    <div id="tab-authors" class="tab-content hidden space-y-6">
        <div class="flex flex-wrap justify-between items-center bg-white p-6 rounded-3xl border border-slate-200 gap-4">
            <h2 id="author-title" class="text-xl font-black">All Contributors</h2>
            <div class="flex items-center gap-3">
                <button id="reset-filter" onclick="resetAuthorFilter()"
                        class="hidden bg-blue-50 text-blue-600 px-4 py-2 rounded-xl text-xs font-black uppercase hover:bg-blue-100 transition-all">
                    Clear Filter
                </button>
                <div class="flex gap-1 text-xs font-bold">
                    <button onclick="setSortKey('impact')"  id="sort-impact"  class="px-3 py-1.5 rounded-lg bg-slate-900 text-white">Impact</button>
                    <button onclick="setSortKey('commits')" id="sort-commits" class="px-3 py-1.5 rounded-lg text-slate-500 hover:bg-slate-100 transition-all">Commits</button>
                    <button onclick="setSortKey('lines')"   id="sort-lines"   class="px-3 py-1.5 rounded-lg text-slate-500 hover:bg-slate-100 transition-all">Lines</button>
                </div>
            </div>
        </div>
        <div class="card overflow-hidden shadow-xl" style="padding:0">
            <table class="w-full text-left border-collapse" id="author-table"></table>
        </div>
    </div>

    <!-- ═══ TEAMS ═════════════════════════════════════════════════════════════ -->
    <div id="tab-teams" class="tab-content hidden{teams_tab_hidden}">
        <div id="teams-grid" class="grid grid-cols-1 md:grid-cols-2 gap-6"></div>
    </div>

    <!-- ═══ RELEASES ══════════════════════════════════════════════════════════ -->
    <div id="tab-tags" class="tab-content hidden space-y-8">
        {tags_tab_html}
    </div>

    <!-- ═══ COMPONENTS ════════════════════════════════════════════════════════ -->
    <div id="tab-components" class="tab-content hidden">
{component_cards_html}
    </div>

</div><!-- /max-w-7xl -->

<!-- ═══════════════════════════════════════════════════════════════ JAVASCRIPT -->
<script>
const authorData     = {authors_json};
const teamsData      = {teams_json};
const authorAliases  = {author_aliases_json};
const teamColors     = {team_colors_json};
const hasTeams       = {has_teams_js};
const impactWCommits = {iw_commits_js};
const impactWLines   = {iw_lines_js};
const impactWTenure  = {iw_tenure_js};
const impactWMerges  = {iw_merges_js};
const componentData     = {component_json};
const teamComponentData = {team_component_json};
const totalCommits   = {tcom};

// Per-repo chart data: one entry per repo (main first, then support repos).
// Labels for support repos carry a "reponame:path" prefix to avoid collision.
const repoCharts = {repo_charts_json};

// Pre-compute how many component paths share each short (last-segment) name
// across all repos, so compLabel() can decide whether to abbreviate.
const _compShortCount = {{}};
repoCharts.forEach(repo => {{
    repo.labels.forEach(k => {{
        const colon = k.indexOf(':');
        const p = colon >= 0 ? k.slice(colon + 1) : k;
        const s = p.includes('/') ? p.split('/').pop() : p;
        _compShortCount[s] = (_compShortCount[s] || 0) + 1;
    }});
}});

let currentSortKey   = 'impact';
let currentFilter    = null;
let currentFilterType = null;
let impactTeamFilter = null;

// ─── Navigation ──────────────────────────────────────────────────────────────
// Charts that live in non-default tabs must be initialized lazily (only when the
// tab is first shown) so Chart.js doesn't try to render onto a hidden canvas.
const _chartInited = {{}};

function showTab(t, btn) {{
    document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    document.getElementById('tab-' + t).classList.remove('hidden');
    if (!_chartInited[t]) {{
        _chartInited[t] = true;
        if (t === 'impact')  initImpactChart();
        if (t === 'components') initComponentChart();
    }}
    window.scrollTo({{top: 0, behavior: 'smooth'}});
}}

function navToAuthors() {{
    const btn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.textContent === 'Authors');
    showTab('authors', btn);
}}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function teamColor(team) {{ return teamColors[team] || '#94a3b8'; }}

function fmt(n) {{ return Number(n).toLocaleString(); }}

// Return a display label for a component path.
// Support repo paths carry a "reponame:path" prefix which is stripped first.
// Uses only the last path segment unless that segment is ambiguous (shared by
// multiple components across all repos), in which case the full path is returned.
function compLabel(path) {{
    const colon = path.indexOf(':');
    const p = colon >= 0 ? path.slice(colon + 1) : path;
    const short = p.includes('/') ? p.split('/').pop() : p;
    return _compShortCount[short] > 1 ? p : short;
}}

function impactBar(score, color) {{
    return `<div class="impact-bar"><div class="impact-fill" style="width:${{score}}%;background:${{color}}"></div></div>`;
}}

function teamBadge(team) {{
    // Returns empty string when no teams are configured — avoids a meaningless
    // "Community" label appearing on every author row and leaderboard entry.
    if (!hasTeams) return '';
    const c = teamColor(team);
    return `<span class="text-[10px] px-2 py-0.5 rounded font-black uppercase inline-block"
                  style="background:${{c}}22;color:${{c}}">${{team}}</span>`;
}}

function teamBadges(s) {{
    // For authors who have belonged to multiple teams, render one badge per team.
    // The current (most-recent) team appears first; others are sorted by commit count.
    if (!hasTeams) return '';
    const tc = s.team_commits || {{}};
    const teams = Object.keys(tc);
    if (teams.length <= 1) return teamBadge(s.team);
    const sorted = Object.entries(tc).sort((a, b) => {{
        if (a[0] === s.team) return -1;
        if (b[0] === s.team) return 1;
        return b[1] - a[1];
    }});
    return sorted.map(([t]) => teamBadge(t)).join(' ');
}}

function authorImpactTooltip(name, s) {{
    // Build the title attribute value for the impact score cell.
    // Shows per-dimension factors used to compute the lifetime impact score.
    const days   = Math.floor((s.last - s.first) / 86400);
    const plural = days !== 1 ? 's' : '';
    const lines  = [];
    lines.push(name);
    lines.push('Impact score breakdown:');
    if (impactWCommits > 0) lines.push(`  Commits: ${{(s.commits || 0).toLocaleString()}}`);
    if (impactWLines   > 0) lines.push(`  Eff. Lines: ${{(s.eff_lines || 0).toLocaleString()}}`);
    if (impactWTenure  > 0) lines.push(`  Tenure: ${{days}} day${{plural}}`);
    if (impactWMerges  > 0) lines.push(`  Merges: ${{(s.merges || 0).toLocaleString()}}`);
    lines.push(`Score: ${{s.impact || 0}} / 100`);
    return lines.join('&#10;');
}}

function authorImpactBar(impact, s) {{
    // Renders the impact bar for an author row.
    // Single-team authors get a solid bar in their team colour.
    // Multi-team authors get a segmented bar: current team first (leftmost),
    // then remaining teams by commit count, each segment proportional to their
    // share of that author's total commits.
    const tc = s.team_commits || {{}};
    const teams = Object.keys(tc);
    if (!hasTeams || teams.length <= 1) {{
        const color = hasTeams ? teamColor(s.team) : '#3b82f6';
        return `<div class="impact-bar flex-1"><div class="impact-fill" style="width:${{impact}}%;background:${{color}}"></div></div>`;
    }}
    const total = Object.values(tc).reduce((a, b) => a + b, 0) || 1;
    const sorted = Object.entries(tc).sort((a, b) => {{
        if (a[0] === s.team) return -1;
        if (b[0] === s.team) return 1;
        return b[1] - a[1];
    }});
    const segs = sorted.map(([t, c]) =>
        `<div style="width:${{(c/total*100).toFixed(2)}}%;background:${{teamColor(t)}};flex-shrink:0"></div>`
    ).join('');
    return `<div class="impact-bar flex-1"><div style="width:${{impact}}%;height:100%;display:flex">${{segs}}</div></div>`;
}}

// ─── Sort controls ────────────────────────────────────────────────────────────
function setSortKey(key) {{
    currentSortKey = key;
    ['impact','commits','lines'].forEach(k => {{
        const b = document.getElementById('sort-' + k);
        b.className = k === key
            ? 'px-3 py-1.5 rounded-lg bg-slate-900 text-white text-xs font-bold'
            : 'px-3 py-1.5 rounded-lg text-slate-500 hover:bg-slate-100 transition-all text-xs font-bold';
    }});
    renderAuthorTable(currentFilter, currentFilterType);
}}

// ─── Author table ─────────────────────────────────────────────────────────────
function resetAuthorFilter() {{
    currentFilter = null;
    currentFilterType = null;
    renderAuthorTable();
}}

function renderAuthorTable(filter, filterType) {{
    if (filter     !== undefined) currentFilter     = filter;
    if (filterType !== undefined) currentFilterType = filterType;

    const table    = document.getElementById('author-table');
    const title    = document.getElementById('author-title');
    const resetBtn = document.getElementById('reset-filter');

    let authors      = Object.entries(authorData);
    let displayTotal = totalCommits;

    if (currentFilter && currentFilterType === 'component') {{
        const filtered = componentData[currentFilter] || {{}};
        authors      = authors.filter(([n]) => filtered[n]);
        displayTotal = Object.values(filtered).reduce((a,b) => a+b, 0) || 1;
        const colon  = currentFilter.indexOf(':');
        const repoTag = colon >= 0 ? ` [${{currentFilter.slice(0, colon)}}]` : '';
        title.innerText = `Component: ${{compLabel(currentFilter)}}${{repoTag}}`;
        resetBtn.classList.remove('hidden');
    }} else if (currentFilter && currentFilterType === 'team') {{
        authors      = authors.filter(([,d]) => d.team === currentFilter);
        displayTotal = (teamsData[currentFilter]?.commits) || 1;
        title.innerText = `Team: ${{currentFilter}}`;
        resetBtn.classList.remove('hidden');
    }} else {{
        title.innerText = 'All Contributors';
        resetBtn.classList.add('hidden');
    }}

    authors.sort((a, b) => {{
        if (currentSortKey === 'impact')  return b[1].impact  - a[1].impact;
        if (currentSortKey === 'commits') return b[1].commits - a[1].commits;
        if (currentSortKey === 'lines')   return (b[1].add + b[1].del) - (a[1].add + a[1].del);
        return 0;
    }});

    const rows = authors.map(([name, s]) => {{
        const commits = (currentFilter && currentFilterType === 'component')
            ? ((componentData[currentFilter] || {{}})[name] || 0)
            : s.commits;
        const share      = ((commits / displayTotal) * 100).toFixed(1);
        const activeDays = Math.floor((s.last - s.first) / 86400);
        const color      = teamColor(s.team);
        const impact     = s.impact || 0;
        const merges     = s.merges || 0;
        const als = authorAliases[name] || [];
        const authorTip = als.length ? `${{name}}\nAliases: ${{als.join(', ')}}` : name;
        return `<tr class="hover:bg-slate-50 transition-colors">
            <td class="px-8 py-4">
                <div class="font-bold text-slate-800 cursor-default" title="${{authorTip}}">${{name}}</div>
                <div class="flex flex-wrap gap-1 mt-0.5">${{teamBadges(s)}}</div>
            </td>
            <td class="font-mono text-sm font-bold">${{fmt(commits)}}</td>
            <td>
                <div class="flex items-center gap-2">
                    <span class="font-bold text-blue-600 text-sm w-12">${{share}}%</span>
                    <div class="w-16 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                        <div class="h-full bg-blue-500 rounded-full" style="width:${{Math.min(share,100)}}%"></div>
                    </div>
                </div>
            </td>
            <td class="text-sm font-mono">
                <span class="text-emerald-600">+${{fmt(s.add)}}</span>
                <span class="text-slate-300 mx-1">/</span>
                <span class="text-red-400">-${{fmt(s.del)}}</span>
            </td>
            <td class="px-4 font-mono text-sm font-bold text-slate-600">${{merges > 0 ? merges : '<span class="text-slate-300">—</span>'}}</td>
            <td>
                <div class="flex items-center gap-2" style="min-width:140px">
                    <span class="text-xs font-black text-slate-700 w-8 cursor-default"
                          title="${{authorImpactTooltip(name, s)}}">${{impact}}</span>
                    ${{authorImpactBar(impact, s)}}
                </div>
            </td>
            <td class="px-6 text-slate-400 font-bold text-xs">${{activeDays}}d</td>
        </tr>`;
    }}).join('');

    table.innerHTML = `
        <thead class="bg-slate-50 border-b text-[10px] uppercase font-bold text-slate-500">
            <tr>
                <th class="px-8 py-5">Developer</th>
                <th>Commits</th>
                <th>Share</th>
                <th>Lines +/−</th>
                <th class="px-4">Merges</th>
                <th>Impact Score</th>
                <th class="px-6">Tenure</th>
            </tr>
        </thead>
        <tbody class="divide-y divide-slate-100 bg-white">${{rows}}</tbody>`;
}}

// ─── Impact leaderboard ───────────────────────────────────────────────────────
function filterImpactByTeam(team) {{
    impactTeamFilter = team;
    renderImpactLeaderboard();
}}

function clearImpactFilter() {{
    impactTeamFilter = null;
    renderImpactLeaderboard();
}}

function renderImpactLeaderboard() {{
    const medals = ['🥇','🥈','🥉'];

    // Authors — apply team filter if active
    const allAuthors = Object.entries(authorData).sort((a,b) => b[1].impact - a[1].impact);
    const filtered   = impactTeamFilter
        ? allAuthors.filter(([, s]) => s.team === impactTeamFilter)
        : allAuthors.slice(0, 12);

    const titleEl    = document.getElementById('impact-authors-title');
    const clearBtn   = document.getElementById('impact-clear-btn');
    if (impactTeamFilter) {{
        titleEl.innerText = `${{impactTeamFilter}} Contributors`;
        clearBtn.classList.remove('hidden');
    }} else {{
        titleEl.innerText = 'Top Contributors';
        clearBtn.classList.add('hidden');
    }}

    document.getElementById('impact-authors').innerHTML = filtered.length
        ? filtered.map(([name, s], i) => {{
            const color  = teamColor(s.team);
            const rank   = allAuthors.findIndex(([n]) => n === name);
            const prefix = rank < 3
                ? `<span class="text-xl w-8 text-center shrink-0">${{medals[rank]}}</span>`
                : `<span class="text-xs font-black text-slate-300 w-8 text-center shrink-0">#${{rank+1}}</span>`;
            return `<div class="flex items-center gap-3 p-3 rounded-xl hover:bg-slate-50 transition-colors">
                ${{prefix}}
                <div class="flex-1 min-w-0">
                    <div class="flex items-center justify-between mb-1">
                        <span class="font-bold text-slate-800 truncate">${{name}}</span>
                        <span class="text-sm font-black text-slate-700 ml-2 shrink-0">${{s.impact}}</span>
                    </div>
                    ${{impactBar(s.impact, '#3b82f6')}}
                    <div class="flex items-center gap-2 mt-1">
                        ${{teamBadge(s.team)}}
                        <span class="text-[10px] text-slate-400">${{fmt(s.commits)}} commits · +${{fmt(s.add)}} lines</span>
                    </div>
                </div>
            </div>`;
          }}).join('')
        : `<p class="text-sm text-slate-400 font-medium py-4">No contributors found for this team.</p>`;

    // Teams — highlight active filter
    const topTeams = Object.entries(teamsData).sort((a,b) => b[1].impact - a[1].impact);

    document.getElementById('impact-teams').innerHTML = topTeams.map(([name, s], i) => {{
        const color      = teamColor(name);
        const members    = Array.isArray(s.members) ? s.members.length : 0;
        const isActive   = impactTeamFilter === name;
        const prefix     = i < 3
            ? `<span class="text-xl w-8 text-center shrink-0">${{medals[i]}}</span>`
            : `<span class="text-xs font-black text-slate-300 w-8 text-center shrink-0">#${{i+1}}</span>`;
        const activeCls  = isActive
            ? `border border-2 rounded-xl`
            : `hover:bg-slate-50 rounded-xl`;
        const activeStyle = isActive ? `border-color:${{color}};background:${{color}}11` : '';
        return `<div class="flex items-center gap-3 p-3 ${{activeCls}} transition-colors cursor-pointer"
                     style="${{activeStyle}}" data-team="${{name.replace(/"/g,'&quot;')}}"
                     onclick="filterImpactByTeam(this.dataset.team)"
                     title="Click to filter contributors">
            ${{prefix}}
            <div class="flex-1 min-w-0">
                <div class="flex items-center justify-between mb-1">
                    <span class="font-bold truncate" style="color:${{color}}">${{name}}</span>
                    <span class="text-sm font-black text-slate-700 ml-2 shrink-0">${{s.impact}}</span>
                </div>
                ${{impactBar(s.impact, color)}}
                <span class="text-[10px] text-slate-400">${{members}} members · ${{fmt(s.commits)}} commits</span>
            </div>
        </div>`;
    }}).join('');

}}

function initImpactChart() {{
    const top15 = Object.entries(authorData).sort((a,b) => b[1].impact - a[1].impact).slice(0,15);
    new Chart(document.getElementById('impactChart'), {{
        type: 'bar',
        data: {{
            labels: top15.map(([n]) => n.split(' ')[0]),
            datasets: [{{
                label: 'Impact Score',
                data: top15.map(([,s]) => s.impact),
                backgroundColor: top15.map(([,s]) => teamColor(s.team)),
                borderRadius: 6, barThickness: 24,
            }}]
        }},
        options: {{
            responsive: true, maintainAspectRatio: false,
            plugins: {{
                legend: {{ display: false }},
                tooltip: {{
                    callbacks: {{
                        title:     items  => top15[items[0].dataIndex][0],
                        afterBody: items  => {{
                            const [,s] = top15[items[0].dataIndex];
                            return [`Team: ${{s.team}}`, `Commits: ${{fmt(s.commits)}}`, `+${{fmt(s.add)}} lines`];
                        }}
                    }}
                }}
            }},
            scales: {{
                y: {{ max:100, grid:{{ color:'#f1f5f9' }}, ticks:{{ font:{{ weight:'bold' }} }} }},
                x: {{ grid:{{ display:false }}, ticks:{{ font:{{ weight:'bold' }} }} }}
            }}
        }}
    }});
}}

// ─── Teams grid ───────────────────────────────────────────────────────────────
function renderTeamsGrid() {{
    const medals = ['🥇','🥈','🥉'];
    const sorted = Object.entries(teamsData).sort((a,b) => b[1].impact - a[1].impact);

    document.getElementById('teams-grid').innerHTML = sorted.map(([name, s], i) => {{
        const color          = teamColor(name);
        const members        = Array.isArray(s.members)          ? s.members          : [];
        const prevMembers    = Array.isArray(s.previous_members) ? s.previous_members : [];
        const activeDays     = Math.floor((s.last - s.first) / 86400);
        const topDomains     = Object.entries(teamComponentData[name] || {{}}).slice(0, 5);
        const medalHtml      = i < 3 ? `<span class="text-2xl">${{medals[i]}}</span>` : '';

        function memberChip(m) {{
            const als = authorAliases[m] || [];
            const tip = als.length ? `${{m}}\nAliases: ${{als.join(', ')}}` : m;
            return `<span class="text-xs px-2 py-0.5 rounded bg-slate-100 text-slate-600 font-bold cursor-default"
                          title="${{tip}}">${{m.split(' ')[0]}}</span>`;
        }}
        function prevMemberChip(m) {{
            const als = authorAliases[m] || [];
            const tip = als.length ? `${{m}}\nAliases: ${{als.join(', ')}}` : m;
            return `<span class="text-xs px-2 py-0.5 rounded bg-slate-50 text-slate-400 font-bold cursor-default line-through"
                          title="${{tip}}">${{m.split(' ')[0]}}</span>`;
        }}

        return `<div class="card hover:shadow-md transition-shadow cursor-pointer"
                     onclick="filterByTeam(${{JSON.stringify(name)}})" title="Click to view contributors">
            <div class="flex justify-between items-start mb-3">
                <div>
                    <div class="flex items-center gap-2 mb-0.5">
                        ${{medalHtml}}
                        <h3 class="text-xl font-black tracking-tight" style="color:${{color}}">${{name}}</h3>
                    </div>
                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider">
                        ${{members.length}} Members · ${{activeDays}} Days Active
                    </p>
                </div>
                <div class="text-right">
                    <div class="text-3xl font-black text-slate-900">${{s.impact}}</div>
                    <div class="text-[10px] font-black text-slate-400 uppercase">Impact</div>
                </div>
            </div>

            ${{impactBar(s.impact, color)}}

            <div class="grid grid-cols-${{impactWMerges > 0 ? 4 : 3}} gap-2 my-4 text-center text-sm">
                <div class="bg-slate-50 rounded-xl p-3">
                    <div class="font-black text-slate-900">${{fmt(s.commits)}}</div>
                    <div class="text-[10px] font-bold text-slate-400 uppercase">Commits</div>
                </div>
                ${{impactWMerges > 0 ? `
                <div class="bg-slate-50 rounded-xl p-3">
                    <div class="font-black text-violet-500">${{fmt(s.merges || 0)}}</div>
                    <div class="text-[10px] font-bold text-slate-400 uppercase">Merges</div>
                </div>` : ''}}
                <div class="bg-slate-50 rounded-xl p-3">
                    <div class="font-black text-emerald-600">+${{fmt(s.add)}}</div>
                    <div class="text-[10px] font-bold text-slate-400 uppercase">Added</div>
                </div>
                <div class="bg-slate-50 rounded-xl p-3">
                    <div class="font-black text-red-400">-${{fmt(s.del)}}</div>
                    <div class="text-[10px] font-bold text-slate-400 uppercase">Removed</div>
                </div>
            </div>

            ${{topDomains.length ? `
            <div class="mb-4">
                <div class="text-[10px] font-black uppercase text-slate-400 mb-1.5">Primary Components</div>
                <div class="flex flex-wrap gap-1">
                    ${{topDomains.map(([d]) =>
                        `<span class="text-[10px] px-2 py-0.5 rounded font-black uppercase"
                               style="background:${{color}}22;color:${{color}}">${{compLabel(d)}}</span>`
                    ).join('')}}
                </div>
            </div>` : ''}}

            <div class="${{prevMembers.length ? 'mb-3' : ''}}">
                <div class="text-[10px] font-black uppercase text-slate-400 mb-1.5">Members</div>
                <div class="flex flex-wrap gap-1">
                    ${{members.map(memberChip).join('')}}
                </div>
            </div>

            ${{prevMembers.length ? ('<div>' +
                '<div class="text-[10px] font-black uppercase text-slate-400 mb-1.5">Previous Members</div>' +
                '<div class="flex flex-wrap gap-1">' +
                prevMembers.map(prevMemberChip).join('') +
                '</div></div>') : ''}}
        </div>`;
    }}).join('');
}}

// ─── Filter helpers ───────────────────────────────────────────────────────────
function filterByTeam(team) {{
    renderAuthorTable(team, 'team');
    navToAuthors();
}}

function initComponentChart() {{
    // Initialise one Chart.js bar chart per repo (main first, then support repos).
    repoCharts.forEach(repo => {{
        if (!repo.labels.length) return;
        const canvas = document.getElementById('componentChart-' + repo.id);
        if (!canvas) return;
        const chartHeight = Math.max(300, repo.labels.length * 36 + 80);
        const wrapper = document.getElementById('componentChartWrapper-' + repo.id);
        if (wrapper) wrapper.style.height = chartHeight + 'px';
        new Chart(canvas, {{
            type: 'bar',
            data: {{
                labels: repo.labels.map(compLabel),
                datasets: [{{ label:'Churn', data:repo.values, backgroundColor:'#3b82f6', borderRadius:8, barThickness:16 }}],
            }},
            options: {{
                responsive: true, maintainAspectRatio: false, indexAxis: 'y',
                plugins: {{ legend: {{ display: false }} }},
                onClick: (e, elements) => {{
                    if (!elements.length) return;
                    // Use the raw label key (which carries the "reponame:" prefix
                    // for support repos) so componentData lookup is correct.
                    const component = repo.labels[elements[0].index];
                    renderAuthorTable(component, 'component');
                    navToAuthors();
                }},
            }},
        }});
    }});
}}

// ─── Charts ───────────────────────────────────────────────────────────────────
const commonOpts = {{ responsive:true, maintainAspectRatio:false, plugins:{{ legend:{{ display:false }} }} }};

const monthlyKeys       = {monthly_keys_json};
const monthlyTopAuthors = {monthly_top_authors_json};
new Chart(document.getElementById('monthlyChart'), {{
    type: 'line',
    data: {{
        labels: {monthly_labels_json},
        datasets: [{{
            data: {monthly_counts_json},
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59,130,246,0.07)',
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 4,
            borderWidth: 2,
        }}],
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{
            legend: {{ display: false }},
            tooltip: {{
                callbacks: {{
                    afterBody: (items) => {{
                        const key  = monthlyKeys[items[0].dataIndex];
                        const top3 = (monthlyTopAuthors[key] || []);
                        if (!top3.length) return [];
                        return ['', 'Top authors:',
                            ...top3.map((a, i) => `  ${{i+1}}. ${{a.name}} (${{a.commits}})`),
                        ];
                    }},
                }},
            }},
        }},
        scales: {{
            x: {{
                ticks: {{ maxTicksLimit: 12, maxRotation: 0, font: {{ size: 11 }} }},
                grid:  {{ display: false }},
            }},
            y: {{
                beginAtZero: true,
                ticks: {{ precision: 0, font: {{ size: 11 }} }},
                grid:  {{ color: 'rgba(0,0,0,0.04)' }},
            }},
        }},
    }},
}});

new Chart(document.getElementById('hourChart'), {{
    type: 'bar',
    data: {{
        labels: Array.from({{length:24}},(_,i)=>i+':00'),
        datasets: [{{ data: {hour_data}, backgroundColor: '#1e3a8a', borderRadius: 4 }}],
    }},
    options: commonOpts,
}});

// ─── Init ─────────────────────────────────────────────────────────────────────
// Summary tab is visible by default — monthlyChart and hourChart are initialized
// above at load time.
_chartInited['summary'] = true;
renderAuthorTable();
renderImpactLeaderboard();
renderTeamsGrid();
// Hide the Team Rankings card on the Impact tab when no teams are configured.
if (!hasTeams) {{
    const card = document.getElementById('impact-teams-card');
    if (card) card.classList.add('hidden');
}}
</script>

<footer class="text-center py-8 mt-4">
    <span class="text-sm font-black tracking-tighter text-slate-300 uppercase italic select-none">
        Git<span class="text-blue-300">Stats</span>
    </span>
</footer>
</body>
</html>"""
        with open(output, 'w') as f:
            f.write(html)

        # Copy externals JS files (tailwind.js, chart.js) next to the output HTML for portable use
        out_dir = os.path.dirname(os.path.abspath(output))
        for fname in ('tailwind.js', 'chart.js'):
            src = os.path.join(external_dir, fname)
            dst = os.path.join(out_dir, fname)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)

        print(f"Report generated: {os.path.abspath(output)}")


def main() -> int:
    """CLI entry point. Parse arguments, run analysis, write report. Returns exit code."""
    parser = argparse.ArgumentParser(
        description="Generate a self-contained HTML statistics report for a Git repository."
    )
    parser.add_argument(
        "-s", "--source", required=True,
        help="Path to the source Git repository"
    )
    parser.add_argument(
        "-c", "--config", default=None,
        help="Path to a config JSON file (default: all settings use built-in defaults)"
    )
    parser.add_argument(
        "-o", "--output", required=True, default="/tmp/index.html",
        help="Output HTML file path (default: /tmp/index.html)"
    )
    parser.add_argument(
        '-externals', '--externals', default=os.path.join(os.getcwd(), 'externals'),
        help="Path to 'externals' directory containing our CSS and Javascript dependencies (default ./externals)"
    )
    parser.add_argument(
        '-support', '--support', action='append', default=[],
        metavar='REPO_PATH',
        help=("Path to an additional git repository whose commit history contributes "
              "to the combined author/team/activity statistics. May be specified "
              "multiple times for multiple support repositories.")
    )
    args = parser.parse_args()

    for repo_path in [args.source, *args.support]:
        if not os.path.isdir(repo_path):
            print(f"Provided repository directory does not exist at {repo_path}")
            return 1

        if not os.path.isdir(os.path.join(repo_path, ".git")):
            print(f"Provided repository directory does not look like it contains a git (.git) repository: {repo_path}")
            return 1

    if not os.path.isdir(args.externals):
        print(f"Provided externals dependencies directory does not exist at {args.externals}")
        return 1

    config_path = args.config
    if config_path:
        if os.path.exists(config_path):
            print(f"Using config: {config_path}")
        else:
            print(f"Warning: config file not found at {config_path!r} — running with defaults.")
            config_path = None

    stats = GitStats(args.source, config_path, support_paths=args.support)
    stats.collect()
    stats.generate_report(args.externals, args.output)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(e)
        sys.exit(1)

