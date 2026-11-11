"""cloud-dispatch: start, steer and watch Claude Code cloud sessions from Hermes.

`claude --cloud "<task>"` refuses to run without a TTY, so cloud_start runs it
under a pseudo-terminal and parses the session ID it prints. Follow-ups use
`claude -p --cloud <id>`, which works headless.

A cloud session's replies can't be read from the CLI; its results land on
GitHub. cloud_status reports the branch and PR state, and cloud_watch polls it
in a background thread and injects a [cloud-dispatch] message into the calling
session (ctx.inject_message) when a PR opens, its checks finish, or it is
merged or closed. Watches live in the gateway process and are lost on restart.

cloud_watch needs plugins.entries.cloud-dispatch.allow_gateway_injection: true
in the profile's config.yaml.

Requires `claude` (signed in with a claude.ai account), `git` and `gh` (signed
in). Linux and macOS.
"""

from __future__ import annotations

import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
SESSION_RE = re.compile(r"\b((?:session|cse)_[A-Za-z0-9]{8,})\b")
URL_RE = re.compile(r"https://claude\.ai/code/[^\s]+")
TITLE_RE = re.compile(r"Created cloud session:\s*(.+)")
NOT_ON_GITHUB_RE = re.compile(r"Branch \S+ is not on GitHub[^\n]*")
GITHUB_RE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")

# Kept but not registered until the basics work (also left out of plugin.yaml).
HIDDEN_TOOLS = {"cloud_watch"}
BASE_REF = "origin/main"  # new branches start here; the local ref is used as-is, never fetched
START_TIMEOUT = 180  # seconds; the CLI prints the ID once the session exists
SEND_TIMEOUT = 60
WATCH_EVENTS = ("push", "pr", "checks")
DEFAULT_WATCH_EVENTS = ["pr", "checks"]
DEFAULT_WATCH_MIN = 240
POLL_SECONDS = 60
MAX_POLL_FAILURES = 5

# GUI apps and launchd/systemd services often start with a minimal PATH.
EXTRA_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.claude/local")

PENDING_CHECK_STATES = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
FAILED_CHECK_STATES = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}

_ctx = None
_watches: dict[tuple[str, str, str], threading.Thread] = {}
_watches_lock = threading.Lock()


class CloudError(Exception):
    pass


# --- helpers ---------------------------------------------------------------


def _bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        path = os.path.join(os.path.expanduser(d), name)
        if os.access(path, os.X_OK):
            return path
    raise CloudError(f"{name} not found on PATH")


def _run(argv: list[str], cwd: str | None = None, timeout: int = 30, stdin: str | None = None) -> str:
    argv = [_bin(argv[0]), *argv[1:]]
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CloudError(f"{os.path.basename(argv[0])} {argv[1]} timed out after {timeout}s")
    if p.returncode != 0:
        raise CloudError((p.stderr or p.stdout).strip() or f"{argv[0]} exited {p.returncode}")
    return p.stdout.strip()


def _git(repo_dir: str, *args: str) -> str:
    return _run(["git", "-C", repo_dir, *args])


def _path_map() -> list[tuple[str, str]]:
    """(sandbox prefix, host prefix) pairs, longest sandbox prefix first.

    Hermes's file and terminal tools may run in a Docker sandbox, so the model
    passes sandbox paths, while this plugin runs on the host. Pairs come from
    plugins.entries.cloud-dispatch.settings.path_map ({sandbox: host}) and from
    the profile's terminal.docker_volumes ("host:sandbox[:mode]")."""
    pairs: dict[str, str] = {}
    try:
        from hermes_cli.config import load_config_readonly  # not a public plugin API

        terminal = (load_config_readonly() or {}).get("terminal") or {}
        for vol in terminal.get("docker_volumes") or []:
            parts = str(vol).split(":")
            if len(parts) >= 2 and parts[0].startswith("/") and parts[1].startswith("/"):
                pairs[parts[1].rstrip("/") or "/"] = parts[0].rstrip("/") or "/"
    except Exception:
        logger.debug("cloud-dispatch: could not read terminal.docker_volumes", exc_info=True)
    if _ctx is not None:
        try:
            for sandbox, host in (_ctx.get_config("path_map", {}) or {}).items():
                pairs[str(sandbox).rstrip("/") or "/"] = os.path.expanduser(str(host)).rstrip("/") or "/"
        except Exception:
            logger.debug("cloud-dispatch: could not read path_map setting", exc_info=True)
    return sorted(pairs.items(), key=lambda p: len(p[0]), reverse=True)


def _remap(path: str, pairs: list[tuple[str, str]]) -> str | None:
    for src, dst in pairs:
        if path == src or path.startswith(src.rstrip("/") + "/"):
            return dst + path[len(src):] if src != "/" else dst.rstrip("/") + path
    return None


def _to_host(path: str) -> str:
    """Host path for a path the model gave, translating sandbox paths."""
    if os.path.exists(path):
        return path
    return _remap(path, _path_map()) or path


def _to_sandbox(path: str) -> str:
    """The sandbox's view of a host path, for messages back to the model."""
    return _remap(path, [(host, sandbox) for sandbox, host in _path_map()]) or path


def _repo_dir(args: dict) -> str:
    # No default: the session's working directory isn't reliable when Hermes
    # runs tools in a sandbox, so the model always names the repository.
    repo_dir = str(args.get("repo_dir") or "").strip()
    if not repo_dir:
        raise CloudError("repo_dir is required: a checkout of the repository (bare is fine)")
    given = repo_dir
    repo_dir = _to_host(os.path.abspath(os.path.expanduser(repo_dir)))
    shown = given if repo_dir == given else f"{given} ({repo_dir} on the host)"
    if not os.path.isdir(repo_dir):
        raise CloudError(
            f"{shown} is not a directory on the host. If Hermes runs tools in a sandbox, "
            "map its paths with plugins.entries.cloud-dispatch.settings.path_map {sandbox: host}"
        )
    try:
        _git(repo_dir, "rev-parse", "--git-dir")
    except CloudError:
        raise CloudError(f"{shown} is not inside a git repository; pass the repository itself")
    return repo_dir


def _source_checkout(repo: str) -> str:
    """The repository's parent: the bare repo, or a normal clone's main checkout.

    herdr only creates and opens worktrees from there, not from a linked
    worktree; git lists it first."""
    first = _git(repo, "worktree", "list", "--porcelain").splitlines()[0]
    return first[len("worktree "):]


def _worktree(repo_dir: str) -> str:
    """Top of the working tree containing repo_dir; a clear error for a bare repo."""
    if _git(repo_dir, "rev-parse", "--is-bare-repository") == "true":
        listing = _git(repo_dir, "worktree", "list", "--porcelain")
        all_trees: list[tuple[str, str]] = []
        path = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line.startswith("branch ") and path:
                all_trees.append((path, line[len("branch refs/heads/"):]))
                path = None
        # With a sandbox, only offer worktrees mounted inside it.
        visible = [(p, b) for p, b in all_trees if _to_sandbox(p) != p] if _path_map() else all_trees
        trees = [f"{_to_sandbox(p)} ({b})" for p, b in (visible or all_trees)]
        shown = "; ".join(trees[:8]) + ("; ..." if len(trees) > 8 else "")
        raise CloudError(
            f"{_to_sandbox(repo_dir)} is a bare repository with no current branch; pass a worktree "
            f"checked out on the branch the session should use{': ' + shown if shown else ''}"
        )
    return _git(repo_dir, "rev-parse", "--show-toplevel")


def _github_repo(repo_dir: str, branch: str) -> str:
    """owner/name of the GitHub remote that branch tracks (origin by default)."""
    try:
        remote = _git(repo_dir, "config", f"branch.{branch}.remote")
    except CloudError:
        remote = "origin"
    url = _git(repo_dir, "remote", "get-url", remote)
    m = GITHUB_RE.search(url)
    if not m:
        raise CloudError(f"remote {url} is not on github.com; cloud sessions clone from GitHub")
    return m.group(1)


def _current_branch(repo_dir: str) -> str:
    branch = _git(repo_dir, "branch", "--show-current")
    if not branch:
        raise CloudError("HEAD is detached; check out the branch or pass branch explicitly")
    return branch


def _remote_sha(github_repo: str, branch: str) -> str | None:
    """Head of branch on GitHub, or None if it doesn't exist there. Uses gh's
    stored token rather than git over SSH, so no SSH agent is needed."""
    ref = urllib.parse.quote(f"heads/{branch}", safe="/")
    try:
        return _run(["gh", "api", f"repos/{github_repo}/git/ref/{ref}", "--jq", ".object.sha"], timeout=60)
    except CloudError as e:
        if "HTTP 404" in str(e):
            return None
        raise


def _summarize_checks(rollup: list[dict]) -> dict:
    counts: dict[str, int] = {}
    failed: list[str] = []
    pending = 0
    for c in rollup or []:
        status = (c.get("status") or "").upper()
        if status and status != "COMPLETED":
            key = status
        else:
            key = (c.get("conclusion") or c.get("state") or "UNKNOWN").upper()
        counts[key] = counts.get(key, 0) + 1
        if key in PENDING_CHECK_STATES:
            pending += 1
        elif key in FAILED_CHECK_STATES:
            failed.append(c.get("name") or c.get("context") or "?")
    return {"counts": counts, "failed": failed, "pending": pending, "total": len(rollup or [])}


def _status(github_repo: str, branch: str) -> dict:
    fields = "number,title,url,state,isDraft,reviewDecision,headRefOid,updatedAt,statusCheckRollup"
    prs = json.loads(
        _run(
            ["gh", "pr", "list", "-R", github_repo, "--head", branch, "--state", "all",
             "--json", fields, "--limit", "5"],
            timeout=60,
        )
        or "[]"
    )
    for pr in prs:
        pr["checks"] = _summarize_checks(pr.pop("statusCheckRollup", None) or [])
    return {
        "github_repo": github_repo,
        "branch": branch,
        "remote_sha": _remote_sha(github_repo, branch),
        "pull_requests": prs,
    }


def _read_pty(argv: list[str], cwd: str, timeout: int) -> tuple[str, int | None]:
    """Run argv on a pseudo-terminal until it prints a session ID or exits.

    Returns the ANSI-stripped output and the exit code (None if it was still
    running; it is then stopped, since the cloud session doesn't depend on it)."""
    argv = [_bin(argv[0]), *argv[1:]]
    master, slave = pty.openpty()
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    except (ImportError, OSError):
        pass
    env = {**os.environ, "TERM": os.environ.get("TERM") or "xterm-256color"}
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True
        )
    finally:
        os.close(slave)

    raw = b""
    deadline = time.monotonic() + timeout
    found_at: float | None = None
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # Linux raises EIO once the child closes the pty
                    chunk = b""
                if not chunk:
                    break
                raw += chunk
                if found_at is None and SESSION_RE.search(ANSI_RE.sub("", raw.decode("utf-8", "replace"))):
                    found_at = time.monotonic()
            elif proc.poll() is not None:
                break
            now = time.monotonic()
            # Give the CLI a moment to print the rest and exit on its own.
            if found_at is not None and now - found_at > 5:
                break
            if now > deadline:
                break
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        os.close(master)
    text = ANSI_RE.sub("", raw.decode("utf-8", "replace")).replace("\r", "")
    return text, proc.returncode


def _session_key(kwargs: dict) -> str:
    try:
        from gateway.session_context import bound_session_key

        key = bound_session_key()
    except Exception:
        key = None
    return key or str(kwargs.get("session_key") or "")


def _tool(fn):
    """Wrap a handler: CloudError -> {"ok": false, "error": ...}, result -> JSON."""

    def handler(args: dict, **kwargs) -> str:
        try:
            return json.dumps({"ok": True, **fn(args or {}, **kwargs)})
        except CloudError as e:
            return json.dumps({"ok": False, "error": str(e)})
        except Exception as e:
            logger.exception("cloud-dispatch: %s failed", fn.__name__)
            return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})

    return handler


# --- tools -----------------------------------------------------------------


def _herdr(*args: str) -> dict:
    try:
        out = _run(["herdr", *args], timeout=120)
    except CloudError as e:
        out = str(e)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise CloudError(f"herdr {' '.join(args[:2])}: {out}")
    if "error" in data:
        raise CloudError(f"herdr {' '.join(args[:2])}: {data['error'].get('message')}")
    return data.get("result", data)


def _branch_worktree(repo: str, branch: str) -> str | None:
    """Path of the worktree that has branch checked out, if any."""
    path = None
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path:
            return path
    return None


def _has_ref(repo: str, ref: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", ref)
        return True
    except CloudError:
        return False


def _create_remote_branch(github_repo: str, branch: str, sha: str) -> None:
    _run(["gh", "api", "-X", "POST", f"repos/{github_repo}/git/refs",
          "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}"], timeout=60)


def _cloud_start(args: dict, **kwargs) -> dict:
    task = str(args.get("task") or "")
    if not task.strip():
        raise CloudError("task is empty")
    if task.lstrip().startswith("-"):
        raise CloudError("task must not start with '-' (it would be parsed as a CLI flag)")
    branch = str(args.get("branch") or "").strip()
    if not branch:
        raise CloudError("branch is required")
    try:
        _run(["git", "check-ref-format", "--branch", branch])
    except CloudError:
        raise CloudError(f"{branch!r} is not a valid branch name")
    repo = _source_checkout(_repo_dir(args))
    github_repo = _github_repo(repo, branch)
    notes = []

    # 1. The session starts from the branch as GitHub has it, so make sure it
    #    exists there. A new branch starts at the local origin/main (never fetched).
    remote_sha = _remote_sha(github_repo, branch)
    if remote_sha is None:
        try:
            remote_sha = _git(repo, "rev-parse", "--verify", f"{BASE_REF}^{{commit}}")
        except CloudError:
            raise CloudError(f"{BASE_REF} not found in {_to_sandbox(repo)}")
        _create_remote_branch(github_repo, branch, remote_sha)
        notes.append(f"created {branch} on GitHub at {BASE_REF} ({remote_sha[:12]})")

    # 2. A worktree for the branch, opened in a background herdr workspace.
    #    herdr creates it in its own location, so this works for bare and normal repos.
    if _branch_worktree(repo, branch):
        res = _herdr("worktree", "open", "--cwd", repo, "--branch", branch, "--no-focus")
    elif _has_ref(repo, f"refs/heads/{branch}"):
        res = _herdr("worktree", "create", "--cwd", repo, "--branch", branch, "--no-focus")
    elif _has_ref(repo, f"{remote_sha}^{{commit}}"):
        res = _herdr("worktree", "create", "--cwd", repo, "--branch", branch,
                     "--base", remote_sha, "--no-focus")
    else:
        raise CloudError(
            f"{branch} is on GitHub at {remote_sha[:12]}, which isn't in the local repository yet; "
            f"fetch it first (git fetch origin {branch})"
        )
    worktree = res["worktree"]["path"]
    workspace = res.get("workspace") or {}

    # Track origin/<branch> so pulling the session's pushes works (config only, no fetch).
    try:
        _git(worktree, "config", f"branch.{branch}.remote")
    except CloudError:
        _git(worktree, "config", f"branch.{branch}.remote", "origin")
        _git(worktree, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    local_sha = _git(worktree, "rev-parse", "HEAD")
    if local_sha != remote_sha:
        notes.append(
            f"the worktree is at {local_sha[:12]}; the session starts from GitHub's {branch} at "
            f"{remote_sha[:12]}"
        )

    # 3. Start the session from the worktree, i.e. on the branch.
    output, code = _read_pty(["claude", "--cloud", task], worktree, START_TIMEOUT)
    # claude's own notice if it couldn't start from the branch after all.
    notes += [m.group(0).strip() for m in NOT_ON_GITHUB_RE.finditer(output)]
    session = SESSION_RE.search(output)
    if not session:
        tail = "\n".join(output.strip().splitlines()[-20:])
        raise CloudError(
            f"the worktree and herdr workspace are ready, but no session ID in claude output "
            f"(exit {code}):\n{tail}"
        )
    url = URL_RE.search(output)
    title = TITLE_RE.search(output)
    result = {
        "session_id": session.group(1),
        "url": url.group(0) if url else f"https://claude.ai/code/{session.group(1)}",
        "title": title.group(1).strip() if title else None,
        "github_repo": github_repo,
        "branch": branch,
        "github_commit": remote_sha,
        "worktree": _to_sandbox(worktree),
        "herdr_workspace": workspace.get("workspace_id"),
        "herdr_workspace_label": workspace.get("label"),
    }
    if notes:
        result["notes"] = notes
    return result


def _cloud_send(args: dict, **kwargs) -> dict:
    match = SESSION_RE.search(str(args.get("session_id") or ""))
    if not match:
        raise CloudError(f"not a session ID or URL: {args.get('session_id')!r}")
    message = str(args.get("message") or "")
    if not message.strip():
        raise CloudError("message is empty")
    # The message goes on stdin so its content can never be read as a flag.
    out = _run(
        ["claude", "-p", "--cloud", match.group(1), "--output-format", "json"],
        timeout=SEND_TIMEOUT,
        stdin=message,
    )
    try:
        res = json.loads(out)
    except json.JSONDecodeError:
        return {"output": out}
    if res.get("ok") is False:
        raise CloudError(res.get("error") or out)
    res.pop("ok", None)
    return res


def _cloud_status(args: dict, **kwargs) -> dict:
    repo_dir = _repo_dir(args)
    branch = str(args.get("branch") or "").strip() or _current_branch(_worktree(repo_dir))
    return _status(_github_repo(repo_dir, branch), branch)


def _cloud_watch(args: dict, **kwargs) -> dict:
    repo_dir = _repo_dir(args)
    branch = str(args.get("branch") or "").strip() or _current_branch(_worktree(repo_dir))
    github_repo = _github_repo(repo_dir, branch)
    events = [e for e in (args.get("notify_on") or DEFAULT_WATCH_EVENTS) if e in WATCH_EVENTS]
    events = events or list(DEFAULT_WATCH_EVENTS)
    timeout_min = max(1, int(args.get("timeout_minutes") or DEFAULT_WATCH_MIN))

    session_key = _session_key(kwargs)
    if not session_key:
        raise CloudError("no session key for this call; cannot notify back")
    key = (session_key, github_repo, branch)
    with _watches_lock:
        if key in _watches and _watches[key].is_alive():
            return {"status": "already_watching", "github_repo": github_repo, "branch": branch}
    baseline = _status(github_repo, branch)
    with _watches_lock:
        if key in _watches and _watches[key].is_alive():
            return {"status": "already_watching", "github_repo": github_repo, "branch": branch}
        thread = threading.Thread(
            target=_run_watch,
            args=(session_key, github_repo, branch, baseline, events, timeout_min),
            daemon=True,
            name=f"cloud-watch-{branch}",
        )
        _watches[key] = thread
        thread.start()
    return {
        "status": "watching",
        "github_repo": github_repo,
        "branch": branch,
        "notify_on": events,
        "timeout_minutes": timeout_min,
        "current": baseline,
        "note": "You will get a [cloud-dispatch] message when something happens; no need to wait or poll.",
    }


# --- watch -----------------------------------------------------------------


def _run_watch(session_key: str, github_repo: str, branch: str, baseline: dict,
               events: list[str], timeout_min: int) -> None:
    where = f"{github_repo}@{branch}"
    last_sha = baseline["remote_sha"]
    seen_prs = {pr["number"] for pr in baseline["pull_requests"]}
    # A PR whose checks were already done when the watch started isn't news.
    checks_reported = {
        (pr["number"], pr["headRefOid"])
        for pr in baseline["pull_requests"]
        if pr["checks"]["total"] and not pr["checks"]["pending"]
    }
    closed_seen = {pr["number"] for pr in baseline["pull_requests"] if pr["state"] != "OPEN"}
    deadline = time.monotonic() + timeout_min * 60
    failures = 0
    try:
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            try:
                st = _status(github_repo, branch)
                failures = 0
            except Exception as e:
                failures += 1
                logger.warning("cloud-dispatch: poll of %s failed (%d): %s", where, failures, e)
                if failures >= MAX_POLL_FAILURES:
                    _notify(session_key, f"stopped watching {where}: GitHub polling failed "
                                         f"{failures} times in a row ({e})")
                    return
                continue

            lines: list[str] = []
            done = False
            if "push" in events and st["remote_sha"] and st["remote_sha"] != last_sha:
                lines.append(f"{where} was pushed: now at {st['remote_sha'][:12]}")
            last_sha = st["remote_sha"]
            for pr in st["pull_requests"]:
                n, head, checks = pr["number"], pr["headRefOid"], pr["checks"]
                label = f"PR #{n} ({pr['title']})"
                if n not in seen_prs:
                    seen_prs.add(n)
                    if "pr" in events:
                        draft = " as a draft" if pr.get("isDraft") else ""
                        lines.append(f"{label} opened{draft}: {pr['url']}")
                if pr["state"] != "OPEN" and n not in closed_seen:
                    closed_seen.add(n)
                    lines.append(f"{label} was {pr['state'].lower()}: {pr['url']}")
                    done = True
                    continue
                if ("checks" in events and pr["state"] == "OPEN" and head == st["remote_sha"]
                        and checks["total"] and not checks["pending"]
                        and (n, head) not in checks_reported):
                    checks_reported.add((n, head))
                    if checks["failed"]:
                        lines.append(f"{label} checks finished at {head[:12]} with failures: "
                                     f"{', '.join(checks['failed'])}. {pr['url']}")
                    else:
                        review = (pr.get("reviewDecision") or "no review yet").lower().replace("_", " ")
                        lines.append(f"{label} checks passed at {head[:12]} ({review}). {pr['url']}")
            if lines:
                _notify(session_key, "\n".join(lines) + ("\nWatch ended." if done else ""))
            if done:
                return
        _notify(session_key, f"stopped watching {where} after {timeout_min} minutes "
                             "(call cloud_watch again to keep watching)")
    except Exception:
        logger.exception("cloud-dispatch: watcher for %s crashed", where)
    finally:
        with _watches_lock:
            _watches.pop((session_key, github_repo, branch), None)


def _notify(session_key: str, text: str) -> None:
    msg = (f"[cloud-dispatch] {text}\n\n"
           "This comes from GitHub state, not the cloud session itself. Use cloud_status "
           "for details or cloud_send to steer the session.")
    if _ctx is None or not _ctx.inject_message(msg, role="user", session_key=session_key):
        logger.warning("cloud-dispatch: could not inject into session %s: %s", session_key, text)


# --- registration ----------------------------------------------------------

REPO_DIR_PARAM = {
    "type": "string",
    "description": "The repository: its main clone, any worktree, or a bare repository, e.g. "
                   "/workspace/work/Faraday/node.git. Paths as your file tools see them are fine; "
                   "sandbox paths are mapped to the host.",
}

TOOLS = [
    (
        "cloud_start", _cloud_start, "🚀", "Start a Claude Code cloud session on a branch, with a local worktree",
        {
            "description": (
                "Start a Claude Code cloud session on a branch. If the branch isn't on GitHub "
                f"yet it is created there from the local {BASE_REF} (not fetched first). A local "
                "worktree for the branch is created (or reused) and opened in a background herdr "
                "workspace for the user. The session clones the branch from GitHub and does its "
                "own commits and pushes. Returns the session ID and URL, the worktree path and "
                "the herdr workspace; the session keeps running afterwards."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The prompt for the cloud session."},
                    "branch": {
                        "type": "string",
                        "description": "Branch for the session to work on, e.g. a Linear issue's "
                                       "branchName. Created if it doesn't exist.",
                    },
                    "repo_dir": REPO_DIR_PARAM,
                },
                "required": ["task", "branch", "repo_dir"],
            },
        },
    ),
    (
        "cloud_send", _cloud_send, "📨", "Queue a message into a running cloud session",
        {
            "description": (
                "Queue a message into a running Claude Code cloud session. Claude reads it when "
                "it next checks for input; it does not interrupt work in progress. Returns once "
                "the message is queued, without a reply: the session's replies can't be read "
                "from here."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (session_... or cse_...) or claude.ai/code URL.",
                    },
                    "message": {"type": "string", "description": "The message to send."},
                },
                "required": ["session_id", "message"],
            },
        },
    ),
    (
        "cloud_status", _cloud_status, "🔎", "Show a cloud session's branch and PR state on GitHub",
        {
            "description": (
                "Report what a cloud session has pushed: the branch head on GitHub and any PR, "
                "with its review decision and check results (counts, failed check names, "
                "pending count)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_dir": REPO_DIR_PARAM,
                    "branch": {
                        "type": "string",
                        "description": "The session's branch, as passed to cloud_start.",
                    },
                },
                "required": ["repo_dir", "branch"],
            },
        },
    ),
    (
        "cloud_watch", _cloud_watch, "👀", "Get notified when a cloud session's PR opens or its checks finish",
        {
            "description": (
                "Watch a cloud session's branch on GitHub in the background and get notified in "
                "this conversation. Returns immediately; do not wait or poll afterwards -- a "
                "message starting with [cloud-dispatch] arrives as a new turn when the branch "
                f"gets a PR, that PR's checks finish, or it is merged or closed (which ends the "
                f"watch). Polls every {POLL_SECONDS}s."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_dir": REPO_DIR_PARAM,
                    "branch": {
                        "type": "string",
                        "description": "The session's branch. Defaults to repo_dir's current branch.",
                    },
                    "notify_on": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(WATCH_EVENTS)},
                        "description": "Events to report: push (branch moved), pr (PR opened), checks "
                                       "(PR checks finished). PR merge/close is always reported. "
                                       f"Default: {', '.join(DEFAULT_WATCH_EVENTS)}.",
                    },
                    "timeout_minutes": {
                        "type": "integer",
                        "description": f"Stop watching after this long. Default {DEFAULT_WATCH_MIN}.",
                    },
                },
            },
        },
    ),
]


def register(ctx):
    global _ctx
    _ctx = ctx
    for name, fn, emoji, short, schema in TOOLS:
        if name in HIDDEN_TOOLS:
            continue
        ctx.register_tool(
            name=name, toolset="cloud_dispatch", schema={"name": name, **schema},
            handler=_tool(fn), description=short, emoji=emoji,
        )
