import os
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
      - Activity heatmap, LOC history, hourly punchcard
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
          "members": ["author name", "author@email.com", ...]
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

    def __init__(self, repo_path, config_file=None):
        self.repo_path = repo_path
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
        # Config format: {"Team Name": {"color": "#hex", "members": [name/email, ...]}}
        # If a team omits "color", it is assigned the next color in TEAM_COLORS.
        # Email members are stored lowercased for case-insensitive matching.
        # Authors not listed in any team fall back to the built-in "Community" team.
        teams_config = config.get('teams', {})
        self.author_to_team = {}   # name/email → team name
        self.team_colors = {}      # team name  → hex color string
        for i, (team, value) in enumerate(teams_config.items()):
            self.team_colors[team] = value.get('color') or TEAM_COLORS[i % len(TEAM_COLORS)]
            for m in value.get('members', []):
                key = m.lower() if '@' in m else m
                self.author_to_team[key] = team
        self.team_colors['Community'] = '#94a3b8'  # slate — always last / fallback

        # Only git tags whose name begins with this prefix are shown in the
        # Releases tab. An empty string (the default) means all tags are included.
        self.release_tag_prefix = config.get('release_tag_prefix', '')

        # Cap on how many tags to show in the Releases tab.
        # Set to 0 in config to display all matching tags with no limit.
        self.max_release_tags = int(config.get('max_release_tags', 20))

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
            'general': {'total_commits': 0, 'total_files': 0, 'total_lines': 0, 'age_days': 0},
            'activity': {'hour': Counter(), 'weekday': Counter(), 'heatmap': Counter()},
            'authors': {},   # canonical name → {commits, add, del, first, last, team, impact}
            'teams': {},     # team name      → {commits, add, del, members, first, last, impact}
            'component_contributions': defaultdict(lambda: Counter()),  # component path → {author: commit_count}
            'team_components': defaultdict(lambda: Counter()),           # team name      → {component path: churn lines}
            'files': Counter(),      # file extension → count
            'components': Counter(), # component path → total churn lines
            'tags': [],              # list of release dicts built in step 3 of collect()
            'loc_history': [],       # running net LOC per file-change event, oldest-first after reversal
        }

    # ------------------------------------------------------------------ helpers

    def _load_config(self, path):
        """Return parsed JSON config, or an empty dict if the file is absent or unspecified."""
        if not path or not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    def _get_component(self, path):
        """Map a repo-relative file path to its component directory.

        A component is any directory that directly contains make.py,
        pyproject.toml, or setup.py (discovered during collect()).
        self._component_dirs is pre-sorted longest-first so the most
        specific (deepest) ancestor wins when directories are nested.

        Returns the component directory string, '(root)' for top-level
        marker files, or None if the file is not inside any component.
        """
        for comp in self._component_dirs:
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

    def _get_team(self, author, email):
        """Return the team name for a given canonical author name and email.

        Lookup order: email (lowercased) → author name → 'Community' fallback.
        """
        return self.author_to_team.get(email.lower(), self.author_to_team.get(author, 'Community'))

    def _run_git(self, args):
        """Run a git subcommand inside self.repo_path and return its stdout as a string.

        stderr is suppressed. Raises subprocess.CalledProcessError on non-zero exit.
        """
        return subprocess.check_output(
            ['git', '-C', self.repo_path] + args,
            stderr=subprocess.DEVNULL
        ).decode('utf-8', 'ignore')

    # ------------------------------------------------------------------ collect

    def collect(self):
        """Populate self.data by running git commands against the repository.

        Three phases:
          1. File inventory + component discovery (git ls-files)
          2. Full commit history with per-file line stats (git log --numstat)
          3. Release tag breakdown (git tag + git log per tag range)

        Must be called before generate_report().
        """
        print(f"Analyzing {self.data['project_name']}...")

        # ── Phase 1: file inventory + component discovery ─────────────────────
        # git ls-files lists every tracked file. We use it to:
        #   a) count files by extension for the general stats
        #   b) find component roots (dirs that contain a marker file)
        ls_files = self._run_git(['ls-files']).splitlines()
        self.data['general']['total_files'] = len(ls_files)
        component_markers = {'make.py', 'pyproject.toml', 'setup.py'}
        component_dirs = set()
        for f in ls_files:
            ext = os.path.splitext(f)[1].lower() or 'source'
            self.data['files'][ext] += 1
            if os.path.basename(f) in component_markers:
                # The directory containing the marker file is the component root.
                component_dirs.add(os.path.dirname(f))
        # Longest paths first so _get_component() matches the deepest ancestor.
        self._component_dirs = sorted(component_dirs, key=len, reverse=True)

        # ── Phase 2: commit history ───────────────────────────────────────────
        # --numstat emits one "COMMIT|..." header per commit followed by
        # tab-separated (added, deleted, filepath) lines for each changed file.
        log_data = self._run_git(['log', '--numstat', '--pretty=format:COMMIT|%at|%an|%ae'])

        # State carried across lines within the same commit
        current_author = current_team = None
        running_loc = 0   # net lines added across all commits so far (used for LOC chart)
        all_ts = []       # all commit timestamps, used to compute repo age
        ts = 0

        # Per-commit line accumulators for the current commit being processed.
        # Flushed into commit_lines[] on each new COMMIT header (and after the loop).
        # _compute_impact() uses these lists to filter out meaningless mass changes.
        current_commit_ts = 0
        current_commit_adds = 0
        current_commit_dels = 0

        for line in log_data.splitlines():
            if line.startswith('COMMIT|'):
                # ── Commit header ─────────────────────────────────────────────
                # Before moving on, flush the previous commit's accumulated line
                # counts into that author's commit_lines list so _compute_impact()
                # can apply per-commit noise filtering later.
                if current_author is not None:
                    self.data['authors'][current_author]['commit_lines'].append(
                        (current_commit_ts, current_commit_adds, current_commit_dels))
                current_commit_adds = 0
                current_commit_dels = 0

                _, ts_str, name, email = line.split('|', 3)
                ts = int(ts_str)
                current_commit_ts = ts
                dt = datetime.datetime.fromtimestamp(ts)
                author = self._get_author(name, email)
                team = self._get_team(author, email)
                all_ts.append(ts)
                current_author = author
                current_team = team

                # Initialize author record on first encounter.
                # commit_lines stores (timestamp, adds, dels) per commit —
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

                    # Accumulate this file's lines into the current commit bucket
                    # so they can be flushed as a single (ts, adds, dels) entry.
                    current_commit_adds += a
                    current_commit_dels += d

                    # Track running net LOC for the LOC history chart.
                    # We append after every file change, then reverse the list
                    # at the end so it runs oldest → newest.
                    running_loc += (a - d)
                    self.data['loc_history'].append(running_loc)

                    # Attribute churn to the file's component (if any)
                    component = self._get_component(path)
                    if component is not None:
                        self.data['components'][component] += (a + d)
                        self.data['component_contributions'][component][current_author] += 1
                        self.data['team_components'][current_team][component] += (a + d)
                except (ValueError, IndexError):
                    continue

        # Flush the final commit's line stats (the loop only flushes on the *next*
        # COMMIT header, so the last commit in the log would otherwise be lost).
        if current_author is not None:
            self.data['authors'][current_author]['commit_lines'].append(
                (current_commit_ts, current_commit_adds, current_commit_dels))

        # Derive repo age from the span between oldest and newest commit
        if all_ts:
            self.data['general']['age_days'] = (max(all_ts) - min(all_ts)) // 86400
        self.data['general']['total_lines'] = running_loc

        # git log walks newest-first, so reverse to get chronological order
        self.data['loc_history'].reverse()

        # For large repos, decimating to ≤2000 points keeps the chart responsive
        # without meaningfully reducing visual fidelity.
        if len(self.data['loc_history']) > 2000:
            step = len(self.data['loc_history']) // 2000
            self.data['loc_history'] = self.data['loc_history'][::step]

        # ── Phase 3: release tag breakdown ───────────────────────────────────
        # Tags are sorted newest-first. For each tag we attribute the commits
        # between it and the previous tag (tag_range) to authors and teams.
        # The oldest tag uses just its name as the range (all commits up to it).
        try:
            tags = self._run_git(['tag', '--sort=-creatordate']).splitlines()
        except subprocess.CalledProcessError:
            tags = []

        if self.release_tag_prefix:
            tags = [t for t in tags if t.startswith(self.release_tag_prefix)]

        # 0 means no limit; any positive value caps the list
        if self.max_release_tags:
            tags = tags[:self.max_release_tags]

        for i, tag in enumerate(tags):
            # Range: commits between the next-older tag and this one.
            # For the oldest tag in the list, include all its commits.
            tag_range = tag if i == len(tags) - 1 else f"{tags[i + 1]}..{tag}"
            try:
                tag_log = self._run_git(['log', tag_range, '--pretty=format:%an|%ae'])
            except subprocess.CalledProcessError:
                continue
            author_counts = Counter()
            team_counts = Counter()
            for tag_log_line in tag_log.splitlines():
                if '|' in tag_log_line:
                    n, e = tag_log_line.split('|', 1)
                    canon = self._get_author(n, e)
                    author_counts[canon] += 1
                    team_counts[self._get_team(canon, e)] += 1
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
        for a in authors.values():
            a['_eff'] = [
                abs(adds - dels) if use_net else (adds + dels)
                for _, adds, dels in a.get('commit_lines', [])
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
            for (ts, raw_a, raw_d), e in zip(commits, eff):
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
        # Team effective lines = sum of member effective lines so that the same
        # noise filtering applied to authors flows through to the team score.
        teams = self.data['teams']
        if teams:
            team_eff = defaultdict(float)
            for name, a in authors.items():
                team_eff[a.get('team', 'Community')] += eff_map.get(name, 0)

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
            ])
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

    def generate_report(self, output="index.html"):
        """Render all collected data into a self-contained HTML file.

        Serializes self.data to JSON and injects it directly into the HTML
        as JavaScript constants, so the output file has zero external data
        dependencies (Chart.js and Tailwind are loaded from CDN).
        """
        # Convert member sets to sorted lists so they are JSON-serializable
        for t in self.data['teams'].values():
            t['members'] = sorted(list(t['members']))

        # ── JSON blobs injected into the <script> block ───────────────────────
        authors_json    = json.dumps(self.data['authors'])
        teams_json      = json.dumps(self.data['teams'])
        team_colors_json = json.dumps(self.team_colors)
        # component_contributions: {path: {author: commit_count}} — used by the
        # Components tab click-to-filter feature in the Authors tab.
        component_json      = json.dumps({k: dict(v) for k, v in self.data['component_contributions'].items()})
        # team_components: top 8 components per team — used by the Teams tab cards.
        team_component_json = json.dumps({k: dict(v.most_common(8)) for k, v in self.data['team_components'].items()})
        heatmap_json        = json.dumps(dict(self.data['activity']['heatmap']))
        loc_json            = json.dumps(self.data['loc_history'])
        hour_data           = json.dumps([self.data['activity']['hour'][i] for i in range(24)])

        sorted_components      = self.data['components'].most_common(30)
        component_labels       = json.dumps([c[0] for c in sorted_components])
        component_values       = json.dumps([c[1] for c in sorted_components])

        pname  = self.data['project_name']
        adate  = self.data['analysis_date']
        tcom   = self.data['general']['total_commits']
        tlines = self.data['general']['total_lines']
        nauth  = len(self.data['authors'])
        nteams = len(self.data['teams'])

        iw_commits = self.IMPACT_W_COMMITS
        iw_lines   = self.IMPACT_W_LINES
        iw_tenure  = self.IMPACT_W_TENURE

        # Noise-reduction settings — displayed in the impact explanation section
        # so viewers know exactly what filtering was applied to this report.
        cfg_use_net      = 'on' if self.use_net_lines else 'off'
        cfg_wash_days    = self.wash_window_days
        cfg_wash_min     = self.wash_min_gross
        cfg_cap_pct      = self.line_cap_percentile
        cfg_wash_status  = f'{cfg_wash_days}-day window, ≥{cfg_wash_min} gross lines' if cfg_wash_days else 'off'
        cfg_cap_status   = f'{cfg_cap_pct}th percentile' if cfg_cap_pct else 'off'

        html = f"""<!DOCTYPE html>
<html class="scroll-smooth">
<head>
    <meta charset="UTF-8">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <title>GitStats: {pname}</title>
    <style>
        .heatmap-cell {{ width:12px; height:12px; border-radius:2px; transition:transform 0.1s; cursor:help; }}
        .heatmap-cell:hover {{ transform:scale(1.3); }}
        .lvl-0 {{ background:#f1f5f9; }} .lvl-1 {{ background:#93c5fd; }}
        .lvl-2 {{ background:#3b82f6; }} .lvl-3 {{ background:#2563eb; }} .lvl-4 {{ background:#1e3a8a; }}
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
            <button onclick="showTab('activity',this)" class="tab-btn active">Activity</button>
            <button onclick="showTab('impact',this)"   class="tab-btn">Impact</button>
            <button onclick="showTab('authors',this)"  class="tab-btn">Authors</button>
            <button onclick="showTab('teams',this)"    class="tab-btn">Teams</button>
            <button onclick="showTab('tags',this)"     class="tab-btn">Releases</button>
            <button onclick="showTab('components',this)"  class="tab-btn">Components</button>
        </div>
    </nav>

    <!-- ═══ ACTIVITY ═════════════════════════════════════════════════════════ -->
    <div id="tab-activity" class="tab-content space-y-10">
        <div class="card">
            <h3 class="text-xs font-black uppercase text-slate-400 mb-6 tracking-widest">Contribution Heatmap</h3>
            <div class="overflow-x-auto pb-4">
                <div id="heatmap" class="flex flex-wrap gap-1.5" style="width:950px"></div>
            </div>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-10">
            <div class="card" style="height:400px"><h3 class="font-bold mb-4">LOC History</h3><canvas id="locChart"></canvas></div>
            <div class="card" style="height:400px"><h3 class="font-bold mb-4">Hourly Punchcard</h3><canvas id="hourChart"></canvas></div>
        </div>
    </div>

    <!-- ═══ IMPACT ═══════════════════════════════════════════════════════════ -->
    <div id="tab-impact" class="tab-content hidden space-y-8">
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
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
            <div class="card">
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
    <div id="tab-teams" class="tab-content hidden">
        <div id="teams-grid" class="grid grid-cols-1 md:grid-cols-2 gap-6"></div>
    </div>

    <!-- ═══ RELEASES ══════════════════════════════════════════════════════════ -->
    <div id="tab-tags" class="tab-content hidden space-y-8">
        {self._render_tags_html()}
    </div>

    <!-- ═══ COMPONENTS ════════════════════════════════════════════════════════ -->
    <div id="tab-components" class="tab-content hidden">
        <div class="card" id="componentChartCard">
            <div class="mb-6">
                <h3 class="text-xl font-black">Component Churn Analysis</h3>
                <p class="text-sm text-slate-400 font-medium mt-1">Click any bar to filter contributors by component.</p>
            </div>
            <canvas id="componentChart"></canvas>
        </div>
    </div>

</div><!-- /max-w-7xl -->

<!-- ═══════════════════════════════════════════════════════════════ JAVASCRIPT -->
<script>
const authorData     = {authors_json};
const teamsData      = {teams_json};
const teamColors     = {team_colors_json};
const componentData     = {component_json};
const teamComponentData = {team_component_json};
const totalCommits   = {tcom};

// Pre-compute how many component paths share each short (last-segment) name.
// Used by compLabel() to decide whether to show the short name or the full path.
const _compShortCount = {{}};
Object.keys(componentData).forEach(k => {{
    const s = k.includes('/') ? k.split('/').pop() : k;
    _compShortCount[s] = (_compShortCount[s] || 0) + 1;
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
// Uses only the last path segment unless that segment is ambiguous (shared by
// multiple components), in which case the full path is returned instead.
function compLabel(path) {{
    const short = path.includes('/') ? path.split('/').pop() : path;
    return _compShortCount[short] > 1 ? path : short;
}}

function impactBar(score, color) {{
    return `<div class="impact-bar"><div class="impact-fill" style="width:${{score}}%;background:${{color}}"></div></div>`;
}}

function teamBadge(team) {{
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
        title.innerText = `Component: ${{compLabel(currentFilter)}}`;
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
    const componentKeys = {component_labels};
    const canvas = document.getElementById('componentChart');
    const chartHeight = Math.max(300, componentKeys.length * 36 + 80);
    canvas.style.height = chartHeight + 'px';
    document.getElementById('componentChartCard').style.height = (chartHeight + 120) + 'px';
    const dChart = new Chart(canvas, {{
        type: 'bar',
        data: {{
            labels: componentKeys.map(compLabel),
            datasets: [{{ label:'Churn', data:{component_values}, backgroundColor:'#3b82f6', borderRadius:8, barThickness:16 }}],
        }},
        options: {{
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: {{ legend: {{ display: false }} }},
            onClick: (e, elements) => {{
                if (!elements.length) return;
                const component = componentKeys[elements[0].index];
                renderAuthorTable(component, 'component');
                navToAuthors();
            }},
        }},
    }});
}}

// ─── Heatmap ──────────────────────────────────────────────────────────────────
const hmd  = {heatmap_json};
const hmc  = document.getElementById('heatmap');
const today = new Date();
for (let i = 364; i >= 0; i--) {{
    const d  = new Date(); d.setDate(today.getDate() - i);
    const ds = d.toISOString().split('T')[0];
    const c  = hmd[ds] || 0;
    const cell = document.createElement('div');
    cell.className = `heatmap-cell lvl-${{c === 0 ? 0 : Math.min(4, Math.ceil(c / 5))}}`;
    cell.title = `${{ds}}: ${{c}} commits`;
    hmc.appendChild(cell);
}}

// ─── Charts ───────────────────────────────────────────────────────────────────
const commonOpts = {{ responsive:true, maintainAspectRatio:false, plugins:{{ legend:{{ display:false }} }} }};

const locData = {loc_json};
new Chart(document.getElementById('locChart'), {{
    type: 'line',
    data: {{
        labels: locData.map((_,i) => i),
        datasets: [{{
            label: 'LOC', data: locData,
            borderColor: '#3b82f6', pointRadius: 0, fill: true,
            backgroundColor: 'rgba(59,130,246,0.05)', tension: 0.1,
        }}],
    }},
    options: {{ ...commonOpts, scales: {{ x: {{ display:false }} }} }},
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
// Activity tab is visible by default — init its charts immediately
_chartInited['activity'] = true;
renderAuthorTable();
renderImpactLeaderboard();
renderTeamsGrid();
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
    args = parser.parse_args()

    if not os.path.isdir(args.source):
        print(f"Provided source Git repository directory does not exist at {args.source}")
        return 1

    config_path = args.config
    if config_path and os.path.exists(config_path):
        print(f"Using config: {config_path}")
    else:
        print("No config file found — all authors will appear as 'Community'.")
        print('Create config.json with format: {"teams": {"Team": {"color": "#3b82f6", "members": ["name", "email"]}}, "aliases": {}}')

    stats = GitStats(args.source, config_path)
    stats.collect()
    stats.generate_report(args.output)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(e)
        sys.exit(1)

