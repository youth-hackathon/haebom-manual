"""저장된 단계 찾기와 저장하기. 규칙을 다루는 곳은 이 폴더뿐이다.

    find(q, steps)     지금 화면에 맞는 저장된 단계 하나와, 그 대상을 지금 화면에서 찾은 동작. 없으면 None
    review(q, trace)   완료된 세션에서 사용자가 해낸 단계들 가운데 남길 것을 순서대로 고른다
    learn(q, reply)    모델이 고른 동작(사용자가 해낸 것이 확인됨)을 저장할 단계로 바꾼다. 저장할 만하지 않으면 None

단계는 세션이 완료된 뒤에만 등록한다. 안내 서버가 review로 고르고, 남은 것마다 learn을 불러 등록한다.

q는 dict이다:
    manual_id, device_model
    screen        지금 화면 (screen.py의 Screen)
    earlier       이 세션에서 앞 단계들을 안내한 화면들 (learn에서만 쓴다)
    suggest       learn에서만: 이 화면을 알아볼 요소를 모델에 묻는 함수. 없을 수도 있다

이 함수들이 오류를 내면 서버는 결과가 없는 것으로 보고 artemis 서버에 묻는다.
대상이 지금 화면에 하나만 있는지는 find가 확인한다. 틀린 안내는 느린 안내보다 나쁘다.
"""

from __future__ import annotations

from screen import Node, Screen, evaluate, find_nodes, locate  # noqa: F401

TARGET_ACTIONS = ("click", "long_press", "input_text")
# 대상이 없는 동작은 화면 조건만으로 알아봐야 한다
PLAIN_ACTIONS = ("press_key", "swipe", "manage_app")
_TARGET_KEYS = ("target", "target_point_px", "target_text", "target_bounds_px", "target_description", "target_error")
MAX_ANCHORS = 2
# 켜고 끄는 요소가 이보다 많은 화면에서는 대상과 같은 줄에 있는 것의 상태만 조건에 넣는다
MAX_SWITCHES = 2
MAX_LOOKALIKES = 4


# ---------------------------------------------------------------- find

def find(q: dict, steps: list[dict]) -> dict | None:
    """The saved step to guide on this screen: among the steps whose condition holds, the last one in order.

    Taking the last one is what makes a finished step get skipped and going back get followed. Steps learned on this
    device model are looked at first; another model's steps are used only when none of this model's hold.
    """
    screen: Screen = q["screen"]
    own = [s for s in steps if s.get("device_model") == q.get("device_model")]
    # The entry step ("outside this manual's apps: go home") names a device's own launcher and apps: never another device's.
    other = [s for s in steps if s.get("device_model") != q.get("device_model") and not s.get("entry")]
    for group in (own, other):
        holding = [s for s in group if kernel_holds(s, screen)]
        if not holding:
            continue
        step = max(holding, key=lambda s: s["seq"])
        action = resolve(step["done_condition"], screen)
        # The last holding step decides. If its target is not uniquely on screen, the model is asked rather than
        # falling back to an earlier step.
        return {"step": step, "action": action} if action is not None else None
    return None


def kernel_holds(step: dict, screen: Screen) -> bool:
    """A step's kernel: its own screen condition, and everything earlier steps achieved that it relies on (`requires`).

    This is the triangle table of PLANEX (Fikes, Hart & Nilsson 1972): the kernel of step i holds when the rest of
    the plan, from i on, still leads to the goal. Guiding the highest-numbered step whose kernel holds gives progress,
    skipping what is already done, and going back when something an earlier step set has been undone (a wrong tap).
    """
    return evaluate(step["screen_condition"], screen) and all(evaluate(r, screen) for r in step.get("requires") or [])


def changes(before: Screen, after: Screen) -> list[dict]:
    """What a step left on the screen: named fields that now show another value, and switches that flipped.

    Only state the screen keeps showing counts (the hour field of a time picker reading "8", "오전" being selected).
    Each is a condition that holds on `after` and did not hold on `before`.
    """
    def named(s: Screen) -> dict[str, Node]:
        seen: dict[str, list[Node]] = {}
        for n in s.app_nodes:
            if n.id:
                seen.setdefault(n.id, []).append(n)
        return {k: v[0] for k, v in seen.items() if len(v) == 1}

    out: list[dict] = []
    was, now = named(before), named(after)
    for key, n in now.items():
        old = was.get(key)
        if old is not None and n.text and n.text != old.text and len(n.text) <= 30 and not n.editable and not n.checkable:
            out.append({"node": {"id": key, "text": n.text}})
    for n in after.app_nodes:
        if not n.checkable:
            continue
        locator = locator_for(n, after)
        if locator is None:
            continue
        old = locate(locator, before)
        if old is not None and old.checkable and old.checked != n.checked:
            out.append({"node": {**locator, "checked": n.checked}})
    return [c for c in out if evaluate(c, after) and not evaluate(c, before)]


def chain(path: list[tuple[dict, Screen]]) -> dict:
    """The requirements each step of a followed path carries: {step id: [conditions]}.

    `path` is the steps of one run that reached the end, in order, each with the screen it was guided on. What step i
    changed is read from the next step's screen. Step j requires every earlier change that its own screen still showed
    in that run: a later change of the same field has replaced the earlier one there, so only what stood is required.
    """
    effects = [changes(path[i][1], path[i + 1][1]) for i in range(len(path) - 1)]
    out: dict = {}
    for j, (step, screen) in enumerate(path):
        requires: list[dict] = []
        for i in range(j):
            for c in effects[i]:
                if c not in requires and evaluate(c, screen):
                    requires.append(c)
        out[step["id"]] = requires
    return out


def resolve(done: dict, screen: Screen) -> dict | None:
    """The saved action with its target located on the current screen, or None when the target is not there exactly once."""
    action = {k: v for k, v in done.items() if k != "target"}
    locator = done.get("target")
    if locator is None:
        return action
    n = locate(locator, screen)
    if n is None:
        return None
    action["target_point_px"] = list(n.center)
    action["target_bounds_px"] = list(n.bounds)
    action["target_text"] = n.label
    return action


# ---------------------------------------------------------------- review

def review(q: dict, trace: list[dict]) -> list[dict]:
    """The steps of a finished session worth keeping, in order.

    `trace` holds what the person did, one entry per step: {"source": "saved" | "model", "screen", "reply" | "step"}.
    A stretch that leaves a screen and later comes back to the same screen is a wrong turn and the way back: it is
    cut, and what was done on that screen the last time is kept.
    """
    kept: list[dict] = []
    marks: list[tuple] = []
    for e in trace:
        mark = _screen_mark(e["screen"])
        if mark in marks:
            cut = marks.index(mark)
            del kept[cut:], marks[cut:]
        kept.append(e)
        marks.append(mark)
    return kept


def _screen_mark(screen: Screen) -> tuple:
    """Two observations with the same mark are the same screen in the same state."""
    return screen.package, frozenset((n.text, n.desc, n.id, n.checked if n.checkable else None) for n in screen.app_nodes if not n.editable)


# ---------------------------------------------------------------- learn

def learn(q: dict, reply: dict) -> dict | None:
    """A step to save for the action the model chose on q["screen"]: {"instruction", "screen_condition", "done_condition"}.

    The condition must hold on this screen and on none of the screens earlier steps were guided on. With "the last
    holding step wins" that keeps a later step from being picked too early.
    """
    screen: Screen = q["screen"]
    earlier: list[Screen] = q.get("earlier") or []
    action = reply.get("action") or {}
    name = action.get("name")
    done = {k: v for k, v in action.items() if k not in _TARGET_KEYS}
    parts: list[dict] = [{"foreground": screen.package}]

    if name in TARGET_ACTIONS:
        node = target_node(action, screen)
        locator = locator_for(node, screen) if node is not None else None
        if locator is None:
            return None  # nothing on this screen names the target uniquely
        done["target"] = locator
        parts.append({"node": locator})
    elif name == "report_task_status":
        if action.get("status") != "completed":
            return None
    elif name not in PLAIN_ACTIONS:
        return None  # waits and questions are not steps of the manual
    # A switch's state is part of what the screen says: "turn it on" is a step only while it is off, and the task is
    # done only once it is on.
    states = [{"node": s} for s in _switch_states(screen, node if name in TARGET_ACTIONS else None)]

    # Every step also carries nodes that say which screen this is. The target alone does not: a back arrow or a
    # search button is on many screens, and a step saved for one of them would be offered on all of them. A switch's
    # state alone does not either.
    condition = None
    for anchors in _anchor_choices(q, screen, earlier, reply):
        candidate = {"all": parts + [{"node": a} for a in anchors if {"node": a} not in parts] + states}
        if _valid(candidate, screen, earlier):
            condition = candidate
            break
    if condition is None:
        return None
    instruction = (reply.get("explain") or "").strip() or default_instruction(name, done)
    return {"instruction": instruction, "screen_condition": condition, "done_condition": done}


def _switch_states(screen: Screen, target: Node | None) -> list[dict]:
    """Locators with `checked` for the screen's switches and checkboxes: all of them when there are few, else the
    ones on the target's row."""
    switches = [n for n in screen.app_nodes if n.checkable]
    if len(switches) > MAX_SWITCHES:
        if target is None:
            return []
        top, bottom = target.bounds[1], target.bounds[3]
        switches = [n for n in switches if top <= n.center[1] < bottom]
    out = []
    for n in switches:
        locator = locator_for(n, screen)
        if locator is not None:
            out.append({**locator, "checked": n.checked})
    return out


def _valid(condition: dict, screen: Screen, earlier: list[Screen]) -> bool:
    return evaluate(condition, screen) and not any(evaluate(condition, e) for e in earlier)


def target_node(action: dict, screen: Screen) -> Node | None:
    """The app node the model's target lands on: the smallest labelled node under the point."""
    point = action.get("target_point_px")
    if not point:
        return None
    x, y = int(point[0]), int(point[1])
    under = sorted((n for n in screen.app_nodes if n.contains(x, y)), key=lambda n: n.area)
    for n in under:
        if n.label:
            return n
    for n in under:
        if n.id:
            return n
    return None


def locator_for(node: Node, screen: Screen) -> dict | None:
    """A locator that finds exactly this node on its screen: its text, else its description, else its id."""
    for key, value in (("text", node.text), ("desc", node.desc), ("id", node.id)):
        if value and len(find_nodes({key: value}, screen)) == 1:
            return {key: value}
    # One of a few look-alikes (two sliders, each with its own "bigger" button): say which, counted from the top.
    for key, value in (("text", node.text), ("desc", node.desc)):
        same = find_nodes({key: value}, screen) if value else []
        if 1 < len(same) <= MAX_LOOKALIKES and node in same:
            return {key: value, "nth": same.index(node), "of": len(same)}
    return None


def _anchor_choices(q: dict, screen: Screen, earlier: list[Screen], reply: dict):
    """Sets of nodes that could say which screen this is, best first: the model's suggestion, then what none of the
    earlier screens show, then the screen's heading."""
    suggest = q.get("suggest")
    if suggest is not None:
        try:
            suggested = [a for a in suggest(screen, earlier, reply) if _is_locator(a) and len(find_nodes(a, screen)) == 1]
        except Exception as exc:  # noqa: BLE001 - a failed suggestion only means the plain rules are used
            print(f"[cache] suggestion failed: {exc}", flush=True)
            suggested = []
        if suggested:
            yield suggested[:MAX_ANCHORS]
    own = _candidates(screen)
    fresh = [a for a in own if not any(find_nodes(a, e) for e in earlier)]
    if fresh:
        yield fresh[:MAX_ANCHORS]
    heading = [a for a in own if a not in fresh][:1]
    if heading:
        yield heading + fresh[:1]


def _is_locator(a) -> bool:
    return isinstance(a, dict) and len(a) == 1 and next(iter(a)) in ("text", "desc", "id") and bool(next(iter(a.values())))


def _candidates(screen: Screen) -> list[dict]:
    """Locators for nodes that are likely to be on this screen every time, the most telling first: short fixed texts
    from the top of the screen down (a heading comes first), then ids. Buttons are left out: the same back arrow and
    menu button sit on many screens."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(key: str, value: str) -> None:
        if (key, value) not in seen and len(find_nodes({key: value}, screen)) == 1:
            seen.add((key, value))
            out.append({key: value})

    for n in sorted(screen.app_nodes, key=lambda n: (n.bounds[1], n.bounds[0])):
        for key, value in (("text", n.text), ("desc", n.desc)):
            t = value.strip()
            if t and not n.editable and not n.checkable and not n.clickable and len(t) <= 30 and not any(ch.isdigit() for ch in t):
                add(key, value)
    for n in screen.app_nodes:
        if n.id and not n.editable and not n.clickable:
            add("id", n.id)
    # A field with an id that shows a value (the minutes of a time picker reading "00"): what it shows is the state of
    # the screen. Text with digits is left out above because clocks and counters change by themselves; a named field
    # is tied to its id here, and it comes last, so it is used only when nothing else tells the screen apart.
    for n in sorted(screen.app_nodes, key=lambda n: (n.bounds[1], n.bounds[0])):
        t = n.text.strip()
        if n.id and t and not n.editable and len(t) <= 8 and any(ch.isdigit() for ch in t):
            both = {"id": n.id, "text": n.text}
            if both not in out and len(find_nodes(both, screen)) == 1:
                out.append(both)
    return out


def default_instruction(name: str, done: dict) -> str:
    if name == "report_task_status":
        return "다 했어요"
    if name == "press_key":
        return {"HOME": "홈 화면으로 가세요", "BACK": "뒤로 가세요"}.get(str(done.get("key", "")).upper(), "버튼을 누르세요")
    if name == "swipe":
        return "화면을 밀어 주세요"
    if name == "manage_app":
        return f"‘{done.get('app_name', '앱')}’을 여세요"
    label = next(iter((done.get("target") or {}).values()), "")
    return f"‘{label}’ 누르세요" if label else "여기를 누르세요"
