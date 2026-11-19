"""End-to-end tests against a fake Jira client. Run: python3 -m unittest discover tests"""
import copy
import sys
import tempfile
import unittest
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
        self.assertIn("Initial sync — 3 tickets", self.read("CHANGES.md"))
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
        changes = self.read("CHANGES.md")
        self.assertIn("1 added · 1 changed · 1 removed · 3 in scope", changes)
        self.assertIn("- SHOP-13 [To Do] Story · unassigned · Refunds", changes)
        self.assertIn("- SHOP-12 [last: To Do] API keys", changes)
        self.assertIn("2026-11-19 08:00 Ana: status: To Do → In Progress", changes)
        self.assertIn("Ana: description edited", changes)
        self.assertIn("Bo commented: Keys are in the vault now.", changes)
        self.assertNotIn("Old", changes)  # pre-dates the previous sync
        self.assertTrue((self.out / "archive/SHOP-12.md").exists())
        self.assertFalse((self.out / "tickets/SHOP-12.md").exists())
        self.assertEqual(len(list((self.out / "history").iterdir())), 2)

        # 3. nothing changed -> nothing fetched, no new history entry
        jira.fetched.clear()
        r = jira_sync.sync(jira, "sprint in openSprints()", self.out,
                           now=datetime(2026, 11, 19, 10, tzinfo=timezone.utc))
        self.assertEqual(jira.fetched, [])
        self.assertIn("# No changes", self.read("CHANGES.md"))
        self.assertEqual(len(list((self.out / "history").iterdir())), 2)


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


if __name__ == "__main__":
    unittest.main()
