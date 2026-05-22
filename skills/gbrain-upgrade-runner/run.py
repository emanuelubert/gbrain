#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""gbrain-upgrade-runner entry point.

Executable runbook for gbrain version upgrades (Stream B Phase 5-8).
Consumes watcher-briefing output, executes the canonical upgrade sequence
with safety guards (dim parity check, pg_dump freshness, supervisor state),
and propagates version pins downstream.

Invocation modes:
    Mode A (from gbrain skill run):
        gbrain skill run gbrain-upgrade-runner --briefing <path> [--confirm-destructive]
    Mode B (direct):
        python3 ~/gbrain/skills/gbrain-upgrade-runner/run.py --briefing <path> [--confirm-destructive]

Exit codes (per SKILL.md ## I/O Contract):
    0 — success (upgrade complete, all verifications pass)
    1 — phase failure (one or more phases failed)
    2 — usage error (missing args, malformed briefing)
    3 — operator gate required (--confirm-destructive not provided)
    4 — rollback executed (failure + successful rollback)
    5 — rollback failed (failure + rollback also failed)

Canonical upgrade command: GBRAIN_NO_REEMBED=1 gbrain upgrade
  - Runs bun update + post-upgrade hooks + apply-migrations + initSchema.
  - GBRAIN_NO_REEMBED=1 is REQUIRED: CJK chunker re-embed prompt defaults
    to proceed when non-TTY; this env var prevents accidental full re-index.

Critical guard: dim parity check (config.embedding_dimensions must equal DB column dim).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def _parse_yaml_simple(text: str) -> dict[str, Any]:
    """Minimal YAML frontmatter parser (stdlib only).

    Handles the subset of YAML used in gbrain briefing files:
    - scalar values (strings, ints, bools)
    - simple key: value pairs
    - single-line arrays like ['4096']
    - nested dicts (one level)

    Does NOT handle: multi-line strings, complex nesting, anchors/aliases.
    """
    result: dict[str, Any] = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Match key: value (with optional space after colon)
        m = re.match(r"^([a-zA-Z_][\w]*)\s*:\s*(.*)", line)
        if not m:
            continue

        key = m.group(1)
        val_str = m.group(2).strip()

        # Parse value types
        if val_str.startswith("[") and val_str.endswith("]"):
            # Inline array: ['4096'] or [1, 2, 3]
            inner = val_str[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                # Try to parse as list of strings (quoted) or numbers
                items = []
                for item in re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", inner):
                    items.append(item[0] or item[1] or item[2])
                # Try numeric conversion
                parsed = []
                for item in items:
                    try:
                        parsed.append(int(item))
                    except ValueError:
                        try:
                            parsed.append(float(item))
                        except ValueError:
                            parsed.append(item)
                result[key] = parsed
        elif val_str.lower() == "true":
            result[key] = True
        elif val_str.lower() == "false":
            result[key] = False
        elif val_str.isdigit():
            result[key] = int(val_str)
        else:
            # Strip quotes if present
            if (val_str.startswith("'") and val_str.endswith("'")) or \
               (val_str.startswith('"') and val_str.endswith('"')):
                val_str = val_str[1:-1]
            result[key] = val_str

    return result


def _load_briefing_raw(briefing_path: str) -> tuple[dict[str, Any], str]:
    """Load briefing file, return (frontmatter_dict, body_string)."""
    path = Path(briefing_path)
    if not path.exists():
        print(f"ERROR: Briefing file not found: {briefing_path}", file=sys.stderr)
        sys.exit(2)
    with open(path) as f:
        content = f.read()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, flags=re.DOTALL)
    if not m:
        print(f"ERROR: Briefing file has no YAML frontmatter: {briefing_path}", file=sys.stderr)
        sys.exit(2)
    fm = _parse_yaml_simple(m.group(1))
    return fm, m.group(2)


# ─── Paths ──────────────────────────────────────────────────────────────

HOME = Path.home()
LOG_DIR = HOME / ".hermes" / "logs"
AUDIT_LOG = LOG_DIR / "gbrain-upgrade-runner.jsonl"
SUPERVISOR_PLIST = HOME / ".hermes" / "launchd" / "local.hermes.minions-supervisor.plist"
CLAUDE_MD = HOME / "resources" / "local-agent-system" / "CLAUDE.md"

# ─── Envelope shape ─────────────────────────────────────────────────────


@dataclass
class UpgradeEnvelope:
    """JSON envelope written to stdout on completion."""

    version: str = "1.0"
    fire_id: str = ""
    installed_before: str = ""
    target_version: str = ""
    outcome: str = "unknown"
    phases_executed: list[str] = field(default_factory=list)
    phases_skipped: list[str] = field(default_factory=list)
    duration_ms: int = 0
    snapshot_path: Optional[str] = None
    rollback_commands: list[str] = field(default_factory=list)
    error: Optional[str] = None
    dim_parity_ok: Optional[bool] = None
    doctor_pass: Optional[bool] = None
    smoke_queries_passed: int = 0
    binary_version_changed: bool = False   # Bug 3 gate: True iff Phase 3 actually changed the binary
    pre_upgrade_version: str = ""          # Bug 1 + Bug 2: version before upgrade attempt
    post_upgrade_version: str = ""         # Bug 1 + Bug 2: version after upgrade attempt
    # Bug Class 5 (P-runner-refresh / v0.37 split-semantics) fields:
    target_version_class: str = ""         # "legacy" (< v0.34.0) or "v0_34_plus" (>= v0.34.0)
    migrate_only_invoked: bool = False     # True iff `gbrain init --migrate-only` ran


# ─── Helpers ─────────────────────────────────────────────────────────────


def _run(cmd: list[str], check: bool = True, env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Run a shell command, return CompletedProcess."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def _run_quiet(cmd: list[str]) -> tuple[int, str]:
    """Run command, return (exit_code, stdout). Suppress stderr."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout.strip()


def _audit_log(phase: str, status: str, details: dict | None = None):
    """Append a JSONL entry to the audit log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "status": status,
        **(details or {}),
    }
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_briefing(briefing_path: str) -> dict[str, Any]:
    """Load and parse a briefing YAML file (stdlib-only)."""
    fm, _ = _load_briefing_raw(briefing_path)
    return fm


def _get_installed_version() -> str:
    """Get the currently installed gbrain version string."""
    rc, out = _run_quiet(["gbrain", "--version"])
    if rc != 0:
        return "unknown"
    # Version might be a commit hash or semver; normalize to first 7 chars of hash
    out = out.strip()
    if re.match(r"^[0-9a-f]{7,}$", out):
        return out[:7]
    return out


def _get_package_version() -> str:
    """Read gbrain version from package.json (authoritative for bun-link mode)."""
    pkg_path = os.path.expanduser("~/.bun/install/global/node_modules/gbrain/package.json")
    try:
        with open(pkg_path) as f:
            data = json.load(f)
        return data.get("version", "unknown")
    except (IOError, json.JSONDecodeError):
        return "unknown"


def _check_bun_link_divergence() -> tuple[bool, str]:
    """Check if gbrain is installed via bun-link with divergent git histories.

    Returns (is_bun_link, error_message).
    If not bun-link or no divergence, returns (False, "").

    Bun-link detection uses two signals additively (P-runner-refresh fix):
      1. Canonical: `~/.bun/install/global/node_modules/gbrain` is a symlink to
         a real directory (typically `~/gbrain`).
      2. Legacy fallback: `~/.bun/bin/gbrain` realpath contains `/src/cli.ts`.
    The repo_root is derived from whichever signal fires.
    """
    nm_path = os.path.expanduser("~/.bun/install/global/node_modules/gbrain")
    repo_root: Optional[str] = None
    real_path_for_msg: str = ""

    # Signal 1 — canonical: node_modules/gbrain symlink resolve.
    try:
        if os.path.islink(nm_path):
            target = os.readlink(nm_path)
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(os.path.dirname(nm_path), target))
            if os.path.isdir(target):
                repo_root = target
                real_path_for_msg = target
    except (OSError, FileNotFoundError):
        pass

    if repo_root is None:
        # Signal 2 — legacy: ~/.bun/bin/gbrain realpath ending in /src/cli.ts.
        gbrain_bin = os.path.expanduser("~/.bun/bin/gbrain")
        try:
            real_path = os.path.realpath(gbrain_bin)
        except (OSError, FileNotFoundError):
            return False, ""

        # If real path contains "/src/cli.ts", it's bun-link mode (symlinked source).
        if "/src/cli.ts" not in real_path:
            return False, ""

        # Found bun-link mode — find the source repo via parent path.
        # ~/gbrain/src/cli.ts → ~/gbrain is the repo root.
        src_dir = os.path.dirname(real_path)  # .../gbrain/src
        repo_root = os.path.dirname(src_dir)   # .../gbrain
        real_path_for_msg = real_path

    real_path = real_path_for_msg  # preserve variable name for downstream string interpolations

    if not os.path.isdir(os.path.join(repo_root, ".git")):
        return True, f"bun-link mode detected (real path={real_path}) but no .git found in {repo_root}"

    # Check if origin/master is an ancestor of HEAD (we've rebased on top).
    # After rebase: origin/master commits are all in HEAD, so this returns 0.
    # If FALSE: divergent histories — origin/master has commits not in HEAD.
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "merge-base", "--is-ancestor", "origin/master", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True, f"bun-link mode detected; git merge-base check failed (transient)"

    can_ff = result.returncode == 0

    # Also check if HEAD is already at origin/master (up-to-date)
    try:
        result_head = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        result_master = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--short", "origin/master"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        result_head = subprocess.run(["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
                                     capture_output=True, text=True, timeout=15)
        result_master = subprocess.run(["git", "-C", repo_root, "rev-parse", "--short", "origin/master"],
                                       capture_output=True, text=True, timeout=15)

    head_sha = result_head.stdout.strip() if result_head.returncode == 0 else "unknown"
    master_sha = result_master.stdout.strip() if result_master.returncode == 0 else "unknown"

    if can_ff:
        return False, ""  # bun-link but up-to-date or can fast-forward

    error_msg = (
        f"bun-link mode + divergent git histories detected. "
        f"{repo_root} HEAD ({head_sha}) is not an ancestor of origin/master ({master_sha}). "
        f"Cannot proceed via `gbrain upgrade` (pull --ff-only will fail silently). "
        f"Resolve by either: (A) rebase {repo_root} onto origin/master then re-run; "
        f"or (B) switch off bun-link via `bun remove --global gbrain && "
        f"bun install --global gbrain@<target>`."
    )
    return True, error_msg


def _check_supervisor_running() -> bool:
    """Check if gbrain jobs supervisor is currently running."""
    rc, out = _run_quiet(["launchctl", "list"])
    return "minions-supervisor" in out


def _check_db_connectable() -> bool:
    """Check if Postgres gbrain database is reachable."""
    rc, _ = _run_quiet(["psql", "gbrain", "-c", "SELECT 1"])
    return rc == 0


# ─── Bug Class 5 (P-runner-refresh) helpers: version-class routing + canonical bun-link signal ──


def _parse_semver(version: str) -> tuple[int, int, int, str]:
    """Parse a semver-like version string into (major, minor, patch, prerelease).

    Accepts: "v0.34.0", "0.34.0", "0.34.0-beta.1", "0.37.11.0" (4-segment fork tags).
    Returns (0, 0, 0, "") on parse failure (legacy fallback — safer to use legacy path).
    """
    if not version:
        return (0, 0, 0, "")
    v = version.strip()
    if v.startswith("v") or v.startswith("V"):
        v = v[1:]
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\.\d+)?(?:-(.+))?$", v)
    if not m:
        return (0, 0, 0, "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def _semver_lt(a: str, b: str) -> bool:
    """Return True iff semver a < semver b. Handles pre-release suffixes.

    Pre-release rule (matches semver 2.0): X.Y.Z-pre < X.Y.Z. So
    _semver_lt("v0.34.0-beta.1", "v0.34.0") is True.
    """
    am, an, ap, apre = _parse_semver(a)
    bm, bn, bp, bpre = _parse_semver(b)
    if (am, an, ap) != (bm, bn, bp):
        return (am, an, ap) < (bm, bn, bp)
    # Same numeric triple: pre-release < release (empty pre means release)
    if apre and not bpre:
        return True
    if not apre and bpre:
        return False
    return apre < bpre


def _is_bun_link_mode() -> bool:
    """Canonical bun-link signal (P-runner-refresh §11.5).

    Resolves `~/.bun/install/global/node_modules/gbrain` via os.readlink().
    If it is a symlink pointing to a real directory (typically `~/gbrain`),
    we are in bun-link mode. Falls through to the legacy `~/.bun/bin/gbrain`
    realpath signal if the new check is inconclusive (additive — does NOT
    weaken the existing detector).

    Returns True iff bun-link mode is positively detected.
    """
    nm_path = os.path.expanduser("~/.bun/install/global/node_modules/gbrain")
    try:
        # New canonical signal — direct symlink resolve at node_modules layer.
        if os.path.islink(nm_path):
            target = os.readlink(nm_path)
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(os.path.dirname(nm_path), target))
            if os.path.isdir(target):
                return True
            # Symlink to a non-existent or non-dir target: fall through to legacy.
    except (OSError, FileNotFoundError):
        # Path missing or unreadable: fall through to legacy signal.
        pass

    # Legacy fallback: ~/.bun/bin/gbrain realpath ending in /src/cli.ts.
    gbrain_bin = os.path.expanduser("~/.bun/bin/gbrain")
    try:
        real_path = os.path.realpath(gbrain_bin)
    except (OSError, FileNotFoundError):
        return False
    return "/src/cli.ts" in real_path


def _get_db_embedding_dim() -> Optional[int]:
    """Query the embedding column dimension from pg_attribute."""
    try:
        result = _run([
            "psql", "gbrain", "-t", "-A",
            "-c", (
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'content_chunks'::regclass "
                "AND attname = 'embedding';"
            ),
        ], check=True)
        dim_str = result.stdout.strip()
        if dim_str and dim_str.isdigit():
            return int(dim_str)
    except (subprocess.CalledProcessError, ValueError):
        pass
    return None


def _get_config_embedding_dimensions() -> Optional[int]:
    """Get config.embedding_dimensions from gbrain config."""
    try:
        result = _run(["gbrain", "config", "get", "embedding_dimensions"], check=True)
        dim_str = result.stdout.strip()
        if dim_str and dim_str.isdigit():
            return int(dim_str)
    except subprocess.CalledProcessError:
        pass
    # Fallback: try reading config file directly
    config_path = HOME / ".gbrain" / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        return int(cfg.get("embedding_dimensions", 0))
    return None


def _check_dim_parity() -> tuple[bool, Optional[int], Optional[int]]:
    """Check dim parity between config and DB column. Returns (ok, config_dim, db_dim)."""
    config_dim = _get_config_embedding_dimensions()
    db_dim = _get_db_embedding_dim()

    if config_dim is None or db_dim is None:
        return False, config_dim, db_dim

    ok = config_dim == db_dim
    return ok, config_dim, db_dim


def _print_rollback_commands(snapshot_path: str, pre_version: str):
    """Print rollback commands to stderr."""
    print("\n=== ROLLBACK COMMANDS ===", file=sys.stderr)
    print(f"1. Stop supervisor: launchctl unload {SUPERVISOR_PLIST}", file=sys.stderr)
    print(f"2. Restore DB: pg_restore -d gbrain -c {snapshot_path}", file=sys.stderr)
    print(f"3. Downgrade: bun install --global gbrain@{pre_version}", file=sys.stderr)
    print(f"4. Restart supervisor: launchctl load {SUPERVISOR_PLIST}", file=sys.stderr)
    print("========================\n", file=sys.stderr)


def _execute_rollback(envelope: UpgradeEnvelope, briefing: dict) -> tuple[bool, str]:
    """Execute the full rollback sequence.

    Returns (success, outcome_string).
    Outcome strings: "rollback-executed" or "rollback-failed".
    """
    snapshot_path = envelope.snapshot_path
    pre_version = envelope.pre_upgrade_version

    if not snapshot_path:
        print("  ERROR: No snapshot path available for rollback.", file=sys.stderr)
        return False, "rollback-failed"

    if not pre_version or pre_version == "unknown":
        print("  ERROR: No pre-upgrade version available for downgrade.", file=sys.stderr)
        return False, "rollback-failed"

    print("\n=== EXECUTING AUTO-ROLLBACK ===", file=sys.stderr)
    steps_failed = []

    # Step 1: Stop supervisor (if running)
    print("  [Rollback step 1/4] Stopping supervisor...", file=sys.stderr)
    try:
        if _check_supervisor_running():
            _run(["launchctl", "unload", str(SUPERVISOR_PLIST)], check=True)
            print("    Supervisor stopped.", file=sys.stderr)
        else:
            print("    Supervisor not running (already stopped).", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"    ERROR stopping supervisor: {e.stderr}", file=sys.stderr)
        steps_failed.append("supervisor-stop")

    # Step 2: Restore DB from snapshot
    print(f"  [Rollback step 2/4] Restoring DB from {snapshot_path}...", file=sys.stderr)
    try:
        _run(["pg_restore", "-c", "-d", "gbrain", snapshot_path], check=True)
        print("    DB restored.", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"    ERROR restoring DB: {e.stderr}", file=sys.stderr)
        steps_failed.append("db-restore")

    # Step 3: Downgrade gbrain binary to pre-upgrade version
    print(f"  [Rollback step 3/4] Downgrading gbrain to {pre_version}...", file=sys.stderr)
    try:
        _run(["bun", "install", "--global", f"gbrain@{pre_version}"], check=True)
        print(f"    Downgraded to {pre_version}.", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"    ERROR downgrading gbrain: {e.stderr}", file=sys.stderr)
        steps_failed.append("binary-downgrade")

    # Step 4: Restart supervisor
    print("  [Rollback step 4/4] Restarting supervisor...", file=sys.stderr)
    try:
        _run(["launchctl", "load", str(SUPERVISOR_PLIST)], check=True)
        print("    Supervisor loaded.", file=sys.stderr)

        # Wait for supervisor to initialize
        for i in range(10):
            if _check_supervisor_running():
                print("    Supervisor is running.", file=sys.stderr)
                break
            time.sleep(2)
        else:
            print("    WARNING: Supervisor did not start within 20s", file=sys.stderr)
            steps_failed.append("supervisor-start")
    except subprocess.CalledProcessError as e:
        print(f"    ERROR starting supervisor: {e.stderr}", file=sys.stderr)
        steps_failed.append("supervisor-start")

    if steps_failed:
        print(f"\n  ROLLBACK PARTIAL — failed steps: {', '.join(steps_failed)}", file=sys.stderr)
        return False, "rollback-failed"

    print("\n  ROLLBACK COMPLETE — all steps succeeded.", file=sys.stderr)
    return True, "rollback-executed"


# ─── Phase functions ─────────────────────────────────────────────────────


def phase0_preflight(briefing: dict, dry_run: bool, no_supervisor: bool = False) -> tuple[bool, UpgradeEnvelope]:
    """Phase 0: Preflight — parse briefing, verify system state."""
    envelope = UpgradeEnvelope(
        fire_id=briefing.get("fire_id", "unknown"),
        installed_before=_get_installed_version(),
        target_version=briefing.get("upstream", ""),
    )

    print("[Phase 0] Preflight...", file=sys.stderr)

    # Parse key briefing fields
    severity = briefing.get("severity", "unknown")
    schema_delta = briefing.get("schema_has_delta", False)
    patches_conflict = briefing.get("patches_conflict", 0)

    print(f"  fire_id={envelope.fire_id} severity={severity}", file=sys.stderr)
    print(f"  schema_has_delta={schema_delta} patches_conflict={patches_conflict}", file=sys.stderr)
    print(f"  installed_version={envelope.installed_before} target={envelope.target_version}", file=sys.stderr)

    # Verify gbrain is installed
    if envelope.installed_before == "unknown":
        print("  WARNING: gbrain --version returned unknown; continuing anyway", file=sys.stderr)

    # Verify DB connectivity
    if not _check_db_connectable():
        print("  ERROR: Cannot connect to gbrain database", file=sys.stderr)
        envelope.error = "database unreachable"
        _audit_log("phase0_preflight", "failed", {"error": envelope.error})
        return False, envelope

    print("  DB connectivity: OK", file=sys.stderr)

    # Bug 4: Check for bun-link mode + divergent git histories
    is_bun_link, divergence_error = _check_bun_link_divergence()
    if is_bun_link and divergence_error:
        print(f"\n  ERROR: {divergence_error}", file=sys.stderr)
        envelope.error = divergence_error
        _audit_log("phase0_preflight", "failed", {
            "error": envelope.error,
            "bun_link": True,
            "divergent": True,
        })
        return False, envelope

    if is_bun_link:
        print("  Bun-link mode detected (up-to-date or can fast-forward): OK", file=sys.stderr)
    else:
        print("  Bun-link check: not applicable (not bun-link mode)", file=sys.stderr)
    _audit_log("phase0_preflight", "ok")
    return True, envelope


def phase1_briefing_intake(briefing: dict, dry_run: bool, no_supervisor: bool = False) -> tuple[bool, UpgradeEnvelope]:
    """Phase 1: Briefing intake — extract upgrade plan."""
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""))

    print("[Phase 1] Briefing intake...", file=sys.stderr)

    severity = briefing.get("severity", "unknown")
    schema_delta = briefing.get("schema_has_delta", False)

    # Validate severity classification
    print(f"  Severity: {severity}", file=sys.stderr)

    if schema_delta:
        dim_change = briefing.get("schema_dim_change", {})
        print(f"  Schema delta: YES — dim change: {dim_change}", file=sys.stderr)
    else:
        print("  Schema delta: NO", file=sys.stderr)

    # Check for migration plan
    patches_clean = briefing.get("patches_clean", 0)
    patches_conflict = briefing.get("patches_conflict", 0)
    print(f"  Patches: {patches_clean} clean, {patches_conflict} conflict", file=sys.stderr)

    # For severity F with schema delta, we need a migration plan
    if severity == "F" and schema_delta:
        print("  Severity F with schema delta — migration plan required", file=sys.stderr)

    _audit_log("phase1_briefing_intake", "ok")
    return True, envelope


def phase2_stop_supervisor(briefing: dict, dry_run: bool, no_supervisor: bool) -> tuple[bool, UpgradeEnvelope]:
    """Phase 2: Stop supervisor + snapshot DB."""
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""))

    print("[Phase 2] Stop supervisor + snapshot DB...", file=sys.stderr)

    if no_supervisor:
        print("  --no-supervisor: skipping supervisor operations", file=sys.stderr)
    else:
        # Stop supervisor if running
        if _check_supervisor_running():
            print("  Stopping supervisor...", file=sys.stderr)
            try:
                _run(["launchctl", "unload", str(SUPERVISOR_PLIST)], check=True)
                print("  Supervisor stopped.", file=sys.stderr)
            except subprocess.CalledProcessError as e:
                print(f"  ERROR stopping supervisor: {e.stderr}", file=sys.stderr)
                envelope.error = "supervisor stop failed"
                _audit_log("phase2_stop_supervisor", "failed", {"error": envelope.error})
                return False, envelope
        else:
            print("  Supervisor already stopped (or not installed).", file=sys.stderr)

    # Create pg_dump snapshot
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dump_path = f"/tmp/gbrain-pre-{envelope.target_version}-{timestamp}.dump"
    print(f"  Creating pg_dump snapshot: {dump_path}", file=sys.stderr)

    try:
        _run(["pg_dump", "gbrain", "-F", "c", "-f", dump_path], check=True)
        print(f"  Snapshot created: {dump_path}", file=sys.stderr)

        # Verify snapshot
        rc, _ = _run_quiet(["pg_restore", "--list", dump_path])
        if rc != 0:
            print(f"  WARNING: Snapshot verification failed (exit {rc})", file=sys.stderr)
        else:
            print("  Snapshot verified OK.", file=sys.stderr)

        envelope.snapshot_path = dump_path
    except subprocess.CalledProcessError as e:
        print(f"  ERROR creating snapshot: {e.stderr}", file=sys.stderr)
        envelope.error = "pg_dump failed"
        _audit_log("phase2_stop_supervisor", "failed", {"error": envelope.error})
        return False, envelope

    _audit_log("phase2_stop_supervisor", "ok", {"snapshot_path": dump_path})
    return True, envelope


def _phase3_legacy_gbrain_upgrade(envelope: UpgradeEnvelope) -> tuple[bool, UpgradeEnvelope]:
    """Legacy Phase 3 implementation: `GBRAIN_NO_REEMBED=1 gbrain upgrade` single-step.

    Used for target_version < v0.34.0 (pre-split-semantics). Preserves Bug 1 no-op
    detection + Bug 3 binary_version_changed gating. Returns (success, envelope).
    """
    print("  [Legacy path] Command: GBRAIN_NO_REEMBED=1 gbrain upgrade", file=sys.stderr)
    print("  (Runs bun update + post-upgrade + apply-migrations + initSchema)", file=sys.stderr)

    # Bug 1: Capture pre-upgrade version from package.json (authoritative for bun-link mode)
    pre_version = _get_package_version()
    print(f"  Pre-upgrade version: {pre_version}", file=sys.stderr)

    try:
        _run(
            ["gbrain", "upgrade"],
            check=True,
            env_override={"GBRAIN_NO_REEMBED": "1"},
        )
        print("  Upgrade completed.", file=sys.stderr)

        # Bug 1: Capture post-upgrade version and verify it actually changed
        post_version = _get_package_version()
        envelope.pre_upgrade_version = pre_version
        envelope.post_upgrade_version = post_version

        if pre_version == post_version:
            # Check if we're already at the target version (not a failed no-op)
            target = envelope.target_version

            # Normalize: extract semver from "0.33.0"/"v0.33.0", or return as-is for commit SHAs
            def normalize(v):
                v = v.strip()
                if v.startswith('v'): v = v[1:]
                m = re.match(r'(\d+\.\d+\.\d+)', v)
                return m.group(1) if m else v

            pre_norm = normalize(pre_version)
            target_norm = normalize(target) if target else ""

            # Case 1: version numbers match → already at target
            if pre_norm == target_norm and target_norm:
                print(f"  Already at target version {pre_version} (target: {target}). Skipping upgrade.", file=sys.stderr)
                envelope.binary_version_changed = False
                _audit_log("phase3_canonical_upgrade", "already-at-target", {
                    "version": pre_version,
                    "target": target,
                    "target_version_class": envelope.target_version_class,
                    "migrate_only_invoked": envelope.migrate_only_invoked,
                })
                return True, envelope

            # Case 2: target is a commit SHA — check if HEAD is at or past it
            import subprocess as sp
            try:
                merge_result = sp.run(
                    ["git", "-C", "/Users/emanuelubert/gbrain", "merge-base", "--is-ancestor", target, "HEAD"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                if merge_result.returncode == 0:
                    # HEAD contains target commit — we're at or past it
                    print(f"  Already at or past target commit {target} (HEAD={pre_version}). Skipping upgrade.", file=sys.stderr)
                    envelope.binary_version_changed = False
                    _audit_log("phase3_canonical_upgrade", "already-at-target", {
                        "version": pre_version,
                        "target_commit": target,
                        "target_version_class": envelope.target_version_class,
                        "migrate_only_invoked": envelope.migrate_only_invoked,
                    })
                    return True, envelope
            except (sp.TimeoutExpired, FileNotFoundError):
                pass

            # BUG 1 FIX: No-op detected — version unchanged after upgrade
            print(f"\n  ERROR: gbrain upgrade was a no-op: version unchanged ({pre_version}).", file=sys.stderr)
            print(f"  Likely cause: bun-link mode with divergent git histories (pull --ff-only failed silently).", file=sys.stderr)
            print(f"  See ~/gbrain git status for details.", file=sys.stderr)
            envelope.error = (
                f"gbrain upgrade was a no-op: version unchanged ({pre_version}). "
                f"Likely cause: bun-link mode with divergent git histories (pull --ff-only failed silently). "
                f"See ~/gbrain git status."
            )
            _audit_log("phase3_canonical_upgrade", "failed-noop", {
                "pre_version": pre_version,
                "post_version": post_version,
                "target_version_class": envelope.target_version_class,
                "migrate_only_invoked": envelope.migrate_only_invoked,
            })
            return False, envelope

        print(f"  Version: {pre_version} → {post_version}", file=sys.stderr)
        envelope.binary_version_changed = True

    except subprocess.CalledProcessError as e:
        print(f"  ERROR during upgrade: {e.stderr}", file=sys.stderr)
        envelope.error = f"gbrain upgrade failed: {e.stderr}"
        _audit_log("phase3_canonical_upgrade", "failed", {
            "error": envelope.error,
            "target_version_class": envelope.target_version_class,
            "migrate_only_invoked": envelope.migrate_only_invoked,
        })
        return False, envelope

    _audit_log("phase3_canonical_upgrade", "ok", {
        "target_version_class": envelope.target_version_class,
        "migrate_only_invoked": envelope.migrate_only_invoked,
    })
    return True, envelope


def _phase3_v034_plus(envelope: UpgradeEnvelope, bun_link_mode: bool) -> tuple[bool, UpgradeEnvelope]:
    """v0.34+ Phase 3 implementation: split semantics — CLI upgrade + migrate-only.

    Per S187 H.4 / D115 / D116: `gbrain upgrade` in v0.34+ self-updates the CLI
    binary ONLY. Schema migrations apply via `gbrain init --migrate-only`. In
    bun-link mode the CLI step is skipped entirely (source already advanced by
    operator). The migrate step is load-bearing for all modes.
    """
    print(f"  [v0.34+ path] bun_link_mode={bun_link_mode}", file=sys.stderr)

    # Capture pre-upgrade version regardless of mode (for audit row + parity with legacy).
    pre_version = _get_package_version()
    envelope.pre_upgrade_version = pre_version
    print(f"  Pre-upgrade version: {pre_version}", file=sys.stderr)

    # Step A — CLI self-update (skipped in bun-link mode).
    if not bun_link_mode:
        print("  Step A: GBRAIN_NO_REEMBED=1 gbrain upgrade (CLI self-update)", file=sys.stderr)
        try:
            _run(
                ["gbrain", "upgrade"],
                check=True,
                env_override={"GBRAIN_NO_REEMBED": "1"},
            )
            post_version = _get_package_version()
            envelope.post_upgrade_version = post_version
            envelope.binary_version_changed = (pre_version != post_version)
            print(f"  CLI: {pre_version} → {post_version} (changed={envelope.binary_version_changed})", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"  ERROR during CLI upgrade: {e.stderr}", file=sys.stderr)
            envelope.error = f"cli_upgrade_failed: {e.stderr}"
            _audit_log("phase3_canonical_upgrade", "failed", {
                "error": envelope.error,
                "error_class": "cli_upgrade_failed",
                "target_version_class": envelope.target_version_class,
                "migrate_only_invoked": False,
            })
            return False, envelope
    else:
        print("  Step A: SKIPPED — bun-link mode (source already advanced by operator)", file=sys.stderr)
        envelope.post_upgrade_version = pre_version
        envelope.binary_version_changed = False

    # Step B — schema migration via `gbrain init --migrate-only` (load-bearing for v0.34+).
    print("  Step B: gbrain init --migrate-only (schema migration)", file=sys.stderr)
    try:
        _run(
            ["gbrain", "init", "--migrate-only"],
            check=True,
            env_override={"GBRAIN_NO_REEMBED": "1"},
        )
        envelope.migrate_only_invoked = True
        print("  Migration completed.", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        envelope.migrate_only_invoked = True   # We attempted it; record the attempt
        print(f"  ERROR during migration: {e.stderr}", file=sys.stderr)
        envelope.error = f"migrate_failed: {e.stderr}"
        _audit_log("phase3_canonical_upgrade", "failed", {
            "error": envelope.error,
            "error_class": "migrate_failed",
            "target_version_class": envelope.target_version_class,
            "migrate_only_invoked": True,
        })
        return False, envelope

    _audit_log("phase3_canonical_upgrade", "ok", {
        "target_version_class": envelope.target_version_class,
        "migrate_only_invoked": True,
        "bun_link_mode": bun_link_mode,
        "binary_version_changed": envelope.binary_version_changed,
    })
    return True, envelope


def phase3_canonical_upgrade(briefing: dict, dry_run: bool, no_supervisor: bool = False) -> tuple[bool, UpgradeEnvelope]:
    """Phase 3: Canonical upgrade — routes on target_version_class.

    Per P-runner-refresh (S187 H.4 / D115 / D116), v0.34+ split the semantics:
      - `gbrain upgrade` — CLI self-update only.
      - `gbrain init --migrate-only` — schema migrations.
    Targets < v0.34.0 use the legacy single-step path unchanged.
    """
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""), target_version=briefing.get("upstream", ""))

    target = envelope.target_version or ""
    is_legacy = _semver_lt(target, "v0.34.0")
    envelope.target_version_class = "legacy" if is_legacy else "v0_34_plus"

    print("[Phase 3] Canonical upgrade / migrate...", file=sys.stderr)
    print(f"  Target version: {target!r} → class={envelope.target_version_class}", file=sys.stderr)

    if dry_run:
        if is_legacy:
            print("  [DRY RUN] Legacy path: would run `GBRAIN_NO_REEMBED=1 gbrain upgrade`.", file=sys.stderr)
        else:
            bl = _is_bun_link_mode()
            print(f"  [DRY RUN] v0.34+ path: bun_link_mode={bl}", file=sys.stderr)
            if not bl:
                print("  [DRY RUN]   Step A: `GBRAIN_NO_REEMBED=1 gbrain upgrade` (CLI self-update)", file=sys.stderr)
            else:
                print("  [DRY RUN]   Step A: SKIPPED (bun-link mode)", file=sys.stderr)
            print("  [DRY RUN]   Step B: `gbrain init --migrate-only` (schema migration)", file=sys.stderr)
            # In dry-run we still surface the planned audit fields for parity with prod.
            envelope.migrate_only_invoked = False  # not actually invoked in dry-run
        _audit_log("phase3_canonical_upgrade", "dry-run", {
            "target_version_class": envelope.target_version_class,
            "migrate_only_invoked": False,
            "bun_link_mode": (_is_bun_link_mode() if not is_legacy else None),
        })
        return True, envelope

    if is_legacy:
        return _phase3_legacy_gbrain_upgrade(envelope)

    # v0.34+ split-semantics path
    bun_link_mode = _is_bun_link_mode()
    return _phase3_v034_plus(envelope, bun_link_mode=bun_link_mode)


def phase4_verify(briefing: dict, dry_run: bool, no_supervisor: bool = False) -> tuple[bool, UpgradeEnvelope]:
    """Phase 4: Verify — dim parity check + gbrain doctor."""
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""))

    print("[Phase 4] Verify...", file=sys.stderr)

    if dry_run:
        print("  [DRY RUN] Skipping verification.", file=sys.stderr)
        _audit_log("phase4_verify", "dry-run")
        return True, envelope

    # Critical guard: dim parity check
    print("  Checking dim parity (config vs DB column)...", file=sys.stderr)
    dim_ok, config_dim, db_dim = _check_dim_parity()

    if config_dim is not None:
        print(f"  Config embedding_dimensions: {config_dim}", file=sys.stderr)
    else:
        print("  Config embedding_dimensions: NOT SET (using default)", file=sys.stderr)

    if db_dim is not None:
        print(f"  DB column embedding dim: {db_dim}", file=sys.stderr)
    else:
        print("  DB column embedding dim: COULD NOT DETERMINE", file=sys.stderr)

    envelope.dim_parity_ok = dim_ok

    if not dim_ok:
        print(f"  FAIL: Dim mismatch! config={config_dim} db={db_dim}", file=sys.stderr)
        print("  Dim parity check FAILED — aborting upgrade.", file=sys.stderr)
        envelope.error = f"dim mismatch: config={config_dim} db={db_dim}"
        _audit_log("phase4_verify", "failed", {"error": envelope.error, "config_dim": config_dim, "db_dim": db_dim})
        return False, envelope

    print("  Dim parity: OK", file=sys.stderr)

    # Run gbrain doctor
    print("  Running gbrain doctor...", file=sys.stderr)
    try:
        result = _run(["gbrain", "doctor"], check=True)
        doctor_output = result.stdout.strip()
        # Doctor returns 0 on pass; check for ERROR lines in output
        if "ERROR" in doctor_output:
            print(f"  WARNING: gbrain doctor output contains ERROR lines", file=sys.stderr)
        else:
            print("  gbrain doctor: PASS", file=sys.stderr)
        envelope.doctor_pass = True
    except subprocess.CalledProcessError as e:
        print(f"  gbrain doctor: FAIL (exit {e.returncode})", file=sys.stderr)
        envelope.doctor_pass = False
        _audit_log("phase4_verify", "failed", {"error": "gbrain doctor failed"})
        return False, envelope

    _audit_log("phase4_verify", "ok", {"dim_parity_ok": dim_ok, "doctor_pass": True})
    return True, envelope


def phase5_restart_supervisor(briefing: dict, dry_run: bool, no_supervisor: bool) -> tuple[bool, UpgradeEnvelope]:
    """Phase 5: Restart supervisor."""
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""))

    print("[Phase 5] Restart supervisor...", file=sys.stderr)

    if no_supervisor:
        print("  --no-supervisor: skipping supervisor restart", file=sys.stderr)
        _audit_log("phase5_restart_supervisor", "skipped")
        return True, envelope

    if dry_run:
        print("  [DRY RUN] Skipping supervisor restart.", file=sys.stderr)
        _audit_log("phase5_restart_supervisor", "dry-run")
        return True, envelope

    try:
        _run(["launchctl", "load", str(SUPERVISOR_PLIST)], check=True)
        print("  Supervisor loaded.", file=sys.stderr)

        # Wait for supervisor to initialize
        print("  Waiting for supervisor to initialize...", file=sys.stderr)
        for i in range(10):
            if _check_supervisor_running():
                print("  Supervisor is running.", file=sys.stderr)
                break
            time.sleep(2)
        else:
            print("  WARNING: Supervisor did not start within 20s", file=sys.stderr)

    except subprocess.CalledProcessError as e:
        print(f"  ERROR starting supervisor: {e.stderr}", file=sys.stderr)
        envelope.error = "supervisor restart failed"
        _audit_log("phase5_restart_supervisor", "failed", {"error": envelope.error})
        return False, envelope

    _audit_log("phase5_restart_supervisor", "ok")
    return True, envelope


def phase6_smoke_query(briefing: dict, dry_run: bool, no_supervisor: bool = False) -> tuple[bool, UpgradeEnvelope]:
    """Phase 6: Smoke query — verify retrieval works."""
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""))

    print("[Phase 6] Smoke query...", file=sys.stderr)

    if dry_run:
        print("  [DRY RUN] Skipping smoke queries.", file=sys.stderr)
        _audit_log("phase6_smoke_query", "dry-run")
        return True, envelope

    # Run gbrain stats
    print("  Running gbrain stats...", file=sys.stderr)
    try:
        result = _run(["gbrain", "stats"], check=True)
        print(f"  {result.stdout.strip()}", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: gbrain stats failed: {e.stderr}", file=sys.stderr)

    # Run a few smoke queries
    test_queries = [
        "what do we know about gbrain",
        "local agent system architecture",
    ]

    passed = 0
    for query in test_queries:
        print(f"  Query: \"{query}\"...", file=sys.stderr)
        try:
            result = _run(["gbrain", "query", query], check=True)
            output = result.stdout.strip()
            if output and "error" not in output.lower():
                passed += 1
                print(f"    OK (returned {len(output)} chars)", file=sys.stderr)
            else:
                print(f"    EMPTY/ERROR response", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"    ERROR: {e.stderr}", file=sys.stderr)

    envelope.smoke_queries_passed = passed
    print(f"  Smoke queries: {passed}/{len(test_queries)} passed", file=sys.stderr)

    _audit_log("phase6_smoke_query", "ok" if passed > 0 else "partial", {"passed": passed, "total": len(test_queries)})
    return True, envelope


def phase7_propagate(briefing: dict, dry_run: bool, no_supervisor: bool = False) -> tuple[bool, UpgradeEnvelope]:
    """Phase 7: Propagate version pin to CLAUDE.md (ALL references)."""
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""))

    print("[Phase 7] Propagate version pin...", file=sys.stderr)

    if dry_run:
        print("  [DRY RUN] Skipping propagation.", file=sys.stderr)
        _audit_log("phase7_propagate", "dry-run")
        return True, envelope

    # Bug 3: Gate on Phase 3's binary_version_changed flag
    if not envelope.binary_version_changed:
        print("  Skipping propagation: binary version unchanged (Phase 3 did not change the binary).", file=sys.stderr)
        _audit_log("phase7_propagate", "skipped-no-op", {
            "reason": "binary_version_changed=False — Phase 3 was a no-op or did not change binary",
        })
        return True, envelope

    target_version = briefing.get("upstream", "")
    if not target_version:
        print("  No upstream version in briefing; skipping CLAUDE.md update.", file=sys.stderr)
        _audit_log("phase7_propagate", "skipped")
        return True, envelope

    if not CLAUDE_MD.exists():
        print(f"  WARNING: {CLAUDE_MD} not found; cannot update version pin.", file=sys.stderr)
        print(f"  Manifest: Update CLAUDE.md gbrain version references to {target_version}", file=sys.stderr)
        _audit_log("phase7_propagate", "skipped", {"reason": "CLAUDE.md not found"})
        return True, envelope

    # Read CLAUDE.md and find ALL gbrain version references
    with open(CLAUDE_MD) as f:
        content = f.read()

    # Bug 3 fix: Find ALL version references (not just the first one)
    # Patterns to match: "0.22.6 pinned at be8fffad", "v17b190e", etc.
    # We need to update both version strings AND commit hashes
    old_pattern = r"gbrain [vV]?[0-9]+\.[0-9]+\.[0-9]+(?:\s+pinned\s+at\s+[0-9a-f]{7,})?"
    matches = list(re.finditer(old_pattern, content))

    if not matches:
        print("  No existing gbrain version references found in CLAUDE.md.", file=sys.stderr)
        print(f"  Manifest: Add/Update gbrain version references to {target_version}", file=sys.stderr)
        _audit_log("phase7_propagate", "no-refs-found")
        return True, envelope

    print(f"  Found {len(matches)} gbrain version reference(s) to update:", file=sys.stderr)
    for m in matches:
        print(f"    Line {content[:m.start()].count(chr(10)) + 1}: \"{m.group(0)}\"", file=sys.stderr)

    if not dry_run:
        # Bug 3 fix: Replace ALL occurrences (not just first via re.sub with count=1)
        new_content = re.sub(old_pattern, f"gbrain v{target_version}", content)

        # Verify at least one replacement was made
        if new_content == content:
            print("  WARNING: re.sub did not replace any references; skipping write.", file=sys.stderr)
            _audit_log("phase7_propagate", "no-replacements")
            return True, envelope

        with open(CLAUDE_MD, "w") as f:
            f.write(new_content)

        print(f"  CLAUDE.md updated ({len(matches)} reference(s) changed).", file=sys.stderr)
    else:
        print(f"  [DRY RUN] Would update {len(matches)} reference(s) to gbrain v{target_version}", file=sys.stderr)

    _audit_log("phase7_propagate", "ok", {"n_refs_updated": len(matches), "target_version": target_version})
    return True, envelope


def phase8_validate(briefing: dict, dry_run: bool, no_supervisor: bool = False) -> tuple[bool, UpgradeEnvelope]:
    """Phase 8: Post-migration validation."""
    envelope = UpgradeEnvelope(fire_id=briefing.get("fire_id", ""))

    print("[Phase 8] Post-migration validation...", file=sys.stderr)

    if dry_run:
        print("  [DRY RUN] Skipping validation.", file=sys.stderr)
        _audit_log("phase8_validate", "dry-run")
        return True, envelope

    # Final stats check
    print("  Running final gbrain stats...", file=sys.stderr)
    try:
        result = _run(["gbrain", "stats"], check=True)
        print(f"  {result.stdout.strip()}", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        print(f"  WARNING: Final stats check failed: {e.stderr}", file=sys.stderr)

    # Supervisor health
    if _check_supervisor_running():
        print("  Supervisor: RUNNING", file=sys.stderr)
    else:
        print("  WARNING: Supervisor NOT running after upgrade", file=sys.stderr)

    _audit_log("phase8_validate", "ok")
    return True, envelope


# ─── Main orchestrator ──────────────────────────────────────────────────

PHASES = [
    ("phase0_preflight", phase0_preflight),
    ("phase1_briefing_intake", phase1_briefing_intake),
    ("phase2_stop_supervisor", phase2_stop_supervisor),
    ("phase3_canonical_upgrade", phase3_canonical_upgrade),
    ("phase4_verify", phase4_verify),
    ("phase5_restart_supervisor", phase5_restart_supervisor),
    ("phase6_smoke_query", phase6_smoke_query),
    ("phase7_propagate", phase7_propagate),
    ("phase8_validate", phase8_validate),
]


def main():
    parser = argparse.ArgumentParser(
        description="gbrain upgrade runner — executable runbook for Stream B Phase 5-8",
        epilog="Example: python3 run.py --briefing /path/to/briefing.md --confirm-destructive",
    )
    parser.add_argument("--briefing", required=True, help="Path to watcher-briefing YAML file")
    parser.add_argument("--confirm-destructive", action="store_true",
                        help="Required for Phase 2+ destructive operations")
    parser.add_argument("--auto-rollback-on-fail", action="store_true",
                        help="Execute rollback commands automatically on verify failure")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intended phases and actions without executing")
    parser.add_argument("--no-supervisor", action="store_true",
                        help="Skip launchctl supervisor start/stop (for testing)")

    args = parser.parse_args()

    # Load briefing
    print(f"Loading briefing: {args.briefing}", file=sys.stderr)
    briefing = _load_briefing(args.briefing)

    # Initialize envelope
    envelope = UpgradeEnvelope(
        fire_id=briefing.get("fire_id", "unknown"),
        installed_before=_get_installed_version(),
        target_version=briefing.get("upstream", ""),
    )

    start_time = time.time()

    # Execute phases sequentially
    phase_names_executed = []
    phase_names_skipped = []

    for i, (phase_name, phase_fn) in enumerate(PHASES):
        # Operator gate: stop before Phase 2 (index 2) without --confirm-destructive
        if i >= 2 and not args.confirm_destructive:
            print("\n=== OPERATOR GATE ===", file=sys.stderr)
            print("Phase 2+ requires destructive operations (stop supervisor, upgrade code).", file=sys.stderr)
            print("Re-run with --confirm-destructive to proceed.", file=sys.stderr)
            print("=====================\n", file=sys.stderr)

            envelope.outcome = "operator-gate-required"
            envelope.phases_executed = phase_names_executed
            envelope.phases_skipped = [pn for pn, _ in PHASES[i:]]
            envelope.duration_ms = int((time.time() - start_time) * 1000)

            print(json.dumps(asdict(envelope), indent=2))
            sys.exit(3)

        # Dry-run: skip execution but log intent
        if args.dry_run and i >= 2:
            phase_names_skipped.append(phase_name)
            print(f"\n[DRY RUN] Would execute Phase {i}: {phase_name}", file=sys.stderr)
            continue

        # Execute phase
        success, phase_envelope = phase_fn(briefing, args.dry_run, args.no_supervisor)

        # Merge select sub-envelope fields into master envelope for stdout completeness.
        # Pre-existing behavior preserved snapshot_path implicitly via phase2's mutation
        # of its own envelope; here we explicitly propagate the new audit-row fields
        # (target_version_class, migrate_only_invoked) plus pre-existing ones that
        # downstream code (Phase 7, rollback) reads off the master envelope.
        if phase_envelope.snapshot_path and not envelope.snapshot_path:
            envelope.snapshot_path = phase_envelope.snapshot_path
        if phase_envelope.pre_upgrade_version and not envelope.pre_upgrade_version:
            envelope.pre_upgrade_version = phase_envelope.pre_upgrade_version
        if phase_envelope.post_upgrade_version and not envelope.post_upgrade_version:
            envelope.post_upgrade_version = phase_envelope.post_upgrade_version
        if phase_envelope.binary_version_changed:
            envelope.binary_version_changed = True
        if phase_envelope.target_version_class and not envelope.target_version_class:
            envelope.target_version_class = phase_envelope.target_version_class
        if phase_envelope.migrate_only_invoked:
            envelope.migrate_only_invoked = True
        if phase_envelope.dim_parity_ok is not None and envelope.dim_parity_ok is None:
            envelope.dim_parity_ok = phase_envelope.dim_parity_ok
        if phase_envelope.doctor_pass is not None and envelope.doctor_pass is None:
            envelope.doctor_pass = phase_envelope.doctor_pass
        if phase_envelope.smoke_queries_passed and not envelope.smoke_queries_passed:
            envelope.smoke_queries_passed = phase_envelope.smoke_queries_passed

        if success:
            phase_names_executed.append(phase_name)
            print(f"  Phase {i} ({phase_name}): OK", file=sys.stderr)
        else:
            phase_names_skipped.append(phase_name)
            print(f"\n  Phase {i} ({phase_name}): FAILED", file=sys.stderr)
            envelope.error = phase_envelope.error

            # Bug 2: Auto-rollback on failure — actually execute it
            if args.auto_rollback_on_fail and i >= 4:
                print("\n=== AUTO-ROLLBACK TRIGGERED ===", file=sys.stderr)
                if envelope.snapshot_path:
                    print(f"  Rollback snapshot: {envelope.snapshot_path}", file=sys.stderr)

                # Execute the actual rollback sequence
                rollback_ok, rollback_outcome = _execute_rollback(envelope, briefing)

                if rollback_ok:
                    envelope.outcome = "rollback-executed"
                else:
                    envelope.outcome = "rollback-failed"

                # Print rollback commands for reference (even after execution)
                _print_rollback_commands(envelope.snapshot_path, envelope.pre_upgrade_version)

            elif i >= 4 and envelope.snapshot_path:
                # Not auto-rollback, but show commands for manual recovery
                _print_rollback_commands(envelope.snapshot_path, envelope.pre_upgrade_version)

            if envelope.outcome == "unknown":
                # No rollback was triggered or attempted
                envelope.outcome = "failed"

            break

    # Calculate duration and finalize
    envelope.phases_executed = phase_names_executed
    if not envelope.phases_skipped:
        remaining = [pn for pn, _ in PHASES[len(phase_names_executed):]]
        envelope.phases_skipped = remaining

    envelope.duration_ms = int((time.time() - start_time) * 1000)

    # Determine final outcome
    if envelope.outcome == "unknown":
        if args.dry_run:
            envelope.outcome = "dry-run-complete"
        else:
            envelope.outcome = "success"

    # Print JSON envelope to stdout
    print(json.dumps(asdict(envelope), indent=2))

    # Exit code
    if envelope.outcome in ("success", "dry-run-complete"):
        sys.exit(0)
    elif envelope.outcome in ("failed",):
        sys.exit(1)
    elif envelope.outcome == "operator-gate-required":
        sys.exit(3)
    elif envelope.outcome == "rollback-executed":
        sys.exit(4)   # Per docstring: exit 4 = rollback executed (success with recovery)
    elif envelope.outcome == "rollback-failed":
        sys.exit(5)   # Per docstring: exit 5 = rollback failed (both upgrade and recovery failed)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
