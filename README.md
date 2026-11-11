# cloud-dispatch

A Hermes plugin for starting and steering
[Claude Code cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web),
each with a local worktree in a [herdr](https://herdr.dev) workspace.

| Tool | What it does |
|---|---|
| `cloud_start(task, branch, repo_dir)` | Starts a cloud session on `branch`, with a local worktree for it in a background herdr workspace. Returns the session ID and URL, the worktree and the workspace. |
| `cloud_send(session_id, message)` | Queues a follow-up message into a running session. Doesn't wait for a reply. |
| `cloud_status(repo_dir, branch)` | Reports the branch head on GitHub and any PR, with review and check results. |

`repo_dir` is any checkout of the repository: the main clone, a worktree, or a
bare repository. It's required, because a sandboxed Hermes session's working
directory doesn't reliably point at the repository. All tools are in the
`cloud_dispatch` toolset.

A cloud session's replies can't be read from the CLI. Open the session URL to
see the conversation, and use `cloud_status` to see what it has pushed.

## What `cloud_start` does

1. **Branch on GitHub.** If `branch` isn't on GitHub, it's created there at the
   local `origin/main`. The plugin never fetches: keeping `origin/main` current
   is up to you.
2. **Worktree.** If a worktree already has `branch` checked out, it's reused.
   Otherwise `herdr worktree create` makes one in herdr's worktree location,
   from the local branch if there is one, else from the GitHub commit (which
   must already be in the local repository). The repository's own git hooks
   run as usual. The local branch is set to track `origin/<branch>`.
3. **herdr workspace.** The worktree is opened in a herdr workspace in the
   background, with a shell.
4. **Session.** `claude --cloud "<task>"` runs from the worktree, so the
   session starts on `branch` as GitHub has it and does its own commits and
   pushes.

Nothing is cleaned up afterwards: the branch, worktree and workspace stay, also
when starting the session fails.

## Requirements

- `claude`, signed in with a claude.ai account (`claude auth login`) whose plan
  and organization allow cloud sessions, with GitHub connected
- `gh`, signed in (`gh auth login`), with push access to the repository
- `git`
- `herdr`, with its server running

Linux and macOS. No Python dependencies beyond Hermes itself.

## Install

1. Install the plugin into your profile:

   ```
   hermes --profile <profile> plugins install oatmealraisin/cloud-dispatch
   ```

   The repository is private, so your git needs access to it. Alternatively,
   clone it and symlink the clone into your profile's plugins directory, for
   example `~/.hermes/profiles/<profile>/plugins/cloud-dispatch` (or
   `~/.hermes/plugins/cloud-dispatch` for the default profile).
2. In that profile's `config.yaml`, enable the plugin and turn the toolset on
   for the platforms that should have it:

   ```yaml
   platform_toolsets:
     cli:
       - cloud_dispatch
       # ...your existing toolsets
   plugins:
     enabled:
       - cloud-dispatch
   ```

   Only list `cloud_dispatch` on platforms you trust: anyone who can talk to
   Hermes there can start sessions as you.
3. Start a new Hermes session, then check with
   `hermes --profile <profile> plugins show cloud-dispatch`.

## Docker sandbox paths

If the profile runs tools in a Docker sandbox (`terminal.backend: docker`),
Hermes passes paths as the sandbox sees them, while the plugin runs on the
host. The plugin maps them back using the profile's `terminal.docker_volumes`
(`host:sandbox[:mode]`), so a mounted checkout works without configuration.
For anything that mapping doesn't cover, add explicit pairs:

```yaml
plugins:
  entries:
    cloud-dispatch:
      settings:
        path_map:
          /workspace/work: ~/work   # sandbox path: host path
```

## Behaviour

- **`claude --cloud` needs a terminal**, so `cloud_start` runs it under a
  pseudo-terminal and parses the session ID from its output. If a Claude Code
  update changes that output, `cloud_start` returns the output tail in its
  error.
- **`cloud_send` queues, it doesn't interrupt.** Claude reads the message when
  it next checks for input.
- **It acts as you.** Sessions use your claude.ai login and plan usage, and
  anything they push or comment on GitHub appears under your account.
- GitHub lookups use `gh`'s stored token, not git over SSH. If Hermes runs
  with a minimal `PATH` (common for macOS apps), the plugin also looks for
  `claude`, `gh`, `git` and `herdr` in `~/.local/bin`, `/opt/homebrew/bin`,
  `/usr/local/bin` and `~/.claude/local`.
- A `cloud_watch` tool (background PR and check notifications) is in the code
  but not enabled yet.
