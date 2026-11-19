---
name: jira-sprint
description: Pull the latest Jira sprint changes and get up to speed on the sprint. Use when the user asks to sync or pull Jira, catch up on ticket changes, asks what's going on in the sprint, or wants to plan implementing sprint tickets.
---

# Jira sprint sync

A deterministic script does all the gathering. Your job is to read as little as possible and think about what matters.

`jira_sync.py` and `env.example` sit next to this file, in this skill's base directory.

1. Run `python3 "<this skill's base directory>/jira_sync.py"`. It prints one summary line, which names the output directory (by default `~/.jira-sync/sprint`). Pass `--full` only if the user asks for a rebuild.
   - If it fails because config is missing, help the user create `~/.jira-sync/.env` from `env.example`. Never ask them to paste an API token into the chat; they should put it in the file themselves. They can also export the same `JIRA_*` variables in their environment.
2. Read `CHANGES.md` in the output directory. It's the delta since the last sync: tickets added to scope, tickets removed, and for each changed ticket the field changes and comments since then.
3. Read `SPRINT.md` (one line per ticket) if you don't already have the sprint picture in this conversation, or if CHANGES.md says it was an initial sync.
4. Open `tickets/KEY.md` **only** for tickets that matter to the question at hand: ones that changed in an interesting way, ones the user asks about, or ones you need detail on to plan an implementation. Don't read every ticket file.
5. Report briefly: what moved, scope changes, new blockers, and anything that needs the user's attention. Offer deeper dives; don't do them unprompted.

Notes:
- `[blocked by X]` in SPRINT.md means an unresolved "is blocked by" link.
- If the user hasn't synced in a while or asks about earlier changes, older deltas are in `history/` (one file per sync, named by UTC time).
- Tickets that left scope are in `archive/`.
