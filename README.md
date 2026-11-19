# jira-sync

A Claude Code plugin that mirrors your current Jira sprint into small Markdown files, so Claude can catch up on what changed without burning tokens on gathering and organizing.

## Why not the Atlassian MCP server?

Because I'd rather not depend on a vendor to keep their tooling lean. This is one Python file with no dependencies beyond the standard library. It makes read-only GET requests to the Jira REST API with your token and writes plain Markdown to disk. You can read the whole thing in a sitting and know exactly what it does.

The split is deliberate. A deterministic script does the gathering and organizing, and Claude only spends tokens on the part that needs judgment.

## How it works

Each sync:

1. Runs one search that returns only the `updated` timestamp of every ticket in scope.
2. Fetches full detail only for tickets that are new or whose timestamp moved since the last sync.
3. Writes a delta feed from Jira's changelog and comments, covering only what happened since the last sync.

Output goes to `~/.jira-sync/sprint/`:

| file | what |
| --- | --- |
| `CHANGES.md` | delta since the last sync: added, removed, and changed tickets, with lines like `2026-11-19 08:00 Ana: status: To Do → In Progress` |
| `SPRINT.md` | sprint header, points by status, one line per ticket, blockers |
| `tickets/KEY.md` | full ticket: fields, links, subtasks, description, comments |
| `history/` | every non-empty `CHANGES.md`, archived |
| `archive/` | tickets that dropped out of scope |

The script itself prints one line, so running it costs Claude almost nothing. The bundled skill tells Claude to read `CHANGES.md` first and to open individual ticket files only when it needs them.

## Install

In Claude Code:

```
/plugin marketplace add richarmstrong/jira-sync
/plugin install jira-sync@jira-sync
```

Then create your config. The template is [`skills/jira-sprint/env.example`](skills/jira-sprint/env.example).

```sh
mkdir -p ~/.jira-sync
curl -fsSL https://raw.githubusercontent.com/richarmstrong/jira-sync/main/skills/jira-sprint/env.example -o ~/.jira-sync/.env
chmod 600 ~/.jira-sync/.env
$EDITOR ~/.jira-sync/.env
```

You'll need your Jira URL, your email, an [API token](https://id.atlassian.com/manage-profile/security/api-tokens), and either a project key or a JQL query. You can set the same `JIRA_*` variables as real environment variables instead, which is useful in cloud or CI environments. Environment variables take precedence over the file.

Now ask Claude to "sync jira and catch me up on the sprint."

## Running it directly

```sh
python3 skills/jira-sprint/jira_sync.py           # incremental sync
python3 skills/jira-sprint/jira_sync.py --full    # re-fetch every ticket
```

Other flags: `--jql` and `--project` override the config, and `--out DIR` changes where the files go. Set `JIRA_SYNC_HOME` to move the whole `~/.jira-sync` directory.

The script supports Jira Cloud (REST v3) and Server/Data Center (REST v2, with a personal access token and `JIRA_DEPLOYMENT=server`). It needs Python 3.9 or later.

## Tests

```sh
python3 -m unittest discover tests
```

## License

MIT
