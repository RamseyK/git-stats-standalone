# /// script
# requires-python = ">=3.11"
# dependencies = [
# ]
# ///

import os
import shutil
import subprocess
import sys
import json
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

    Config file (./config.json or custom path) format:
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

    # ── Impact score weights (must sum to 100) ──────────────────────────────────
    # Each metric is normalized to the max value across all authors/teams (0–1),
    # then multiplied by its weight, yielding a final score in the range 0–100.
    IMPACT_W_COMMITS = 40   # commit count
    IMPACT_W_LINES   = 40   # total lines changed (additions + deletions)
    IMPACT_W_TENURE  = 20   # active tenure in days (first commit → last commit)

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
    COMPONENT_MARKERS = {'make.py', 'pyproject.toml', 'setup.py', 'Makefile', 'meta.yaml'}

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
        self.team_colors['Community'] = '#94a3b8'  # slate — always last / fallback

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

        # ── Summary tab configuration ─────────────────────────────────────────
        # Day windows for commit velocity cards.  Each entry produces one card
        # comparing commits in the last N days vs the prior N days.  The default
        # [30, 90] shows a 30-day and a 90-day view side by side.  Uses the
        # combined heatmap (main repo + all support repos).
        self.summary_velocity_days = list(config.get('summary_velocity_days', [30, 90]))

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

        # Central data store populated by collect() and consumed by generate_report().
        self.data = {
            'project_name': os.path.basename(os.path.abspath(repo_path)),
            'analysis_date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            'general': {'total_commits': 0, 'total_files': 0, 'total_lines': 0,
                        'total_repo_lines': 0, 'age_days': 0},
            'activity': {'hour': Counter(), 'weekday': Counter(), 'heatmap': Counter()},
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

    def _get_team(self, author, email, ts=0):
        """Return the team name for a given canonical author name, email, and commit timestamp.

        Checks the email (lowercased) first, then the author display name. For each
        key, iterates through its (team, from_ts, to_ts) ranges and returns the
        first team whose range contains ts. Falls back to 'Community' when no range
        matches — including when an author has left all configured teams.

        ts defaults to 0 so callers that don't have a timestamp (e.g. tests) get a
        sensible result for unbounded entries.
        """
        for key in (email.lower(), author):
            for team, from_ts, to_ts in self.author_to_team_ranges.get(key, []):
                if from_ts <= ts <= to_ts:
                    return team
        return 'Community'

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

                # Initialize author record on first encounter.
                # commit_lines stores (ts, adds, dels, team) per commit —
                # used by _compute_impact() to filter noise before scoring.
                if author not in self.data['authors']:
                    self.data['authors'][author] = {
                        'commits': 0, 'add': 0, 'del': 0,
                        'first': ts, 'last': ts, 'team': team,
                        'commit_lines': [],
                    }
                au = self.data['authors'][author]
                au['commits'] += 1
                au['last'] = max(au['last'], ts)
                au['first'] = min(au['first'], ts)

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
        #   c) count current lines of code (binary newline count — fast, works for
        #      all encodings; binary files contribute negligibly to the total)
        ls_files = self._run_git(['ls-files']).splitlines()
        self.data['general']['total_files'] = len(ls_files)
        component_dirs_set = set()
        _repo_lines = 0
        for f in ls_files:
            ext = os.path.splitext(f)[1].lower() or 'source'
            self.data['files'][ext] += 1
            if os.path.basename(f) in self.component_markers:
                component_dirs_set.add(os.path.dirname(f))
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
            self.data['general']['age_days']         = (max(main_all_ts) - min(main_all_ts)) // 86400
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
                # Include %at (Unix timestamp) so time-ranged team membership is
                # resolved correctly for each commit within this release range.
                tag_log = self._run_git(['log', tag_range, '--pretty=format:%at|%an|%ae'])
            except subprocess.CalledProcessError:
                continue
            author_counts = Counter()
            team_counts = Counter()
            for tag_log_line in tag_log.splitlines():
                if '|' in tag_log_line:
                    ts_str, n, e = tag_log_line.split('|', 2)
                    commit_ts = int(ts_str) if ts_str.isdigit() else 0
                    canon = self._get_author(n, e)
                    author_counts[canon] += 1
                    team_counts[self._get_team(canon, e, commit_ts)] += 1
            if author_counts:
                self.data['tags'].append({
                    'name': tag,
                    'count': sum(author_counts.values()),
                    'top_authors': author_counts.most_common(20),
                    'top_teams': team_counts.most_common(10),
                })

        self._compute_impact()
        print(f"   → {self.data['general']['total_commits']:,} commits · "
              f"{len(self.data['authors'])} authors · "
              f"{len(self.data['teams'])} teams")

    # ------------------------------------------------------------------ impact

    def _compute_impact(self):
        """Compute and store impact scores for every author and team.

        Score formula (produces a value in the range 0–100):
            score = (commits / max_commits)         * IMPACT_W_COMMITS
                  + (effective_lines / max_eff)     * IMPACT_W_LINES
                  + (tenure_days / max_tenure)      * IMPACT_W_TENURE

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

            window_secs = wash_days * 86400
            # Each bucket accumulates per-commit effective lines AND the raw
            # adds/dels needed to detect the wash condition.
            buckets = defaultdict(lambda: {'eff': 0, 'raw_a': 0, 'raw_d': 0})
            for (ts, raw_a, raw_d, _team), e in zip(commits, eff):
                b = ts // window_secs
                buckets[b]['eff']   += e
                buckets[b]['raw_a'] += raw_a
                buckets[b]['raw_d'] += raw_d

            total = 0
            for b in buckets.values():
                gross = b['raw_a'] + b['raw_d']
                net   = abs(b['raw_a'] - b['raw_d'])
                # Wash condition: bucket is large AND adds/dels are roughly balanced.
                # min/gross > 0.4 → min/(min+max) > 0.4 → min/max > 2/3 ≈ 67%,
                # meaning the smaller side is at least 67% of the larger side,
                # indicating substantial cancellation (e.g. delete 1000 + add 900).
                # Apply cap_val to the net for consistency with the non-wash path,
                # which uses per-commit effective values already capped at cap_val.
                if gross > wash_min and gross > 0 and min(b['raw_a'], b['raw_d']) / gross > 0.4:
                    total += min(net, cap_val)    # discard the cancelled-out churn
                else:
                    total += b['eff']
            return total

        # ── Score authors ─────────────────────────────────────────────────────
        eff_map = {}   # author name → effective lines (used for team rollup too)
        if authors:
            eff_map = {name: effective_lines(a) for name, a in authors.items()}
            max_c = max(a['commits'] for a in authors.values()) or 1
            max_k = max(eff_map.values()) or 1
            max_d = max((a['last'] - a['first']) // 86400 for a in authors.values()) or 1
            for name, a in authors.items():
                days = (a['last'] - a['first']) // 86400
                a['impact'] = round(
                    (a['commits'] / max_c) * wc +
                    (eff_map[name]  / max_k) * wl +
                    (days / max_d)           * wt, 1
                )

        # ── Score teams ───────────────────────────────────────────────────────
        # Team effective lines are computed per-commit using the team recorded in
        # each commit_lines entry. This correctly handles authors who switched teams:
        # their commits are credited to whichever team they belonged to at the time
        # of each commit, not to their current team.
        teams = self.data['teams']
        if teams:
            team_eff = defaultdict(float)
            for a in authors.values():
                for (_, _ra, _rd, team), e in zip(a.get('commit_lines', []), a.get('_eff', [])):
                    team_eff[team] += e

            max_c = max(t['commits'] for t in teams.values()) or 1
            max_k = max(team_eff.values()) or 1
            max_d = max((t['last'] - t['first']) // 86400 for t in teams.values()) or 1
            for tname, t in teams.items():
                days = (t['last'] - t['first']) // 86400
                t['impact'] = round(
                    (t['commits']        / max_c) * wc +
                    (team_eff[tname]     / max_k) * wl +
                    (days                / max_d) * wt, 1
                )

        # Clean up internal scratch fields — they must not appear in JSON output.
        for a in authors.values():
            a.pop('commit_lines', None)
            a.pop('_eff', None)

    # ------------------------------------------------------------------ HTML helpers

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
        for t in self.data['tags']:
            author_items = ''.join([
                f'<div class="flex justify-between items-center text-sm bg-slate-50 p-2 rounded-xl '
                f'border border-transparent hover:border-slate-200 transition-all">'
                f'<span class="truncate font-bold text-slate-600">{i + 1}. {auth}</span>'
                f'<b class="text-blue-600 ml-2 shrink-0">{count}</b></div>'
                for i, (auth, count) in enumerate(t['top_authors'])
            ])
            # Only render team badges when teams are explicitly configured.
            # With no teams, every commit maps to "Community", which adds no signal.
            team_badges = ''.join([
                f'<span class="inline-flex items-center gap-1.5 text-[10px] px-2.5 py-1 rounded-lg font-black uppercase" '
                f'style="background:{self.team_colors.get(team, "#94a3b8")}18;'
                f'color:{self.team_colors.get(team, "#94a3b8")}">'
                f'{team}'
                f'<span class="opacity-60">·</span>'
                f'<span>{count} commits</span>'
                f'<span class="opacity-60">·</span>'
                f'<span class="bg-white/60 rounded px-1 py-px">⚡ {self.data["teams"].get(team, {}).get("impact", 0)}</span>'
                f'</span>'
                for team, count in t['top_teams']
            ]) if self.has_teams else ''
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

    # ------------------------------------------------------------------ report

    def generate_report(self, external_dir, output="index.html"):
        """Render all collected data into a self-contained HTML file.

        Serializes self.data to JSON and injects it directly into the HTML
        as JavaScript constants, so the output file has zero external data
        dependencies. externals/tailwind.js and externals/chart.js are copied
        next to the output file so the report works fully offline.
        """
        # Convert member sets to sorted lists so they are JSON-serializable
        for t in self.data['teams'].values():
            t['members'] = sorted(list(t['members']))

        # ── JSON blobs injected into the <script> block ───────────────────────
        authors_json    = json.dumps(self.data['authors'])
        teams_json      = json.dumps(self.data['teams'])
        team_colors_json = json.dumps(self.team_colors)
        # team_components: top 8 components per team — used by the Teams tab cards.
        # Drawn from the main repo only; support repo component data appears in
        # the separate per-repo cards on the Components tab.
        team_component_json = json.dumps({k: dict(v.most_common(8)) for k, v in self.data['team_components'].items()})
        hour_data           = json.dumps([self.data['activity']['hour'][i] for i in range(24)])

        # ── Per-repo component chart data ─────────────────────────────────────
        # repo_charts: one entry per repo (main first, then support repos).
        # Each entry has id, name, labels (component paths), and values (churn).
        # Support repo component paths are prefixed as "reponame:path" in the
        # labels list to avoid collision with main repo paths in componentData.
        repo_charts = []
        main_top = self.data['components'].most_common(30)
        repo_charts.append({
            'id': 'main',
            'name': self.data['project_name'],
            'labels': [c[0] for c in main_top],
            'values': [c[1] for c in main_top],
        })
        for i, sr in enumerate(self.data['support_repos']):
            sr_top = sorted(sr['components'].items(), key=lambda x: x[1], reverse=True)[:30]
            repo_charts.append({
                'id': f'support-{i}',
                'name': sr['name'],
                'labels': [f"{sr['name']}:{c[0]}" for c in sr_top],
                'values': [c[1] for c in sr_top],
            })
        repo_charts_json = json.dumps(repo_charts)

        # Unified component contributions for click-to-filter in the Authors tab.
        # Main repo paths are bare; support repo paths carry the "reponame:" prefix.
        all_comp_contrib = {k: dict(v) for k, v in self.data['component_contributions'].items()}
        for sr in self.data['support_repos']:
            prefix = sr['name']
            for path, authors in sr['component_contributions'].items():
                all_comp_contrib[f'{prefix}:{path}'] = dict(authors)
        component_json = json.dumps(all_comp_contrib)

        # ── Per-repo component chart cards HTML ───────────────────────────────
        # Main repo card first, then one card per support repo.
        has_support = bool(self.support_paths)
        component_cards = []
        for rc in repo_charts:
            cid = rc['id']
            is_main = cid == 'main'
            margin_cls = '' if is_main else ' mt-6'
            if has_support and is_main:
                tag_html = (' <span class="text-xs font-bold text-slate-400 '
                            'uppercase tracking-widest ml-2">Main Repository</span>')
            elif not is_main:
                tag_html = (' <span class="text-xs font-bold text-slate-400 '
                            'uppercase tracking-widest ml-2">Support Repository</span>')
            else:
                tag_html = ''
            component_cards.append(
                f'        <div class="card{margin_cls}" id="componentChartCard-{cid}">\n'
                f'            <div class="mb-6">\n'
                f'                <h3 class="text-xl font-black">'
                f'Component Churn \u2014 {rc["name"]}{tag_html}</h3>\n'
                f'                <p class="text-sm text-slate-400 font-medium mt-1">'
                f'Click any bar to filter contributors by component.</p>\n'
                f'            </div>\n'
                f'            <canvas id="componentChart-{cid}"></canvas>\n'
                f'        </div>'
            )
        component_cards_html = '\n'.join(component_cards)

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

        # Hide the Teams tab entirely when no teams are defined in config.
        teams_tab_hidden = ' hidden' if not self.has_teams else ''

        # Noise-reduction settings — displayed in the impact explanation section
        # so viewers know exactly what filtering was applied to this report.
        cfg_use_net      = 'on' if self.use_net_lines else 'off'
        cfg_wash_days    = self.wash_window_days
        cfg_wash_min     = self.wash_min_gross
        cfg_cap_pct      = self.line_cap_percentile
        cfg_wash_status  = f'{cfg_wash_days}-day window, ≥{cfg_wash_min} gross lines' if cfg_wash_days else 'off'
        cfg_cap_status   = f'{cfg_cap_pct}th percentile' if cfg_cap_pct else 'off'

        # ── Summary tab computed values ───────────────────────────────────────

        # First commit date for display
        _first_ts = self.data['general'].get('first_commit_ts', 0)
        first_commit_date = (
            datetime.datetime.fromtimestamp(_first_ts).strftime('%b %d, %Y')
            if _first_ts else 'N/A'
        )

        # Average weekly commit cadence over the lifetime of the repo
        _age_weeks = age_days / 7
        avg_weekly = round(tcom / _age_weeks, 1) if _age_weeks > 0 else 0

        # Total release tags (before any display cap)
        total_tags_count  = self.data['general'].get('total_tags', len(self.data['tags']))
        shown_tags_count  = len(self.data['tags'])
        tags_note_html    = (
            f'<div class="text-xs text-slate-400 mt-1">Showing {shown_tags_count} of {total_tags_count}</div>'
            if shown_tags_count < total_tags_count else
            '<div class="text-xs text-slate-400 mt-1">&nbsp;</div>'
        )

        # Support repo pills for summary card header
        if self.support_paths:
            _pills = ' '.join(
                f'<span class="text-[10px] px-2 py-0.5 rounded font-black uppercase '
                f'bg-slate-100 text-slate-500">'
                f'+ {os.path.basename(os.path.abspath(p))}</span>'
                for p in self.support_paths
            )
            support_repos_html = f'<div class="flex flex-wrap gap-1 mt-1.5">{_pills}</div>'
        else:
            support_repos_html = ''

        # Commit velocity — last N days vs prior N days, from the combined heatmap.
        _heatmap   = self.data['activity']['heatmap']
        _today_dt  = datetime.date.today()
        _today_str = _today_dt.isoformat()
        velocity_windows = []
        for _days in self.summary_velocity_days:
            _cur_start  = (_today_dt - datetime.timedelta(days=_days)).isoformat()
            _prev_start = (_today_dt - datetime.timedelta(days=_days * 2)).isoformat()
            _current = sum(v for k, v in _heatmap.items() if _cur_start <= k <= _today_str)
            _prior   = sum(v for k, v in _heatmap.items() if _prev_start <= k < _cur_start)
            _delta   = round((_current - _prior) / _prior * 100, 1) if _prior else None
            velocity_windows.append({'days': _days, 'current': _current, 'prior': _prior, 'delta': _delta})

        _ncols    = min(len(velocity_windows), 4)
        _vcols_cls = f'md:grid-cols-{_ncols}' if _ncols > 1 else ''
        _vcards   = []
        for _vw in velocity_windows:
            _d, _cur, _pri, _dlt = _vw['days'], _vw['current'], _vw['prior'], _vw['delta']
            if _dlt is None:
                _trend = '<span class="text-slate-400 text-sm font-medium">— no prior data</span>'
            elif _dlt > 0:
                _trend = (f'<span class="text-emerald-600 text-sm font-bold">'
                          f'&#8593; +{_dlt}% vs prior {_d}d</span>')
            elif _dlt < 0:
                _trend = (f'<span class="text-red-500 text-sm font-bold">'
                          f'&#8595; {_dlt}% vs prior {_d}d</span>')
            else:
                _trend = (f'<span class="text-slate-500 text-sm font-bold">'
                          f'&rarr; 0% vs prior {_d}d</span>')
            _all_repos = ' (all repos)' if self.support_paths else ''
            _vcards.append(
                f'        <div class="card">\n'
                f'            <div class="text-[10px] uppercase font-bold text-slate-400 mb-2 tracking-widest">Last {_d} Days</div>\n'
                f'            <div class="text-4xl font-black text-slate-900 mb-1">{_cur:,}</div>\n'
                f'            <div class="text-sm text-slate-500 mb-3">commits{_all_repos}</div>\n'
                f'            {_trend}\n'
                f'            <div class="text-xs text-slate-400 mt-2">Prior {_d}d: {_pri:,}</div>\n'
                f'        </div>'
            )
        velocity_cards_html = (
            f'    <div class="grid grid-cols-1 {_vcols_cls} gap-6">\n'
            + '\n'.join(_vcards)
            + '\n    </div>'
        )

        # Bus factor — fewest contributors whose combined commits reach the threshold.
        _sorted_authors = sorted(
            self.data['authors'].items(), key=lambda x: x[1]['commits'], reverse=True
        )
        _total_for_bf = sum(a['commits'] for _, a in _sorted_authors) or 1
        _threshold    = _total_for_bf * self.bus_factor_threshold
        _running      = 0
        _bf_authors   = []
        for _aname, _adata in _sorted_authors:
            _running += _adata['commits']
            _bf_authors.append({
                'name':    _aname,
                'commits': _adata['commits'],
                'pct':     round(_adata['commits'] / _total_for_bf * 100, 1),
                'team':    _adata.get('team', 'Community'),
            })
            if _running >= _threshold:
                break
        _bus_count = len(_bf_authors)
        _bus_pct   = int(self.bus_factor_threshold * 100)

        _bf_rows = []
        for _ba in _bf_authors:
            _c = self.team_colors.get(_ba['team'], '#94a3b8') if self.has_teams else '#3b82f6'
            _bf_rows.append(
                f'                <div class="flex items-center gap-3 py-2">\n'
                f'                    <span class="text-sm font-bold text-slate-700 w-36 truncate shrink-0">{_ba["name"]}</span>\n'
                f'                    <div class="flex-1 h-2 bg-slate-100 rounded-full overflow-hidden">\n'
                f'                        <div class="h-full rounded-full" style="width:{min(_ba["pct"], 100)}%;background:{_c}"></div>\n'
                f'                    </div>\n'
                f'                    <span class="text-sm font-mono font-bold text-slate-500 w-10 text-right shrink-0">{_ba["pct"]}%</span>\n'
                f'                    <span class="text-xs text-slate-400 w-24 text-right shrink-0">{_ba["commits"]:,} commits</span>\n'
                f'                </div>'
            )
        _bf_rows_html = '\n'.join(_bf_rows)
        _plural_s    = 's' if _bus_count != 1 else ''
        _plural_verb = 'account' if _bus_count != 1 else 'accounts'
        bus_factor_card_html = (
            f'    <div class="card">\n'
            f'        <div class="flex items-start gap-8 flex-wrap">\n'
            f'            <div class="shrink-0 text-center min-w-[5rem]">\n'
            f'                <div class="text-5xl font-black text-slate-900">{_bus_count}</div>\n'
            f'                <div class="text-[10px] uppercase font-bold text-slate-400 mt-1 tracking-widest">Bus Factor</div>\n'
            f'            </div>\n'
            f'            <div class="flex-1 min-w-0">\n'
            f'                <p class="text-sm text-slate-600 font-medium mb-4">\n'
            f'                    <strong>{_bus_count} contributor{_plural_s}</strong> {_plural_verb} for\n'
            f'                    {_bus_pct}% of all commits.\n'
            f'                </p>\n'
            f'{_bf_rows_html}\n'
            f'            </div>\n'
            f'        </div>\n'
            f'        <p class="text-xs text-slate-400 mt-4 border-t border-slate-100 pt-3">\n'
            f'            Threshold: {_bus_pct}% \u00b7 configurable via\n'
            f'            <span class="font-mono">bus_factor_threshold</span> in config.json\n'
            f'        </p>\n'
            f'    </div>'
        )

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

    <!-- ═══ SUMMARY ════════════════════════════════════════════════════════════ -->
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
                    {tags_note_html}
                </div>
            </div>
        </div>

{velocity_cards_html}

{bus_factor_card_html}

        <!-- Hourly Punchcard -->
        <div class="card" style="height:400px">
            <h3 class="font-bold mb-4">Hourly Punchcard</h3>
            <canvas id="hourChart"></canvas>
        </div>

    </div>

    <!-- ═══ IMPACT ═══════════════════════════════════════════════════════════ -->
    <div id="tab-impact" class="tab-content hidden space-y-8">
        <div class="{'grid grid-cols-1 lg:grid-cols-2' if self.has_teams else 'grid grid-cols-1'} gap-8">
            <div class="card">
                <div class="flex items-start justify-between mb-0.5">
                    <h3 class="text-xl font-black text-slate-900" id="impact-authors-title">Top Contributors</h3>
                    <button id="impact-clear-btn" onclick="clearImpactFilter()"
                            class="hidden text-[10px] font-black px-2.5 py-1 rounded-lg bg-slate-100 text-slate-500 hover:bg-slate-200 transition-all uppercase tracking-wide">
                        Clear Filter
                    </button>
                </div>
                <p class="text-xs text-slate-400 font-medium mb-6">Impact = commits&nbsp;{iw_commits}%&nbsp;·&nbsp;lines&nbsp;{iw_lines}%&nbsp;·&nbsp;tenure&nbsp;{iw_tenure}%</p>
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
            <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">
                <div class="bg-slate-50 rounded-2xl p-5">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-sm font-black text-slate-700">Commit Volume</span>
                        <span class="text-2xl font-black text-blue-600">{iw_commits}%</span>
                    </div>
                    <div class="impact-bar mb-3"><div class="impact-fill" style="width:{iw_commits}%"></div></div>
                    <p class="text-xs text-slate-500">Total number of commits authored. Rewards consistent contribution frequency over time.</p>
                    <p class="text-[11px] text-slate-400 mt-2 font-mono">score += (commits / max_commits) × {iw_commits}</p>
                </div>
                <div class="bg-slate-50 rounded-2xl p-5">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-sm font-black text-slate-700">Lines Changed</span>
                        <span class="text-2xl font-black text-blue-600">{iw_lines}%</span>
                    </div>
                    <div class="impact-bar mb-3"><div class="impact-fill" style="width:{iw_lines}%"></div></div>
                    <p class="text-xs text-slate-500">Effective lines changed, filtered to remove noise. Reformats, mass moves, and revert pairs are discounted before scoring.</p>
                    <p class="text-[11px] text-slate-400 mt-2 font-mono">score += (effective_lines / max_lines) × {iw_lines}</p>
                </div>
                <div class="bg-slate-50 rounded-2xl p-5">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-sm font-black text-slate-700">Active Tenure</span>
                        <span class="text-2xl font-black text-blue-600">{iw_tenure}%</span>
                    </div>
                    <div class="impact-bar mb-3"><div class="impact-fill" style="width:{iw_tenure}%"></div></div>
                    <p class="text-xs text-slate-500">Days between a contributor's first and last commit. Rewards long-term, sustained engagement.</p>
                    <p class="text-[11px] text-slate-400 mt-2 font-mono">score += (tenure_days / max_tenure) × {iw_tenure}</p>
                </div>
            </div>
            <div class="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-800 mb-4">
                <span class="font-black">Formula: </span>
                Impact = (commits / max_commits) × {iw_commits} &nbsp;+&nbsp; (effective_lines / max_lines) × {iw_lines} &nbsp;+&nbsp; (tenure_days / max_tenure) × {iw_tenure}
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
        {self._render_tags_html()}
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
const teamColors     = {team_colors_json};
const hasTeams       = {json.dumps(self.has_teams)};
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
        return `<tr class="hover:bg-slate-50 transition-colors">
            <td class="px-8 py-4">
                <div class="font-bold text-slate-800">${{name}}</div>
                ${{teamBadge(s.team)}}
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
            <td>
                <div class="flex items-center gap-2" style="min-width:140px">
                    <span class="text-xs font-black text-slate-700 w-8">${{impact}}</span>
                    <div class="impact-bar flex-1"><div class="impact-fill" style="width:${{impact}}%"></div></div>
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
        const color      = teamColor(name);
        const members    = Array.isArray(s.members) ? s.members : [];
        const activeDays = Math.floor((s.last - s.first) / 86400);
        const topDomains = Object.entries(teamComponentData[name] || {{}}).slice(0, 5);
        const medalHtml  = i < 3 ? `<span class="text-2xl">${{medals[i]}}</span>` : '';

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

            <div class="grid grid-cols-3 gap-2 my-4 text-center text-sm">
                <div class="bg-slate-50 rounded-xl p-3">
                    <div class="font-black text-slate-900">${{fmt(s.commits)}}</div>
                    <div class="text-[10px] font-bold text-slate-400 uppercase">Commits</div>
                </div>
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

            <div>
                <div class="text-[10px] font-black uppercase text-slate-400 mb-1.5">Members</div>
                <div class="flex flex-wrap gap-1">
                    ${{members.map(m =>
                        `<span class="text-xs px-2 py-0.5 rounded bg-slate-100 text-slate-600 font-bold">${{m.split(' ')[0]}}</span>`
                    ).join('')}}
                </div>
            </div>
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
        canvas.style.height = chartHeight + 'px';
        const card = document.getElementById('componentChartCard-' + repo.id);
        if (card) card.style.height = (chartHeight + 120) + 'px';
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

new Chart(document.getElementById('hourChart'), {{
    type: 'bar',
    data: {{
        labels: Array.from({{length:24}},(_,i)=>i+':00'),
        datasets: [{{ data: {hour_data}, backgroundColor: '#1e3a8a', borderRadius: 4 }}],
    }},
    options: commonOpts,
}});

// ─── Init ─────────────────────────────────────────────────────────────────────
// Summary tab is visible by default — hourChart is initialized above at load time
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
        "-c", "--config", default=os.path.join(os.getcwd(), "config.json"),
        help="Path to teams/aliases config JSON (default: ./config.json)"
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

    if not os.path.isdir(args.source):
        print(f"Provided source Git repository directory does not exist at {args.source}")
        return 1

    if not os.path.isdir(args.externals):
        print(f"Provided externals dependencies directory does not exist at {args.externals}")
        return 1

    for sp in args.support:
        if not os.path.isdir(sp):
            print(f"Provided support repository directory does not exist at {sp}")
            return 1

    config_path = args.config
    if config_path and os.path.exists(config_path):
        print(f"Using config: {config_path}")
    else:
        print("No config file found — all authors will appear as 'Community'.")
        print('Create config.json with format: {"teams": {"Team": {"color": "#3b82f6", "members": ["name", "email"]}}, "aliases": {}}')

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

