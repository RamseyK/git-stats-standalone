#!/usr/bin/env python3
"""
generate_screenshots.py — Build a demo repo, run gitstats, screenshot each tab.

Builds a synthetic git repository with a realistic multi-author history (6
contributors across 3 teams, 5 releases, 3 components), runs gitstats to
produce an HTML report, then uses Chrome headless to screenshot each tab for
use in documentation.

Usage:
    python generate_screenshots.py [--out DIR]

Requirements:
    Google Chrome installed at the default macOS path.

Output (default ./screenshots/):
    01_summary.png
    02_impact.png
    03_contributors.png
    04_teams.png
    05_releases.png
    06_components.png
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import gitstats  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCREENSHOT_WIDTH  = 1440
SCREENSHOT_HEIGHT = 900

# (tab_id, button_label, output_filename_stem)
TABS = [
    ("summary",    "Summary",    "01_summary"),
    ("impact",     "Impact",     "02_impact"),
    ("authors",    "Authors",    "03_contributors"),
    ("teams",      "Teams",      "04_teams"),
    ("tags",       "Releases",   "05_releases"),
    ("components", "Components", "06_components"),
]

# ── Demo repository configuration ─────────────────────────────────────────────

DEMO_CONFIG = {
    "release_tag_prefix": "v",
    "max_release_tags":   10,
    "primary_branch":     "main",
    "teams": {
        "Core": {
            "members": [
                "Alice Chen",    "alice@example.com",
                "Bob Martinez",  "bob@example.com",
            ],
            "color": "#3b82f6",
        },
        "Platform": {
            "members": [
                "Carol Williams", "carol@example.com",
                "David Park",     "david@example.com",
            ],
            "color": "#10b981",
        },
        "Community": {
            "members": [
                "Eve Johnson", "eve@example.com",
                "Frank Lee",   "frank@example.com",
                "Grace Kim",   "grace@example.com",
            ],
            "color": "#f59e0b",
        },
    },
    "aliases": {},
    "impact_w_commits": 35,
    "impact_w_lines":   35,
    "impact_w_tenure":  15,
    "impact_w_merges":  15,
    "impact_use_net_lines":       True,
    "impact_wash_window_days":    7,
    "impact_wash_min_gross":      50,
    "impact_line_cap_percentile": 95,
    "summary_velocity_days": [30, 90],
    "monthly_top_authors":   3,
    "bus_factor_threshold":  0.5,
    "component_markers": ["pyproject.toml"],
    "loc_extensions":    [".py"],
}

# ── Source content generators ──────────────────────────────────────────────────

def _pyproject(name: str, version: str) -> str:
    return (
        f'[project]\n'
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f'description = "The {name} component."\n'
        f'requires-python = ">=3.11"\n'
    )


def _module(component: str, n: int) -> str:
    """Return fake Python source with n functions for the named component."""
    VERBS   = ["process", "validate", "transform", "apply", "compute",
                "resolve", "dispatch", "aggregate", "normalize", "emit"]
    OBJECTS = ["payload", "context", "record", "batch", "event",
               "config",  "request", "response", "token",  "result"]
    lines = [
        f'"""{component.title()} module."""',
        "",
        "from __future__ import annotations",
        "import os",
        "import sys",
        "import logging",
        "from typing import Any, Dict, List, Optional, Tuple",
        "",
        f"log = logging.getLogger(__name__)",
        "",
    ]
    for i in range(n):
        verb = VERBS[i % len(VERBS)]
        obj  = OBJECTS[i % len(OBJECTS)]
        fn   = f"{verb}_{obj}"
        lines += [
            "",
            f"def {fn}(",
            f"    value: Any,",
            f"    options: Optional[Dict[str, Any]] = None,",
            f"    *,",
            f"    strict: bool = False,",
            f") -> Any:",
            f'    """{component.title()}: {verb} the {obj}.',
            f"",
            f"    Args:",
            f"        value:   Input {obj} to {verb}.",
            f"        options: Optional configuration overrides.",
            f"        strict:  Raise on validation failure when True.",
            f"",
            f"    Returns:",
            f"        Processed {obj} or None on soft failure.",
            f'    """',
            f"    if options is None:",
            f"        options = {{}}",
            f"    log.debug('%s called with %r', '{fn}', value)",
            f"    result: Any = value",
            f"    for key, setting in options.items():",
            f"        if strict and setting is None:",
            f"            raise ValueError(f'Required option {{key!r}} is missing')",
            f"        result = _apply(result, key, setting)",
            f"    return result",
            "",
            "",
            f"def _{fn}_internal(state: Dict[str, Any]) -> Dict[str, Any]:",
            f'    """Internal helper for {fn}."""',
            f"    return {{k: v for k, v in state.items() if v is not None}}",
        ]
    lines += ["", "",
              "def _apply(value: Any, key: str, setting: Any) -> Any:",
              '    """Apply a single configuration key to value."""',
              "    return value",
              ""]
    return "\n".join(lines)


def _test_module(subject: str, n: int) -> str:
    """Return fake pytest test source with n test functions."""
    lines = [
        f'"""Tests for the {subject} component."""',
        "",
        "import pytest",
        f"from {subject} import *",
        "",
    ]
    VERBS = ["returns", "raises", "handles", "validates", "processes",
             "transforms", "rejects", "accepts", "converts", "emits"]
    for i in range(n):
        verb = VERBS[i % len(VERBS)]
        lines += [
            "",
            f"def test_{subject}_{verb}_{i:02d}():",
            f'    """Verify {subject} {verb} case {i:02d}."""',
            f"    value = object()",
            f"    result = process_payload(value)",
            f"    assert result is not None",
        ]
    lines.append("")
    return "\n".join(lines)


def _readme(version: str, features: List[str]) -> str:
    lines = [
        "# Demo Project",
        "",
        f"Current release: **{version}**",
        "",
        "A realistic multi-team application platform demonstrating gitstats.",
        "",
        "## Features",
        "",
    ]
    for feat in features:
        lines.append(f"- {feat}")
    lines += [
        "",
        "## Quick Start",
        "",
        "```bash",
        "pip install demo-project",
        "demo-project --help",
        "```",
        "",
        "## Components",
        "",
        "| Component | Description |",
        "|-----------|-------------|",
        "| core      | Authentication, engine, scheduling |",
        "| platform  | API layer, configuration, middleware |",
        "| tests     | Integration and unit test suite |",
        "",
    ]
    return "\n".join(lines)


# ── Repository builder ─────────────────────────────────────────────────────────

def build_demo_repo(repo_dir: str) -> None:
    """Populate repo_dir with a synthetic multi-author git history."""

    _no_sign = {
        "GIT_CONFIG_COUNT":   "1",
        "GIT_CONFIG_KEY_0":   "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false",
    }

    def git(*args, name="Alice Chen", email="alice@example.com",
            date="2023-01-01T12:00:00"):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME":     name,
            "GIT_AUTHOR_EMAIL":    email,
            "GIT_COMMITTER_NAME":  name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE":     date,
            "GIT_COMMITTER_DATE":  date,
            **_no_sign,
        }
        subprocess.check_call(
            ["git", "-C", repo_dir] + list(args),
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def write(rel_path: str, content: str) -> None:
        full = os.path.join(repo_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        pathlib.Path(full).write_text(content, encoding="utf-8")

    def commit(msg, files, name, email, date):
        for path, content in files.items():
            write(path, content)
        git("add", ".")
        git("commit", "-m", msg, name=name, email=email, date=date)

    def merge_pr(pr_num, branch, title, name, email, date):
        """Merge feature branch into main as a GitHub-style PR merge."""
        git("checkout", "-b", branch, name=name, email=email, date=date)
        # commit already placed on branch by caller; switch back and merge
        git("checkout", "main", name=name, email=email, date=date)
        git("merge", "--no-ff", branch,
            "-m", f"Merge pull request #{pr_num} from {email.split('@')[0]}/{branch}",
            name=name, email=email, date=date)
        git("branch", "-d", branch, name=name, email=email, date=date)

    def tag(name_tag, date, tagger="Alice Chen", tagger_email="alice@example.com"):
        env = {
            **os.environ,
            "GIT_COMMITTER_NAME":  tagger,
            "GIT_COMMITTER_EMAIL": tagger_email,
            "GIT_COMMITTER_DATE":  date,
            **_no_sign,
        }
        subprocess.check_call(
            ["git", "-C", repo_dir, "tag", name_tag],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # Shortcuts for each contributor
    A = dict(name="Alice Chen",    email="alice@example.com")
    B = dict(name="Bob Martinez",  email="bob@example.com")
    C = dict(name="Carol Williams",email="carol@example.com")
    D = dict(name="David Park",    email="david@example.com")
    E = dict(name="Eve Johnson",   email="eve@example.com")
    F = dict(name="Frank Lee",     email="frank@example.com")
    G = dict(name="Grace Kim",     email="grace@example.com")

    # ── Init ──────────────────────────────────────────────────────────────────
    git("init", "-b", "main")
    git("config", "user.email", "alice@example.com")
    git("config", "user.name",  "Alice Chen")

    # ── Phase 1: Foundations (2023-01 – 2023-05, toward v1.0) ─────────────────

    commit("Initial project structure and core skeleton", {
        "README.md": _readme("0.1.0-dev", ["Authentication", "API layer"]),
        "core/pyproject.toml":     _pyproject("core",     "0.1.0"),
        "platform/pyproject.toml": _pyproject("platform", "0.1.0"),
        "tests/pyproject.toml":    _pyproject("tests",    "0.1.0"),
        "core/auth.py":     _module("auth",     4),
        "platform/api.py":  _module("api",      3),
        "tests/test_auth.py": _test_module("auth", 4),
    }, date="2023-01-05T10:00:00", **A)

    commit("Add core engine with basic dispatch loop", {
        "core/engine.py": _module("engine", 5),
    }, date="2023-01-12T11:00:00", **A)

    commit("Platform configuration module", {
        "platform/config.py": _module("config", 4),
    }, date="2023-01-18T14:00:00", **C)

    commit("Extend auth module: token validation and refresh", {
        "core/auth.py": _module("auth", 7),
    }, date="2023-01-25T09:30:00", **A)

    commit("Add API endpoint handlers", {
        "platform/api.py": _module("api", 6),
    }, date="2023-02-02T10:00:00", **C)

    commit("Initial test coverage for platform config", {
        "tests/test_api.py": _test_module("api", 5),
    }, date="2023-02-08T15:00:00", **E)

    commit("Engine: add event queue and retry logic", {
        "core/engine.py": _module("engine", 8),
    }, date="2023-02-14T10:30:00", **B)

    commit("Auth: session management and expiry", {
        "core/auth.py": _module("auth", 9),
    }, date="2023-02-20T11:00:00", **A)

    commit("Config: environment-based overrides", {
        "platform/config.py": _module("config", 7),
    }, date="2023-02-27T13:00:00", **D)

    commit("Test: expand auth coverage to edge cases", {
        "tests/test_auth.py": _test_module("auth", 8),
    }, date="2023-03-06T09:00:00", **E)

    commit("Engine: telemetry hooks and structured logging", {
        "core/engine.py": _module("engine", 11),
    }, date="2023-03-13T10:00:00", **B)

    commit("API: rate limiting and request validation", {
        "platform/api.py": _module("api", 9),
    }, date="2023-03-20T14:00:00", **C)

    commit("Auth: add PKCE and OAuth2 code flow", {
        "core/auth.py": _module("auth", 12),
    }, date="2023-04-03T10:00:00", **A)

    commit("Config: add hot-reload support", {
        "platform/config.py": _module("config", 9),
    }, date="2023-04-10T11:30:00", **D)

    commit("Tests: integration test scaffolding", {
        "tests/test_integration.py": _test_module("integration", 6),
    }, date="2023-04-17T09:00:00", **F)

    commit("Engine: graceful shutdown and drain", {
        "core/engine.py": _module("engine", 13),
    }, date="2023-04-24T10:00:00", **B)

    commit("API: pagination and cursor support", {
        "platform/api.py": _module("api", 12),
    }, date="2023-05-02T11:00:00", **C)

    commit("Auth: RBAC permission checks", {
        "core/auth.py": _module("auth", 14),
    }, date="2023-05-09T10:30:00", **A)

    commit("README: update feature list for v1.0", {
        "README.md": _readme("1.0.0", [
            "OAuth2 / PKCE authentication",
            "RBAC permission model",
            "Paginated REST API",
            "Hot-reload configuration",
            "Structured telemetry",
        ]),
    }, date="2023-05-16T14:00:00", **A)

    commit("Tests: raise API coverage to 80%", {
        "tests/test_api.py":         _test_module("api", 10),
        "tests/test_integration.py": _test_module("integration", 9),
    }, date="2023-05-23T09:00:00", **E)

    commit("Fix: engine retry storm under high load", {
        "core/engine.py": _module("engine", 14),
    }, date="2023-05-30T10:00:00", **B)

    tag("v1.0", "2023-06-01T12:00:00")

    # ── Phase 2: v1.1 patch cycle (2023-06 – 2023-09) ─────────────────────────

    commit("Auth: fix token clock-skew on expiry check", {
        "core/auth.py": _module("auth", 15),
    }, date="2023-06-07T10:00:00", **A)

    commit("API: fix 500 on malformed JSON body", {
        "platform/api.py": _module("api", 13),
    }, date="2023-06-13T11:00:00", **C)

    commit("Config: fix reload race condition", {
        "platform/config.py": _module("config", 10),
    }, date="2023-06-20T14:00:00", **D)

    commit("Engine: add circuit-breaker pattern", {
        "core/engine.py": _module("engine", 15),
    }, date="2023-06-27T10:00:00", **B)

    commit("Tests: regression tests for v1.0 bugs", {
        "tests/test_auth.py":        _test_module("auth", 11),
        "tests/test_api.py":         _test_module("api", 12),
    }, date="2023-07-05T09:00:00", **G)

    commit("Auth: cache warm-up on startup", {
        "core/auth.py": _module("auth", 16),
    }, date="2023-07-11T10:30:00", **A)

    commit("API: CORS preflight handling", {
        "platform/api.py": _module("api", 14),
    }, date="2023-07-18T11:00:00", **C)

    commit("Engine: add dead-letter queue", {
        "core/engine.py": _module("engine", 16),
    }, date="2023-07-25T10:00:00", **B)

    commit("Tests: add performance benchmarks", {
        "tests/test_integration.py": _test_module("integration", 12),
    }, date="2023-08-02T09:00:00", **F)

    commit("Config: schema validation with jsonschema", {
        "platform/config.py": _module("config", 12),
    }, date="2023-08-09T11:00:00", **D)

    commit("Auth: MFA support (TOTP)", {
        "core/auth.py": _module("auth", 18),
    }, date="2023-08-16T10:00:00", **A)

    commit("Fix: API timeout not propagated to upstream calls", {
        "platform/api.py": _module("api", 15),
    }, date="2023-08-23T14:30:00", **C)

    commit("README: v1.1 changelog and migration notes", {
        "README.md": _readme("1.1.0", [
            "OAuth2 / PKCE authentication",
            "RBAC permission model",
            "MFA (TOTP) support",
            "Circuit-breaker pattern",
            "Dead-letter queue",
            "CORS preflight handling",
            "JSON schema validation",
        ]),
    }, date="2023-08-30T14:00:00", **A)

    tag("v1.1", "2023-09-01T12:00:00")

    # ── Phase 3: v2.0 major release (2023-09 – 2024-01) ──────────────────────

    commit("Add core scheduler for background jobs", {
        "core/scheduler.py": _module("scheduler", 5),
    }, date="2023-09-08T10:00:00", **B)

    commit("Platform: middleware pipeline", {
        "platform/middleware.py": _module("middleware", 5),
    }, date="2023-09-15T11:00:00", **C)

    commit("Scheduler: cron expression parser", {
        "core/scheduler.py": _module("scheduler", 8),
    }, date="2023-09-22T10:00:00", **B)

    commit("Auth: service-to-service mTLS", {
        "core/auth.py": _module("auth", 20),
    }, date="2023-09-29T10:30:00", **A)

    commit("Middleware: request tracing and correlation IDs", {
        "platform/middleware.py": _module("middleware", 8),
    }, date="2023-10-06T11:00:00", **C)

    commit("Engine: distributed lock support", {
        "core/engine.py": _module("engine", 18),
    }, date="2023-10-13T10:00:00", **B)

    commit("API: GraphQL layer over REST endpoints", {
        "platform/api.py": _module("api", 18),
    }, date="2023-10-20T14:00:00", **D)

    commit("Tests: contract tests for API v2 schema", {
        "tests/test_api.py":         _test_module("api", 16),
        "tests/test_integration.py": _test_module("integration", 15),
    }, date="2023-10-27T09:00:00", **E)

    commit("Scheduler: priority queue and job dependencies", {
        "core/scheduler.py": _module("scheduler", 11),
    }, date="2023-11-03T10:00:00", **B)

    commit("Auth: audit log for all auth events", {
        "core/auth.py": _module("auth", 22),
    }, date="2023-11-10T10:30:00", **A)

    commit("Middleware: response compression and ETag caching", {
        "platform/middleware.py": _module("middleware", 11),
    }, date="2023-11-17T11:00:00", **C)

    commit("Config: multi-environment profile support", {
        "platform/config.py": _module("config", 14),
    }, date="2023-11-24T13:00:00", **D)

    commit("Engine: back-pressure and flow control", {
        "core/engine.py": _module("engine", 20),
    }, date="2023-12-01T10:00:00", **B)

    commit("Tests: load test harness for scheduler", {
        "tests/test_integration.py": _test_module("integration", 18),
    }, date="2023-12-08T09:00:00", **G)

    commit("Auth: key rotation without downtime", {
        "core/auth.py": _module("auth", 24),
    }, date="2023-12-15T10:00:00", **A)

    commit("API: versioned routing (/v1, /v2)", {
        "platform/api.py": _module("api", 20),
    }, date="2023-12-22T11:00:00", **C)

    commit("README: v2.0 release notes and upgrade guide", {
        "README.md": _readme("2.0.0", [
            "OAuth2 / PKCE + mTLS service auth",
            "MFA (TOTP) support",
            "Background job scheduler with cron",
            "GraphQL layer",
            "Request tracing with correlation IDs",
            "Versioned API routing",
            "Key rotation without downtime",
            "Back-pressure and flow control",
        ]),
        "core/pyproject.toml":     _pyproject("core",     "2.0.0"),
        "platform/pyproject.toml": _pyproject("platform", "2.0.0"),
        "tests/pyproject.toml":    _pyproject("tests",    "2.0.0"),
    }, date="2023-12-29T14:00:00", **A)

    tag("v2.0", "2024-01-02T12:00:00")

    # ── Phase 4: v2.1 incremental (2024-01 – 2024-06) ─────────────────────────

    commit("Scheduler: observability metrics (Prometheus)", {
        "core/scheduler.py": _module("scheduler", 13),
    }, date="2024-01-09T10:00:00", **B)

    commit("Middleware: gzip streaming response", {
        "platform/middleware.py": _module("middleware", 13),
    }, date="2024-01-16T11:00:00", **C)

    commit("Auth: refresh token rotation", {
        "core/auth.py": _module("auth", 25),
    }, date="2024-01-23T10:30:00", **A)

    commit("Engine: plugin system for custom processors", {
        "core/engine.py": _module("engine", 22),
    }, date="2024-01-30T10:00:00", **B)

    commit("API: OpenAPI 3.1 spec generation", {
        "platform/api.py": _module("api", 22),
    }, date="2024-02-06T11:00:00", **D)

    commit("Tests: mutation testing integration", {
        "tests/test_auth.py":        _test_module("auth", 16),
        "tests/test_api.py":         _test_module("api", 18),
    }, date="2024-02-13T09:00:00", **F)

    commit("Config: distributed config sync via etcd", {
        "platform/config.py": _module("config", 16),
    }, date="2024-02-20T13:00:00", **D)

    commit("Scheduler: job result caching", {
        "core/scheduler.py": _module("scheduler", 15),
    }, date="2024-02-27T10:00:00", **B)

    commit("Auth: SSO via SAML 2.0", {
        "core/auth.py": _module("auth", 27),
    }, date="2024-03-05T10:30:00", **A)

    commit("Middleware: add request body size limits", {
        "platform/middleware.py": _module("middleware", 15),
    }, date="2024-03-12T11:00:00", **C)

    commit("Tests: API contract tests for OpenAPI spec", {
        "tests/test_integration.py": _test_module("integration", 21),
    }, date="2024-03-19T09:00:00", **E)

    commit("Engine: hot-swap processor modules", {
        "core/engine.py": _module("engine", 24),
    }, date="2024-03-26T10:00:00", **B)

    commit("Auth: device authorization grant flow", {
        "core/auth.py": _module("auth", 28),
    }, date="2024-04-02T10:30:00", **A)

    commit("API: webhook delivery with retry", {
        "platform/api.py": _module("api", 24),
    }, date="2024-04-09T11:00:00", **C)

    commit("Config: secret management via Vault", {
        "platform/config.py": _module("config", 18),
    }, date="2024-04-16T13:00:00", **D)

    commit("Tests: security-focused fuzz test suite", {
        "tests/test_auth.py": _test_module("auth", 20),
    }, date="2024-04-23T09:00:00", **G)

    commit("README: v2.1 changelog", {
        "README.md": _readme("2.1.0", [
            "SSO via SAML 2.0",
            "Device authorization grant",
            "OpenAPI 3.1 spec generation",
            "Webhook delivery with retry",
            "Secret management via Vault",
            "Plugin system for custom processors",
            "Scheduler Prometheus metrics",
        ]),
        "core/pyproject.toml":     _pyproject("core",     "2.1.0"),
        "platform/pyproject.toml": _pyproject("platform", "2.1.0"),
        "tests/pyproject.toml":    _pyproject("tests",    "2.1.0"),
    }, date="2024-05-01T14:00:00", **A)

    tag("v2.1", "2024-05-03T12:00:00")

    # ── Phase 5: v3.0 major release (2024-05 – 2025-01) ──────────────────────

    commit("Engine: async/await throughout core", {
        "core/engine.py": _module("engine", 27),
    }, date="2024-05-10T10:00:00", **B)

    commit("Auth: passkey / FIDO2 support", {
        "core/auth.py": _module("auth", 30),
    }, date="2024-05-17T10:30:00", **A)

    commit("Scheduler: distributed leader election", {
        "core/scheduler.py": _module("scheduler", 18),
    }, date="2024-05-24T10:00:00", **B)

    commit("Platform: event-driven architecture via message bus", {
        "platform/api.py":        _module("api",        27),
        "platform/middleware.py": _module("middleware",  18),
    }, date="2024-06-04T11:00:00", **C)

    commit("Config: per-tenant config isolation", {
        "platform/config.py": _module("config", 21),
    }, date="2024-06-11T13:00:00", **D)

    commit("Tests: end-to-end test with real message bus", {
        "tests/test_integration.py": _test_module("integration", 25),
    }, date="2024-06-18T09:00:00", **E)

    commit("Auth: zero-trust policy engine", {
        "core/auth.py": _module("auth", 32),
    }, date="2024-07-02T10:30:00", **A)

    commit("Engine: multi-region failover", {
        "core/engine.py": _module("engine", 30),
    }, date="2024-07-16T10:00:00", **B)

    commit("API: streaming responses via Server-Sent Events", {
        "platform/api.py": _module("api", 29),
    }, date="2024-07-30T11:00:00", **C)

    commit("Scheduler: backoff strategies (exponential, jitter)", {
        "core/scheduler.py": _module("scheduler", 20),
    }, date="2024-08-13T10:00:00", **B)

    commit("Auth: policy-as-code via Rego integration", {
        "core/auth.py": _module("auth", 34),
    }, date="2024-08-27T10:30:00", **A)

    commit("Middleware: adaptive rate limiting", {
        "platform/middleware.py": _module("middleware", 21),
    }, date="2024-09-10T11:00:00", **D)

    commit("Tests: chaos testing framework", {
        "tests/test_integration.py": _test_module("integration", 29),
    }, date="2024-09-24T09:00:00", **F)

    commit("Engine: WASM plugin runtime", {
        "core/engine.py": _module("engine", 33),
    }, date="2024-10-08T10:00:00", **B)

    commit("Config: declarative config DSL", {
        "platform/config.py": _module("config", 24),
    }, date="2024-10-22T13:00:00", **D)

    commit("Auth: ephemeral credential support", {
        "core/auth.py": _module("auth", 36),
    }, date="2024-11-05T10:30:00", **A)

    commit("API: GraphQL subscriptions", {
        "platform/api.py": _module("api", 32),
    }, date="2024-11-19T11:00:00", **C)

    commit("Tests: property-based testing with Hypothesis", {
        "tests/test_auth.py":        _test_module("auth", 24),
        "tests/test_api.py":         _test_module("api", 22),
    }, date="2024-12-03T09:00:00", **G)

    commit("Scheduler: job DAG with parallel execution", {
        "core/scheduler.py": _module("scheduler", 23),
    }, date="2024-12-17T10:00:00", **B)

    commit("README: v3.0 release notes and migration guide", {
        "README.md": _readme("3.0.0", [
            "Passkey / FIDO2 authentication",
            "Zero-trust policy engine",
            "Policy-as-code via Rego",
            "Ephemeral credentials",
            "Async/await throughout",
            "WASM plugin runtime",
            "GraphQL subscriptions",
            "SSE streaming responses",
            "Multi-region failover",
            "Distributed leader election",
            "Job DAG with parallel execution",
            "Adaptive rate limiting",
            "Declarative config DSL",
        ]),
        "core/pyproject.toml":     _pyproject("core",     "3.0.0"),
        "platform/pyproject.toml": _pyproject("platform", "3.0.0"),
        "tests/pyproject.toml":    _pyproject("tests",    "3.0.0"),
    }, date="2024-12-30T14:00:00", **A)

    tag("v3.0", "2025-01-02T12:00:00")

    # ── Post-release work (2025-01 – present) ─────────────────────────────────

    commit("Auth: post-quantum crypto primitives (experimental)", {
        "core/auth.py": _module("auth", 38),
    }, date="2025-01-09T10:30:00", **A)

    commit("Engine: observable workflow engine", {
        "core/engine.py": _module("engine", 35),
    }, date="2025-01-14T10:00:00", **B)

    commit("API: batch mutation support for GraphQL", {
        "platform/api.py": _module("api", 34),
    }, date="2025-01-21T11:00:00", **C)

    commit("Tests: upgrade to pytest 8 and add async fixtures", {
        "tests/test_integration.py": _test_module("integration", 32),
    }, date="2025-01-28T09:00:00", **E)

    commit("Config: hot-patch without pod restart", {
        "platform/config.py": _module("config", 26),
    }, date="2025-02-04T13:00:00", **D)

    commit("Auth: audit log streaming to SIEM", {
        "core/auth.py": _module("auth", 40),
    }, date="2025-02-11T10:30:00", **A)

    commit("Scheduler: cooperative multi-tenancy isolation", {
        "core/scheduler.py": _module("scheduler", 25),
    }, date="2025-02-18T10:00:00", **B)

    commit("Tests: add coverage gating to CI", {
        "tests/test_auth.py":        _test_module("auth", 28),
        "tests/test_api.py":         _test_module("api", 26),
    }, date="2025-02-25T09:00:00", **F)

    commit("Middleware: WebSocket upgrade path", {
        "platform/middleware.py": _module("middleware", 24),
    }, date="2025-03-04T11:00:00", **C)

    commit("Engine: structured concurrency via TaskGroup", {
        "core/engine.py": _module("engine", 37),
    }, date="2025-03-11T10:00:00", **B)

    commit("Auth: compliance report generator", {
        "core/auth.py": _module("auth", 42),
    }, date="2025-03-18T10:30:00", **A)

    # ── Merge commits (PR simulations) ────────────────────────────────────────
    # Each PR follows the pattern: branch → commits on branch → merge to main.
    # git merge --no-ff with "Merge pull request #N" subject is picked up by
    # the 'pull request #' heuristic in gitstats._detect_merge().

    def pr(num, branch, msg, files, author, date_branch, date_merge):
        """Create a feature branch, commit, then merge as a PR."""
        git("checkout", "-b", branch, **author, date=date_branch)
        for path, content in files.items():
            write(path, content)
        git("add", ".")
        git("commit", "-m", msg, **author, date=date_branch)
        git("checkout", "main", **author, date=date_merge)
        git(
            "merge", "--no-ff", branch,
            "-m", f"Merge pull request #{num} from "
                  f"{author['email'].split('@')[0]}/{branch}",
            **author, date=date_merge,
        )
        git("branch", "-d", branch, **author, date=date_merge)

    pr(12, "auth/session-invalidation",
       "Auth: session invalidation on password change",
       {"core/auth.py": _module("auth", 43)},
       A, "2023-03-25T10:00:00", "2023-03-26T09:00:00")

    pr(27, "platform/bulk-endpoints",
       "API: bulk create/update endpoints",
       {"platform/api.py": _module("api", 16)},
       C, "2023-07-14T10:00:00", "2023-07-15T09:00:00")

    pr(41, "engine/backpressure-v2",
       "Engine: improved back-pressure with adaptive windows",
       {"core/engine.py": _module("engine", 21)},
       B, "2023-11-10T10:00:00", "2023-11-11T09:00:00")

    pr(58, "auth/delegation-tokens",
       "Auth: delegation token chains",
       {"core/auth.py": _module("auth", 31)},
       A, "2024-06-24T10:00:00", "2024-06-25T09:00:00")

    pr(73, "scheduler/distributed-tracing",
       "Scheduler: propagate trace context across jobs",
       {"core/scheduler.py": _module("scheduler", 24)},
       B, "2024-09-19T10:00:00", "2024-09-20T09:00:00")

    pr(89, "platform/graphql-persisted-queries",
       "API: persisted query support for GraphQL",
       {"platform/api.py": _module("api", 33)},
       D, "2025-01-16T10:00:00", "2025-01-17T09:00:00")


# ── Screenshot engine ──────────────────────────────────────────────────────────

# Injected into each tab's HTML copy to activate the right tab on load.
# showTab(id, btn) requires the button element to apply the .active class.
# CSS disables transitions/animations so Chrome headless doesn't capture a
# half-applied .active state on the tab button.
_TAB_INJECT = """
<style>
  *, *::before, *::after {{
      transition: none !important;
      animation: none !important;
  }}
  html {{ scroll-behavior: auto !important; }}
</style>
<script>
window.addEventListener('load', function () {{
    var label = {label!r};
    var btn = Array.from(document.querySelectorAll('.tab-btn'))
                   .find(function (b) {{ return b.textContent.trim() === label; }});
    showTab({tab_id!r}, btn);
}});
</script>
"""


def take_screenshots(html_path: str, out_dir: str) -> None:
    """Screenshot every tab of the report using Chrome headless.

    Temp HTML files are written into the same directory as the original report
    so that relative asset references (tailwind.js, chart.js) resolve correctly.
    """
    if not os.path.isfile(CHROME):
        sys.exit(
            f"Chrome not found at:\n  {CHROME}\n"
            "Install Google Chrome or update the CHROME path in this script."
        )

    os.makedirs(out_dir, exist_ok=True)
    report_dir = os.path.dirname(os.path.abspath(html_path))
    src = pathlib.Path(html_path).read_text(encoding="utf-8")

    for tab_id, label, stem in TABS:
        print(f"  {label:<14} → {stem}.png")
        injected = src.replace(
            "</body>",
            _TAB_INJECT.format(tab_id=tab_id, label=label) + "</body>",
            1,
        )
        # Write the tmp file next to the original report so relative JS paths work.
        tmp_html = os.path.join(report_dir, f"_tab_{tab_id}.html")
        out_png  = os.path.join(out_dir, f"{stem}.png")
        try:
            pathlib.Path(tmp_html).write_text(injected, encoding="utf-8")
            subprocess.check_call(
                [
                    CHROME,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--hide-scrollbars",
                    f"--window-size={SCREENSHOT_WIDTH},{SCREENSHOT_HEIGHT}",
                    f"--screenshot={out_png}",
                    f"file://{os.path.abspath(tmp_html)}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            if os.path.exists(tmp_html):
                os.unlink(tmp_html)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(SCRIPT_DIR, "screenshots"),
                        help="Output directory for PNG files (default: ./screenshots)")
    args = parser.parse_args()

    externals = os.path.join(SCRIPT_DIR, "externals")
    if not os.path.isdir(externals):
        sys.exit(f"externals/ directory not found at {externals}")

    with tempfile.TemporaryDirectory(prefix="gitstats_demo_") as tmp:
        repo_dir   = os.path.join(tmp, "demo-project")
        cfg_path   = os.path.join(tmp, "config.json")
        report_dir = os.path.join(tmp, "report")
        html_path  = os.path.join(report_dir, "index.html")

        os.makedirs(repo_dir)
        os.makedirs(report_dir)

        # Write config
        with open(cfg_path, "w") as f:
            json.dump(DEMO_CONFIG, f, indent=2)

        # Build demo repository
        print("Building demo repository...")
        build_demo_repo(repo_dir)

        # Collect stats and generate report
        print("Running gitstats analysis...")
        gs = gitstats.GitStats(repo_dir, cfg_path)
        gs.collect()
        gs.generate_report(externals, html_path)

        # Screenshot each tab
        print(f"Taking screenshots → {args.out}/")
        take_screenshots(html_path, args.out)

    print(f"\nDone. {len(TABS)} screenshots saved to: {args.out}/")


if __name__ == "__main__":
    main()
