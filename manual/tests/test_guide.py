"""guide.py: 매뉴얼이 먼저 답하고, 아니면 artemis 서버로 넘긴다. 서버도 모델도 폰도 필요 없다. 파이썬만 있으면 돈다.

artemis 서버 자리에는 정해진 답을 차례로 내는 가짜를 둔다. 화면은 tests/fake.py의 손으로 쓴 화면이다.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import fake  # noqa: E402
from guide import MAX_WAITS, Guide  # noqa: E402
from store import Manuals, norm_task  # noqa: E402

TASK = "데이터 절약 모드를 켜고 싶어요"
NOTE = "\n\nThe person does every action of this task themselves now, following your guide."


class FakeArtemis:
    """Stands where the artemis server is: answers from a list, and keeps what it was asked."""

    def __init__(self, replies: list[dict]):
        self.replies, self.asked = list(replies), []

    def __call__(self, req: dict) -> tuple[int, dict]:
        self.asked.append(req)
        if not self.replies:
            return 502, {"error": "nothing left to answer"}
        return 200, {"memo": "", "meta": {"source": "model", "model_calls": 1}, **self.replies.pop(0)}


def saver(**kw) -> list[tuple]:
    """The data saver session as (screen, what artemis answers on it); the last answer says it is done."""
    return [(scr, reply) for scr, _facts, reply in fake.saver(facts=False, **kw)]


def request(sid: str, step: int, event: str, scr: dict, task: str = TASK + NOTE) -> dict:
    return {"session_id": sid, "task": task, "step": step, "elapsed_s": step * 3, "event": event, "event_detail": "", "screen": scr}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manuals = Manuals(self.tmp.name)

    def guide(self, replies: list[dict], authoring: bool) -> tuple[Guide, FakeArtemis]:
        g = Guide(self.manuals, "http://artemis.invalid", authoring=authoring, device_model=fake.DEVICE)
        art = FakeArtemis(replies)
        g._artemis = art
        return g, art

    def follow(self, g: Guide, sid: str, steps: list[tuple]) -> list[dict]:
        """Send the screens of a session in order, each after the last guide was 'met'. Returns the answers."""
        out = []
        for i, (scr, _reply) in enumerate(steps):
            code, body = g.next(request(sid, i + 1, "start" if i == 0 else "met", scr))
            self.assertEqual(code, 200)
            out.append(body)
        return out

    def authored(self) -> dict:
        """A manual written from one finished session in which artemis answered every step."""
        m = self.manuals.create(TASK, "saver")
        steps = saver()
        g, _ = self.guide([reply for _scr, reply in steps], authoring=True)
        self.follow(g, "author-1", steps)
        self.registered = g.register("author-1")
        return self.manuals.get(m["id"])


class Authoring(Case):
    def test_a_finished_session_becomes_the_manuals_steps(self):
        m = self.authored()
        self.assertEqual(self.registered["new"], 5)
        self.assertEqual([s["done_condition"]["name"] for s in m["steps"]], ["click"] * 4 + ["report_task_status"])

    def test_nothing_is_saved_from_a_session_that_did_not_reach_the_end(self):
        self.manuals.create(TASK, "saver")
        steps = saver()[:2]
        g, _ = self.guide([reply for _scr, reply in steps], authoring=True)
        self.follow(g, "half", steps)
        self.assertEqual(g.register("half")["new"], 0)
        self.assertEqual(self.manuals.get("saver")["steps"], [])

    def test_the_second_time_the_manual_answers_and_artemis_is_not_asked(self):
        self.authored()
        g, art = self.guide([], authoring=True)
        answers = self.follow(g, "author-2", saver(clock="09:41"))
        self.assertEqual([a["source"] for a in answers], ["manual"] * 5)
        self.assertEqual(art.asked, [])
        self.assertTrue(g.summary("author-2")["completed"])

    def test_a_step_that_left_the_person_stuck_is_removed_while_authoring(self):
        m = self.authored()
        g, art = self.guide([{"explain": "", "action": {"name": "wait_for_delay", "time_in_ms": 500}}], authoring=True)
        g.next(request("s", 1, "start", fake.home()))
        g.next(request("s", 2, "declared_stuck", fake.home()))
        self.assertEqual(len(self.manuals.get(m["id"])["steps"]), 4)
        self.assertEqual(len(art.asked), 1)


    def test_a_rule_that_says_done_where_the_device_says_not_done_is_removed(self):
        m = self.authored()
        g, _ = self.guide([], authoring=True)
        self.follow(g, "author-3", saver())
        self.assertEqual(g.reject("author-3"), 5)
        self.assertEqual([s["done_condition"]["name"] for s in self.manuals.get(m["id"])["steps"]], ["click"] * 4)
        self.assertEqual(g.register("author-3")["new"], 0)


class Front(Case):
    def test_a_draft_is_not_used_for_people(self):
        self.authored()  # status stays draft until every start state went through clean
        g, art = self.guide([{"explain": "", "action": {"name": "click", "target_point_px": [1, 1]}}], authoring=False)
        code, body = g.next(request("u", 1, "start", fake.home()))
        self.assertEqual((code, body["source"], len(art.asked)), (200, "model", 1))

    def verified(self) -> dict:
        m = self.authored()
        m["status"] = "verified"
        self.manuals.save(m)
        return m

    def test_a_verified_manual_answers_first(self):
        self.verified()
        g, art = self.guide([], authoring=False)
        answers = self.follow(g, "u", saver())
        self.assertEqual([a["source"] for a in answers], ["manual"] * 5)
        self.assertEqual(answers[0]["action"]["name"], "click")
        self.assertIn("target_point_px", answers[0]["action"])
        self.assertEqual(art.asked, [])

    def test_another_request_goes_straight_to_artemis_unchanged(self):
        self.verified()
        g, art = self.guide([{"explain": "x", "action": {"name": "click", "target_point_px": [1, 1]}}], authoring=False)
        req = request("u", 1, "start", fake.home(), task="와이파이 켜줘" + NOTE)
        code, body = g.next(req)
        self.assertEqual((code, body["source"]), (200, "model"))
        self.assertEqual(art.asked[0], req)

    def test_the_request_is_matched_as_the_person_wrote_it(self):
        self.verified()
        self.assertIsNotNone(self.manuals.match("데이터 절약 모드를  켜고 싶어요!" + NOTE))
        self.assertIsNone(self.manuals.match("데이터 절약 모드를 끄고 싶어요"))
        self.assertEqual(norm_task(TASK + NOTE), norm_task(TASK))

    def test_a_screen_no_step_knows_falls_back_and_artemis_is_told_the_step_before(self):
        self.verified()
        g, art = self.guide([{"explain": "뒤로", "action": {"name": "press_key", "key": "BACK"}}], authoring=False)
        first = g.next(request("u", 1, "start", fake.home()))[1]
        again = g.next(request("u", 2, "met", fake.connected()))[1]  # a wrong turn: not a screen of this manual
        code, body = g.next(request("u", 3, "wait_done", fake.connected()))
        # one more look first (the screen a step led to may still be loading), then artemis
        self.assertEqual((first["source"], again["source"], again["action"]["name"]), ("manual", "wait", "wait_for_delay"))
        self.assertEqual((body["source"], body["action"]["name"]), ("model", "press_key"))
        self.assertEqual(art.asked[0]["replay"]["previous"]["reply"]["action"]["name"], "click")
        self.assertIn("1) click 설정 (met)", art.asked[0]["event_detail"])
        self.assertNotIn("manual_id", art.asked[0])

    def test_an_unreadable_screen_is_waited_for_before_artemis_is_asked(self):
        self.verified()
        g, art = self.guide([{"explain": "", "action": {"name": "wait_for_delay", "time_in_ms": 1000}}], authoring=False)
        blank = fake.screen("com.android.settings", [])
        names = []
        for i in range(MAX_WAITS + 1):
            _, body = g.next(request("u", i + 1, "start" if i == 0 else "wait_done", blank))
            names.append(body["source"])
        self.assertEqual(names, ["wait"] * MAX_WAITS + ["model"])
        self.assertEqual(len(art.asked), 1)

    def test_a_step_that_left_the_person_stuck_stays_in_the_manual_but_is_not_guided_again(self):
        m = self.verified()
        g, art = self.guide([{"explain": "", "action": {"name": "ask_user", "question": "?"}}], authoring=False)
        g.next(request("u", 1, "start", fake.home()))
        _, body = g.next(request("u", 2, "declared_stuck", fake.home()))
        self.assertEqual(body["source"], "model")
        self.assertEqual(len(self.manuals.get(m["id"])["steps"]), 5)

    def test_artemis_being_down_is_an_error_not_a_guess(self):
        self.verified()
        g, _ = self.guide([], authoring=False)
        g.next(request("u", 1, "start", fake.home()))
        g.next(request("u", 2, "met", fake.connected()))
        code, body = g.next(request("u", 3, "wait_done", fake.connected()))
        self.assertEqual(code, 502)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
