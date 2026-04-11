"""
Tests for gitstats.py using the pyodide and pyodide-recipes repositories as
fixed baselines.

All tests run against git worktrees locked to specific commits so results are
deterministic regardless of ongoing development in either repo.

Run from the project root:
    pytest tests/

Requirements:
    pip install pytest
    ~/Downloads/pyodide       — main test repository
    ~/Downloads/pyodide-recipes — support repository used by TestSupportRepos
"""

import json
import os
import subprocess
import sys

import pytest

# Resolve the project root (one level up from this file) and make gitstats importable.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
import gitstats  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PYODIDE_REPO   = os.path.expanduser('~/Downloads/pyodide')
LOCKED_COMMIT  = '2906b146c8bb4700c437ede5581f0dc641459a97'

# Expected values at the locked commit with the standard config.json.
EXPECTED_COMMITS  = 4204
EXPECTED_FILES    = 546
EXPECTED_AUTHORS  = 310
EXPECTED_TEAMS    = 3    # Core, Build & Packaging, Community
EXPECTED_TAGS     = 50   # capped by max_release_tags in config.json

# Support repo — pyodide-recipes
RECIPES_REPO          = os.path.expanduser('~/Downloads/pyodide-recipes')
RECIPES_LOCKED_COMMIT = 'b7c6155fa29d773c53cb0825d28f04d65cf76d32'

# Expected values when pyodide + pyodide-recipes are combined.
EXPECTED_COMBINED_COMMITS = 4613   # 4204 pyodide + 409 recipes
EXPECTED_COMBINED_AUTHORS = 329    # 310 pyodide + 19 recipes-only authors


# ---------------------------------------------------------------------------
# Module-scoped fixture: single worktree for all tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def repo_path(tmp_path_factory):
    """
    Create a detached git worktree at LOCKED_COMMIT and yield its path.
    The worktree is removed after all tests in the module complete.

    Using a worktree avoids touching the user's working copy of pyodide.
    """
    wt = str(tmp_path_factory.mktemp('pyodide_locked'))
    subprocess.run(
        ['git', '-C', PYODIDE_REPO, 'worktree', 'add', '--detach', wt, LOCKED_COMMIT],
        check=True, capture_output=True,
    )
    yield wt
    subprocess.run(
        ['git', '-C', PYODIDE_REPO, 'worktree', 'remove', '--force', wt],
        capture_output=True,
    )


@pytest.fixture(scope='module')
def std_config_path():
    """Path to the project's standard config.json."""
    return os.path.join(PROJECT_ROOT, 'config.json')


@pytest.fixture(scope='module')
def std_gs(repo_path, std_config_path):
    """
    GitStats instance collected against the locked commit with the standard
    config.json.  Shared across all tests that only need to read results.
    """
    gs = gitstats.GitStats(repo_path, std_config_path)
    gs.collect()
    return gs


def make_config(tmp_path, **overrides):
    """
    Write a minimal config JSON file to tmp_path and return the file path.
    Pass keyword arguments to override top-level keys.
    """
    cfg = {
        'release_tag_prefix': '',
        'max_release_tags': 10,
        'teams': {},
        'aliases': {},
    }
    cfg.update(overrides)
    p = os.path.join(str(tmp_path), 'config.json')
    with open(p, 'w') as f:
        json.dump(cfg, f)
    return p


# ---------------------------------------------------------------------------
# Basic stats
# ---------------------------------------------------------------------------

class TestBasicStats:
    def test_total_commits(self, std_gs):
        assert std_gs.data['general']['total_commits'] == EXPECTED_COMMITS

    def test_total_files(self, std_gs):
        assert std_gs.data['general']['total_files'] == EXPECTED_FILES

    def test_author_count(self, std_gs):
        assert len(std_gs.data['authors']) == EXPECTED_AUTHORS

    def test_team_count(self, std_gs):
        assert len(std_gs.data['teams']) == EXPECTED_TEAMS

    def test_repo_age_positive(self, std_gs):
        assert std_gs.data['general']['age_days'] > 0

    def test_loc_history_non_empty(self, std_gs):
        assert len(std_gs.data['loc_history']) > 0


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------

class TestAliases:
    def test_hood_chatham_merged_into_single_author(self, std_gs):
        """Multiple emails for Hood Chatham must resolve to one canonical entry."""
        authors = std_gs.data['authors']
        assert 'Hood Chatham' in authors
        # The raw email identities must NOT appear as separate authors.
        assert 'roberthoodchatham@gmail.com' not in authors
        assert 'hood@mit.edu' not in authors

    def test_roman_yurchak_merged(self, std_gs):
        authors = std_gs.data['authors']
        assert 'Roman Yurchak' in authors
        assert 'rth.yurchak@gmail.com' not in authors
        assert 'rth.yurchak@pm.me' not in authors

    def test_alias_commits_attributed_to_canonical(self, std_gs):
        """All commits from aliased emails must count toward the canonical author."""
        hood = std_gs.data['authors']['Hood Chatham']
        assert hood['commits'] == 1459


# ---------------------------------------------------------------------------
# Team attribution
# ---------------------------------------------------------------------------

class TestTeams:
    def test_expected_teams_present(self, std_gs):
        teams = set(std_gs.data['teams'].keys())
        assert {'Core', 'Build & Packaging', 'Community'} == teams

    def test_commit_totals_add_up(self, std_gs):
        """Sum of per-team commits must equal total commits."""
        team_total = sum(t['commits'] for t in std_gs.data['teams'].values())
        assert team_total == EXPECTED_COMMITS

    def test_hood_chatham_on_core(self, std_gs):
        assert std_gs.data['authors']['Hood Chatham']['team'] == 'Core'

    def test_roman_yurchak_on_build_packaging(self, std_gs):
        assert std_gs.data['authors']['Roman Yurchak']['team'] == 'Build & Packaging'

    def test_unassigned_author_on_community(self, std_gs):
        """Any author not listed in teams must land on Community."""
        # Pick an author that is not in any team config.
        community_authors = [
            name for name, a in std_gs.data['authors'].items()
            if a['team'] == 'Community'
        ]
        assert len(community_authors) > 0

    def test_core_member_count(self, std_gs):
        """Core should have exactly the 3 configured members present in history."""
        core = std_gs.data['teams']['Core']
        # Hood Chatham, Michael Droettboom, Gyeongjae Choi are in config;
        # all three have commits in the locked repo.
        assert len(core['members']) >= 3


# ---------------------------------------------------------------------------
# Time-ranged team membership
# ---------------------------------------------------------------------------

class TestTimeRangedMembership:
    def test_membership_end_date_routes_to_community(self, repo_path, tmp_path):
        """
        Hood Chatham given a membership range ending before the repo's first commit.
        Every one of his commits should fall to Community, and Core gets zero.
        """
        cfg_path = make_config(tmp_path, teams={
            'Core': {'members': [
                {'name': 'Hood Chatham',               'to': '2019-12-31'},
                {'name': 'roberthoodchatham@gmail.com', 'to': '2019-12-31'},
                {'name': 'hood@mit.edu',                'to': '2019-12-31'},
            ]},
        }, aliases={
            'Hood Chatham': ['roberthoodchatham@gmail.com', 'hood@mit.edu'],
        })
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()

        assert gs.data['teams'].get('Core', {}).get('commits', 0) == 0
        assert gs.data['authors']['Hood Chatham']['team'] == 'Community'

    def test_membership_start_date_splits_commits(self, repo_path, tmp_path):
        """
        Hood Chatham joins Core only from 2024-01-01. Commits before that date
        should go to Community; commits on or after should go to Core.
        Both teams must have at least one commit and together sum to the total.
        """
        cfg_path = make_config(tmp_path, teams={
            'Core': {'members': [
                {'name': 'Hood Chatham',               'from': '2024-01-01'},
                {'name': 'roberthoodchatham@gmail.com', 'from': '2024-01-01'},
                {'name': 'hood@mit.edu',                'from': '2024-01-01'},
            ]},
        }, aliases={
            'Hood Chatham': ['roberthoodchatham@gmail.com', 'hood@mit.edu'],
        }, max_release_tags=0)
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()

        core = gs.data['teams'].get('Core', {})
        comm = gs.data['teams'].get('Community', {})
        assert core.get('commits', 0) > 0
        assert comm.get('commits', 0) > 0
        assert core['commits'] + comm['commits'] == EXPECTED_COMMITS
        # Most recent Hood commits are in 2024+, so his display team is Core.
        assert gs.data['authors']['Hood Chatham']['team'] == 'Core'

    def test_membership_date_range_bounded(self, repo_path, tmp_path):
        """
        Hood Chatham on Core only during 2021-01-01 to 2022-12-31.
        Commits outside that window should go to Community.
        """
        cfg_path = make_config(tmp_path, teams={
            'Core': {'members': [
                {'name': 'Hood Chatham',               'from': '2021-01-01', 'to': '2022-12-31'},
                {'name': 'roberthoodchatham@gmail.com', 'from': '2021-01-01', 'to': '2022-12-31'},
                {'name': 'hood@mit.edu',                'from': '2021-01-01', 'to': '2022-12-31'},
            ]},
        }, aliases={
            'Hood Chatham': ['roberthoodchatham@gmail.com', 'hood@mit.edu'],
        }, max_release_tags=0)
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()

        core = gs.data['teams'].get('Core', {})
        comm = gs.data['teams'].get('Community', {})
        # Both must have commits — Hood has work inside and outside the range.
        assert core.get('commits', 0) > 0
        assert comm.get('commits', 0) > 0
        assert core['commits'] + comm['commits'] == EXPECTED_COMMITS
        # Hood's most recent commits (2024+) fall outside the range → Community.
        assert gs.data['authors']['Hood Chatham']['team'] == 'Community'

    def test_plain_string_member_always_attributed(self, repo_path, tmp_path):
        """A plain string member (no date bounds) must behave identically to the
        pre-time-range behavior: all commits attributed to that team."""
        cfg_path = make_config(tmp_path, teams={
            'Core': {'members': [
                'Hood Chatham',
                'roberthoodchatham@gmail.com',
                'hood@mit.edu',
            ]},
        }, aliases={
            'Hood Chatham': ['roberthoodchatham@gmail.com', 'hood@mit.edu'],
        }, max_release_tags=0)
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()

        assert gs.data['authors']['Hood Chatham']['team'] == 'Core'
        assert gs.data['teams']['Core']['commits'] > 0


# ---------------------------------------------------------------------------
# Impact scoring
# ---------------------------------------------------------------------------

class TestImpactScoring:
    def test_top_author_impact_at_most_100(self, std_gs):
        """No author should ever exceed 100.0."""
        top = max(std_gs.data['authors'].values(), key=lambda a: a['impact'])
        assert top['impact'] <= 100.0
        assert top['impact'] > 0.0

    def test_top_committer_gets_full_commits_weight(self, std_gs):
        """The author with the most commits must score at least IMPACT_W_COMMITS,
        confirming the commits dimension is normalized to its full weight."""
        wc = gitstats.GitStats.IMPACT_W_COMMITS
        top_committer = max(std_gs.data['authors'].values(), key=lambda a: a['commits'])
        assert top_committer['impact'] >= wc

    def test_top_team_impact_is_100(self, std_gs):
        top = max(std_gs.data['teams'].values(), key=lambda t: t['impact'])
        assert top['impact'] == 100.0

    def test_all_impact_scores_in_range(self, std_gs):
        for a in std_gs.data['authors'].values():
            assert 0.0 <= a['impact'] <= 100.0
        for t in std_gs.data['teams'].values():
            assert 0.0 <= t['impact'] <= 100.0

    def test_hood_chatham_is_top_author(self, std_gs):
        top_name = max(std_gs.data['authors'], key=lambda n: std_gs.data['authors'][n]['impact'])
        assert top_name == 'Hood Chatham'

    def test_core_is_top_team(self, std_gs):
        top_name = max(std_gs.data['teams'], key=lambda n: std_gs.data['teams'][n]['impact'])
        assert top_name == 'Core'

    def test_net_lines_flag_affects_score(self, repo_path, tmp_path_factory):
        """
        Disabling use_net_lines (gross lines) should produce different impact scores
        than the default (net lines) for authors with high add/del ratios.

        Each config gets its own temp directory so they don't overwrite each other.
        """
        base_cfg = {
            'teams': {'Core': {'members': ['Hood Chatham', 'roberthoodchatham@gmail.com', 'hood@mit.edu']}},
            'aliases': {'Hood Chatham': ['roberthoodchatham@gmail.com', 'hood@mit.edu']},
            'max_release_tags': 0,
        }
        net_dir   = str(tmp_path_factory.mktemp('net'))
        gross_dir = str(tmp_path_factory.mktemp('gross'))
        net_path   = make_config(net_dir,   impact_use_net_lines=True,  **base_cfg)
        gross_path = make_config(gross_dir, impact_use_net_lines=False, **base_cfg)

        gs_net   = gitstats.GitStats(repo_path, net_path)
        gs_net.collect()
        gs_gross = gitstats.GitStats(repo_path, gross_path)
        gs_gross.collect()

        net_scores   = {n: a['impact'] for n, a in gs_net.data['authors'].items()}
        gross_scores = {n: a['impact'] for n, a in gs_gross.data['authors'].items()}

        # At least one author must differ between the two modes.
        assert any(net_scores[n] != gross_scores[n] for n in net_scores)

    def test_impact_weights_sum_to_100(self):
        total = (gitstats.GitStats.IMPACT_W_COMMITS +
                 gitstats.GitStats.IMPACT_W_LINES   +
                 gitstats.GitStats.IMPACT_W_TENURE)
        assert total == 100

    # ── Stats invariants (real repo) ─────────────────────────────────────────

    def test_sum_of_author_commits_equals_total_commits(self, std_gs):
        """Every commit must be credited to exactly one canonical author."""
        author_total = sum(a['commits'] for a in std_gs.data['authors'].values())
        assert author_total == std_gs.data['general']['total_commits']

    def test_sum_of_team_commits_equals_total_commits(self, std_gs):
        """Every commit must be credited to exactly one team (including Community)."""
        team_total = sum(t['commits'] for t in std_gs.data['teams'].values())
        assert team_total == std_gs.data['general']['total_commits']

    def test_author_add_del_nonnegative(self, std_gs):
        """No author may have negative raw add or del counts."""
        for name, a in std_gs.data['authors'].items():
            assert a['add'] >= 0, f"{name} has negative add"
            assert a['del'] >= 0, f"{name} has negative del"

    def test_team_add_del_sum_matches_author_sum(self, std_gs):
        """Team raw line totals must equal author raw line totals.

        Each file-stat line in git log is credited once to the author and once
        to their team, so the cross-author and cross-team sums must be equal.
        """
        author_adds = sum(a['add'] for a in std_gs.data['authors'].values())
        author_dels = sum(a['del'] for a in std_gs.data['authors'].values())
        team_adds   = sum(t['add'] for t in std_gs.data['teams'].values())
        team_dels   = sum(t['del'] for t in std_gs.data['teams'].values())
        assert team_adds == author_adds
        assert team_dels == author_dels

    def test_author_first_le_last(self, std_gs):
        """Every author's first commit timestamp must be ≤ their last commit timestamp."""
        for name, a in std_gs.data['authors'].items():
            assert a['first'] <= a['last'], f"{name}: first={a['first']} > last={a['last']}"


# ---------------------------------------------------------------------------
# Impact scoring — unit tests with synthetic data
# ---------------------------------------------------------------------------

_DAY = 86400  # seconds in one day


def _make_synthetic_gs(tmp_path, authors_spec, **gs_overrides):
    """Return a GitStats instance with injected author data for testing _compute_impact().

    authors_spec maps author name → dict with:
        commit_lines: list of (timestamp_secs, adds, dels, team_name) tuples
        team:         display team (optional, default 'Community')

    gs_overrides are applied to the GitStats instance after construction so
    individual tests can override use_net_lines, wash_window_days, etc.
    The instance has no real git repo; call only _compute_impact(), not collect().
    """
    import pathlib
    pathlib.Path(tmp_path).mkdir(parents=True, exist_ok=True)
    cfg = make_config(str(tmp_path))
    gs = gitstats.GitStats(str(tmp_path), cfg)
    for k, v in gs_overrides.items():
        setattr(gs, k, v)

    authors = {}
    teams = {}
    for name, spec in authors_spec.items():
        cls = spec['commit_lines']
        team = spec.get('team', 'Community')
        timestamps = [ts for ts, _, _, _ in cls]
        authors[name] = {
            'commits':      len(cls),
            'add':          sum(a for _, a, _, _ in cls),
            'del':          sum(d for _, _, d, _ in cls),
            'first':        min(timestamps) if timestamps else 0,
            'last':         max(timestamps) if timestamps else 0,
            'team':         team,
            'commit_lines': list(cls),
        }
        for ts, a, d, t in cls:
            if t not in teams:
                teams[t] = {
                    'commits': 0, 'add': 0, 'del': 0,
                    'members': set(), 'first': ts, 'last': ts,
                }
            teams[t]['commits'] += 1
            teams[t]['add']     += a
            teams[t]['del']     += d
            teams[t]['members'].add(name)
            teams[t]['first'] = min(teams[t]['first'], ts)
            teams[t]['last']  = max(teams[t]['last'],  ts)

    gs.data['authors'] = authors
    gs.data['teams']   = teams
    return gs


class TestImpactLogicUnit:
    """Unit tests for _compute_impact() using synthetic commit data.

    These tests bypass git entirely — author commit_lines are injected directly —
    so the formula, noise-reduction steps, and edge cases can be verified precisely.
    """

    # ── Score formula ─────────────────────────────────────────────────────────

    def test_formula_two_authors_exact(self, tmp_path):
        """Verify exact scores with two authors where Alice maximises every dimension.

        With caps/wash disabled and gross lines:
            max_commits=2, max_eff=200, max_tenure=365
            Alice: (2/2)*40 + (200/200)*40 + (365/365)*20 = 100.0
            Bob:   (2/2)*40 + (100/200)*40 + (183/365)*20 ≈ 70.0
        """
        gs = _make_synthetic_gs(tmp_path, {
            'Alice': {'commit_lines': [
                (0,            100, 0, 'C'),
                (_DAY * 365,   100, 0, 'C'),
            ]},
            'Bob': {'commit_lines': [
                (0,            50, 0, 'C'),
                (_DAY * 183,   50, 0, 'C'),
            ]},
        }, use_net_lines=False, wash_window_days=0, line_cap_percentile=0)
        gs._compute_impact()
        assert gs.data['authors']['Alice']['impact'] == 100.0
        assert gs.data['authors']['Bob']['impact'] == pytest.approx(70.0, abs=0.2)

    def test_single_author_scores_100(self, tmp_path):
        """A repository with exactly one author must score 100 (leads every dimension)."""
        gs = _make_synthetic_gs(tmp_path, {
            'Solo': {'commit_lines': [
                (0,           500, 100, 'C'),
                (_DAY * 200,  300,  50, 'C'),
            ]},
        }, wash_window_days=0, line_cap_percentile=0)
        gs._compute_impact()
        assert gs.data['authors']['Solo']['impact'] == 100.0

    # ── Edge cases (no division errors) ──────────────────────────────────────

    def test_all_zero_lines_no_division_error(self, tmp_path):
        """Authors with zero effective lines (metadata-only commits) must not crash."""
        gs = _make_synthetic_gs(tmp_path, {
            'Alice': {'commit_lines': [(0, 0, 0, 'C'), (_DAY * 100, 0, 0, 'C')]},
            'Bob':   {'commit_lines': [(0, 0, 0, 'C')]},
        }, wash_window_days=0, line_cap_percentile=0)
        gs._compute_impact()
        for a in gs.data['authors'].values():
            assert 0.0 <= a['impact'] <= 100.0

    def test_single_commit_zero_tenure(self, tmp_path):
        """An author whose first == last commit (zero tenure) must produce a valid score."""
        gs = _make_synthetic_gs(tmp_path, {
            'OneHit':  {'commit_lines': [(_DAY * 10,  100, 0, 'C')]},
            'Veteran': {'commit_lines': [(0, 50, 0, 'C'), (_DAY * 200, 50, 0, 'C')]},
        }, wash_window_days=0, line_cap_percentile=0)
        gs._compute_impact()
        for a in gs.data['authors'].values():
            assert 0.0 <= a['impact'] <= 100.0

    # ── Net vs gross lines ────────────────────────────────────────────────────

    def test_net_lines_balanced_commit_scores_zero_lines(self, tmp_path):
        """A commit with equal adds and deletes contributes 0 effective lines under net mode.

        Only the commits weight (40) contributes; the lines and tenure weights are 0.
        """
        gs = _make_synthetic_gs(tmp_path, {
            'Reformatter': {'commit_lines': [(_DAY, 1000, 1000, 'C')]},
        }, use_net_lines=True, wash_window_days=0, line_cap_percentile=0)
        gs._compute_impact()
        # tenure = 0 days → tenure term = 0; eff_lines = 0 → lines term = 0
        # score = (1/1)*40 + 0 + 0 = 40.0
        assert gs.data['authors']['Reformatter']['impact'] == pytest.approx(40.0, abs=0.1)

    def test_gross_lines_scores_both_sides(self, tmp_path):
        """With net mode off, adds+dels are both counted, so a balanced commit scores high."""
        gs_net   = _make_synthetic_gs(tmp_path / 'net', {
            'A': {'commit_lines': [(_DAY, 1000, 1000, 'C')]},
        }, use_net_lines=True,  wash_window_days=0, line_cap_percentile=0)
        gs_gross = _make_synthetic_gs(tmp_path / 'gross', {
            'A': {'commit_lines': [(_DAY, 1000, 1000, 'C')]},
        }, use_net_lines=False, wash_window_days=0, line_cap_percentile=0)
        gs_net._compute_impact()
        gs_gross._compute_impact()
        # Gross: eff=2000 → full lines weight (40) adds to commits weight (40) = ~80
        # Net:   eff=0    → lines term = 0, score ≈ 40
        assert gs_gross.data['authors']['A']['impact'] > gs_net.data['authors']['A']['impact']

    def test_net_lines_asymmetric_commit(self, tmp_path):
        """An asymmetric commit contributes abs(adds - dels) under net mode.

        A single commit → tenure=0 → tenure term=0. With one author leading on
        commits (1/1=1) and lines (full weight), score = commits_w + lines_w = 80.
        """
        gs = _make_synthetic_gs(tmp_path, {
            'A': {'commit_lines': [(_DAY, 1000, 300, 'C')]},
        }, use_net_lines=True, wash_window_days=0, line_cap_percentile=0)
        gs._compute_impact()
        wc = gitstats.GitStats.IMPACT_W_COMMITS
        wl = gitstats.GitStats.IMPACT_W_LINES
        # tenure=0 → tenure term contributes 0; commits and lines both at max
        assert gs.data['authors']['A']['impact'] == pytest.approx(wc + wl, abs=0.1)

    # ── Winsorization ─────────────────────────────────────────────────────────

    def test_winsorization_reduces_outlier_dominance(self, tmp_path):
        """A single bulk-import commit should not completely overshadow steady contributors.

        With N total commits, cap_idx = int(N * cap_pct/100). For the 95th-percentile cap
        to actually clamp the outlier we need N > 20 (otherwise cap_idx == N-1 and the
        cap equals the outlier itself). We use 20 regular commits (21 total).
        """
        # 20 regular commits of 100 lines; 1 importer commit of 1,000,000 lines → N=21
        # cap_idx = int(21 * 0.95) = 19; sorted _eff[19] = 100 → outlier capped to 100
        regular_commits = [(_DAY * i, 100, 0, 'C') for i in range(20)]
        gs_capped   = _make_synthetic_gs(tmp_path / 'cap', {
            'Regular':  {'commit_lines': regular_commits},
            'Importer': {'commit_lines': [(_DAY * 25, 1_000_000, 0, 'C')]},
        }, use_net_lines=False, wash_window_days=0, line_cap_percentile=95)
        gs_uncapped = _make_synthetic_gs(tmp_path / 'nocap', {
            'Regular':  {'commit_lines': regular_commits},
            'Importer': {'commit_lines': [(_DAY * 25, 1_000_000, 0, 'C')]},
        }, use_net_lines=False, wash_window_days=0, line_cap_percentile=0)
        gs_capped._compute_impact()
        gs_uncapped._compute_impact()
        capped_reg   = gs_capped.data['authors']['Regular']['impact']
        uncapped_reg = gs_uncapped.data['authors']['Regular']['impact']
        # Capping the outlier raises Regular's lines contribution, increasing their score.
        assert capped_reg > uncapped_reg

    def test_winsorization_disabled_at_zero_percentile(self, tmp_path):
        """line_cap_percentile=0 must disable the cap entirely (no crash, single author = 100)."""
        gs = _make_synthetic_gs(tmp_path, {
            'A': {'commit_lines': [(_DAY, 10_000, 0, 'C'), (_DAY * 100, 10_000, 0, 'C')]},
        }, line_cap_percentile=0, wash_window_days=0)
        gs._compute_impact()
        # Single author with positive commits, lines, and tenure → score = 100.
        assert gs.data['authors']['A']['impact'] == 100.0

    # ── Wash-window detection ─────────────────────────────────────────────────

    def test_wash_window_revert_pair_scores_lower(self, tmp_path):
        """A delete-then-re-add within one week bucket should score near zero for lines.

        Alice: +1000 day 1, -1000 day 3 — same 7-day bucket → effective ≈ 0.
        Bob:   three genuine commits spread across three separate weeks → effective = 600,
               and Bob dominates all three dimensions so Alice clearly scores lower.
        """
        WEEK = 7 * _DAY
        gs = _make_synthetic_gs(tmp_path, {
            'Alice': {'commit_lines': [
                (_DAY,          1000, 0,    'C'),
                (_DAY * 3,      0,    1000, 'C'),
            ]},
            'Bob': {'commit_lines': [
                (_DAY,          200, 0, 'C'),   # week 0
                (WEEK + _DAY,   200, 0, 'C'),   # week 1 (different bucket)
                (2*WEEK + _DAY, 200, 0, 'C'),   # week 2 (different bucket)
            ]},
        }, use_net_lines=True, wash_window_days=7, wash_min_gross=200, line_cap_percentile=0)
        gs._compute_impact()
        # Alice: gross=2000 > 200, min/gross=0.5 > 0.4 → washed → effective = 0
        # Bob: each bucket gross=200, 200 > 200 is False → not triggered → effective = 600
        # Bob leads on commits (3 vs 2), lines (600 vs 0), and tenure (14d vs 2d).
        assert gs.data['authors']['Alice']['impact'] < gs.data['authors']['Bob']['impact']

    def test_wash_window_below_min_gross_not_triggered(self, tmp_path):
        """A balanced bucket with gross == wash_min_gross must NOT be suppressed.

        The condition is `gross > wash_min` (strictly greater), so gross=200 with
        wash_min_gross=200 leaves the bucket intact. The commit's full gross effective
        value (200) is retained, giving the same result as with wash disabled.
        """
        gs_wash = _make_synthetic_gs(tmp_path / 'wash', {
            'A': {'commit_lines': [(_DAY, 100, 100, 'C')]},
        }, use_net_lines=False, wash_window_days=7, wash_min_gross=200, line_cap_percentile=0)
        gs_no   = _make_synthetic_gs(tmp_path / 'nowash', {
            'A': {'commit_lines': [(_DAY, 100, 100, 'C')]},
        }, use_net_lines=False, wash_window_days=0,            line_cap_percentile=0)
        gs_wash._compute_impact()
        gs_no._compute_impact()
        # gross=200 is NOT > wash_min_gross=200 → bucket intact in both cases → same score
        assert gs_wash.data['authors']['A']['impact'] == gs_no.data['authors']['A']['impact']

    def test_wash_window_separate_buckets_not_suppressed(self, tmp_path):
        """Add in week 1 and delete in week 2 (different buckets) must NOT be washed out."""
        WEEK = 7 * _DAY
        gs = _make_synthetic_gs(tmp_path, {
            'Alice': {'commit_lines': [
                (_DAY,           1000, 0,    'C'),   # bucket 0
                (WEEK + _DAY,    0,    1000, 'C'),   # bucket 1
            ]},
            'Bob': {'commit_lines': [(_DAY, 100, 0, 'C')]},
        }, use_net_lines=True, wash_window_days=7, wash_min_gross=200, line_cap_percentile=0)
        gs._compute_impact()
        # Each bucket has gross=1000, net=1000, min/gross=0 → not balanced → not washed.
        # Alice's effective = 1000+1000=2000 >> Bob's 100 → Alice scores higher.
        assert gs.data['authors']['Alice']['impact'] > gs.data['authors']['Bob']['impact']

    def test_wash_window_disabled_with_zero_days(self, tmp_path):
        """wash_window_days=0 must disable wash detection: revert pair scores full effective."""
        gs_on  = _make_synthetic_gs(tmp_path / 'on', {
            'A': {'commit_lines': [
                (_DAY,      1000, 0,    'C'),
                (_DAY * 3,  0,    1000, 'C'),
            ]},
        }, use_net_lines=True, wash_window_days=7, wash_min_gross=200, line_cap_percentile=0)
        gs_off = _make_synthetic_gs(tmp_path / 'off', {
            'A': {'commit_lines': [
                (_DAY,      1000, 0,    'C'),
                (_DAY * 3,  0,    1000, 'C'),
            ]},
        }, use_net_lines=True, wash_window_days=0, line_cap_percentile=0)
        gs_on._compute_impact()
        gs_off._compute_impact()
        # Wash on: effective ≈ 0 → score ≈ commits weight only ≈ 40
        # Wash off: effective = 1000+1000=2000 → lines term adds → higher score
        assert gs_off.data['authors']['A']['impact'] > gs_on.data['authors']['A']['impact']

    # ── Team scoring consistency ──────────────────────────────────────────────

    def test_team_wash_window_matches_author_wash_window(self, tmp_path):
        """Team effective lines must apply the same wash-window logic as author scoring.

        Setup:
          Reverts team — Alice: +5000 on day 1, -5000 on day 5 (same 7-day bucket).
          Genuine team — Bob: +300 on day 1, +300 on day 100 (different buckets).
          Both teams have 2 commits, but Genuine has much longer tenure (99 days vs 4 days).

        With wash on:  Reverts bucket is washed (gross=10000, net=0, ratio=0.5 > 0.4) →
                       team_eff['Reverts']=0; Genuine team gets 600. Genuine wins.
        With wash off: Reverts gets 10000 effective lines >> Genuine's 600. Reverts wins.
        """
        spec = {
            'Alice': {'commit_lines': [
                (_DAY,      5000, 0,    'Reverts'),   # bucket 0
                (_DAY * 5,  0,    5000, 'Reverts'),   # bucket 0 (same week)
            ], 'team': 'Reverts'},
            'Bob': {'commit_lines': [
                (_DAY,       300, 0, 'Genuine'),      # bucket 0
                (_DAY * 100, 300, 0, 'Genuine'),      # bucket 14 (different week)
            ], 'team': 'Genuine'},
        }
        gs_on  = _make_synthetic_gs(tmp_path / 'on',  spec,
                                    use_net_lines=True, wash_window_days=7,
                                    wash_min_gross=200, line_cap_percentile=0)
        gs_off = _make_synthetic_gs(tmp_path / 'off', spec,
                                    use_net_lines=True, wash_window_days=0,
                                    line_cap_percentile=0)
        gs_on._compute_impact()
        gs_off._compute_impact()

        genuine_on   = gs_on.data['teams']['Genuine']['impact']
        reverts_on   = gs_on.data['teams']['Reverts']['impact']
        genuine_off  = gs_off.data['teams']['Genuine']['impact']
        reverts_off  = gs_off.data['teams']['Reverts']['impact']

        # With wash on: Reverts washed to 0 effective lines → Genuine dominates lines+tenure.
        assert genuine_on > reverts_on
        # Without wash: Reverts gets 10000 effective lines → wins the lines dimension.
        assert reverts_off > genuine_off

    def test_team_add_del_computed_from_raw_lines(self, tmp_path):
        """Team raw add/del totals must equal the sum of their members' raw add/del."""
        gs = _make_synthetic_gs(tmp_path, {
            'Alice': {'commit_lines': [(_DAY, 300, 100, 'Core')], 'team': 'Core'},
            'Bob':   {'commit_lines': [(_DAY, 200,  50, 'Core')], 'team': 'Core'},
            'Carol': {'commit_lines': [(_DAY, 400, 200, 'Docs')], 'team': 'Docs'},
        }, wash_window_days=0, line_cap_percentile=0)
        gs._compute_impact()
        # Verify raw add/del on the data injected (before _compute_impact cleans commit_lines).
        assert gs.data['teams']['Core']['add'] == 500   # 300+200
        assert gs.data['teams']['Core']['del'] == 150   # 100+50
        assert gs.data['teams']['Docs']['add'] == 400
        assert gs.data['teams']['Docs']['del'] == 200


# ---------------------------------------------------------------------------
# Release tags
# ---------------------------------------------------------------------------

class TestReleaseTags:
    def test_max_release_tags_respected(self, std_gs):
        assert len(std_gs.data['tags']) == EXPECTED_TAGS

    def test_tags_ordered_newest_first(self, std_gs):
        """Tags should be in descending chronological order (newest first)."""
        assert std_gs.data['tags'][0]['name'] == '0.29.3'
        assert std_gs.data['tags'][-1]['name'] == '0.22.0a1'

    def test_max_release_tags_zero_means_no_limit(self, repo_path, tmp_path):
        cfg_path = make_config(tmp_path, max_release_tags=0)
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()
        all_tags = subprocess.check_output(
            ['git', '-C', repo_path, 'tag', '--sort=-creatordate']
        ).decode().splitlines()
        # Tags whose commit range is empty are intentionally excluded (they
        # carry no attribution data), so the result may be slightly less than
        # the raw tag count — but never more.
        assert len(gs.data['tags']) <= len(all_tags)
        # Every included tag must have at least one commit.
        for tag in gs.data['tags']:
            assert tag['count'] > 0

    def test_release_tag_prefix_filters_tags(self, repo_path, tmp_path):
        """Only tags matching the prefix should appear."""
        cfg_path = make_config(tmp_path, release_tag_prefix='0.27', max_release_tags=0)
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()
        for tag in gs.data['tags']:
            assert tag['name'].startswith('0.27')

    def test_release_tag_prefix_no_match_yields_empty(self, repo_path, tmp_path):
        cfg_path = make_config(tmp_path, release_tag_prefix='nonexistent-prefix-xyz', max_release_tags=0)
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()
        assert gs.data['tags'] == []

    def test_tag_team_counts_sum_to_tag_commit_count(self, std_gs):
        """For each release, the sum of per-team commit counts must equal the total."""
        for tag in std_gs.data['tags']:
            team_sum = sum(count for _, count in tag['top_teams'])
            assert team_sum == tag['count']


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

class TestComponents:
    def test_components_detected(self, std_gs):
        assert len(std_gs.data['components']) > 0

    def test_root_component_present(self, std_gs):
        """The repo root has a pyproject.toml, so '(root)' must be a component."""
        assert '(root)' in std_gs.data['components']

    def test_src_py_component_present(self, std_gs):
        assert 'src/py' in std_gs.data['components']

    def test_component_churn_positive(self, std_gs):
        for churn in std_gs.data['components'].values():
            assert churn > 0


# ---------------------------------------------------------------------------
# No-teams configuration
# ---------------------------------------------------------------------------

class TestNoTeams:
    @pytest.fixture(scope='class')
    def no_teams_gs(self, repo_path, tmp_path_factory):
        tmp = str(tmp_path_factory.mktemp('no_teams'))
        cfg_path = make_config(tmp, max_release_tags=5)
        gs = gitstats.GitStats(repo_path, cfg_path)
        gs.collect()
        return gs

    def test_has_teams_false(self, no_teams_gs):
        assert no_teams_gs.has_teams is False

    def test_all_authors_in_community(self, no_teams_gs):
        for a in no_teams_gs.data['authors'].values():
            assert a['team'] == 'Community'

    def test_single_team_in_data(self, no_teams_gs):
        assert list(no_teams_gs.data['teams'].keys()) == ['Community']

    def test_teams_tab_hidden_in_html(self, no_teams_gs, tmp_path_factory):
        tmp = str(tmp_path_factory.mktemp('html_no_teams'))
        out = os.path.join(tmp, 'report.html')
        no_teams_gs.generate_report(os.path.join(PROJECT_ROOT, 'externals'), out)
        html = open(out).read()
        # The Teams tab button must carry the hidden class.
        assert 'class="tab-btn hidden"' in html or "tab-btn hidden" in html
        # The JS constant must be false.
        assert 'const hasTeams       = false;' in html

    def test_teams_tab_visible_with_teams(self, std_gs, tmp_path_factory):
        tmp = str(tmp_path_factory.mktemp('html_with_teams'))
        out = os.path.join(tmp, 'report.html')
        std_gs.generate_report(os.path.join(PROJECT_ROOT, 'externals'), out)
        html = open(out).read()
        assert 'const hasTeams       = true;' in html
        # The Teams button must NOT be hidden.
        assert 'showTab(\'teams\',this)"    class="tab-btn"' in html or \
               'showTab(\'teams\',this)"    class="tab-btn ' in html


# ---------------------------------------------------------------------------
# Support repositories
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def recipes_repo_path(tmp_path_factory):
    """
    Create a detached git worktree of pyodide-recipes at RECIPES_LOCKED_COMMIT.
    The worktree is removed after all tests in the module complete.
    """
    wt = str(tmp_path_factory.mktemp('recipes_locked'))
    subprocess.run(
        ['git', '-C', RECIPES_REPO, 'worktree', 'add', '--detach', wt, RECIPES_LOCKED_COMMIT],
        check=True, capture_output=True,
    )
    yield wt
    subprocess.run(
        ['git', '-C', RECIPES_REPO, 'worktree', 'remove', '--force', wt],
        capture_output=True,
    )


@pytest.fixture(scope='module')
def combined_gs(repo_path, recipes_repo_path, std_config_path):
    """
    GitStats instance with pyodide as main repo and pyodide-recipes as a
    support repo, both locked to fixed commits.  Shared across all tests in
    TestSupportRepos that only need to read the result.
    """
    gs = gitstats.GitStats(repo_path, std_config_path, support_paths=[recipes_repo_path])
    gs.collect()
    return gs


class TestSupportRepos:
    # ── Combined stats ──────────────────────────────────────────────────────

    def test_combined_commit_count(self, combined_gs):
        """Total commits must equal pyodide + pyodide-recipes combined."""
        assert combined_gs.data['general']['total_commits'] == EXPECTED_COMBINED_COMMITS

    def test_combined_author_count(self, combined_gs):
        """Authors from both repos must be merged into a single set."""
        assert len(combined_gs.data['authors']) == EXPECTED_COMBINED_AUTHORS

    def test_recipes_only_authors_appear(self, combined_gs, std_gs):
        """Authors who only committed to pyodide-recipes must appear in the combined result."""
        combined = set(combined_gs.data['authors'].keys())
        main_only = set(std_gs.data['authors'].keys())
        new_authors = combined - main_only
        # pyodide-recipes adds 19 authors not present in pyodide alone.
        assert len(new_authors) == 19

    def test_shared_author_commits_accumulate(self, combined_gs, std_gs):
        """An author who committed to both repos must have a higher combined commit count."""
        # Agriya Khetarpal is in config.json (Build & Packaging) and has commits in both repos.
        main_commits = std_gs.data['authors']['Agriya Khetarpal']['commits']
        combined_commits = combined_gs.data['authors']['Agriya Khetarpal']['commits']
        assert combined_commits > main_commits

    # ── Main-repo-only fields ───────────────────────────────────────────────

    def test_file_count_is_main_repo_only(self, combined_gs, std_gs):
        """File count must equal the main repo count regardless of support repos."""
        assert combined_gs.data['general']['total_files'] == std_gs.data['general']['total_files']

    def test_loc_history_is_main_repo_only(self, combined_gs, std_gs):
        """LOC history must be identical to the main-repo-only result."""
        assert combined_gs.data['loc_history'] == std_gs.data['loc_history']

    def test_age_days_is_main_repo_only(self, combined_gs, std_gs):
        """Repo age must reflect the main repo span, not the support repo."""
        assert combined_gs.data['general']['age_days'] == std_gs.data['general']['age_days']

    def test_total_lines_is_main_repo_only(self, combined_gs, std_gs):
        """Net line count must derive from the main repo only."""
        assert combined_gs.data['general']['total_lines'] == std_gs.data['general']['total_lines']

    def test_release_tags_are_main_repo_only(self, combined_gs, std_gs):
        """Release tags must be unchanged when a support repo is added."""
        assert len(combined_gs.data['tags']) == len(std_gs.data['tags'])
        assert combined_gs.data['tags'][0]['name'] == std_gs.data['tags'][0]['name']

    # ── Support repo component data ─────────────────────────────────────────

    def test_support_repo_entry_present(self, combined_gs):
        """data['support_repos'] must have one entry for the support repo."""
        assert len(combined_gs.data['support_repos']) == 1

    def test_support_repo_has_components(self, combined_gs):
        """The support repo must have detected at least one component."""
        sr = combined_gs.data['support_repos'][0]
        assert len(sr['components']) > 0

    def test_support_repo_components_separate_from_main(self, combined_gs):
        """Support repo components must not appear in the main repo component dict."""
        main_keys = set(combined_gs.data['components'].keys())
        sr_keys   = set(combined_gs.data['support_repos'][0]['components'].keys())
        # The two component sets may share names (e.g. '(root)') but they are
        # stored in separate dicts, so their values are independent.
        assert isinstance(main_keys, set)
        assert isinstance(sr_keys, set)

    # ── Single-repo regression ───────────────────────────────────────────────

    def test_no_support_repos_list_is_empty(self, std_gs):
        """A GitStats instance without support_paths must have an empty support_repos list."""
        assert std_gs.data['support_repos'] == []

    def test_single_repo_commit_count_unchanged(self, std_gs):
        """Adding a support repo to a different instance must not affect single-repo counts."""
        assert std_gs.data['general']['total_commits'] == EXPECTED_COMMITS

    # ── HTML output ─────────────────────────────────────────────────────────

    def test_html_support_repo_component_card(self, combined_gs, recipes_repo_path, tmp_path_factory):
        """The generated HTML must contain a separate component card for the support repo."""
        tmp = str(tmp_path_factory.mktemp('html_combined'))
        out = os.path.join(tmp, 'report.html')
        combined_gs.generate_report(os.path.join(PROJECT_ROOT, 'externals'), out)
        html = open(out).read()
        sr_name = os.path.basename(os.path.abspath(recipes_repo_path))
        # The support repo card must have a canvas with id="componentChart-support-0"
        assert 'id="componentChart-support-0"' in html
        # The support repo pill must appear on the Summary tab header.
        assert f'+ {sr_name}' in html
        # Both "Main Repository" and "Support Repository" labels must appear.
        assert 'Main Repository' in html
        assert 'Support Repository' in html


# ---------------------------------------------------------------------------
# Component markers
# ---------------------------------------------------------------------------

class TestComponentMarkers:
    """Verify that the component_markers config key controls which directories
    are detected as component roots in the Components tab churn chart."""

    # ── Data-layer correctness ───────────────────────────────────────────────

    def test_empty_markers_yields_no_components(self, repo_path, tmp_path):
        """Setting component_markers to [] must disable all component detection."""
        cfg = make_config(tmp_path, component_markers=[])
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        assert len(gs.data['components']) == 0

    def test_pyproject_toml_only_detects_expected_dirs(self, repo_path, tmp_path):
        """Using only 'pyproject.toml' as a marker must detect exactly the
        directories that contain a pyproject.toml — and no others."""
        cfg = make_config(tmp_path, component_markers=['pyproject.toml'])
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        components = set(gs.data['components'].keys())
        # src/py and (root) both have pyproject.toml in the locked repo.
        assert '(root)' in components
        assert 'src/py' in components
        # cpython, docs, emsdk, packages are only reachable via Makefile/other markers.
        assert 'cpython' not in components
        assert 'docs' not in components

    def test_makefile_only_detects_expected_dirs(self, repo_path, tmp_path):
        """Using only 'Makefile' as a marker must detect exactly the directories
        that contain a Makefile."""
        cfg = make_config(tmp_path, component_markers=['Makefile'])
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        components = set(gs.data['components'].keys())
        # (root), cpython, docs, emsdk, packages all have Makefiles.
        assert '(root)' in components
        assert 'cpython' in components
        assert 'docs' in components
        assert 'emsdk' in components
        assert 'packages' in components
        # src/py is only reachable via pyproject.toml, not Makefile.
        assert 'src/py' not in components

    def test_custom_markers_subset_of_default(self, repo_path, tmp_path_factory):
        """A single-marker config must produce fewer components than the full default set."""
        ptoml_dir = str(tmp_path_factory.mktemp('ptoml'))
        default_dir = str(tmp_path_factory.mktemp('default'))
        cfg_ptoml   = make_config(ptoml_dir,   component_markers=['pyproject.toml'])
        cfg_default = make_config(default_dir)  # no component_markers key → uses class default

        gs_ptoml = gitstats.GitStats(repo_path, cfg_ptoml)
        gs_ptoml.collect()
        gs_default = gitstats.GitStats(repo_path, cfg_default)
        gs_default.collect()

        # pyproject.toml alone finds 9 components; the full default finds 37.
        assert len(gs_ptoml.data['components']) == 9
        assert len(gs_default.data['components']) == 37
        # Every pyproject.toml-detected component must also appear in the default set.
        assert set(gs_ptoml.data['components']).issubset(set(gs_default.data['components']))

    def test_omitting_config_key_uses_class_default(self, repo_path, tmp_path):
        """When 'component_markers' is absent from config, COMPONENT_MARKERS is used."""
        cfg = make_config(tmp_path)  # no component_markers key
        gs = gitstats.GitStats(repo_path, cfg)
        assert gs.component_markers == gitstats.GitStats.COMPONENT_MARKERS

    def test_config_key_replaces_default_entirely(self, repo_path, tmp_path):
        """Providing component_markers in config must replace the default set, not extend it."""
        cfg = make_config(tmp_path, component_markers=['pyproject.toml'])
        gs = gitstats.GitStats(repo_path, cfg)
        assert gs.component_markers == {'pyproject.toml'}
        assert 'Makefile' not in gs.component_markers

    # ── HTML output ──────────────────────────────────────────────────────────

    def test_html_chart_labels_reflect_custom_markers(self, repo_path, tmp_path_factory):
        """The repoCharts JSON in the generated HTML must contain exactly the
        component labels produced by the active marker set."""
        tmp = str(tmp_path_factory.mktemp('html_markers'))
        cfg = make_config(tmp, component_markers=['pyproject.toml'])
        out = os.path.join(tmp, 'report.html')
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        gs.generate_report(os.path.join(PROJECT_ROOT, 'externals'), out)
        html = open(out).read()

        # src/py is detected by pyproject.toml; must appear in the chart data.
        assert '"src/py"' in html
        # cpython is only detected by Makefile; must be absent from the chart data.
        assert '"cpython"' not in html

    def test_html_no_components_card_when_empty_markers(self, repo_path, tmp_path_factory):
        """With zero components detected the chart canvas must still be rendered
        (empty chart), and the repoCharts labels list must be empty."""
        tmp = str(tmp_path_factory.mktemp('html_empty'))
        cfg = make_config(tmp, component_markers=[])
        out = os.path.join(tmp, 'report.html')
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        gs.generate_report(os.path.join(PROJECT_ROOT, 'externals'), out)
        html = open(out).read()

        # The main repo chart card must still be present.
        assert 'id="componentChart-main"' in html
        # The labels array for the main repo chart must be empty.
        assert '"labels": []' in html or '"labels":[]' in html


# ---------------------------------------------------------------------------
# Summary tab
# ---------------------------------------------------------------------------

class TestSummaryTab:
    """Verify the Summary tab data and HTML.

    Covers:
      - collect() stores first/last commit timestamps and true tag count before cap.
      - __init__ reads summary_velocity_days and bus_factor_threshold from config,
        with correct defaults.
      - Bus factor computation: monotonicity w.r.t. threshold and the core invariant
        (top N contributors cover the threshold; top N-1 do not).
      - generate_report() renders the Summary tab, removes the Activity tab, produces
        velocity cards matching the configured windows, and retains the punchcard.
    """

    # ── Class-scoped HTML fixture ────────────────────────────────────────────

    @pytest.fixture(scope='class')
    def std_html(self, std_gs, tmp_path_factory):
        """Generate the standard-config report HTML once for all HTML-checking tests."""
        tmp = str(tmp_path_factory.mktemp('summary_std_html'))
        out = os.path.join(tmp, 'report.html')
        std_gs.generate_report(os.path.join(PROJECT_ROOT, 'externals'), out)
        return open(out).read()

    # ── Data collected by collect() ──────────────────────────────────────────

    def test_first_commit_ts_stored(self, std_gs):
        """collect() must store a positive first_commit_ts in general."""
        assert std_gs.data['general']['first_commit_ts'] > 0

    def test_last_commit_ts_at_or_after_first(self, std_gs):
        """last_commit_ts must be greater than or equal to first_commit_ts."""
        g = std_gs.data['general']
        assert g['last_commit_ts'] >= g['first_commit_ts']

    def test_age_days_consistent_with_timestamps(self, std_gs):
        """age_days must equal (last_commit_ts - first_commit_ts) // 86400."""
        g = std_gs.data['general']
        expected = (g['last_commit_ts'] - g['first_commit_ts']) // 86400
        assert g['age_days'] == expected

    def test_total_tags_stored_before_cap(self, repo_path, tmp_path):
        """total_tags must reflect the true tag count before max_release_tags truncation.

        make_config defaults to max_release_tags=10.  Pyodide has well over 10 tags,
        so total_tags must be strictly greater than the displayed 5.
        """
        cfg = make_config(tmp_path, max_release_tags=5)
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        assert len(gs.data['tags']) == 5
        assert gs.data['general']['total_tags'] > 5

    def test_total_tags_no_cap_geq_shown(self, repo_path, tmp_path):
        """With max_release_tags=0 (unlimited), total_tags must be >= len(data['tags']).

        total_tags counts every raw git tag before filtering; data['tags'] only
        contains tags whose commit range has at least one commit, so the raw count
        may be slightly higher (tags with no commits in their range are excluded).
        """
        cfg = make_config(tmp_path, max_release_tags=0)
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        assert gs.data['general']['total_tags'] >= len(gs.data['tags'])

    # ── Config defaults and overrides ────────────────────────────────────────

    def test_default_velocity_days(self, repo_path, tmp_path):
        """summary_velocity_days must default to [30, 90] when absent from config."""
        cfg = make_config(tmp_path)
        gs = gitstats.GitStats(repo_path, cfg)
        assert gs.summary_velocity_days == [30, 90]

    def test_default_bus_factor_threshold(self, repo_path, tmp_path):
        """bus_factor_threshold must default to 0.5 when absent from config."""
        cfg = make_config(tmp_path)
        gs = gitstats.GitStats(repo_path, cfg)
        assert gs.bus_factor_threshold == 0.5

    def test_custom_velocity_days_from_config(self, repo_path, tmp_path):
        """summary_velocity_days in config must override the default list."""
        cfg = make_config(tmp_path, summary_velocity_days=[7, 365])
        gs = gitstats.GitStats(repo_path, cfg)
        assert gs.summary_velocity_days == [7, 365]

    def test_custom_bus_factor_threshold_from_config(self, repo_path, tmp_path):
        """bus_factor_threshold in config must override the default value."""
        cfg = make_config(tmp_path, bus_factor_threshold=0.8)
        gs = gitstats.GitStats(repo_path, cfg)
        assert gs.bus_factor_threshold == 0.8

    # ── Bus factor computation ───────────────────────────────────────────────

    @staticmethod
    def _compute_bus_factor(gs, threshold):
        """Return the bus factor count for a collected GitStats instance."""
        sorted_a = sorted(
            gs.data['authors'].items(), key=lambda x: x[1]['commits'], reverse=True
        )
        total = sum(a['commits'] for _, a in sorted_a) or 1
        cutoff = total * threshold
        running = 0
        for i, (_, a) in enumerate(sorted_a, 1):
            running += a['commits']
            if running >= cutoff:
                return i
        return len(sorted_a)

    def test_bus_factor_invariant(self, std_gs):
        """Top N bus-factor contributors must reach the 50% threshold; top N-1 must not."""
        threshold = 0.5
        sorted_a = sorted(
            std_gs.data['authors'].items(), key=lambda x: x[1]['commits'], reverse=True
        )
        total = sum(a['commits'] for _, a in sorted_a)
        cutoff = total * threshold
        n = self._compute_bus_factor(std_gs, threshold)

        top_n_commits = sum(a['commits'] for _, a in sorted_a[:n])
        assert top_n_commits >= cutoff, "Top N contributors must reach the threshold"

        if n > 1:
            top_n_minus_1 = sum(a['commits'] for _, a in sorted_a[:n - 1])
            assert top_n_minus_1 < cutoff, "Top N-1 contributors must not reach the threshold"

    def test_bus_factor_monotone_with_threshold(self, std_gs):
        """A lower threshold requires no more contributors than a higher one."""
        bf_10 = self._compute_bus_factor(std_gs, 0.1)
        bf_50 = self._compute_bus_factor(std_gs, 0.5)
        bf_90 = self._compute_bus_factor(std_gs, 0.9)
        assert bf_10 <= bf_50 <= bf_90

    def test_bus_factor_top_author_exceeds_10_pct(self, std_gs):
        """Hood Chatham holds ~34.7% of commits, so threshold=0.1 yields bus factor 1."""
        sorted_a = sorted(
            std_gs.data['authors'].items(), key=lambda x: x[1]['commits'], reverse=True
        )
        total = sum(a['commits'] for _, a in sorted_a)
        top_share = sorted_a[0][1]['commits'] / total
        # Top author alone exceeds 10%, so bus factor at 0.1 must be exactly 1.
        assert top_share >= 0.1
        assert self._compute_bus_factor(std_gs, 0.1) == 1

    # ── HTML output ──────────────────────────────────────────────────────────

    def test_summary_tab_present(self, std_html):
        """The Summary tab div must be present in the generated HTML."""
        assert 'id="tab-summary"' in std_html

    def test_activity_tab_removed(self, std_html):
        """The old Activity tab div must not appear in the generated HTML."""
        assert 'id="tab-activity"' not in std_html

    def test_summary_nav_button_present(self, std_html):
        """The nav bar must use showTab('summary') and not showTab('activity')."""
        assert "showTab('summary'" in std_html
        assert "showTab('activity'" not in std_html

    def test_punchcard_canvas_retained_in_summary(self, std_html):
        """The hourChart canvas must be present — punchcard is kept in the Summary tab."""
        assert 'id="hourChart"' in std_html

    def test_heatmap_and_loc_chart_removed(self, std_html):
        """The heatmap div and LOC history canvas must no longer appear."""
        assert 'id="heatmap"' not in std_html
        assert 'id="locChart"' not in std_html

    def test_bus_factor_section_in_html(self, std_html):
        """The Bus Factor heading must appear in the Summary tab."""
        assert 'Bus Factor' in std_html

    def test_velocity_cards_match_default_config(self, std_html):
        """Default velocity config [30, 90] must produce 'Last 30 Days' and 'Last 90 Days'."""
        assert 'Last 30 Days' in std_html
        assert 'Last 90 Days' in std_html

    def test_custom_velocity_days_produce_matching_labels(self, repo_path, tmp_path_factory):
        """Custom summary_velocity_days must produce exactly the configured day-window labels."""
        tmp = str(tmp_path_factory.mktemp('vel_custom_html'))
        cfg = make_config(tmp, summary_velocity_days=[7, 180])
        out = os.path.join(tmp, 'report.html')
        gs = gitstats.GitStats(repo_path, cfg)
        gs.collect()
        gs.generate_report(os.path.join(PROJECT_ROOT, 'externals'), out)
        html = open(out).read()

        assert 'Last 7 Days' in html
        assert 'Last 180 Days' in html
        # The default 30-day and 90-day labels must not appear.
        assert 'Last 30 Days' not in html
        assert 'Last 90 Days' not in html

    def test_summary_stats_tiles_present(self, std_html):
        """The four summary stat tiles (Days Old, Lines of Code, Commits / Week, Releases) must appear."""
        assert 'Days Old' in std_html
        assert 'Lines of Code' in std_html
        assert 'Commits / Week' in std_html
        assert 'Releases' in std_html

    def test_summary_tile_says_lines_of_code_not_net_lines(self, std_html):
        """The summary tab tile label must be 'Lines of Code', not the old 'Net Lines'.

        'Net Lines' still appears in the global header (historical net LOC from git history),
        so we verify that 'Lines of Code' appears inside the tab-summary div specifically.
        """
        summary_start = std_html.find('id="tab-summary"')
        assert summary_start != -1
        # Find the closing boundary of the summary tab section.
        # The next tab div begins with id="tab-impact".
        summary_end = std_html.find('id="tab-impact"', summary_start)
        summary_section = std_html[summary_start:summary_end] if summary_end != -1 else std_html[summary_start:]
        assert 'Lines of Code' in summary_section
        assert 'Net Lines' not in summary_section

    # ── Data-layer: total_repo_lines ─────────────────────────────────────────

    def test_total_repo_lines_positive(self, std_gs):
        """total_repo_lines must be a positive integer after collect().

        This field is the actual newline count of all tracked files — not the
        historical net additions/deletions — and must be well above zero for pyodide.
        """
        assert std_gs.data['general']['total_repo_lines'] > 0

    def test_total_repo_lines_differs_from_total_lines(self, std_gs):
        """total_repo_lines (file newline count) must differ from total_lines (net git LOC).

        total_lines is computed as running additions minus deletions across the full git
        history and will undercount the actual file content. The two values represent
        different measurements and must not be equal for a real-world repository.
        """
        repo_lines = std_gs.data['general']['total_repo_lines']
        net_lines  = std_gs.data['general']['total_lines']
        assert repo_lines != net_lines

    def test_total_repo_lines_is_main_repo_only(self, combined_gs, std_gs):
        """total_repo_lines must be identical with or without support repos.

        Support repo file content is not counted; only the main repo's tracked files
        contribute to this metric.
        """
        assert combined_gs.data['general']['total_repo_lines'] == std_gs.data['general']['total_repo_lines']

if __name__ == "__main__":
    sys.exit(pytest.main())
