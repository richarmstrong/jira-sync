"""End-to-end tests against a fake Jira client. Run: python3 -m unittest discover tests"""
import copy
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "jira-sprint"))
import jira_sync  # noqa: E402

TODO = {"name": "To Do", "statusCategory": {"key": "new"}}
DOING = {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}
DONE = {"name": "Done", "statusCategory": {"key": "done"}}
SPRINT = [{"id": 7, "name": "Sprint 42", "state": "active", "startDate": "2026-11-10T09:00:00.000Z",
           "endDate": "2026-11-24T09:00:00.000Z", "goal": "Ship checkout v2"}]


def adf(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def para(*inline):
    return {"type": "paragraph", "content": list(inline)}


def text(s, *marks):
    return {"type": "text", "text": s, "marks": [{"type": m} for m in marks]}


def issue(key, summary, status, updated, points=None, desc=None, links=(), comments=(), histories=(),
          assignee="Ana"):
    return {"key": key, "fields": {
        "summary": summary, "status": status, "issuetype": {"name": "Story"}, "priority": {"name": "High"},
        "assignee": {"displayName": assignee} if assignee else None, "reporter": {"displayName": "Bo"},
        "labels": [], "components": [], "fixVersions": [], "subtasks": [], "issuelinks": list(links),
        "parent": {"key": "SHOP-1", "fields": {"summary": "Checkout v2"}},
        "description": desc, "comment": {"comments": list(comments), "total": len(comments)},
        "created": "2026-11-01T10:00:00.000+0000", "updated": updated, "resolution": None, "duedate": None,
        "customfield_10016": points, "customfield_10020": SPRINT,
    }, "changelog": {"histories": list(histories), "total": len(histories)}}


class FakeJira:
    cloud, api = True, "/rest/api/3"

    def __init__(self, issues):
        self.issues = {i["key"]: i for i in issues}
        self.fetched = []

    def fields(self):
        return [{"id": "customfield_10016", "name": "Story point estimate"},
                {"id": "customfield_10020", "name": "Sprint"},
                {"id": "summary", "name": "Summary"}]

    def search(self, jql, fields):
        for k, i in self.issues.items():
            yield {"key": k, "fields": {"updated": i["fields"]["updated"]}}

    def issue(self, key, fields):
        self.fetched.append(key)
        return copy.deepcopy(self.issues[key])


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, rel):
        return (self.out / rel).read_text()

    def test_incremental_flow(self):
        desc = adf(para(text("Use "), text("Stripe", "strong"), text(" for payments.")),
                   {"type": "bulletList", "content": [
                       {"type": "listItem", "content": [para(text("card"))]},
                       {"type": "listItem", "content": [para(text("apple pay")),
                                                        {"type": "bulletList", "content": [
                                                            {"type": "listItem", "content": [para(text("web only"))]}]}]}]},
                   {"type": "codeBlock", "attrs": {"language": "py"}, "content": [{"type": "text", "text": "pay()"}]})
        blocked = {"type": {"inward": "is blocked by", "outward": "blocks"},
                   "inwardIssue": {"key": "SHOP-12", "fields": {"summary": "API keys", "status": TODO}}}
        jira = FakeJira([
            issue("SHOP-10", "Card payments", TODO, "2026-11-18T09:00:00.000+0000", 5, desc, links=[blocked]),
            issue("SHOP-11", "Receipts", DOING, "2026-11-18T09:00:00.000+0000", 3),
            issue("SHOP-12", "API keys", TODO, "2026-11-18T09:00:00.000+0000", 2),
        ])

        # 1. initial sync
        r = jira_sync.sync(jira, "sprint in openSprints()", self.out,
                           now=datetime(2026, 11, 18, 10, tzinfo=timezone.utc))
        self.assertTrue(r["first"])
        self.assertEqual(sorted(jira.fetched), ["SHOP-10", "SHOP-11", "SHOP-12"])
        self.assertEqual(r["new_events"], 3)  # one "created" per ticket
        self.assertIn("- 10:00 SHOP-11 Bo: created: Receipts", self.read("FEED.md"))
        sprint = self.read("SPRINT.md")
        self.assertIn("# Sprint 42 (2026-11-10 → 2026-11-24)", sprint)
        self.assertIn("Goal: Ship checkout v2", sprint)
        self.assertIn("3 tickets (10 pts)", sprint)
        self.assertIn("SHOP-10 Story P:High 5pt @Ana — Card payments ^SHOP-1 [blocked by SHOP-12]", sprint)
        self.assertLess(sprint.index("## In Progress"), sprint.index("## To Do"))
        t10 = self.read("tickets/SHOP-10.md")
        self.assertIn("Use **Stripe** for payments.", t10)
        self.assertIn("- apple pay\n  - web only", t10)
        self.assertIn("```py\npay()\n```", t10)
        self.assertIn("- is blocked by SHOP-12 [To Do] API keys", t10)

        # 2. SHOP-10 moves + gets a comment, SHOP-12 leaves scope, SHOP-13 arrives, SHOP-11 untouched
        i10 = jira.issues["SHOP-10"]
        i10["fields"]["status"] = DOING
        i10["fields"]["updated"] = "2026-11-19T08:05:00.000+0000"
        i10["changelog"]["histories"] = [
            {"created": "2026-11-17T08:00:00.000+0000", "author": {"displayName": "Old"},
             "items": [{"field": "summary", "fromString": "x", "toString": "y"}]},
            {"created": "2026-11-19T08:00:00.000+0000", "author": {"displayName": "Ana"},
             "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"},
                       {"field": "description", "fromString": "a" * 200, "toString": "b" * 200}]}]
        i10["fields"]["comment"] = {"total": 1, "comments": [
            {"author": {"displayName": "Bo"}, "created": "2026-11-19T08:05:00.000+0000",
             "updated": "2026-11-19T08:05:00.000+0000", "body": adf(para(text("Keys are in the vault now.")))}]}
        del jira.issues["SHOP-12"]
        jira.issues["SHOP-13"] = issue("SHOP-13", "Refunds", TODO, "2026-11-19T07:00:00.000+0000", 8,
                                       assignee=None)
        jira.fetched.clear()

        r = jira_sync.sync(jira, "sprint in openSprints()", self.out,
                           now=datetime(2026, 11, 19, 9, tzinfo=timezone.utc))
        self.assertEqual(sorted(jira.fetched), ["SHOP-10", "SHOP-13"])
        self.assertEqual((r["added"], r["changed"], r["removed"]), (1, 1, 1))
        # SHOP-10: old summary edit (backfilled) + status + description + comment;
        # SHOP-13: created + entered scope; SHOP-12: left scope.
        self.assertEqual(r["new_events"], 7)
        self.assertEqual(r["feed_events"], 10)
        new = jira_sync.show(self.out, new=True)
        self.assertIn("## 2026-11-17\n- 08:00 SHOP-10 Old: summary: x → y", new)  # history is never filtered out
        self.assertIn("- 08:00 SHOP-10 Ana: status: To Do → In Progress", new)
        self.assertIn("- 08:00 SHOP-10 Ana: description edited", new)
        self.assertIn("- 08:05 SHOP-10 Bo: commented: Keys are in the vault now.", new)
        self.assertIn("SHOP-13 Bo: created: Refunds", new)
        self.assertIn("- 09:00 SHOP-12 jira-sync: left sync scope", new)
        self.assertIn("- 09:00 SHOP-13 jira-sync: entered sync scope", new)
        self.assertNotIn("SHOP-11", new)  # ingested by the earlier sync
        feed_md = self.read("FEED.md")
        self.assertIn("10 events · 2026-11-01 → 2026-11-19", feed_md)
        self.assertIn("SHOP-11 Bo: created: Receipts", feed_md)  # earlier syncs' events are kept
        since = jira_sync.show(self.out, since="2026-11-19")
        self.assertNotIn("Old", since)
        self.assertNotIn("created", since)
        one = jira_sync.show(self.out, keys=["shop-10"])
        self.assertEqual({ln.split()[2] for ln in one.splitlines() if ln.startswith("- ")}, {"SHOP-10"})
        self.assertTrue((self.out / "archive/SHOP-12.md").exists())
        self.assertFalse((self.out / "tickets/SHOP-12.md").exists())

        # 3. nothing changed -> nothing fetched, nothing appended
        jira.fetched.clear()
        r = jira_sync.sync(jira, "sprint in openSprints()", self.out,
                           now=datetime(2026, 11, 19, 10, tzinfo=timezone.utc))
        self.assertEqual(jira.fetched, [])
        self.assertEqual(r["new_events"], 0)
        self.assertEqual(jira_sync.show(self.out, new=True), "No matching events.")

        # 4. touched with no changelog detail (e.g. attachment) -> one "updated" event
        jira.issues["SHOP-11"]["fields"]["updated"] = "2026-11-19T10:30:00.000+0000"
        r = jira_sync.sync(jira, "sprint in openSprints()", self.out,
                           now=datetime(2026, 11, 19, 11, tzinfo=timezone.utc))
        self.assertEqual(r["new_events"], 1)
        self.assertIn("- 10:30 SHOP-11: updated (no changelog detail", jira_sync.show(self.out, new=True))

        # 5. --full re-derives everything but appends nothing twice
        r = jira_sync.sync(jira, "sprint in openSprints()", self.out, full=True,
                           now=datetime(2026, 11, 19, 12, tzinfo=timezone.utc))
        self.assertEqual(r["new_events"], 0)
        self.assertEqual(len(self.read("feed.jsonl").splitlines()), 11)
        seqs = [jira_sync.json.loads(ln)["seq"] for ln in self.read("feed.jsonl").splitlines()]
        self.assertEqual(seqs, list(range(1, 12)))


class AdfTest(unittest.TestCase):
    def test_inline_nodes(self):
        doc = adf(para({"type": "mention", "attrs": {"text": "@Ana"}}, text(" see "),
                       {"type": "text", "text": "docs", "marks": [{"type": "link", "attrs": {"href": "https://x"}}]},
                       {"type": "hardBreak"}, {"type": "status", "attrs": {"text": "BLOCKED"}}),
                  {"type": "taskList", "content": [
                      {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [text("a")]},
                      {"type": "taskItem", "attrs": {"state": "TODO"}, "content": [text("b")]}]})
        self.assertEqual(jira_sync.to_md(doc), "@Ana see [docs](https://x)\n[BLOCKED]\n\n- [x] a\n- [ ] b")

    def test_server_sprint_string(self):
        s = jira_sync.parse_sprints(["com.atlassian.greenhopper.service.sprint.Sprint@1[id=3,rapidViewId=1,"
                                     "state=ACTIVE,name=Sprint 9,startDate=2026-11-01T00:00:00.000Z,"
                                     "endDate=<null>,goal=<null>]"])
        self.assertEqual(s, [{"name": "Sprint 9", "state": "active", "start": "2026-11-01", "end": "", "goal": ""}])


class BoardClient(jira_sync.JiraClient):
    """Real board_scope logic over canned Agile API responses."""

    def __init__(self, board_type="scrum", sprints=(), sub_query=None):
        super().__init__("https://x.atlassian.net", "token", email="me@example.com")
        config = {"filter": {"id": 100}}
        if sub_query:
            config["subQuery"] = {"query": sub_query}
        self.routes = {
            "/rest/agile/1.0/board/42": {"id": 42, "name": "Payments", "type": board_type},
            "/rest/agile/1.0/board/42/configuration": config,
            "/rest/agile/1.0/board/42/sprint": {"values": [{"id": i} for i in sprints], "isLast": True},
        }
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.routes[path]


class BoardScopeTest(unittest.TestCase):
    def test_scrum_uses_board_filter_and_active_sprints(self):
        c = BoardClient(sprints=[9, 7])
        self.assertEqual(c.board_scope("42"), ("filter = 100 AND sprint in (7, 9)", "board 42 (Payments)"))
        self.assertEqual(c.calls[-1][1]["state"], "active")

    def test_scrum_without_active_sprint(self):
        self.assertEqual(BoardClient().board_scope("42"), (None, "board 42 (Payments)"))

    def test_kanban_uses_filter_and_subquery(self):
        jql, _ = BoardClient("kanban", sub_query="fixVersion is EMPTY").board_scope("42")
        self.assertEqual(jql, "filter = 100 AND (fixVersion is EMPTY)")
        self.assertEqual(BoardClient("kanban").board_scope("42")[0], "filter = 100")


class MainScopeTest(unittest.TestCase):
    ENV = {"JIRA_BASE_URL": "https://x.atlassian.net", "JIRA_API_TOKEN": "t", "JIRA_EMAIL": "me@example.com",
           "JIRA_JQL": "project = ENV", "JIRA_BOARD_ID": "", "JIRA_PROJECT": "", "JIRA_EXTRA_FIELDS": ""}

    def run_main(self, argv, sprints=(7,)):
        seen = {}

        def fake_sync(client, jql, out, **kw):
            seen["jql"], seen["label"] = jql, kw.get("scope_label")
            return {"first": True, "total": 0, "new_events": 0}

        out = io.StringIO()
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(jira_sync, "JiraClient", lambda *a, **k: BoardClient(sprints=sprints)), \
                mock.patch.object(jira_sync, "sync", fake_sync), redirect_stdout(out):
            jira_sync.main(argv + ["--out", tempfile.gettempdir()])
        return seen, out.getvalue()

    def test_board_flag_beats_env_jql(self):
        seen, _ = self.run_main(["--board", "42"])
        self.assertEqual(seen, {"jql": "filter = 100 AND sprint in (7)", "label": "board 42 (Payments)"})

    def test_env_jql_used_without_flags(self):
        self.assertEqual(self.run_main([])[0]["jql"], "project = ENV")

    def test_no_active_sprint_skips_sync(self):
        seen, printed = self.run_main(["--board", "42"], sprints=())
        self.assertEqual(seen, {})
        self.assertIn("no active sprint; nothing synced", printed)


if __name__ == "__main__":
    unittest.main()
