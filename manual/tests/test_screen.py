"""screen.py: 화면 XML을 읽고, 요소를 찾고, 조건을 판정한다. 파이썬만 있으면 돈다."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake  # noqa: E402
from screen import Screen, evaluate, find_nodes, locate  # noqa: E402


class Parse(unittest.TestCase):
    def test_package_size_and_nodes(self):
        s = Screen.parse(fake.settings_home())
        self.assertEqual((s.package, s.width, s.height), (fake.SETTINGS, 1080, 2400))
        self.assertIn("디스플레이 및 터치", [n.text for n in s.app_nodes])

    def test_the_status_bar_is_not_part_of_the_app(self):
        s = Screen.parse(fake.settings_home())
        self.assertTrue(any(n.id == "clock" for n in s.nodes))
        self.assertFalse(any(n.id == "clock" for n in s.app_nodes))

    def test_id_is_the_part_after_the_package(self):
        s = Screen.parse(fake.data_saver(False))
        self.assertEqual(len(find_nodes({"id": "switch_widget"}, s)), 1)
        self.assertEqual(len(find_nodes({"id": "com.android.settings:id/switch_widget"}, s)), 1)

    def test_broken_or_missing_xml_is_an_empty_screen(self):
        for xml in ("", "<hierarchy><node", None):
            s = Screen.parse({"package": "p", "xml": xml})
            self.assertEqual(s.nodes, [])

    def test_invisible_and_off_screen_nodes_are_dropped(self):
        xml = ('<hierarchy><node text="보임" package="p" bounds="[0,0][100,100]" />'
               '<node text="안 보임" package="p" bounds="[0,0][100,100]" visible-to-user="false" />'
               '<node text="화면 밖" package="p" bounds="[2000,0][2100,100]" />'
               '<node text="크기 없음" package="p" bounds="[10,10][10,10]" /></hierarchy>')
        s = Screen.parse({"package": "p", "width": 1080, "height": 2400, "xml": xml})
        self.assertEqual([n.text for n in s.nodes], ["보임"])

    def test_switch_state_is_read(self):
        on, off = Screen.parse(fake.data_saver(True)), Screen.parse(fake.data_saver(False))
        self.assertTrue(find_nodes({"id": "switch_widget"}, on)[0].checked)
        self.assertFalse(find_nodes({"id": "switch_widget"}, off)[0].checked)


class Locate(unittest.TestCase):
    def test_one_match_is_found_at_its_centre(self):
        n = locate({"text": "디스플레이 및 터치"}, Screen.parse(fake.settings_home()))
        self.assertIsNotNone(n)
        self.assertEqual(n.center[1], (1690 + 36 + 1690 + 102) // 2)

    def test_no_match_is_none(self):
        self.assertIsNone(locate({"text": "없는 메뉴"}, Screen.parse(fake.settings_home())))

    def test_look_alikes_need_nth(self):
        s = Screen.parse(fake.text_size())
        self.assertIsNone(locate({"desc": "확대"}, s))
        first, second = locate({"desc": "확대", "nth": 0, "of": 2}, s), locate({"desc": "확대", "nth": 1, "of": 2}, s)
        self.assertLess(first.center[1], second.center[1])

    def test_nth_is_refused_when_the_count_changed(self):
        self.assertIsNone(locate({"desc": "확대", "nth": 0, "of": 2}, Screen.parse(fake.text_size(bigger=3))))

    def test_text_is_compared_without_case_and_extra_spaces(self):
        self.assertIsNotNone(locate({"text": "  play   스토어 "}, Screen.parse(fake.home())))


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.s = Screen.parse(fake.data_saver(False))

    def test_foreground(self):
        self.assertTrue(evaluate({"foreground": fake.SETTINGS}, self.s))
        self.assertFalse(evaluate({"foreground": fake.LAUNCHER}, self.s))

    def test_node_by_text_desc_id_and_state(self):
        self.assertTrue(evaluate({"node": {"text": "데이터 절약 모드 사용"}}, self.s))
        self.assertTrue(evaluate({"node": {"desc": "데이터 절약 모드"}}, self.s))
        self.assertTrue(evaluate({"node": {"id": "switch_widget", "checked": False}}, self.s))
        self.assertFalse(evaluate({"node": {"id": "switch_widget", "checked": True}}, self.s))

    def test_label_is_text_or_description(self):
        self.assertTrue(evaluate({"node": {"label": "위로 탐색"}}, self.s))

    def test_a_list_of_values_matches_any_of_them(self):
        self.assertTrue(evaluate({"node": {"text": ["Use Data Saver", "데이터 절약 모드 사용"]}}, self.s))

    def test_all_any_not(self):
        yes, no = {"node": {"desc": "위로 탐색"}}, {"node": {"text": "없는 글자"}}
        self.assertTrue(evaluate({"all": [yes, {"not": no}]}, self.s))
        self.assertFalse(evaluate({"all": [yes, no]}, self.s))
        self.assertTrue(evaluate({"any": [no, yes]}, self.s))
        self.assertFalse(evaluate({"any": [no]}, self.s))

    def test_empty_and_unknown_conditions_are_false(self):
        for cond in ({}, {"all": []}, {"something": 1}, {"node": {}}, {"node": {"colour": "red"}}, None, "text"):
            self.assertFalse(evaluate(cond, self.s), cond)

    def test_the_status_bar_never_satisfies_a_condition(self):
        self.assertFalse(evaluate({"node": {"id": "clock"}}, self.s))


if __name__ == "__main__":
    unittest.main()
