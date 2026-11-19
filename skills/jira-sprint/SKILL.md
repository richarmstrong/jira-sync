---
name: jira-sprint
description: Pull the latest Jira sprint changes and get up to speed on the sprint. Use when the user asks to sync or pull Jira, catch up on ticket changes, asks what's going on in the sprint or what happened to a ticket, or wants to plan implementing sprint tickets.
---

# Jira sprint sync

A deterministic script does all the gathering. It keeps a continuous, append-only feed of every Jira change event in scope. Your job is to read the slice of that stream you need, plus the current snapshot, and think about what matters.

`jira_sync.py` and `env.example` sit next to this file, in this skill's base directory. Below, `SYNC` means `python3 "<this skill's base directory>/jira_sync.py"`.

## Catching up

1. Run `SYNC`. It prints one line: tickets in scope, how many new events it appended, and the output directory (by default `~/.jira-sync/sprint`). Pass `--full` only if the user asks for a rebuild. It never duplicates events.
   - If it says the board has no active sprint, nothing was synced. Tell the user; the existing feed and snapshots are still readable.
   - If the user asks about a different board, pass `--board ID --out ~/.jira-sync/board-ID` so each board keeps its own feed.
   - If it fails because config is missing, help the user create `~/.jira-sync/.env` from `env.example`. Never ask them to paste an API token into the chat; they should put it in the file themselves. They can also export the same `JIRA_*` variables in their environment.
2. Read the slice of the feed that fits the question. Each command prints compact Markdown, one line per event, grouped by UTC day. None of them sync or hit the network.
   - `SYNC --new`: events ingested by the sync you just ran. This is the default for "what changed?"
   - `SYNC --since 2026-11-18`: everything that happened since a date or time. Use it for "since Monday" or "the last few days", or when the user hasn't been caught up in a while.
   - `SYNC --key ABC-123`: one ticket's entire history.
   - You can combine `--since` with one or more `--key`.
3. Read `SPRINT.md` (one line per ticket, current state) if you don't already have the sprint picture in this conversation, or if this was the first sync.
4. Open `tickets/KEY.md` (the current description and comments) **only** for tickets that matter to the question. Don't read every ticket file.
5. Report briefly: what moved, scope changes, new blockers, and anything that needs the user's attention. Offer deeper dives; don't do them unprompted.

## Files in the output directory

- `feed.jsonl` is the raw stream, one JSON event per line, never rewritten. Each event has `ts` (when it happened in Jira), `seq` and `synced` (when it was ingested), `key`, `kind` (created / field / comment / comment_edit / updated / scope), `author`, plus `field`/`from`/`to` or `text`. Use it with a script when you need to compute something, such as burnup from Status and Story Points changes, rather than reading it into context.
- `FEED.md` is the whole stream rendered. Prefer the filtered commands above; this file grows forever.
- `SPRINT.md` and `tickets/` are snapshots of current state. `archive/` holds the last snapshot of tickets that left scope.
- `[blocked by X]` in SPRINT.md means an unresolved "is blocked by" link.
- `scope` events are stamped with the sync time, because they're observed by the sync rather than reported by Jira. Jira's own Sprint field changes appear as normal `field` events.
