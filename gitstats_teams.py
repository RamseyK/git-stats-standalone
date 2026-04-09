import os, subprocess, json, datetime, sys
from collections import Counter, defaultdict

class GitStats:
    def __init__(self, repo_path, mapping_file=None):
        self.repo_path = repo_path
        self.mapping_data = self.load_raw_mapping(mapping_file)
        # Reverse mapping: author_alias -> team_name
        self.author_to_team = {alias: team for team, aliases in self.mapping_data.items() for alias in aliases}
        
        self.data = {
            'project_name': os.path.basename(os.path.abspath(repo_path)),
            'analysis_date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            'general': {'total_commits': 0, 'total_files': 0, 'total_lines': 0},
            'authors': {}, 
            'teams': {},   
            'team_domains': defaultdict(lambda: Counter()), # team -> {folder: churn}
            'tags': [], 
            'loc_history': []
        }

    def load_raw_mapping(self, path):
        if not path or not os.path.exists(path): return {}
        with open(path) as f:
            return json.load(f)

    def get_team(self, name, email):
        return self.author_to_team.get(email, self.author_to_team.get(name, "Unassigned"))

    def run_git(self, args):
        return subprocess.check_output(['git', '-C', self.repo_path] + args).decode('utf-8', 'ignore')

    def collect(self):
        print(f"🏗️  Mapping Architecture for {self.data['project_name']}...")
        
        log_data = self.run_git(['log', '--numstat', '--pretty=format:COMMIT|%at|%an|%ae'])
        current_author, current_team, running_loc, all_ts = None, None, 0, []

        for line in log_data.splitlines():
            if line.startswith('COMMIT|'):
                _, ts, name, email = line.split('|')
                ts = int(ts)
                team_name = self.get_team(name, email)
                
                all_ts.append(ts)
                current_author = name
                current_team = team_name
                
                if team_name not in self.data['teams']:
                    self.data['teams'][team_name] = {'commits': 0, 'add': 0, 'del': 0, 'members': set(), 'first': ts, 'last': ts}
                
                if name not in self.data['authors']:
                    self.data['authors'][name] = {'commits': 0, 'add': 0, 'del': 0, 'first': ts, 'last': ts, 'team': team_name}

                for entity in [self.data['teams'][team_name], self.data['authors'][name]]:
                    entity['commits'] += 1
                    entity['last'] = max(entity['last'], ts)
                    entity['first'] = min(entity['first'], ts)
                
                self.data['teams'][team_name]['members'].add(name)
                self.data['general']['total_commits'] += 1
            
            elif '\t' in line:
                try:
                    adds, dels, path = (line.split('\t') + [0,0,""])[:3]
                    a, d = int(adds or 0), int(dels or 0)
                    churn = a + d
                    
                    self.data['teams'][current_team]['add'] += a
                    self.data['teams'][current_team]['del'] += d
                    
                    running_loc += (a - d)
                    self.data['loc_history'].append({'t': ts * 1000, 'y': running_loc})
                    
                    domain = path.split('/')[0] if '/' in path else 'root'
                    self.data['team_domains'][current_team][domain] += churn
                except: continue

        self.data['general']['total_lines'] = running_loc
        self.data['loc_history'].reverse()

        # Tags Logic
        tags = self.run_git(['tag', '--sort=-creatordate']).splitlines()
        for i, tag in enumerate(tags):
            tag_range = tag if i == len(tags)-1 else f"{tags[i+1]}..{tag}"
            tag_log = self.run_git(['log', tag_range, '--pretty=format:%an|%ae'])
            team_counts = Counter()
            for l in tag_log.splitlines():
                if '|' in l:
                    n, e = l.split('|')
                    team_counts[self.get_team(n, e)] += 1
            if team_counts:
                self.data['tags'].append({'name': tag, 'count': sum(team_counts.values()), 'top_teams': team_counts.most_common(10)})

    def generate_report(self, output="index.html"):
        for t in self.data['teams'].values(): t['members'] = list(t['members'])
        
        # Format team domains for Javascript
        formatted_domains = {team: dict(folders.most_common(5)) for team, folders in self.data['team_domains'].items()}

        html = f"""
        <!DOCTYPE html>
        <html class="scroll-smooth">
        <head>
            <meta charset="UTF-8">
            <script src="https://cdn.tailwindcss.com"></script>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <title>Team Architect: {self.data['project_name']}</title>
            <style>
                .card {{ @apply bg-white p-8 rounded-3xl border border-slate-200 shadow-sm transition-all hover:shadow-md; }}
                .tab-btn {{ @apply px-6 py-2 rounded-xl font-bold text-sm transition-all text-slate-500 hover:bg-slate-100; }}
                .tab-btn.active {{ @apply bg-slate-900 text-white shadow-lg; }}
                .folder-tag {{ @apply text-[10px] px-2 py-0.5 rounded bg-blue-50 text-blue-700 font-black uppercase border border-blue-100; }}
            </style>
        </head>
        <body class="bg-[#fcfcfd] text-slate-900 pb-24 font-sans">
            <header class="bg-white border-b border-slate-200 pt-12 pb-8 px-6 mb-8">
                <div class="max-w-7xl mx-auto flex flex-col md:flex-row justify-between items-end gap-6">
                    <div>
                        <div class="flex items-center gap-2 mb-2">
                            <div class="w-3 h-3 rounded-full bg-blue-600 animate-pulse"></div>
                            <span class="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">Architecture Report</span>
                        </div>
                        <h1 class="text-4xl font-black tracking-tighter text-slate-900 uppercase italic">Team<span class="text-blue-600">Stats</span></h1>
                        <h2 class="text-xl font-bold text-slate-600 mt-1">{self.data['project_name']} — {self.data['analysis_date']}</h2>
                    </div>
                </div>
            </header>

            <div class="max-w-7xl mx-auto px-6">
                <nav class="sticky top-6 z-50 mb-12 flex justify-center">
                    <div class="bg-white/80 backdrop-blur-xl border border-slate-200 p-1.5 rounded-2xl shadow-xl flex gap-1">
                        <button onclick="showTab('teams')" class="tab-btn active">Teams</button>
                        <button onclick="showTab('tags')" class="tab-btn">Releases</button>
                        <button onclick="showTab('authors')" class="tab-btn">Authors</button>
                    </div>
                </nav>

                <div id="tab-teams" class="tab-content grid grid-cols-1 md:grid-cols-2 gap-6">
                    {"".join([f'''
                    <div class="card">
                        <div class="flex justify-between items-start mb-6">
                            <div>
                                <h3 class="text-2xl font-black text-slate-900 tracking-tight">{name}</h3>
                                <p class="text-xs font-bold text-slate-400 uppercase tracking-widest">{len(s['members'])} Members • {s['commits']} Commits</p>
                            </div>
                            <div class="text-right">
                                <span class="text-xs font-black text-blue-600 uppercase italic">Primary Ownership</span>
                                <div class="flex flex-wrap gap-1 mt-1 justify-end">
                                    {"".join([f'<span class="folder-tag">{folder}</span>' for folder in formatted_domains[name].keys()])}
                                </div>
                            </div>
                        </div>
                        <div class="h-48 border-t border-slate-50 pt-4">
                            <canvas id="chart-{name.replace(' ', '')}"></canvas>
                        </div>
                    </div>
                    ''' for name, s in sorted(self.data['teams'].items(), key=lambda x: x[1]['commits'], reverse=True)])}
                </div>

                <div id="tab-tags" class="tab-content hidden space-y-6">
                    {"".join([f'''
                    <div class="card">
                        <div class="flex justify-between items-center mb-6 border-b border-slate-50 pb-4">
                            <h3 class="text-2xl font-black text-slate-900 italic tracking-tighter">{t["name"]}</h3>
                            <span class="bg-blue-600 text-white text-[10px] font-black px-4 py-1 rounded-full uppercase italic">Release Engine</span>
                        </div>
                        <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
                            {"".join([f'<div class="p-4 bg-slate-50 rounded-2xl border border-slate-100"><div class="text-[10px] font-black uppercase text-slate-400 mb-1">{team}</div><div class="text-2xl font-black text-blue-600">{count}</div><div class="text-[9px] font-bold text-slate-400 uppercase">Commits</div></div>' for team, count in t["top_teams"]])}
                        </div>
                    </div>
                    ''' for t in self.data['tags']])}
                </div>

                <div id="tab-authors" class="tab-content hidden">
                    <div class="card !p-0 overflow-hidden shadow-2xl">
                        <table class="w-full text-left border-collapse">
                            <thead class="bg-slate-50 border-b text-[10px] uppercase font-bold text-slate-500 tracking-widest">
                                <tr><th class="px-8 py-5">Individual</th><th>Assigned Team</th><th>Volume</th><th>Active Days</th></tr>
                            </thead>
                            <tbody class="divide-y divide-slate-100">
                                {"".join([f'''
                                <tr>
                                    <td class="px-8 py-5 font-bold text-slate-800">{name}</td>
                                    <td><span class="bg-blue-50 text-blue-700 text-[10px] px-3 py-1 rounded-full font-black uppercase">{s["team"]}</span></td>
                                    <td class="font-mono text-sm font-bold">{s["commits"]}</td>
                                    <td class="text-slate-400 text-xs font-bold italic">{(s["last"]-s["first"])//86400} Days</td>
                                </tr>
                                ''' for name, s in sorted(self.data['authors'].items(), key=lambda x: x[1]['commits'], reverse=True)])}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <script>
                const domainStats = {json.dumps(formatted_domains)};

                function showTab(t) {{
                    document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
                    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                    event.currentTarget.classList.add('active');
                    document.getElementById('tab-' + t).classList.remove('hidden');
                    window.scrollTo({{top: 0, behavior: 'smooth'}});
                }}

                // Create per-team charts
                Object.keys(domainStats).forEach(team => {{
                    const ctx = document.getElementById('chart-' + team.replace(' ', ''));
                    if (!ctx) return;
                    new Chart(ctx, {{
                        type: 'bar',
                        data: {{
                            labels: Object.keys(domainStats[team]),
                            datasets: [{{ 
                                label: 'Churn Share', 
                                data: Object.values(domainStats[team]), 
                                backgroundColor: '#3b82f6', 
                                borderRadius: 6 
                            }}]
                        }},
                        options: {{ 
                            indexAxis: 'y', 
                            maintainAspectRatio: false, 
                            plugins: {{ legend: {{ display: false }} }},
                            scales: {{ x: {{ display: false }}, y: {{ grid: {{ display: false }} }} }}
                        }}
                    }});
                }});
            </script>
        </body>
        </html>
        """
        with open(output, 'w') as f: f.write(html)
        print(f"✅ Architectural Report ready: {os.path.abspath(output)}")

if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "."
    m = "mapping.json" if os.path.exists("mapping.json") else None
    stats = GitStatsArchitect(p, m)
    stats.collect()
    stats.generate_report()