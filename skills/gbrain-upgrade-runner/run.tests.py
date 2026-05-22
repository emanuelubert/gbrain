#!/usr/bin/env python3
"""Unit tests for gbrain-upgrade-runner (P-runner-refresh, S190 2026-05-22).

Covers:
  - T-RR-1..7: phase3 routing on (target_version, bun_link_mode) + audit-row fields
  - T-BL-1..4: _is_bun_link_mode canonical-signal detector

Run via:
    python3 ~/gbrain/skills/gbrain-upgrade-runner/run.tests.py

Refs:
  ~/resources/local-agent-system/knowledge-base/cdd-contracts/P-runner-refresh-gbrain-upgrade-runner-v0.37-substrate.md
  (this test file is the §2 test-plan deliverable; parent contract covers it.)
"""

# CDD parent contract sentinel — points to parent file run.py and to the
# CDD contract that authorizes this test file (§2 test plan T-RR-1..7 +
# T-BL-1..4).
# <!-- CDD-CONTRACT: ~/gbrain/skills/gbrain-upgrade-runner/run.py -->
# <!-- CDD-CONTRACT: ~/gbrain/skills/gbrain-upgrade-runner/run.tests.py -->
# Parent contract: P-runner-refresh-gbrain-upgrade-runner-v0.37-substrate.md

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Import the runner module from sibling run.py.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import importlib.util
_spec = importlib.util.spec_from_file_location("runner_under_test", HERE / "run.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


# ─── _semver_lt + _parse_semver ─────────────────────────────────────────────


class SemverTests(unittest.TestCase):
    def test_basic_lt(self):
        self.assertTrue(runner._semver_lt("v0.33.0", "v0.34.0"))
        self.assertTrue(runner._semver_lt("0.33.999", "0.34.0"))
        self.assertFalse(runner._semver_lt("v0.34.0", "v0.34.0"))
        self.assertFalse(runner._semver_lt("v0.35.0", "v0.34.0"))

    def test_prerelease_lt_release(self):
        self.assertTrue(runner._semver_lt("v0.34.0-beta.1", "v0.34.0"))
        self.assertFalse(runner._semver_lt("v0.34.0", "v0.34.0-beta.1"))

    def test_four_segment_tag(self):
        self.assertTrue(runner._semver_lt("v0.33.0", "v0.37.11.0"))
        self.assertFalse(runner._semver_lt("v0.37.11.0", "v0.34.0"))

    def test_unparseable_falls_back_to_zero(self):
        # Commit SHA: not a semver. Falls back to (0,0,0); treated as legacy-class.
        self.assertTrue(runner._semver_lt("17b190e", "v0.34.0"))


# ─── _is_bun_link_mode (T-BL-1..4) ──────────────────────────────────────────


class BunLinkDetectorTests(unittest.TestCase):
    """T-BL-1..4 — symlink-resolve detector at canonical layer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="upgrade-runner-test-"))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".bun" / "install" / "global" / "node_modules").mkdir(parents=True)
        (self.fake_home / ".bun" / "bin").mkdir(parents=True)
        self.real_gbrain = self.tmp / "gbrain"
        self.real_gbrain.mkdir()
        (self.real_gbrain / "src").mkdir()
        (self.real_gbrain / "src" / "cli.ts").write_text("// fake cli\n")

        self._orig_expanduser = os.path.expanduser
        def fake_expanduser(p):
            if p.startswith("~/"):
                return str(self.fake_home / p[2:])
            return self._orig_expanduser(p)
        self._patcher = mock.patch("os.path.expanduser", side_effect=fake_expanduser)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_canonical_symlink(self, target):
        nm = self.fake_home / ".bun" / "install" / "global" / "node_modules" / "gbrain"
        nm.symlink_to(target)

    def _make_canonical_dir(self):
        nm = self.fake_home / ".bun" / "install" / "global" / "node_modules" / "gbrain"
        nm.mkdir()
        (nm / "package.json").write_text("{}")

    def _make_legacy_cli_symlink(self):
        bin_link = self.fake_home / ".bun" / "bin" / "gbrain"
        nm = self.fake_home / ".bun" / "install" / "global" / "node_modules" / "gbrain"
        if not nm.exists():
            nm.mkdir()
            (nm / "src").mkdir()
            (nm / "src" / "cli.ts").write_text("// cli\n")
        bin_link.symlink_to(nm / "src" / "cli.ts")

    def test_T_BL_1_canonical_symlink_to_real_dir(self):
        """T-BL-1: symlink at canonical layer pointing to a real dir -> True."""
        self._make_canonical_symlink(self.real_gbrain)
        self.assertTrue(runner._is_bun_link_mode())

    def test_T_BL_2_regular_dir_not_symlink(self):
        """T-BL-2: regular dir (bun-installed binary, no symlink) -> False."""
        self._make_canonical_dir()
        self.assertFalse(runner._is_bun_link_mode())

    def test_T_BL_3_path_missing(self):
        """T-BL-3: neither node_modules nor bin/gbrain exists -> False."""
        self.assertFalse(runner._is_bun_link_mode())

    def test_T_BL_4_dangling_symlink_falls_through_to_legacy(self):
        """T-BL-4: symlink to non-existent target -> canonical declines;
        legacy fallback fires if a separate legacy signal is present."""
        dangling = self.tmp / "does-not-exist"
        self._make_canonical_symlink(dangling)
        self.assertFalse(runner._is_bun_link_mode())

        canonical = self.fake_home / ".bun" / "install" / "global" / "node_modules" / "gbrain"
        canonical.unlink()
        self._make_legacy_cli_symlink()
        self.assertTrue(runner._is_bun_link_mode())


# ─── phase3_canonical_upgrade routing (T-RR-1..7) ──────────────────────────


class _FakeCompletedProcess:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class Phase3RoutingTests(unittest.TestCase):
    """T-RR-1..7 — phase3 routes on (target_version, bun_link_mode); audit fields."""

    def setUp(self):
        self.subprocess_calls = []
        self.subprocess_rcs = {}

        def fake_run(cmd, capture_output=True, text=True, check=False, env=None, timeout=None):
            self.subprocess_calls.append(list(cmd))
            key = tuple(cmd[:2]) if len(cmd) >= 2 else tuple(cmd)
            rc = self.subprocess_rcs.get(key, 0)
            return _FakeCompletedProcess(rc=rc, stdout="", stderr="" if rc == 0 else "fake error")

        self._pkg_versions = ["0.37.11.0", "0.37.11.0"]

        self._patches = [
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch.object(runner, "_get_package_version", side_effect=self._fake_pkg_version),
            mock.patch.object(runner, "_audit_log"),
        ]
        for p in self._patches:
            p.start()

    def _fake_pkg_version(self):
        if len(self._pkg_versions) > 1:
            return self._pkg_versions.pop(0)
        return self._pkg_versions[0]

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _ran_cmd(self, *prefix):
        return any(call[:len(prefix)] == list(prefix) for call in self.subprocess_calls)

    def test_T_RR_1_legacy_path(self):
        """T-RR-1: target=v0.33.0, bun_link=False -> legacy single-step."""
        with mock.patch.object(runner, "_is_bun_link_mode", return_value=False):
            self._pkg_versions = ["0.32.0", "0.33.0"]
            briefing = {"fire_id": "T-RR-1", "upstream": "v0.33.0"}
            success, env = runner.phase3_canonical_upgrade(briefing, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(env.target_version_class, "legacy")
        self.assertFalse(env.migrate_only_invoked)
        self.assertTrue(self._ran_cmd("gbrain", "upgrade"))
        self.assertFalse(self._ran_cmd("gbrain", "init"))

    def test_T_RR_2_v034_non_bun_link(self):
        """T-RR-2: target=v0.34.0, bun_link=False -> both commands in order."""
        with mock.patch.object(runner, "_is_bun_link_mode", return_value=False):
            self._pkg_versions = ["0.33.0", "0.34.0"]
            briefing = {"fire_id": "T-RR-2", "upstream": "v0.34.0"}
            success, env = runner.phase3_canonical_upgrade(briefing, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(env.target_version_class, "v0_34_plus")
        self.assertTrue(env.migrate_only_invoked)
        self.assertTrue(self._ran_cmd("gbrain", "upgrade"))
        self.assertTrue(self._ran_cmd("gbrain", "init"))
        upgrade_idx = next(i for i, c in enumerate(self.subprocess_calls) if c[:2] == ["gbrain", "upgrade"])
        init_idx = next(i for i, c in enumerate(self.subprocess_calls) if c[:2] == ["gbrain", "init"])
        self.assertLess(upgrade_idx, init_idx)

    def test_T_RR_3_v0_37_bun_link(self):
        """T-RR-3: target=v0.37.11.0, bun_link=True -> skip upgrade, run migrate."""
        with mock.patch.object(runner, "_is_bun_link_mode", return_value=True):
            self._pkg_versions = ["0.37.11.0", "0.37.11.0"]
            briefing = {"fire_id": "T-RR-3", "upstream": "v0.37.11.0"}
            success, env = runner.phase3_canonical_upgrade(briefing, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(env.target_version_class, "v0_34_plus")
        self.assertTrue(env.migrate_only_invoked)
        self.assertFalse(self._ran_cmd("gbrain", "upgrade"))
        self.assertTrue(self._ran_cmd("gbrain", "init"))

    def test_T_RR_4_cli_upgrade_fails(self):
        """T-RR-4: CLI upgrade fails -> early-return, migrate NOT called."""
        with mock.patch.object(runner, "_is_bun_link_mode", return_value=False):
            self._pkg_versions = ["0.33.0", "0.33.0"]
            self.subprocess_rcs[("gbrain", "upgrade")] = 2
            briefing = {"fire_id": "T-RR-4", "upstream": "v0.37.11.0"}
            success, env = runner.phase3_canonical_upgrade(briefing, dry_run=False)
        self.assertFalse(success)
        self.assertIn("cli_upgrade_failed", env.error or "")
        self.assertFalse(env.migrate_only_invoked)
        self.assertTrue(self._ran_cmd("gbrain", "upgrade"))
        self.assertFalse(self._ran_cmd("gbrain", "init"))

    def test_T_RR_5_migrate_fails(self):
        """T-RR-5: init --migrate-only fails -> migrate_failed error."""
        with mock.patch.object(runner, "_is_bun_link_mode", return_value=False):
            self._pkg_versions = ["0.33.0", "0.37.11.0"]
            self.subprocess_rcs[("gbrain", "init")] = 2
            briefing = {"fire_id": "T-RR-5", "upstream": "v0.37.11.0"}
            success, env = runner.phase3_canonical_upgrade(briefing, dry_run=False)
        self.assertFalse(success)
        self.assertIn("migrate_failed", env.error or "")
        self.assertTrue(env.migrate_only_invoked)

    def test_T_RR_6_prerelease_routes_strict_semver(self):
        """T-RR-6: v0.34.0-beta.1 -> strict semver places below release -> legacy.

        Documented deviation from contract §2 wording ('new path'). Implementation
        chose strict semver because the cutoff sits at a release boundary. If
        product intent requires beta-of-target to route new, move the cutoff to
        v0.33.999 — single point of edit. See _semver_lt docstring + SKILL.md.
        """
        with mock.patch.object(runner, "_is_bun_link_mode", return_value=False):
            self._pkg_versions = ["0.33.5", "0.34.0-beta.1"]
            briefing = {"fire_id": "T-RR-6", "upstream": "v0.34.0-beta.1"}
            success, env = runner.phase3_canonical_upgrade(briefing, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(env.target_version_class, "legacy")
        self.assertFalse(env.migrate_only_invoked)

    def test_T_RR_7_audit_row_carries_new_fields(self):
        """T-RR-7: envelope carries migrate_only_invoked + target_version_class."""
        with mock.patch.object(runner, "_is_bun_link_mode", return_value=False):
            self._pkg_versions = ["0.33.0", "0.37.11.0"]
            briefing = {"fire_id": "T-RR-7", "upstream": "v0.37.11.0"}
            _, env = runner.phase3_canonical_upgrade(briefing, dry_run=False)
        self.assertTrue(hasattr(env, "target_version_class"))
        self.assertTrue(hasattr(env, "migrate_only_invoked"))
        self.assertEqual(env.target_version_class, "v0_34_plus")
        self.assertTrue(env.migrate_only_invoked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
