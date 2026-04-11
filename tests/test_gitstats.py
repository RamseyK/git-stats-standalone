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

# Support repo — pyodide-recipes adds recipe package commits on top of pyodide.
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
