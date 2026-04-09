import os, subprocess, json, datetime, sys
from collections import Counter, defaultdict

class GitStats:
    def __init__(self, repo_path, mapping_file=None):
        self.repo_path = repo_path
        self.mapping = self.load_mapping(mapping_file)
        self.data = {
            'project_name': os.path.basename(os.path.abspath(repo_path)),
            'analysis_date': datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            'general': {'total_commits': 0, 'total_files': 0, 'total_lines': 0, 'age_days': 0},
            'activity': {'hour': Counter(), 'weekday': Counter(), 'heatmap': Counter()},
            'authors': {}, 
            'domain_contributions': defaultdict(lambda: Counter()), 
            'files': Counter(),
            'domains': Counter(),
            'tags': [], 
            'loc_history': []
        }

    def load_mapping(self, path):
        if not path or not os.path.exists(path): return {}
        with open(path) as f:
            m = json.load(f)
            return {alias: prim for prim, aliases in m.items() for alias in aliases}

    def get_author(self, name, email):
        return self.mapping.get(email, self.mapping.get(name, name))

    def run_git(self, args):
        return subprocess.check_output(['git', '-C', self.repo_path] + args).decode('utf-8', 'ignore')

    def collect(self):
        print(f"🚀 Analyzing {self.data['project_name']}...")
        
        # 1. File Inventory
        ls_files = self.run_git(['ls-files']).splitlines()
        self.data['general']['total_files'] = len(ls_files)
        for f in ls_files:
            ext = os.path.splitext(f)[1].lower() or 'source'
            self.data['files'][ext] += 1

        # 2. Commit History
        log_data = self.run_git(['log', '--numstat', '--pretty=format:COMMIT|%at|%an|%ae'])
        current_author, running_loc, all_ts = None, 0, []

        for line in log_data.splitlines():
            if line.startswith('COMMIT|'):
                _, ts, name, email = line.split('|')
                ts = int(ts); dt = datetime.datetime.fromtimestamp(ts)
                author = self.get_author(name, email)
                all_ts.append(ts); current_author = author
                
                if author not in self.data['authors']:
                    self.data['authors'][author] = {'commits': 0, 'add': 0, 'del': 0, 'first': ts, 'last': ts}
                
                self.data['authors'][author]['commits'] += 1
                self.data['authors'][author]['last'] = max(self.data['authors'][author]['last'], ts)
                self.data['authors'][author]['first'] = min(self.data['authors'][author]['first'], ts)
                self.data['general']['total_commits'] += 1
                self.data['activity']['hour'][dt.hour] += 1
                self.data['activity']['weekday'][dt.weekday()] += 1
                self.data['activity']['heatmap'][dt.strftime('%Y-%m-%d')] += 1
            
            elif '\t' in line:
                try:
                    adds, dels, path = (line.split('\t') + [0,0,""])[:3]
                    a, d = int(adds or 0), int(dels or 0)
                    self.data['authors'][current_author]['add'] += a
                    self.data['authors'][current_author]['del'] += d
                    running_loc += (a - d)
                    self.data['loc_history'].append({'t': ts * 1000, 'y': running_loc})
                    
                    domain = path.split('/')[0] if '/' in path else 'root'
                    self.data['domains'][domain] += (a + d)
                    self.data['domain_contributions'][domain][current_author] += 1
                except: continue

        if all_ts: self.data['general']['age_days'] = (max(all_ts) - min(all_ts)) // 86400
        self.data['general']['total_lines'] = running_loc
        self.data['loc_history'].reverse()

        # 3. Tag History (Top 20)
        tags = self.run_git(['tag', '--sort=-creatordate']).splitlines()
        for i, tag in enumerate(tags):
            tag_range = tag if i == len(tags)-1 else f"{tags[i+1]}..{tag}"
            tag_log = self.run_git(['log', tag_range, '--pretty=format:%an|%ae'])
            author_counts = Counter()
            for l in tag_log.splitlines():
                if '|' in l:
                    n, e = l.split('|')
                    author_counts[self.get_author(n, e)] += 1
            if author_counts:
                self.data['tags'].append({'name': tag, 'count': sum(author_counts.values()), 'top_authors': author_counts.most_common(20)})

    def generate_report(self, output="index.html"):
        authors_json = json.dumps(self.data['authors'])
        domain_json = json.dumps(dict(self.data['domain_contributions']))
        
        html = f"""
        <!DOCTYPE html>
        <html class="scroll-smooth">
        <head>
            <meta charset="UTF-8">
            <script src="https://cdn.tailwindcss.com"></script>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <title>GitStats: {self.data['project_name']}</title>
            <style>
                .heatmap-cell {{ width: 12px; height: 12px; border-radius: 2px; transition: transform 0.1s; cursor: help; }}
                .heatmap-cell:hover {{ transform: scale(1.3); }}
                .lvl-0 {{ background: #f1f5f9; }} .lvl-1 {{ background: #93c5fd; }}
                .lvl-2 {{ background: #3b82f6; }} .lvl-3 {{ background: #2563eb; }} .lvl-4 {{ background: #1e3a8a; }}
                .card {{ @apply bg-white p-8 rounded-3xl border border-slate-200 shadow-sm transition-all; }}
                .tab-btn {{ @apply px-5 py-2 rounded-xl font-bold text-sm transition-all text-slate-500 hover:text-slate-900; }}
                .tab-btn.active {{ @apply bg-slate-900 text-white shadow-md; }}
                .sticky-header {{ position: sticky; top: 0; z-index: 50; }}
            </style>
        </head>
        <body class="bg-[#f8fafc] text-slate-900 pb-24">
            <header class="bg-white border-b border-slate-200 pt-10 pb-6 px-6 mb-8">
                <div class="max-w-7xl mx-auto flex flex-col md:flex-row justify-between items-center gap-6">
                    <div>
                        <div class="flex items-center gap-3 mb-1">
                            <span class="bg-blue-600 text-white text-[10px] font-black px-2 py-0.5 rounded uppercase tracking-widest">Repository</span>
                            <h1 class="text-3xl font-black tracking-tighter text-slate-900 uppercase italic">Git<span class="text-blue-600">Stats</span></h1>
                        </div>
                        <h2 class="text-2xl font-bold text-slate-800">{self.data['project_name']}</h2>
                        <p class="text-xs font-bold text-slate-400 uppercase tracking-widest mt-1">Last Analysis: {self.data['analysis_date']}</p>
                    </div>
                    <div class="flex gap-4">
                        <div class="text-center px-4 border-r border-slate-100">
                            <span class="block text-xl font-black text-slate-900">{self.data['general']['total_commits']}</span>
                            <span class="text-[10px] uppercase font-bold text-slate-400">Commits</span>
                        </div>
                        <div class="text-center px-4">
                            <span class="block text-xl font-black text-blue-600">{self.data['general']['total_lines']}</span>
                            <span class="text-[10px] uppercase font-bold text-slate-400">Net Lines</span>
                        </div>
                    </div>
                </div>
            </header>

            <div class="max-w-7xl mx-auto px-6">
                <nav class="sticky-header mb-12">
                    <div class="bg-white/70 backdrop-blur-xl border border-white/20 p-1.5 rounded-2xl shadow-xl shadow-slate-200/50 flex gap-1 justify-center max-w-fit mx-auto border-slate-200">
                        <button onclick="showTab('activity')" class="tab-btn active">Activity</button>
                        <button onclick="showTab('authors')" class="tab-btn">Authors</button>
                        <button onclick="showTab('tags')" class="tab-btn">Releases</button>
                        <button onclick="showTab('domains')" class="tab-btn">Modules</button>
                    </div>
                </nav>

                <div id="tab-activity" class="tab-content space-y-10 animate-in fade-in duration-500">
                    <div class="card">
                        <h3 class="text-xs font-black uppercase text-slate-400 mb-6 tracking-widest">Contribution Heatmap</h3>
                        <div class="overflow-x-auto pb-4"><div id="heatmap" class="flex flex-wrap gap-1.5" style="width: 950px;"></div></div>
                    </div>
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-10">
                        <div class="card h-[400px]"><h3 class="font-bold mb-6">LOC History</h3><canvas id="locChart"></canvas></div>
                        <div class="card h-[400px]"><h3 class="font-bold mb-6">Hourly Punchcard</h3><canvas id="hourChart"></canvas></div>
                    </div>
                </div>

                <div id="tab-authors" class="tab-content hidden space-y-6">
                    <div class="flex justify-between items-center bg-white p-6 rounded-3xl border border-slate-200">
                        <h2 id="author-title" class="text-xl font-black">All Contributors</h2>
                        <button id="reset-filter" onclick="renderAuthorTable()" class="hidden bg-blue-50 text-blue-600 px-4 py-2 rounded-xl text-xs font-black uppercase hover:bg-blue-100 transition-all">Clear Module Filter</button>
                    </div>
                    <div class="card !p-0 overflow-hidden shadow-xl">
                        <table class="w-full text-left border-collapse" id="author-table"></table>
                    </div>
                </div>

                <div id="tab-tags" class="tab-content hidden space-y-8">
                    {"".join([f'''
                    <div class="card group">
                        <div class="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 mb-8 border-b border-slate-100 pb-6">
                            <div>
                                <span class="text-3xl font-black text-slate-900 group-hover:text-blue-600 transition-colors">{t["name"]}</span>
                                <p class="text-[10px] font-bold text-slate-400 uppercase tracking-widest mt-1">Release Contributors</p>
                            </div>
                            <div class="px-6 py-2 bg-slate-900 text-white rounded-2xl font-black text-sm">{t["count"]} Commits</div>
                        </div>
                        <div class="grid grid-cols-2 lg:grid-cols-4 xl:grid-cols-5 gap-y-4 gap-x-8">
                            {"".join([f'<div class="flex justify-between items-center text-sm bg-slate-50/50 p-2 rounded-xl border border-transparent hover:border-slate-200 transition-all"><span class="truncate font-bold text-slate-600">{i+1}. {auth}</span><b class="text-blue-600 ml-2">{count}</b></div>' for i, (auth, count) in enumerate(t["top_authors"])])}
                        </div>
                    </div>
                    ''' for t in self.data['tags']])}
                </div>

                <div id="tab-domains" class="tab-content hidden">
                    <div class="card h-[700px]">
                        <div class="mb-8">
                            <h3 class="text-xl font-black">Directory Churn Analysis</h3>
                            <p class="text-sm text-slate-400 font-medium mt-1">Click any bar to see who contributed to that specific module.</p>
                        </div>
                        <canvas id="domainChart"></canvas>
                    </div>
                </div>
            </div>

            <script>
                const authorData = {authors_json};
                const domainData = {domain_json};
                const totalCommits = {self.data['general']['total_commits']};

                function showTab(t) {{
                    document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
                    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                    event.currentTarget.classList.add('active');
                    document.getElementById('tab-' + t).classList.remove('hidden');
                    window.scrollTo({{top: 0, behavior: 'smooth'}});
                }}

                function renderAuthorTable(domain = null) {{
                    const table = document.getElementById('author-table');
                    const title = document.getElementById('author-title');
                    const resetBtn = document.getElementById('reset-filter');
                    
                    let authors = Object.entries(authorData);
                    let displayTotal = totalCommits;

                    if (domain) {{
                        const filtered = domainData[domain];
                        authors = authors.filter(([name]) => filtered[name]).sort((a,b) => filtered[b[0]] - filtered[a[0]]);
                        displayTotal = Object.values(filtered).reduce((a,b) => a+b, 0);
                        title.innerText = `Contributors for: ${{domain}}`;
                        resetBtn.classList.remove('hidden');
                    }} else {{
                        authors.sort((a,b) => b[1].commits - a[1].commits);
                        title.innerText = "All Contributors";
                        resetBtn.classList.add('hidden');
                    }}

                    table.innerHTML = `
                        <thead class="bg-slate-50 border-b text-[10px] uppercase font-bold text-slate-500">
                            <tr>
                                <th class="px-8 py-5">Developer</th>
                                <th>Commits</th>
                                <th>Impact Share</th>
                                <th class="px-8">Active Days</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-100 bg-white">
                            ${{authors.map(([name, s]) => {{
                                const commits = domain ? domainData[domain][name] : s.commits;
                                const share = ((commits/displayTotal)*100).toFixed(1);
                                return `<tr>
                                    <td class="px-8 py-5 font-bold text-slate-800">${{name}}</td>
                                    <td class="font-mono text-sm">${{commits}}</td>
                                    <td>
                                        <div class="flex items-center gap-2">
                                            <span class="font-bold text-blue-600 text-sm">${{share}}%</span>
                                            <div class="w-12 h-1 bg-slate-100 rounded-full overflow-hidden">
                                                <div class="h-full bg-blue-500" style="width: ${{share}}%"></div>
                                            </div>
                                        </div>
                                    </td>
                                    <td class="px-8 text-slate-400 font-bold text-xs">${{Math.floor((s.last-s.first)/86400)}} Days</td>
                                </tr>`;
                            }}).join('')}}
                        </tbody>`;
                }}

                renderAuthorTable();

                const commonOpts = {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }};
                
                // Charts
                const dChart = new Chart(document.getElementById('domainChart'), {{
                    type: 'bar',
                    data: {{
                        labels: {json.dumps(list(self.data['domains'].keys()))},
                        datasets: [{{ label: 'Churn', data: {json.dumps(list(self.data['domains'].values()))}, backgroundColor: '#3b82f6', borderRadius: 8, barThickness: 20 }}]
                    }},
                    options: {{ 
                        ...commonOpts, indexAxis: 'y',
                        onClick: (e, elements) => {{
                            if (elements.length > 0) {{
                                const idx = elements[0].index;
                                const domain = dChart.data.labels[idx];
                                renderAuthorTable(domain);
                                document.querySelector('.tab-btn:nth-child(2)').click();
                            }}
                        }}
                    }}
                }});

                // Heatmap logic
                const hmd = {json.dumps(self.data['activity']['heatmap'])};
                const hmc = document.getElementById('heatmap');
                const today = new Date();
                for(let i=364; i>=0; i--) {{
                    const d = new Date(); d.setDate(today.getDate() - i);
                    const ds = d.toISOString().split('T')[0];
                    const c = hmd[ds] || 0;
                    const cell = document.createElement('div');
                    cell.className = `heatmap-cell lvl-${{c === 0 ? 0 : Math.min(4, Math.ceil(c / 5))}}`;
                    cell.title = `${{ds}}: ${{c}} commits`;
                    hmc.appendChild(cell);
                }}

                new Chart(document.getElementById('locChart'), {{ type: 'line', data: {{ datasets: [{{ label: 'LOC', data: {json.dumps(self.data['loc_history'])}, borderColor: '#3b82f6', pointRadius: 0, fill: true, backgroundColor: 'rgba(59, 130, 246, 0.05)', tension: 0.1 }}] }}, options: {{ ...commonOpts, scales: {{ x: {{ display: false }} }} }} }});
                new Chart(document.getElementById('hourChart'), {{ type: 'bar', data: {{ labels: Array.from({{length:24}},(_,i)=>i+':00'), datasets: [{{ data: {[self.data['activity']['hour'][i] for i in range(24)]}, backgroundColor: '#1e3a8a', borderRadius: 4 }}] }}, options: commonOpts }});
            </script>
        </body>
        </html>
        """
        with open(output, 'w') as f: f.write(html)
        print(f"✅ Ultimate Dashboard generated for {self.data['project_name']}")

if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "."
    m = "mapping.json" if os.path.exists("mapping.json") else None
    stats = GitStatsUltimate(p, m)
    stats.collect()
    stats.generate_report()