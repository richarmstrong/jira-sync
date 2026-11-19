#!/usr/bin/env python3
"""Incrementally mirror a Jira sprint into a continuous change feed plus Markdown snapshots.

Each sync:
  1. Runs a cheap search (only the `updated` field) for every ticket in scope.
  2. Re-fetches full detail only for tickets that are new or whose `updated`
     stamp moved since the last run.
  3. Appends every not-yet-seen event (field changes, comments, comment edits,
     creation, scope moves) to an append-only feed.  A ticket's whole history is
     backfilled the first time it is fetched; nothing is ever dropped.

Output directory (default: ~/.jira-sync/sprint):
  feed.jsonl       the stream: one JSON event per line, append-only
  FEED.md          the whole stream rendered, one line per event, by day
  SPRINT.md        current snapshot: one line per ticket in scope
  tickets/KEY.md   current snapshot of a ticket: fields, links, description, comments
  archive/KEY.md   last snapshot of tickets that have dropped out of scope
  state.json       bookkeeping (per-ticket `updated` stamps, field ids)

Read slices of the feed without syncing:
  jira_sync.py --new                 events ingested by the latest sync
  jira_sync.py --since 2026-11-18    events that happened since a date
  jira_sync.py --key ABC-123         one ticket's whole history

Stdlib only, Python 3.9+.  Config comes from env vars, then ~/.jira-sync/.env
(see env.example).  Set JIRA_SYNC_HOME to move that directory.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Config and output live outside the script's directory so plugin updates never touch them.
SYNC_HOME = Path(os.environ.get("JIRA_SYNC_HOME") or Path.home() / ".jira-sync").expanduser()

BASE_FIELDS = [
    "summary", "status", "issuetype", "priority", "assignee", "reporter",
    "labels", "components", "fixVersions", "parent", "subtasks", "issuelinks",
    "description", "comment", "created", "updated", "resolution", "duedate",
]
# Custom fields located by display name (case-insensitive); ids differ per instance.
CUSTOM_FIELDS = {
    "points": ("story points", "story point estimate"),
    "sprint": ("sprint",),
    "epic": ("epic link",),
}
# Changelog edits to these fields are reported as "edited", not dumped in full.
LONG_TEXT_FIELDS = {"description", "environment", "acceptance criteria"}
STATUS_CATEGORY_ORDER = {"indeterminate": 0, "new": 1, "done": 2}
STATUS_CATEGORY_LABEL = {"indeterminate": "In Progress", "new": "To Do", "done": "Done"}
SNIPPET = 280


# --------------------------------------------------------------------------- utils

def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def parse_ts(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"unrecognised Jira timestamp: {s!r}")


def fmt_ts(s: str | None) -> str:
    """'2026-11-18T14:02:11.000-0500' -> '2026-11-18 19:02' (UTC, so every view agrees)."""
    if not s:
        return ""
    try:
        return parse_ts(s).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s[:16].replace("T", " ")


def fmt_num(v) -> str:
    if v is None or v == "":
        return ""
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def one_line(s: str, limit: int = SNIPPET) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def key_sort(key: str):
    proj, _, num = key.rpartition("-")
    return (proj, int(num) if num.isdigit() else 0, key)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")


def nm(obj, attr: str = "name") -> str:
    return (obj or {}).get(attr) or ""


# ------------------------------------------------------------ ADF -> Markdown

def to_md(value) -> str:
    """Rich-text field -> Markdown. Cloud v3 returns ADF; Server returns wiki text."""
    if value is None:
        return ""
    if isinstance(value, dict) and value.get("type") == "doc":
        return _blocks(value.get("content", [])).strip()
    if isinstance(value, str):
        return value.strip()
    return fmt_value(value)


def _blocks(nodes, indent: str = "") -> str:
    return "\n\n".join(b for b in (_block(n, indent) for n in nodes) if b)


def _block(n: dict, indent: str = "") -> str:
    t, c, a = n.get("type"), n.get("content", []), n.get("attrs", {}) or {}
    if t == "paragraph":
        return indent + _inline(c).replace("\n", "\n" + indent)
    if t == "heading":
        # Demote: '#' is the ticket title and '##' the ticket sections.
        return "#" * min(int(a.get("level", 1)) + 2, 6) + " " + _inline(c)
    if t in ("bulletList", "orderedList", "taskList"):
        lines = []
        for i, item in enumerate(c, 1):
            if t == "bulletList":
                marker = "-"
            elif t == "orderedList":
                marker = f"{i}."
            else:
                marker = "- [x]" if (item.get("attrs") or {}).get("state") == "DONE" else "- [ ]"
            if item.get("type") == "taskItem":
                body = _inline(item.get("content", []))
            else:
                body = "\n".join(
                    b for b in (_block(x, indent + "  ") for x in item.get("content", [])) if b
                ).lstrip()
            lines.append(f"{indent}{marker} {body}")
        return "\n".join(lines)
    if t == "codeBlock":
        return f"{indent}```{a.get('language') or ''}\n{_inline(c)}\n{indent}```"
    if t == "blockquote":
        return "\n".join("> " + ln for ln in _blocks(c).splitlines())
    if t == "panel":
        return "\n".join("> " + ln for ln in (f"**{a.get('panelType', 'note')}:** " + _blocks(c)).splitlines())
    if t == "rule":
        return "---"
    if t == "table":
        rows = []
        for row in c:
            cells = [_blocks(cell.get("content", [])).replace("\n", " ").replace("|", "\\|")
                     for cell in row.get("content", [])]
            rows.append("| " + " | ".join(cells) + " |")
        if rows:
            ncols = rows[0].count(" | ") + 1
            rows.insert(1, "|" + " --- |" * ncols)
        return "\n".join(rows)
    if t in ("mediaSingle", "mediaGroup", "media"):
        return "[attachment]"
    if t in ("expand", "nestedExpand"):
        title = a.get("title") or "details"
        return f"**{title}:**\n\n" + _blocks(c, indent)
    return _blocks(c, indent) if c else _inline([n])


def _inline(nodes) -> str:
    out = []
    for n in nodes:
        t, a = n.get("type"), n.get("attrs", {}) or {}
        if t == "text":
            s = n.get("text", "")
            for m in n.get("marks", []):
                mt = m.get("type")
                if mt == "code":
                    s = f"`{s}`"
                elif mt == "strong":
                    s = f"**{s}**"
                elif mt == "em":
                    s = f"*{s}*"
                elif mt == "strike":
                    s = f"~~{s}~~"
                elif mt == "link":
                    href = (m.get("attrs") or {}).get("href", "")
                    s = href if s == href else f"[{s}]({href})"
            out.append(s)
        elif t == "hardBreak":
            out.append("\n")
        elif t == "mention":
            out.append(a.get("text") or "@someone")
        elif t == "emoji":
            out.append(a.get("text") or a.get("shortName", ""))
        elif t in ("inlineCard", "blockCard", "embedCard"):
            out.append(a.get("url", ""))
        elif t == "status":
            out.append(f"[{a.get('text', '')}]")
        elif t == "date":
            try:
                out.append(datetime.fromtimestamp(int(a["timestamp"]) / 1000, timezone.utc).strftime("%Y-%m-%d"))
            except (KeyError, ValueError):
                pass
        elif t in ("media", "mediaInline"):
            out.append("[attachment]")
        else:
            out.append(_inline(n.get("content", [])))
    return "".join(out)


def fmt_value(v) -> str:
    """Best-effort rendering of an arbitrary (custom) field value."""
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return fmt_num(v)
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if v.get("type") == "doc":
            return to_md(v)
        for k in ("displayName", "name", "value", "key"):
            if k in v:
                return str(v[k])
        return json.dumps(v, sort_keys=True)
    if isinstance(v, list):
        return ", ".join(s for s in (fmt_value(x) for x in v) if s)
    return str(v)


# ------------------------------------------------------------------ Jira client

class JiraClient:
    def __init__(self, base_url: str, token: str, email: str | None = None, cloud: bool = True):
        self.base = base_url.rstrip("/")
        self.cloud = cloud
        self.api = "/rest/api/3" if cloud else "/rest/api/2"
        if cloud:
            cred = base64.b64encode(f"{email}:{token}".encode()).decode()
            self.auth = f"Basic {cred}"
        else:
            self.auth = f"Bearer {token}"

    def get(self, path: str, params: dict | None = None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(5):
            req = urllib.request.Request(url, headers={"Authorization": self.auth, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < 4:
                    retry = e.headers.get("Retry-After")
                    time.sleep(float(retry) if retry and retry.isdigit() else 2 ** attempt)
                    continue
                body = e.read().decode(errors="replace")[:500]
                raise SystemExit(f"Jira HTTP {e.code} on GET {path}: {body}")
        raise SystemExit(f"Jira: gave up on GET {path} after retries")

    def fields(self) -> list[dict]:
        return self.get(f"{self.api}/field")

    def search(self, jql: str, fields: list[str]):
        if self.cloud:  # enhanced search, token-paginated
            token = None
            while True:
                params = {"jql": jql, "fields": ",".join(fields), "maxResults": 100}
                if token:
                    params["nextPageToken"] = token
                data = self.get(f"{self.api}/search/jql", params)
                yield from data.get("issues", [])
                token = data.get("nextPageToken")
                if not token or data.get("isLast"):
                    return
        else:
            start = 0
            while True:
                data = self.get(f"{self.api}/search",
                                {"jql": jql, "fields": ",".join(fields), "startAt": start, "maxResults": 100})
                issues = data.get("issues", [])
                yield from issues
                start += len(issues)
                if not issues or start >= data.get("total", 0):
                    return

    def issue(self, key: str, fields: list[str]) -> dict:
        issue = self.get(f"{self.api}/issue/{key}", {"fields": ",".join(fields), "expand": "changelog"})
        # Embedded comments and changelog are capped; page the rest in if needed.
        cm = issue["fields"].get("comment")
        if cm and cm.get("total", 0) > len(cm.get("comments", [])):
            comments, start = [], 0
            while True:
                page = self.get(f"{self.api}/issue/{key}/comment", {"startAt": start, "maxResults": 100})
                batch = page.get("comments", [])
                comments += batch
                start += len(batch)
                if not batch or start >= page.get("total", 0):
                    break
            cm["comments"] = comments
        cl = issue.get("changelog") or {}
        if self.cloud and cl.get("total", 0) > len(cl.get("histories", [])):
            hist, start = [], 0
            while True:
                page = self.get(f"{self.api}/issue/{key}/changelog", {"startAt": start, "maxResults": 100})
                batch = page.get("values", [])
                hist += batch
                start += len(batch)
                if not batch or page.get("isLast", start >= page.get("total", 0)):
                    break
            issue["changelog"] = {"histories": hist}
        return issue


# ------------------------------------------------------------- normalisation

def resolve_fields(client, extra_names: list[str]) -> dict:
    by_name: dict[str, list[str]] = {}
    for f in client.fields():
        by_name.setdefault(f["name"].strip().lower(), []).append(f["id"])
    fm = {"points": [fid for n in CUSTOM_FIELDS["points"] for fid in by_name.get(n, [])]}
    for role in ("sprint", "epic"):
        ids = [fid for n in CUSTOM_FIELDS[role] for fid in by_name.get(n, [])]
        fm[role] = ids[0] if ids else None
    fm["extras"] = {}
    for name in extra_names:
        ids = by_name.get(name.lower())
        if ids:
            fm["extras"][name] = ids[0]
        else:
            print(f"warning: no Jira field named {name!r}; ignoring", file=sys.stderr)
    fm["extra_names"] = sorted(extra_names)
    return fm


def field_ids(fm: dict) -> list[str]:
    ids = BASE_FIELDS + fm["points"] + [x for x in (fm.get("sprint"), fm.get("epic")) if x]
    return ids + list(fm["extras"].values())


def parse_sprints(value) -> list[dict]:
    out = []
    for s in value or []:
        if isinstance(s, dict):
            d = s
        elif isinstance(s, str):  # old Server format: "com.atlassian...Sprint@x[id=1,name=...,state=ACTIVE,...]"
            d = dict(re.findall(r"(\w+)=([^,\]]*)", s))
        else:
            continue
        out.append({
            "name": d.get("name", "?"),
            "state": str(d.get("state", "")).lower(),
            "start": (d.get("startDate") or "")[:10] if d.get("startDate") not in ("<null>", None) else "",
            "end": (d.get("endDate") or "")[:10] if d.get("endDate") not in ("<null>", None) else "",
            "goal": d.get("goal") or "" if d.get("goal") != "<null>" else "",
        })
    return out


def normalize(issue: dict, fm: dict) -> dict:
    f = issue["fields"]
    status = f.get("status") or {}

    points = next((f[fid] for fid in fm["points"] if f.get(fid) is not None), None)
    parent = f.get("parent")
    parent_key = parent["key"] if parent else (f.get(fm["epic"]) if fm.get("epic") else None)
    parent_summary = ((parent or {}).get("fields") or {}).get("summary", "")

    links = []
    for link in f.get("issuelinks") or []:
        lt = link.get("type") or {}
        if "outwardIssue" in link:
            rel, other = lt.get("outward", "relates to"), link["outwardIssue"]
        elif "inwardIssue" in link:
            rel, other = lt.get("inward", "relates to"), link["inwardIssue"]
        else:
            continue
        of = other.get("fields") or {}
        ostatus = of.get("status") or {}
        links.append({
            "rel": rel, "key": other["key"], "summary": of.get("summary", ""),
            "status": nm(ostatus), "done": nm(ostatus.get("statusCategory"), "key") == "done",
        })

    subtasks = []
    for st in f.get("subtasks") or []:
        sf = st.get("fields") or {}
        subtasks.append({"key": st["key"], "summary": sf.get("summary", ""), "status": nm(sf.get("status"))})

    comments = [{
        "id": str(c.get("id") or c.get("created", "")),
        "author": nm(c.get("author"), "displayName") or "?",
        "created": c.get("created", ""),
        "updated": c.get("updated") or c.get("created", ""),
        "body": to_md(c.get("body")),
    } for c in (f.get("comment") or {}).get("comments", [])]

    return {
        "key": issue["key"],
        "summary": f.get("summary", ""),
        "type": nm(f.get("issuetype")),
        "status": nm(status),
        "cat": nm(status.get("statusCategory"), "key") or "new",
        "priority": nm(f.get("priority")),
        "assignee": nm(f.get("assignee"), "displayName"),
        "reporter": nm(f.get("reporter"), "displayName"),
        "points": points,
        "sprints": parse_sprints(f.get(fm["sprint"])) if fm.get("sprint") else [],
        "parent": parent_key,
        "parent_summary": parent_summary,
        "labels": f.get("labels") or [],
        "components": [nm(c) for c in f.get("components") or []],
        "fix_versions": [nm(v) for v in f.get("fixVersions") or []],
        "resolution": nm(f.get("resolution")),
        "due": f.get("duedate") or "",
        "created": f.get("created", ""),
        "updated": f.get("updated", ""),
        "links": links,
        "subtasks": subtasks,
        "extras": {name: to_md(f.get(fid)) if isinstance(f.get(fid), (dict, str)) else fmt_value(f.get(fid))
                   for name, fid in fm["extras"].items()},
        "description": to_md(f.get("description")),
        "comments": comments,
    }


def index_record(t: dict) -> dict:
    """The slice of a ticket kept in state.json and used to build SPRINT.md."""
    return {
        "summary": t["summary"], "type": t["type"], "status": t["status"], "cat": t["cat"],
        "priority": t["priority"], "assignee": t["assignee"], "points": t["points"],
        "parent": t["parent"], "parent_summary": t["parent_summary"], "updated": t["updated"],
        "sprints": [s["name"] for s in t["sprints"]],
        "blocked_by": [l["key"] for l in t["links"] if l["rel"] == "is blocked by" and not l["done"]],
    }


# ------------------------------------------------------------------ rendering

def render_ticket(t: dict) -> str:
    def row(pairs):
        return " | ".join(f"{k}: {v}" for k, v in pairs if v)

    parent = f"{t['parent']} {t['parent_summary']}".strip() if t["parent"] else ""
    L = [f"# {t['key']}: {t['summary']}"]
    L.append(row([("type", t["type"]), ("status", t["status"]), ("priority", t["priority"]),
                  ("points", fmt_num(t["points"])), ("assignee", t["assignee"] or "unassigned"),
                  ("reporter", t["reporter"])]))
    extra = row([("sprint", ", ".join(s["name"] for s in t["sprints"])), ("parent", parent),
                 ("labels", ", ".join(t["labels"])), ("components", ", ".join(t["components"])),
                 ("fix versions", ", ".join(t["fix_versions"])), ("resolution", t["resolution"]),
                 ("due", t["due"])])
    if extra:
        L.append(extra)
    L.append(row([("created", fmt_ts(t["created"])), ("updated", fmt_ts(t["updated"]))]))
    if t["links"]:
        L += ["", "## Links"] + [f"- {l['rel']} {l['key']} [{l['status']}] {l['summary']}" for l in t["links"]]
    if t["subtasks"]:
        L += ["", "## Subtasks"] + [f"- {s['key']} [{s['status']}] {s['summary']}" for s in t["subtasks"]]
    L += ["", "## Description", t["description"] or "_(empty)_"]
    for name, val in t["extras"].items():
        if val:
            L += ["", f"## {name}", val]
    if t["comments"]:
        L += ["", f"## Comments ({len(t['comments'])})"]
        for c in t["comments"]:
            edited = " (edited)" if c["updated"] != c["created"] else ""
            L += ["", f"**{c['author']}** {fmt_ts(c['created'])}{edited}:", c["body"]]
    return "\n".join(L)


# ----------------------------------------------------------------------- feed
#
# feed.jsonl is an append-only stream: one JSON event per line, never rewritten
# or pruned.  Event ids are stable (derived from Jira's own ids), so re-deriving
# a ticket's events and appending only unseen ids is idempotent, and a ticket's
# full history is backfilled the first time it is fetched.
#
# Event fields: id, seq (ingestion order), synced (when ingested), ts (when it
# happened in Jira), key, kind, author, plus field/from/to (kind=field) or text.
# kinds: created | field | comment | comment_edit | updated | scope

def issue_events(issue: dict, t: dict) -> list[dict]:
    """Every event Jira can tell us about this ticket."""
    key = t["key"]
    ev = []
    if t["created"]:
        ev.append({"id": f"{key}#created", "ts": t["created"], "key": key, "kind": "created",
                   "author": t["reporter"], "text": t["summary"]})
    for h in (issue.get("changelog") or {}).get("histories", []):
        who = nm(h.get("author"), "displayName") or "?"
        hid = h.get("id") or h["created"]
        for i, it in enumerate(h.get("items", [])):
            ev.append({"id": f"{key}#h{hid}.{i}", "ts": h["created"], "key": key, "kind": "field",
                       "author": who, "field": it.get("field", "?"),
                       "from": it.get("fromString") or "", "to": it.get("toString") or ""})
    for c in t["comments"]:
        ev.append({"id": f"{key}#c{c['id']}", "ts": c["created"], "key": key, "kind": "comment",
                   "author": c["author"], "text": c["body"]})
        if c["updated"] != c["created"]:
            ev.append({"id": f"{key}#c{c['id']}@{c['updated']}", "ts": c["updated"], "key": key,
                       "kind": "comment_edit", "author": c["author"], "text": c["body"]})
    return ev


def event_sort(e: dict):
    return (parse_ts(e["ts"]), key_sort(e["key"]), e["id"])


def describe_event(e: dict) -> str:
    kind = e["kind"]
    if kind == "field":
        field, fr, to = e["field"], e["from"], e["to"]
        if field.lower() in LONG_TEXT_FIELDS or len(fr) > 80 or len(to) > 80:
            return f"{field} edited"
        return f"{field}: {one_line(fr) or '∅'} → {one_line(to) or '∅'}"
    if kind == "comment":
        return f"commented: {one_line(e['text'])}"
    if kind == "comment_edit":
        return f"edited a comment: {one_line(e['text'])}"
    if kind == "created":
        return f"created: {one_line(e['text'])}"
    return e.get("text") or kind


def render_events(events: list[dict]) -> str:
    """Chronological (by when it happened in Jira), grouped by UTC day, one line per event."""
    L, day = [], None
    for e in sorted(events, key=event_sort):
        stamp = fmt_ts(e["ts"])
        if stamp[:10] != day:
            day = stamp[:10]
            L += ["", f"## {day}"]
        who = f" {e['author']}" if e.get("author") else ""
        L.append(f"- {stamp[11:]} {e['key']}{who}: {describe_event(e)}")
    return "\n".join(L).lstrip()


def load_feed(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_since(s: str) -> datetime:
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def show(out: Path, since: str | None = None, keys=(), new: bool = False) -> str:
    """Filter the feed without touching the network."""
    events = load_feed(Path(out) / "feed.jsonl")
    if new:
        state_path = Path(out) / "state.json"
        floor = json.loads(state_path.read_text()).get("seq_before_last_sync", 0) if state_path.exists() else 0
        events = [e for e in events if e["seq"] > floor]
    if since:
        cutoff = parse_since(since)
        events = [e for e in events if parse_ts(e["ts"]) >= cutoff]
    if keys:
        wanted = {k.upper() for k in keys}
        events = [e for e in events if e["key"] in wanted]
    return render_events(events) if events else "No matching events."


def render_sprint(index: dict, sprints: dict, jql: str, now_s: str) -> str:
    in_use = {s for r in index.values() for s in r["sprints"]}
    active = sorted((s for n, s in sprints.items() if n in in_use and s["state"] == "active"),
                    key=lambda s: s["name"])
    L = []
    for s in active:
        dates = f" ({s['start']} → {s['end']})" if s["start"] or s["end"] else ""
        L.append(f"# {s['name']}{dates}")
        if s["goal"]:
            L.append(f"Goal: {s['goal']}")
    if not active:
        L.append("# Jira scope")
    L.append(f"Synced {now_s} · JQL: `{jql}`")

    def pts(keys):
        total = sum(index[k]["points"] or 0 for k in keys if isinstance(index[k]["points"], (int, float)))
        return f" ({fmt_num(total)} pts)" if total else ""

    by_cat: dict[str, list[str]] = {}
    for k, r in index.items():
        by_cat.setdefault(r["cat"], []).append(k)
    cats = sorted(by_cat, key=lambda c: STATUS_CATEGORY_ORDER.get(c, 9))
    L.append(f"{len(index)} tickets{pts(list(index))} · " +
             " · ".join(f"{STATUS_CATEGORY_LABEL.get(c, c)} {len(by_cat[c])}{pts(by_cat[c])}" for c in cats))

    parents = {r["parent"]: r["parent_summary"] for r in index.values() if r["parent"]}
    if parents:
        L += ["", "## Parents / epics"] + [f"- {k}: {v}".rstrip(": ") for k, v in sorted(parents.items(), key=lambda kv: key_sort(kv[0]))]

    statuses = sorted({(STATUS_CATEGORY_ORDER.get(r["cat"], 9), r["status"]) for r in index.values()})
    for _, status in statuses:
        keys = sorted((k for k, r in index.items() if r["status"] == status), key=key_sort)
        L += ["", f"## {status} ({len(keys)})"]
        for k in keys:
            r = index[k]
            bits = [k, r["type"]]
            if r["priority"]:
                bits.append(f"P:{r['priority']}")
            if r["points"] is not None:
                bits.append(f"{fmt_num(r['points'])}pt")
            bits.append(f"@{r['assignee']}" if r["assignee"] else "unassigned")
            line = " ".join(bits) + f" — {r['summary']}"
            if r["parent"]:
                line += f" ^{r['parent']}"
            if r["blocked_by"]:
                line += f" [blocked by {', '.join(r['blocked_by'])}]"
            L.append(f"- {line}")
    return "\n".join(L)


# ----------------------------------------------------------------------- sync

def sync(client, jql: str, out: Path, extra_names=(), full: bool = False, now: datetime | None = None,
         workers: int = 6) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    now_s = now.strftime("%Y-%m-%d %H:%M UTC")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    state_path, feed_path = out / "state.json", out / "feed.jsonl"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    prev_index: dict = state.get("tickets", {})
    first = not state
    feed = load_feed(feed_path)
    seen = {e["id"] for e in feed}
    if state and not feed:
        full = True  # state without a feed (older version): backfill every ticket's history

    fm = state.get("fields")
    if full or not fm or fm.get("extra_names") != sorted(extra_names):
        full = full or bool(fm)  # new field set -> refetch everything so files are consistent
        fm = resolve_fields(client, list(extra_names))

    current = {i["key"]: (i.get("fields") or {}).get("updated") for i in client.search(jql, ["updated"])}
    added = sorted((k for k in current if k not in prev_index), key=key_sort)
    removed = sorted((k for k in prev_index if k not in current), key=key_sort)
    changed = sorted((k for k in current if k in prev_index and prev_index[k]["updated"] != current[k]), key=key_sort)
    to_fetch = sorted(current, key=key_sort) if full else added + changed

    ids = field_ids(fm)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        issues = dict(zip(to_fetch, ex.map(lambda k: client.issue(k, ids), to_fetch)))

    index = {k: v for k, v in prev_index.items() if k in current}
    sprints = state.get("sprints", {})
    new_events = []
    for k, issue in issues.items():
        t = normalize(issue, fm)
        events = [e for e in issue_events(issue, t) if e["id"] not in seen]
        if k in changed and not events:
            events = [{"id": f"{k}#updated@{t['updated']}", "ts": t["updated"], "key": k, "kind": "updated",
                       "author": "", "text": "updated (no changelog detail: attachment, worklog, or similar)"}]
        new_events += events
        index[k] = index_record(t)
        for s in t["sprints"]:
            sprints[s["name"]] = s
        write(out / "tickets" / f"{k}.md", render_ticket(t))
        (out / "archive" / f"{k}.md").unlink(missing_ok=True)
    for k in removed:
        src = out / "tickets" / f"{k}.md"
        if src.exists():
            (out / "archive").mkdir(exist_ok=True)
            shutil.move(str(src), str(out / "archive" / f"{k}.md"))
    in_use = {s for r in index.values() for s in r["sprints"]}
    sprints = {n: s for n, s in sprints.items() if n in in_use}

    if not first:  # scope moves are observed by the sync itself, so they're stamped with sync time
        for k in added:
            new_events.append({"id": f"{k}#scope+@{stamp}", "ts": stamp, "key": k, "kind": "scope",
                               "author": "jira-sync", "text": "entered sync scope"})
        for k in removed:
            new_events.append({"id": f"{k}#scope-@{stamp}", "ts": stamp, "key": k, "kind": "scope",
                               "author": "jira-sync", "text": "left sync scope (ticket file moved to archive/)"})

    seq_before = feed[-1]["seq"] if feed else 0
    new_events.sort(key=event_sort)
    for i, e in enumerate(new_events, seq_before + 1):
        e["seq"], e["synced"] = i, stamp
    if new_events:
        with feed_path.open("a", encoding="utf-8") as fh:
            for e in new_events:
                fh.write(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n")
    feed += new_events

    span = f"{fmt_ts(min(feed, key=event_sort)['ts'])[:10]} → {fmt_ts(max(feed, key=event_sort)['ts'])[:10]}" if feed else ""
    write(out / "FEED.md", f"# Jira feed\n{len(feed)} events · {span} · times UTC, oldest first.\n\n"
                           + render_events(feed))
    write(out / "SPRINT.md", render_sprint(index, sprints, jql, now_s))

    state = {"last_sync": now_s, "jql": jql, "fields": fm, "sprints": sprints, "seq_before_last_sync": seq_before,
             "tickets": {k: index[k] for k in sorted(index, key=key_sort)}}
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(state_path)

    return {"total": len(index), "added": len(added), "changed": len(changed), "removed": len(removed),
            "fetched": len(to_fetch), "first": first, "new_events": len(new_events), "feed_events": len(feed)}


def main(argv=None) -> int:
    load_dotenv(SYNC_HOME / ".env")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--jql", help="override JIRA_JQL")
    ap.add_argument("--project", help="override JIRA_PROJECT (used to build the default JQL)")
    ap.add_argument("--out", type=Path,
                    default=Path(os.environ.get("JIRA_SYNC_DIR") or SYNC_HOME / "sprint").expanduser())
    ap.add_argument("--full", action="store_true", help="re-fetch every ticket, not just changed ones")
    view = ap.add_argument_group("reading the feed (no sync, no network)")
    view.add_argument("--show", action="store_true", help="print feed events as Markdown instead of syncing")
    view.add_argument("--since", metavar="WHEN", help="only events at/after WHEN (e.g. 2026-11-18 or 2026-11-18T09:00; UTC unless offset given)")
    view.add_argument("--key", action="append", default=[], metavar="KEY", help="only this ticket (repeatable)")
    view.add_argument("--new", action="store_true", help="only events ingested by the most recent sync")
    args = ap.parse_args(argv)
    if args.show or args.since or args.key or args.new:
        print(show(args.out, since=args.since, keys=args.key, new=args.new))
        return 0

    base = os.environ.get("JIRA_BASE_URL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not base or not token:
        ap.error(f"set JIRA_BASE_URL and JIRA_API_TOKEN (env vars, or {SYNC_HOME / '.env'}; "
                 f"template: {Path(__file__).resolve().parent / 'env.example'})")
    deployment = (os.environ.get("JIRA_DEPLOYMENT") or ("cloud" if ".atlassian.net" in base else "server")).lower()
    cloud = deployment == "cloud"
    email = os.environ.get("JIRA_EMAIL")
    if cloud and not email:
        ap.error("Jira Cloud needs JIRA_EMAIL alongside JIRA_API_TOKEN")

    project = args.project or os.environ.get("JIRA_PROJECT")
    jql = args.jql or os.environ.get("JIRA_JQL") or (
        f'project = "{project}" AND sprint in openSprints()' if project else None)
    if not jql:
        ap.error("set JIRA_JQL or JIRA_PROJECT")
    extra = [x.strip() for x in (os.environ.get("JIRA_EXTRA_FIELDS") or "").split(",") if x.strip()]

    client = JiraClient(base, token, email=email, cloud=cloud)
    r = sync(client, jql, args.out, extra_names=extra, full=args.full)
    if r["first"]:
        print(f"jira-sync: initial sync, {r['total']} tickets, {r['new_events']} events backfilled → {args.out}")
    else:
        print(f"jira-sync: {r['total']} in scope · {r['added']} added · {r['changed']} changed · "
              f"{r['removed']} removed · {r['new_events']} new events ({r['feed_events']} in feed) → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
