import os
import subprocess
import sys
import json
import datetime
import argparse
from collections import Counter, defaultdict

# Distinct colors for up to 12 teams; 'Unassigned' gets slate
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
      - Directory churn chart (click to filter by module)

    Config file (./config.json or custom path) format:
    {
      "teams": {
        "Team Name": ["author name", "author@email.com", ...]
      },
      "aliases": {
        "Canonical Name": ["alias", "old@email.com", ...]
      }
    }

    Members in "teams" are matched against git author names and emails.
    "aliases" merges multiple identities into a single canonical name.
    Authors not assigned to any team appear under "Unassigned".
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

        # aliases: canonical_name -> [alias, email, ...]
        aliases = config.get('aliases', {})
        self.alias_to_canonical = {
            alias: canon for canon, als in aliases.items() for alias in als
        }

        # teams: team_name -> [author name or email, ...]
        teams_config = config.get('teams', {})
        self.author_to_team = {}
        self.team_colors = {}
        for i, (team, members) in enumerate(teams_config.items()):
            self.team_colors[team] = TEAM_COLORS[i % len(TEAM_COLORS)]
            for m in members:
                self.author_to_team[m] = team
        self.team_colors['Unassigned'] = '#94a3b8'

        self.data = {
            'project_name': os.path.basename(os.path.abspath(repo_path)),
            'analysis_date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            'general': {'total_commits': 0, 'total_files': 0, 'total_lines': 0, 'age_days': 0},
            'activity': {'hour': Counter(), 'weekday': Counter(), 'heatmap': Counter()},
            'authors': {},
            'teams': {},
            'domain_contributions': defaultdict(lambda: Counter()),  # domain -> {author: commit_count}
            'team_domains': defaultdict(lambda: Counter()),           # team   -> {domain: churn}
            'files': Counter(),
            'domains': Counter(),
            'tags': [],
            'loc_history': [],
        }

    # ------------------------------------------------------------------ helpers

    def _load_config(self, path):
        if not path or not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    def _get_author(self, name, email):
        return self.alias_to_canonical.get(email, self.alias_to_canonical.get(name, name))

    def _get_team(self, author, email):
        return self.author_to_team.get(email, self.author_to_team.get(author, 'Unassigned'))

    def _run_git(self, args):
        return subprocess.check_output(
            ['git', '-C', self.repo_path] + args,
            stderr=subprocess.DEVNULL
        ).decode('utf-8', 'ignore')

    # ------------------------------------------------------------------ collect

    def collect(self):
        print(f"Analyzing {self.data['project_name']}...")

        # 1. File inventory
        ls_files = self._run_git(['ls-files']).splitlines()
        self.data['general']['total_files'] = len(ls_files)
        for f in ls_files:
            ext = os.path.splitext(f)[1].lower() or 'source'
            self.data['files'][ext] += 1

        # 2. Full commit history with per-file stats
        log_data = self._run_git(['log', '--numstat', '--pretty=format:COMMIT|%at|%an|%ae'])
        current_author = current_team = None
        running_loc = 0
        all_ts = []
        ts = 0

        for line in log_data.splitlines():
            if line.startswith('COMMIT|'):
                _, ts_str, name, email = line.split('|', 3)
                ts = int(ts_str)
                dt = datetime.datetime.fromtimestamp(ts)
                author = self._get_author(name, email)
                team = self._get_team(author, email)
                all_ts.append(ts)
                current_author = author
                current_team = team

                if author not in self.data['authors']:
                    self.data['authors'][author] = {
                        'commits': 0, 'add': 0, 'del': 0,
                        'first': ts, 'last': ts, 'team': team,
                    }
                au = self.data['authors'][author]
                au['commits'] += 1
                au['last'] = max(au['last'], ts)
                au['first'] = min(au['first'], ts)

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

                self.data['general']['total_commits'] += 1
                self.data['activity']['hour'][dt.hour] += 1
                self.data['activity']['weekday'][dt.weekday()] += 1
                self.data['activity']['heatmap'][dt.strftime('%Y-%m-%d')] += 1

            elif '\t' in line and current_author:
                try:
                    parts = line.split('\t', 2)
                    # Binary files show '-' for add/del counts
                    a = int(parts[0]) if parts[0] and parts[0] != '-' else 0
                    d = int(parts[1]) if parts[1] and parts[1] != '-' else 0
                    path = parts[2] if len(parts) > 2 else ''

                    self.data['authors'][current_author]['add'] += a
                    self.data['authors'][current_author]['del'] += d
                    self.data['teams'][current_team]['add'] += a
                    self.data['teams'][current_team]['del'] += d
                    running_loc += (a - d)
                    self.data['loc_history'].append(running_loc)

                    domain = path.split('/')[0] if '/' in path else 'root'
                    self.data['domains'][domain] += (a + d)
                    self.data['domain_contributions'][domain][current_author] += 1
                    self.data['team_domains'][current_team][domain] += (a + d)
                except (ValueError, IndexError):
                    continue

        if all_ts:
            self.data['general']['age_days'] = (max(all_ts) - min(all_ts)) // 86400
        self.data['general']['total_lines'] = running_loc
        self.data['loc_history'].reverse()

        # Decimate LOC history for large repos (keep ≤2000 points)
        if len(self.data['loc_history']) > 2000:
            step = len(self.data['loc_history']) // 2000
            self.data['loc_history'] = self.data['loc_history'][::step]

        # 3. Tag history (top 20 tags)
        try:
            tags = self._run_git(['tag', '--sort=-creatordate']).splitlines()
        except subprocess.CalledProcessError:
            tags = []

        for i, tag in enumerate(tags[:20]):
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
        """
        Impact score (0–100) = normalized(commits)*W_COMMITS
                              + normalized(lines)*W_LINES
                              + normalized(tenure_days)*W_TENURE
        Weights are defined as class constants (IMPACT_W_*).
        Separately computed for authors and teams.
        """
        wc = self.IMPACT_W_COMMITS
        wl = self.IMPACT_W_LINES
        wt = self.IMPACT_W_TENURE

        authors = self.data['authors']
        if authors:
            max_c = max(a['commits'] for a in authors.values()) or 1
            max_k = max(a['add'] + a['del'] for a in authors.values()) or 1
            max_d = max((a['last'] - a['first']) // 86400 for a in authors.values()) or 1
            for a in authors.values():
                days = (a['last'] - a['first']) // 86400
                a['impact'] = round(
                    (a['commits'] / max_c) * wc +
                    ((a['add'] + a['del']) / max_k) * wl +
                    (days / max_d) * wt, 1
                )

        teams = self.data['teams']
        if teams:
            max_c = max(t['commits'] for t in teams.values()) or 1
            max_k = max(t['add'] + t['del'] for t in teams.values()) or 1
            max_d = max((t['last'] - t['first']) // 86400 for t in teams.values()) or 1
            for t in teams.values():
                days = (t['last'] - t['first']) // 86400
                t['impact'] = round(
                    (t['commits'] / max_c) * wc +
                    ((t['add'] + t['del']) / max_k) * wl +
                    (days / max_d) * wt, 1
                )

    # ------------------------------------------------------------------ HTML helpers

    def _render_tags_html(self):
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
                f'<span class="text-[10px] px-2 py-0.5 rounded font-black uppercase" '
                f'style="background:{self.team_colors.get(team, "#94a3b8")}22;'
                f'color:{self.team_colors.get(team, "#94a3b8")}">'
                f'{team}: {count}</span>'
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
        # Finalize sets → sorted lists for JSON serialization
        for t in self.data['teams'].values():
            t['members'] = sorted(list(t['members']))

        # Serialize data for JavaScript
        authors_json    = json.dumps(self.data['authors'])
        teams_json      = json.dumps(self.data['teams'])
        team_colors_json = json.dumps(self.team_colors)
        domain_json     = json.dumps({k: dict(v) for k, v in self.data['domain_contributions'].items()})
        team_domain_json = json.dumps({k: dict(v.most_common(8)) for k, v in self.data['team_domains'].items()})
        heatmap_json    = json.dumps(dict(self.data['activity']['heatmap']))
        loc_json        = json.dumps(self.data['loc_history'])
        hour_data       = json.dumps([self.data['activity']['hour'][i] for i in range(24)])

        sorted_domains  = self.data['domains'].most_common(30)
        domain_labels   = json.dumps([d[0] for d in sorted_domains])
        domain_values   = json.dumps([d[1] for d in sorted_domains])

        pname  = self.data['project_name']
        adate  = self.data['analysis_date']
        tcom   = self.data['general']['total_commits']
        tlines = self.data['general']['total_lines']
        nauth  = len(self.data['authors'])
        nteams = len(self.data['teams'])

        iw_commits = self.IMPACT_W_COMMITS
        iw_lines   = self.IMPACT_W_LINES
        iw_tenure  = self.IMPACT_W_TENURE

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
                <span class="bg-blue-600 text-white text-[10px] font-black px-2 py-0.5 rounded uppercase tracking-widest">Repository</span>
                <h1 class="text-3xl font-black tracking-tighter text-slate-900 uppercase italic">Git<span class="text-blue-600">Stats</span></h1>
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
            <button onclick="showTab('domains',this)"  class="tab-btn">Modules</button>
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
                <h3 class="text-xl font-black text-slate-900 mb-0.5">Top Contributors</h3>
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
                    <p class="text-xs text-slate-500">Sum of lines added and deleted across all commits. Captures the raw volume of code touched.</p>
                    <p class="text-[11px] text-slate-400 mt-2 font-mono">score += ((add + del) / max_lines) × {iw_lines}</p>
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
            <div class="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-800">
                <span class="font-black">Formula: </span>
                Impact = (commits / max_commits) × {iw_commits} &nbsp;+&nbsp; ((add + del) / max_lines) × {iw_lines} &nbsp;+&nbsp; (tenure_days / max_tenure) × {iw_tenure}
                <br><span class="text-blue-500 mt-1 block">All normalizations are computed independently for authors and for teams.</span>
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

    <!-- ═══ MODULES ═══════════════════════════════════════════════════════════ -->
    <div id="tab-domains" class="tab-content hidden">
        <div class="card" style="height:700px">
            <div class="mb-6">
                <h3 class="text-xl font-black">Directory Churn Analysis</h3>
                <p class="text-sm text-slate-400 font-medium mt-1">Click any bar to filter contributors by module.</p>
            </div>
            <canvas id="domainChart"></canvas>
        </div>
    </div>

</div><!-- /max-w-7xl -->

<!-- ═══════════════════════════════════════════════════════════════ JAVASCRIPT -->
<script>
const authorData     = {authors_json};
const teamsData      = {teams_json};
const teamColors     = {team_colors_json};
const domainData     = {domain_json};
const teamDomainData = {team_domain_json};
const totalCommits   = {tcom};

let currentSortKey   = 'impact';
let currentFilter    = null;
let currentFilterType = null;

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
        if (t === 'domains') initDomainChart();
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

    if (currentFilter && currentFilterType === 'domain') {{
        const filtered = domainData[currentFilter] || {{}};
        authors      = authors.filter(([n]) => filtered[n]);
        displayTotal = Object.values(filtered).reduce((a,b) => a+b, 0) || 1;
        title.innerText = `Module: ${{currentFilter}}`;
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
        const commits = (currentFilter && currentFilterType === 'domain')
            ? ((domainData[currentFilter] || {{}})[name] || 0)
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
function renderImpactLeaderboard() {{
    const medals = ['🥇','🥈','🥉'];

    // Authors
    const topAuthors = Object.entries(authorData)
        .sort((a,b) => b[1].impact - a[1].impact).slice(0, 12);

    document.getElementById('impact-authors').innerHTML = topAuthors.map(([name, s], i) => {{
        const color  = teamColor(s.team);
        const prefix = i < 3
            ? `<span class="text-xl w-8 text-center shrink-0">${{medals[i]}}</span>`
            : `<span class="text-xs font-black text-slate-300 w-8 text-center shrink-0">#${{i+1}}</span>`;
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
    }}).join('');

    // Teams
    const topTeams = Object.entries(teamsData).sort((a,b) => b[1].impact - a[1].impact);

    document.getElementById('impact-teams').innerHTML = topTeams.map(([name, s], i) => {{
        const color   = teamColor(name);
        const members = Array.isArray(s.members) ? s.members.length : 0;
        const prefix  = i < 3
            ? `<span class="text-xl w-8 text-center shrink-0">${{medals[i]}}</span>`
            : `<span class="text-xs font-black text-slate-300 w-8 text-center shrink-0">#${{i+1}}</span>`;
        return `<div class="flex items-center gap-3 p-3 rounded-xl hover:bg-slate-50 transition-colors cursor-pointer"
                     onclick="filterByTeam(${{JSON.stringify(name)}})" title="Click to filter contributors">
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
        const topDomains = Object.entries(teamDomainData[name] || {{}}).slice(0, 5);
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
                <div class="text-[10px] font-black uppercase text-slate-400 mb-1.5">Primary Modules</div>
                <div class="flex flex-wrap gap-1">
                    ${{topDomains.map(([d]) =>
                        `<span class="text-[10px] px-2 py-0.5 rounded font-black uppercase"
                               style="background:${{color}}22;color:${{color}}">${{d}}</span>`
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

function initDomainChart() {{
    const dChart = new Chart(document.getElementById('domainChart'), {{
        type: 'bar',
        data: {{
            labels: {domain_labels},
            datasets: [{{ label:'Churn', data:{domain_values}, backgroundColor:'#3b82f6', borderRadius:8, barThickness:16 }}],
        }},
        options: {{
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: {{ legend: {{ display: false }} }},
            onClick: (e, elements) => {{
                if (!elements.length) return;
                const domain = dChart.data.labels[elements[0].index];
                renderAuthorTable(domain, 'domain');
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
</body>
</html>"""
        with open(output, 'w') as f:
            f.write(html)
        print(f"Report generated: {os.path.abspath(output)}")


def main() -> int:
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
        print("No config file found — all authors will appear as 'Unassigned'.")
        print("Create config.json with format: {\"teams\": {\"Team\": [\"name\", \"email\"]}, \"aliases\": {}}")

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

