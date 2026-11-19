# jira-sync

A Claude Code plugin that turns your Jira sprint into a continuous feed of changes, like RSS for your tickets, plus small Markdown snapshots. Claude can catch up on what changed without burning tokens on gathering and organizing.

## Why not the Atlassian MCP server?

Because I'd rather not depend on a vendor to keep their tooling lean. This is one Python file with no dependencies beyond the standard library. It makes read-only GET requests to the Jira REST API with your token and writes plain Markdown to disk. You can read the whole thing in a sitting and know exactly what it does.

The split is deliberate. A deterministic script does the gathering and organizing, and Claude only spends tokens on the part that needs judgment.

## How it works

Each sync:

1. Runs one search that returns only the `updated` timestamp of every ticket in scope.
2. Fetches full detail only for tickets that are new or whose timestamp moved since the last sync.
3. Turns each fetched ticket's changelog and comments into events, and appends any it hasn't seen before to the feed.

The feed is append-only. Nothing is ever pruned or rewritten. The first time a ticket is fetched, its whole Jira history is backfilled. Events have stable IDs taken from Jira's own IDs, so re-syncing, even with `--full`, never duplicates anything.

Output goes to `~/.jira-sync/sprint/`:

| file | what |
| --- | --- |
| `feed.jsonl` | **the stream**: one JSON event per line (field change, comment, comment edit, creation, entering or leaving scope) |
| `FEED.md` | the whole stream rendered, one line per event, like `08:00 SHOP-10 Ana: status: To Do → In Progress` |
| `SPRINT.md` | current snapshot: sprint header, points by status, one line per ticket, blockers |
| `tickets/KEY.md` | current snapshot of a ticket: fields, links, subtasks, description, comments |
| `archive/` | last snapshot of tickets that dropped out of scope |

Read any slice of the stream without syncing:

```sh
jira_sync.py --new                  # events ingested by the latest sync
jira_sync.py --since 2026-11-18     # everything that happened since a date (UTC)
jira_sync.py --key SHOP-10          # one ticket's whole history (repeatable)
```

A sync prints one line, so running it costs Claude almost nothing. The bundled skill tells Claude to read only the slice of the feed it needs, and to open ticket files only when it needs detail.

Because `feed.jsonl` records every Status, Story Points, Sprint, and Assignee change with timestamps, it holds everything needed for burnup charts and timelines.

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

You'll need your Jira URL, your email, an [API token](https://id.atlassian.com/manage-profile/security/api-tokens), and a scope. The scope is normally a board ID: the number in the board's URL, as in `.../boards/42`.

Here's how each kind of scope behaves:

- **Scrum board:** the tickets on the board (using its saved filter) that are in its active sprint or sprints. When a sprint rolls over, carried-over tickets stay in scope, finished ones leave, and the feed records both. Between sprints, when nothing is active, a sync does nothing; it doesn't record every ticket as leaving.
- **Kanban board:** the board's saved filter plus its own sub-filter, which usually hides old finished work.
- **Alternatives:** you can use `JIRA_JQL` or `JIRA_PROJECT` instead of a board. You can set the same `JIRA_*` variables as real environment variables instead, which is useful in cloud or CI environments. Environment variables take precedence over the file.

Now ask Claude to "sync jira and catch me up on the sprint."

## Running it directly

```sh
python3 skills/jira-sprint/jira_sync.py           # incremental sync
python3 skills/jira-sprint/jira_sync.py --full    # re-fetch every ticket
```

Other flags: `--board ID`, `--jql`, and `--project` override whatever scope the config sets. `--out DIR` changes where the files go. If you track more than one board, give each one its own `--out` directory, so their feeds stay separate. Set `JIRA_SYNC_HOME` to move the whole `~/.jira-sync` directory.

The script supports Jira Cloud (REST v3) and Server/Data Center (REST v2, with a personal access token and `JIRA_DEPLOYMENT=server`). It needs Python 3.9 or later.

## Tests

```sh
python3 -m unittest discover tests
```

## License

MIT
