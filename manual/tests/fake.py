"""손으로 쓴 화면과 세션. 실제 폰에서 녹화한 것이 아니다.

에뮬레이터(Pixel, Android 16, 한국어)의 설정 앱에서 본 글자와 id를 따라 썼고, 요소 수와 위치는 줄였다.
테스트(test_*.py)와 규칙 재생(manual/replay.py)의 견본 입력으로 쓴다. 실제 화면은 녹화해서 sessions/에 넣는다.

    python manual/tests/fake.py     # sessions/sample-*.jsonl을 다시 쓴다
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from screen import Screen, find_nodes  # noqa: E402

W, H = 1080, 2400
SETTINGS = "com.android.settings"
LAUNCHER = "com.google.android.apps.nexuslauncher"
SYSTEMUI = "com.android.systemui"
DEVICE = "handwritten-phone"
SESSIONS = HERE / "sessions"


# ---------------------------------------------------------------- 화면을 XML로

def node(text="", desc="", id="", bounds=(0, 0, 0, 0), clickable=False, checkable=False, checked=False,
         cls="android.widget.TextView", pkg="", editable=False) -> dict:
    return {"text": text, "desc": desc, "id": id, "bounds": bounds, "clickable": clickable, "checkable": checkable,
            "checked": checked, "cls": cls, "pkg": pkg, "editable": editable}


def _tf(v: bool) -> str:
    return "true" if v else "false"


def screen(package: str, nodes: list[dict], clock: str = "12:06") -> dict:
    """The `screen` part of a /guide/next request. The XML has the attributes the app writes (ScreenSnapshot.kt)."""
    bar = [node(text=clock, id="clock", pkg=SYSTEMUI, bounds=(36, 40, 160, 90)),
           node(desc="배터리 100퍼센트", id="battery", pkg=SYSTEMUI, bounds=(990, 44, 1040, 86))]
    parts = ["<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n<hierarchy rotation=\"0\">"]
    for i, n in enumerate(bar + nodes):
        pkg = n["pkg"] or package
        rid = f"{pkg}:id/{n['id']}" if n["id"] else ""
        left, top, right, bottom = n["bounds"]
        parts.append(
            f'<node index="{i}" text={quoteattr(n["text"])} resource-id={quoteattr(rid)} class={quoteattr(n["cls"])} '
            f'package={quoteattr(pkg)} content-desc={quoteattr(n["desc"])} checkable="{_tf(n["checkable"])}" '
            f'checked="{_tf(n["checked"])}" clickable="{_tf(n["clickable"])}" editable="{_tf(n["editable"])}" '
            f'bounds="[{left},{top}][{right},{bottom}]" visible-to-user="true" />')
    parts.append("</hierarchy>\n")
    return {"package": package, "width": W, "height": H, "xml": "".join(parts)}


def _row(title: str, top: int, summary: str = "") -> list[dict]:
    """A settings row: a clickable container without a label, the title and a summary under it."""
    out = [node(cls="android.widget.LinearLayout", bounds=(0, top, W, top + 180), clickable=True),
           node(text=title, id="title", bounds=(179, top + 36, min(W - 60, 179 + 44 * len(title)), top + 102))]
    if summary:
        out.append(node(text=summary, id="summary", bounds=(179, top + 104, min(W - 60, 179 + 30 * len(summary)), top + 150)))
    return out


def _top(title: str) -> list[dict]:
    """The top of a settings sub-screen: the back arrow every such screen has, and the title bar."""
    return [node(desc="위로 탐색", cls="android.widget.ImageButton", bounds=(0, 130, 150, 280), clickable=True),
            node(desc=title, id="collapsing_toolbar", cls="android.widget.FrameLayout", bounds=(0, 280, W, 560))]


# ---------------------------------------------------------------- 화면들

def home(clock: str = "12:06") -> dict:
    nodes = [node(id="workspace", cls="android.view.ViewGroup", bounds=(0, 130, W, 2200))]
    for i, name in enumerate(["Play 스토어", "Gmail", "포토", "YouTube"]):
        nodes.append(node(text=name, clickable=True, bounds=(66 + 256 * i, 1460, 276 + 256 * i, 1700)))
    for i, name in enumerate(["휴대전화", "메시지", "Chrome", "설정"]):
        nodes.append(node(text=name, clickable=True, bounds=(68 + 256 * i, 1952, 214 + 256 * i, 2118)))
    return screen(LAUNCHER, nodes, clock)


def settings_home(clock: str = "12:06", scrolled: int = 0, network: str = "네트워크 및 인터넷") -> dict:
    """The first screen of the settings app. `scrolled` moves the list up; `network` renames its first row."""
    nodes = [node(id="settings_homepage_container", cls="android.widget.FrameLayout", bounds=(0, 130, W, H - 130)),
             node(text="설정 검색", id="search_action_bar_title", bounds=(150, 190, 520, 260), clickable=True)]
    rows = [(network, "모바일, Wi-Fi, 핫스팟"), ("연결된 기기", "블루투스, 페어링"), ("앱", "최근 앱, 기본 앱"),
            ("알림", "알림 기록, 대화"), ("배터리", "100%"), ("저장용량", "여유 공간 있음"),
            ("소리 및 진동", "볼륨, 햅틱, 방해 금지 모드"), ("디스플레이 및 터치", "어두운 테마, 글꼴 크기, 터치")]
    for i, (title, summary) in enumerate(rows):
        top = 430 + 180 * i - scrolled
        if top >= 300:  # rows scrolled under the search bar are gone
            nodes += _row(title, top, summary)
    return screen(SETTINGS, nodes, clock)


def network(clock: str = "12:06") -> dict:
    nodes = _top("네트워크 및 인터넷")
    rows = [("인터넷", "AndroidWifi"), ("통화 및 SMS", "T-Mobile"), ("핫스팟 및 테더링", "사용 안함"),
            ("데이터 절약 모드", "사용 안함"), ("VPN", "없음"), ("비공개 DNS", "자동")]
    for i, (title, summary) in enumerate(rows):
        nodes += _row(title, 600 + 180 * i, summary)
    return screen(SETTINGS, nodes, clock)


def data_saver(on: bool, clock: str = "12:06") -> dict:
    nodes = _top("데이터 절약 모드") + [
        node(id="main_switch_bar", cls="android.widget.LinearLayout", bounds=(40, 600, 1040, 820), clickable=True),
        node(text="데이터 절약 모드 사용", id="switch_text", bounds=(90, 670, 640, 750)),
        node(id="switch_widget", cls="android.widget.Switch", bounds=(860, 660, 1000, 760), checkable=True, checked=on),
    ] + _row("무제한 데이터", 900, "앱 0개가 데이터 절약 모드에서 무제한 데이터 사용이 허용됨")
    return screen(SETTINGS, nodes, clock)


def connected(clock: str = "12:06") -> dict:
    """A screen someone lands on by a wrong turn."""
    nodes = _top("연결된 기기") + _row("새 기기와 페어링", 600) + _row("연결 환경설정", 780, "블루투스, Android Auto, NFC")
    return screen(SETTINGS, nodes, clock)


def display(clock: str = "12:06") -> dict:
    nodes = _top("디스플레이 및 터치")
    nodes += _row("밝기 수준", 600, "40%") + _row("잠금 화면", 780, "모든 알림 내용 표시")
    nodes += _row("화면 자동 잠금 시간", 960, "30초 이상 동작이 없을 때")
    nodes += _row("어두운 테마", 1140, "절대 자동으로 사용 설정 안함")
    nodes.append(node(desc="어두운 테마", id="switch_widget", cls="android.widget.Switch", bounds=(900, 1180, 1040, 1280),
                      checkable=True, checked=True))
    nodes += _row("디스플레이 크기 및 텍스트", 1793, "더 선명한 글씨")
    return screen(SETTINGS, nodes, clock)


def text_size(clock: str = "12:06", bigger: int = 2) -> dict:
    """The screen with two sliders, each with its own "확대" button. `bigger` is how many such buttons it shows."""
    nodes = _top("디스플레이 크기 및 텍스트")
    for i, title in enumerate(["글꼴 크기", "디스플레이 크기", "확대/축소"][:bigger]):
        top = 1380 + 320 * i
        nodes += [node(text=title, bounds=(60, top, 420, top + 60)),
                  node(desc="축소", id="icon_start", cls="android.widget.ImageView", bounds=(36, top + 110, 143, top + 217), clickable=True),
                  node(id="seekbar", cls="android.widget.SeekBar", bounds=(160, top + 120, 920, top + 210)),
                  node(desc="확대", id="icon_end", cls="android.widget.ImageView", bounds=(937, top + 110, 1044, top + 217), clickable=True)]
    return screen(SETTINGS, nodes, clock)


# ---------------------------------------------------------------- 모델이 돌려주는 답의 모양

def click(scr: dict, label: str, nth: int = 0, explain: str = "") -> dict:
    """What the artemis layer returns for "click the element labelled `label`" on this screen."""
    n = find_nodes({"label": label}, Screen.parse(scr))[nth]
    return {"memo": "", "explain": explain or f"‘{label}’ 누르세요",
            "action": {"name": "click", "target": 1, "target_point_px": list(n.center), "target_bounds_px": list(n.bounds),
                       "target_text": label}}


def done(text: str = "다 됐어요.") -> dict:
    return {"memo": "", "explain": text, "action": {"name": "report_task_status", "status": "completed", "explanation": text}}


# ---------------------------------------------------------------- 세션

FACTS = {"font_scale": 1.0, "brightness": 102, "media_volume": 5, "ring_volume": 5, "data_saver": False, "dark_mode": True,
         "airplane_mode": False}

MANUALS = {
    "sample-saver": {"id": "sample-saver", "title": "데이터 절약 모드 켜기", "request": "데이터 절약 모드를 켜고 싶어요",
                     "steps": ["설정 앱을 열고 연결 항목으로 들어갑니다.", "데이터 사용량 또는 데이터 사용 메뉴를 누릅니다.",
                               "데이터 절약 모드를 찾아 누릅니다.", "데이터 절약 모드 스위치를 오른쪽으로 밀어 켭니다."],
                     "check": {"fact": "data_saver", "change": "on"}},
    "sample-font": {"id": "sample-font", "title": "글자 크기 키우기", "request": "글자가 작아서 안 보여",
                    "steps": ["설정 앱을 열고 화면 설정으로 들어갑니다.", "글자 크기 메뉴를 찾아 누릅니다.",
                              "막대를 오른쪽으로 밀어서 원하는 크기로 맞춥니다.", "화면에서 글자가 커졌는지 확인합니다."],
                    "check": {"fact": "font_scale", "change": "increased"}},
}

# A session is a list of (screen, the phone's settings that differ from FACTS or None when it reports none,
# the reply that was guided on that screen or None when the phone's setting ended the session).


def saver(clock: str = "12:06", scrolled: int = 0, network_label: str = "네트워크 및 인터넷", facts: bool = True) -> list[tuple]:
    a, b, c, d = home(clock), settings_home(clock, scrolled, network_label), network(clock), data_saver(False, clock)
    steps = [(a, {}, click(a, "설정")), (b, {}, click(b, network_label)), (c, {}, click(c, "데이터 절약 모드")),
             (d, {}, click(d, "데이터 절약 모드 사용"))]
    if facts:
        return steps + [(data_saver(True, clock), {"data_saver": True}, None)]
    # A phone that reports no settings: the model has to say it is done.
    return [(s, None, r) for s, _, r in steps] + [(data_saver(True, clock), None, done("데이터 절약 모드가 켜졌어요."))]


def saver_detour() -> list[tuple]:
    """A wrong turn into "연결된 기기" and back, then the right way."""
    a, b, wrong = home(), settings_home(), connected()
    rest = saver()[1:]
    return [(a, {}, click(a, "설정")), (b, {}, click(b, "연결된 기기")), (wrong, {}, click(wrong, "위로 탐색"))] + rest


def font() -> list[tuple]:
    a, b, c, d = home(), settings_home(), display(), text_size()
    return [(a, {}, click(a, "설정")), (b, {}, click(b, "디스플레이 및 터치")), (c, {}, click(c, "디스플레이 크기 및 텍스트")),
            (d, {}, click(d, "확대", 0)), (text_size(), {"font_scale": 1.15}, None)]


SAMPLES = {
    "sample-saver-first": ("sample-saver", saver()),
    "sample-saver-later": ("sample-saver", saver(clock="09:41")),               # another time of day
    "sample-saver-scrolled": ("sample-saver", saver(scrolled=120)),             # the settings list was scrolled
    "sample-saver-renamed": ("sample-saver", saver(network_label="연결")),       # one menu has another name
    "sample-saver-detour": ("sample-saver", saver_detour()),                    # a wrong turn and back
    "sample-saver-no-facts": ("sample-saver", saver(facts=False)),              # the model says it is done
    "sample-font-first": ("sample-font", font()),
}


def exchanges(manual_id: str, steps: list[tuple], sid: str = "sample", device: str = DEVICE) -> list[dict]:
    """The session as the guide server records it: the manual, then one line per /guide/next request and response."""
    lines: list[dict] = [{"manual": MANUALS[manual_id]}]
    for i, (scr, facts, reply) in enumerate(steps, 1):
        req = {"session_id": sid, "manual_id": manual_id, "device_model": device, "step": i, "elapsed_s": (i - 1) * 8,
               "event": "start" if i == 1 else "met", "event_detail": "" if i == 1 else "the human tapped the highlighted element",
               "screen": scr}
        if facts is not None:
            req["facts"] = {**FACTS, **facts}
        if reply is None:
            reply = {**done("기기 설정값으로 완료를 확인했어요."), "source": "check"}
        else:
            reply = {**reply, "source": "model"}
        lines.append({"request": req, "status": 200, "response": reply})
    return lines


def write_samples(folder: Path = SESSIONS) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for name, (manual_id, steps) in SAMPLES.items():
        p = folder / f"{name}.jsonl"
        p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in exchanges(manual_id, steps, name)),
                     encoding="utf-8", newline="\n")
        out.append(p)
    return out


if __name__ == "__main__":
    for path in write_samples():
        print(path)
