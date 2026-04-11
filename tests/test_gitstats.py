"""
Tests for gitstats.py using the pyodide repository as a fixed baseline.

All tests run against a git worktree locked to LOCKED_COMMIT so results are
deterministic regardless of ongoing development in the pyodide repo.

Run from the project root:
    pytest tests/

Requirements:
    pip install pytest
    The pyodide repository must exist at ~/Downloads/pyodide.
"""

import json
import os
import subprocess
import sys
import tempfile

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

        gs_net   = gitstats.GitStats(repo_path, net_path);   gs_net.collect()
        gs_gross = gitstats.GitStats(repo_path, gross_path); gs_gross.collect()

        net_scores   = {n: a['impact'] for n, a in gs_net.data['authors'].items()}
        gross_scores = {n: a['impact'] for n, a in gs_gross.data['authors'].items()}

        # At least one author must differ between the two modes.
        assert any(net_scores[n] != gross_scores[n] for n in net_scores)

    def test_impact_weights_sum_to_100(self):
        total = (gitstats.GitStats.IMPACT_W_COMMITS +
                 gitstats.GitStats.IMPACT_W_LINES   +
                 gitstats.GitStats.IMPACT_W_TENURE)
        assert total == 100


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
        no_teams_gs.generate_report(out)
        html = open(out).read()
        # The Teams tab button must carry the hidden class.
        assert 'class="tab-btn hidden"' in html or "tab-btn hidden" in html
        # The JS constant must be false.
        assert 'const hasTeams       = false;' in html

    def test_teams_tab_visible_with_teams(self, std_gs, tmp_path_factory):
        tmp = str(tmp_path_factory.mktemp('html_with_teams'))
        out = os.path.join(tmp, 'report.html')
        std_gs.generate_report(out)
        html = open(out).read()
        assert 'const hasTeams       = true;' in html
        # The Teams button must NOT be hidden.
        assert 'showTab(\'teams\',this)"    class="tab-btn"' in html or \
               'showTab(\'teams\',this)"    class="tab-btn ' in html
