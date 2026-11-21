"""rules/: 저장된 단계 찾기(find), 검토(review), 배우기(learn). 서버도 모델도 필요 없다. 파이썬만 있으면 돈다.

여기 있는 테스트는 지켜야 할 것들이다: 틀린 안내를 내지 않는다, 등록한 단계는 다시 찾는다.
캐시를 고친 뒤에도 전부 통과해야 한다. 얼마나 잘 찾는지는 manual/replay.py로 잰다.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import rules as cache  # noqa: E402
import fake  # noqa: E402
import replay  # noqa: E402
from screen import Screen, evaluate  # noqa: E402


def session(manual_id: str, steps: list[tuple], device: str = fake.DEVICE) -> dict:
    """A hand-written session in the shape replay.load returns for a recorded file."""
    lines = fake.exchanges(manual_id, steps, device=device)
    ex = [dict(x, screen=Screen.parse(x["request"]["screen"])) for x in lines[1:]]
    return {"name": "t", "manual": lines[0]["manual"], "manual_id": manual_id, "device": device, "exchanges": ex}


def verdicts(steps: list[dict], s: dict) -> list[str]:
    return [r["verdict"] for r in replay.test(steps, s)]


def find(steps: list[dict], scr: dict, device: str = fake.DEVICE):
    return cache.find({"manual_id": "m", "device_model": device, "screen": Screen.parse(scr)}, steps)


class LearnThenFind(unittest.TestCase):
    def setUp(self):
        self.first = session("sample-saver", fake.saver())
        self.taught = replay.teach(self.first)
        self.steps = self.taught["steps"]

    def test_every_step_of_a_finished_session_is_registered(self):
        self.assertEqual((self.taught["done"], self.taught["kept"], len(self.steps)), (4, 4, 4))

    def test_the_same_session_is_followed_from_the_saved_steps(self):
        self.assertEqual(verdicts(self.steps, self.first), ["맞음"] * 4)

    def test_another_time_of_day_makes_no_difference(self):
        self.assertEqual(verdicts(self.steps, session("sample-saver", fake.saver(clock="09:41"))), ["맞음"] * 4)

    def test_the_target_is_found_where_it_is_now_not_where_it_was(self):
        hit = find(self.steps, fake.settings_home(scrolled=120))
        then = self.first["exchanges"][1]["response"]["action"]["target_point_px"]
        self.assertEqual(hit["action"]["target_text"], "네트워크 및 인터넷")
        self.assertEqual(hit["action"]["target_point_px"][1], then[1] - 120)

    def test_a_renamed_menu_is_missed_and_never_guessed(self):
        got = verdicts(self.steps, session("sample-saver", fake.saver(network_label="연결")))
        self.assertEqual(got, ["맞음", "못 찾음", "맞음", "맞음"])

    def test_starting_in_the_middle_gives_the_step_of_that_screen(self):
        hit = find(self.steps, fake.data_saver(False))
        self.assertEqual((hit["step"]["seq"], hit["action"]["target_text"]), (4, "데이터 절약 모드 사용"))

    def test_among_the_steps_that_hold_the_last_one_is_guided(self):
        # A finished step can still hold on a later screen; the later step is the one to guide.
        early = dict(self.steps[1], id=11, seq=1, screen_condition={"foreground": fake.SETTINGS})
        late = dict(self.steps[1], id=12, seq=2, done_condition={"name": "click", "target": {"text": "연결된 기기"}})
        for steps in ([early, late], [late, early]):
            self.assertEqual(find(steps, fake.settings_home())["action"]["target_text"], "연결된 기기")

    def test_the_last_holding_step_decides_even_when_its_target_is_gone(self):
        early = dict(self.steps[1], id=11, seq=1)
        late = dict(self.steps[1], id=12, seq=2, done_condition={"name": "click", "target": {"text": "없는 메뉴"}})
        self.assertIsNone(find([early, late], fake.settings_home()))

    def test_a_screen_of_another_app_finds_nothing(self):
        other = fake.screen("com.android.chrome", [fake.node(text="네트워크 및 인터넷", bounds=(0, 300, 500, 400))])
        self.assertIsNone(find(self.steps, other))

    def test_an_unfinished_session_teaches_nothing(self):
        cut = session("sample-saver", fake.saver()[:3])
        self.assertEqual(replay.teach(cut)["steps"], [])


class SwitchState(unittest.TestCase):
    def test_turning_on_is_not_a_step_once_it_is_on(self):
        steps = replay.teach(session("sample-saver", fake.saver()))["steps"]
        self.assertIsNone(find(steps, fake.data_saver(True)))

    def test_the_end_is_recognised_by_the_switch_being_on(self):
        steps = replay.teach(session("sample-saver", fake.saver(facts=False)))["steps"]
        self.assertEqual(len(steps), 5)
        self.assertEqual(find(steps, fake.data_saver(True))["action"]["name"], "report_task_status")
        self.assertEqual(find(steps, fake.data_saver(False))["action"]["name"], "click")


class WrongTurns(unittest.TestCase):
    def test_the_review_cuts_a_wrong_turn_and_the_way_back(self):
        taught = replay.teach(session("sample-saver", fake.saver_detour()))
        self.assertEqual((taught["done"], taught["kept"]), (6, 4))
        targets = [next(iter(s["done_condition"]["target"].values())) for s in taught["steps"]]
        self.assertEqual(targets, ["설정", "네트워크 및 인터넷", "데이터 절약 모드", "데이터 절약 모드 사용"])

    def test_a_back_arrow_step_does_not_hold_on_other_screens_with_a_back_arrow(self):
        # Saved steps once called each other without end: "위로 탐색" learned on one screen was offered on every screen.
        wrong = fake.connected()
        earlier = [Screen.parse(fake.home()), Screen.parse(fake.settings_home())]
        step = cache.learn({"manual_id": "m", "device_model": fake.DEVICE, "screen": Screen.parse(wrong), "earlier": earlier},
                           fake.click(wrong, "위로 탐색"))
        self.assertIsNotNone(step)
        self.assertTrue(evaluate(step["screen_condition"], Screen.parse(wrong)))
        for other in (fake.network(), fake.data_saver(False), fake.display(), fake.text_size()):
            self.assertFalse(evaluate(step["screen_condition"], Screen.parse(other)))

    def test_a_condition_never_holds_on_an_earlier_screen_of_its_session(self):
        s = session("sample-saver", fake.saver())
        steps = replay.teach(s)["steps"]
        screens = [e["screen"] for e in s["exchanges"]]
        for i, step in enumerate(steps):
            for before in screens[:i]:
                self.assertFalse(evaluate(step["screen_condition"], before), step["screen_condition"])


class LookAlikes(unittest.TestCase):
    def setUp(self):
        self.steps = replay.teach(session("sample-font", fake.font()))["steps"]

    def test_one_of_two_buttons_is_saved_with_its_place(self):
        self.assertEqual(self.steps[3]["done_condition"]["target"], {"desc": "확대", "nth": 0, "of": 2})

    def test_the_first_button_is_found_again(self):
        self.assertEqual(find(self.steps, fake.text_size())["action"]["target_point_px"], [990, 1543])

    def test_nothing_is_found_when_the_number_of_buttons_changed(self):
        self.assertIsNone(find(self.steps, fake.text_size(bigger=3)))

    def test_a_target_with_too_many_look_alikes_is_not_registered(self):
        nodes = [fake.node(text="열기", clickable=True, bounds=(0, 300 + 200 * i, 400, 400 + 200 * i)) for i in range(cache.MAX_LOOKALIKES + 1)]
        scr = fake.screen("p", nodes)
        self.assertIsNone(cache.learn({"screen": Screen.parse(scr), "earlier": []}, fake.click(scr, "열기", 2)))


class Devices(unittest.TestCase):
    def setUp(self):
        self.theirs = replay.teach(session("sample-saver", fake.saver(), device="other-phone"))["steps"]

    def test_another_phones_steps_are_used_when_this_phone_has_none(self):
        self.assertEqual(find(self.theirs, fake.settings_home(), device="my-phone")["action"]["target_text"], "네트워크 및 인터넷")

    def test_this_phones_own_step_comes_first(self):
        mine = copy.deepcopy(self.theirs[1])
        mine.update(id=99, device_model="my-phone")
        mine["done_condition"]["target"] = {"text": "연결된 기기"}
        hit = find(self.theirs + [mine], fake.settings_home(), device="my-phone")
        self.assertEqual(hit["action"]["target_text"], "연결된 기기")


class Robustness(unittest.TestCase):
    def test_steps_with_empty_or_odd_conditions_are_ignored(self):
        odd = [{"id": 1, "seq": 1, "device_model": fake.DEVICE, "instruction": "", "screen_condition": {}, "done_condition": {}},
               {"id": 2, "seq": 2, "device_model": fake.DEVICE, "instruction": "", "screen_condition": {"what": 1}, "done_condition": {"name": "click"}}]
        self.assertIsNone(find(odd, fake.home()))

    def test_waits_and_questions_are_not_learned(self):
        q = {"screen": Screen.parse(fake.home()), "earlier": []}
        for action in ({"name": "wait", "seconds": 2}, {"name": "ask_user", "question": "?"}, {"name": "report_task_status", "status": "failed"}):
            self.assertIsNone(cache.learn(q, {"explain": "", "action": action}))

    def test_a_click_that_lands_on_nothing_is_not_learned(self):
        reply = {"explain": "", "action": {"name": "click", "target_point_px": [5, 2395]}}
        self.assertIsNone(cache.learn({"screen": Screen.parse(fake.home()), "earlier": []}, reply))


class Samples(unittest.TestCase):
    """The sample files next to the tests are what fake.py writes, and the replay gives no wrong step on them."""

    def test_sample_files_are_current(self):
        for name, (manual_id, steps) in fake.SAMPLES.items():
            path = fake.SESSIONS / f"{name}.jsonl"
            self.assertTrue(path.exists(), f"{path} 가 없습니다: python manual/tests/fake.py")
            self.assertEqual(len(replay.load(path)["exchanges"]), len(steps))

    def test_no_wrong_step_on_any_recorded_or_sample_session(self):
        sessions = [replay.load(p) for p in sorted(fake.SESSIONS.glob("*.jsonl"))]
        for a in sessions:
            if not replay.finished(a):
                continue
            steps = replay.teach(a)["steps"]
            for b in sessions:
                if b["manual_id"] == a["manual_id"]:
                    wrong = [r for r in replay.test(steps, b) if r["verdict"] == "틀림"]
                    self.assertEqual(wrong, [], f"{a['name']}에서 배워 {b['name']}에 넣었을 때")


if __name__ == "__main__":
    unittest.main()
