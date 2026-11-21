"""화면(접근성 트리 XML)을 읽고, 저장된 단계의 화면 조건을 판정한다.

조건의 형식:
    {"foreground": "com.android.settings"}        앞에 있는 앱
    {"node": {"text": "디스플레이"}}                그런 요소가 화면에 있다 (text, desc, label, id, typed, editable, checked)
    {"all": [...]}, {"any": [...]}, {"not": {...}}
글자 값은 목록일 수 있다(["Install", "설치"]): 그중 하나와 같으면 맞는다.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True)
class Node:
    text: str
    desc: str
    id: str  # the part after ":id/"
    cls: str
    pkg: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    editable: bool
    checkable: bool = False
    checked: bool = False

    @property
    def label(self) -> str:
        return self.text or self.desc

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bounds
        return max(0, right - left) * max(0, bottom - top)

    def contains(self, x: int, y: int) -> bool:
        left, top, right, bottom = self.bounds
        return left <= x < right and top <= y < bottom


class Screen:
    """One observation: the foreground package and the nodes that are on screen."""

    def __init__(self, package: str, width: int, height: int, nodes: list[Node]):
        self.package = package
        self.width = width
        self.height = height
        self.nodes = nodes
        # Conditions and targets look at the foreground app only: the status bar's clock and battery change every minute.
        self.app_nodes = [n for n in nodes if n.pkg == package]

    @classmethod
    def parse(cls, screen: dict) -> "Screen":
        width, height = int(screen.get("width", 1080)), int(screen.get("height", 2400))
        nodes: list[Node] = []
        xml = screen.get("xml") or ""
        if xml.strip():
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                root = None
            for el in (root.iter("node") if root is not None else ()):
                a = el.attrib
                if a.get("visible-to-user", "true") == "false":
                    continue
                m = _BOUNDS.fullmatch(a.get("bounds", ""))
                if not m:
                    continue
                left, top, right, bottom = (int(v) for v in m.groups())
                left, top, right, bottom = max(left, 0), max(top, 0), min(right, width), min(bottom, height)
                if right <= left or bottom <= top:
                    continue
                rid = a.get("resource-id", "")
                nodes.append(Node(
                    text=a.get("text", ""), desc=a.get("content-desc", ""), id=rid.split(":id/", 1)[-1] if rid else "",
                    cls=a.get("class", ""), pkg=a.get("package", ""), bounds=(left, top, right, bottom),
                    clickable=a.get("clickable") == "true",
                    editable=a.get("editable") == "true" or a.get("class", "").endswith("EditText"),
                    checkable=a.get("checkable") == "true", checked=a.get("checked") == "true",
                ))
        return cls(str(screen.get("package", "")), width, height, nodes)


def norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def squash(s: str) -> str:
    return "".join(str(s).split()).casefold()


def _values(v) -> list:
    return v if isinstance(v, list) else [v]


def matches(n: Node, m: dict) -> bool:
    """Whether node n has every property of the locator m. An unknown key never matches."""
    if not m:
        return False
    for k, v in m.items():
        if k == "text":
            ok = n.text != "" and norm(n.text) in [norm(x) for x in _values(v)]
        elif k == "desc":
            ok = n.desc != "" and norm(n.desc) in [norm(x) for x in _values(v)]
        elif k == "label":
            want = [norm(x) for x in _values(v)]
            ok = (n.text != "" and norm(n.text) in want) or (n.desc != "" and norm(n.desc) in want)
        elif k == "id":
            ok = n.id != "" and n.id in [str(x).split(":id/", 1)[-1] for x in _values(v)]
        elif k == "typed":
            ok = squash(n.text) in [squash(x) for x in _values(v)]
        elif k == "editable":
            ok = n.editable == bool(v)
        elif k == "checked":
            ok = n.checkable and n.checked == bool(v)
        elif k in ("nth", "of"):
            ok = True  # which of several look-alikes: an option for locating a target, not a property
        else:
            ok = False
        if not ok:
            return False
    return True


def find_nodes(locator: dict, screen: Screen) -> list[Node]:
    """The app nodes the locator matches, from the top of the screen down."""
    return sorted((n for n in screen.app_nodes if matches(n, locator)), key=lambda n: (n.bounds[1], n.bounds[0]))


def locate(locator: dict, screen: Screen) -> Node | None:
    """The one node a target locator points at, or None when the screen does not show it the way it was saved.

    Without `nth` exactly one node must match. With `nth` (the target is one of `of` look-alikes, counted from the top)
    there must be `of` matches again.
    """
    nodes = find_nodes(locator, screen)
    if "nth" in locator:
        nth = int(locator["nth"])
        return nodes[nth] if len(nodes) == int(locator.get("of", len(nodes))) and 0 <= nth < len(nodes) else None
    return nodes[0] if len(nodes) == 1 else None


def evaluate(cond: dict, screen: Screen) -> bool:
    """Whether the screen condition holds. An empty or unknown condition is false."""
    if not isinstance(cond, dict) or not cond:
        return False
    for key, val in cond.items():
        if key == "all":
            ok = bool(val) and all(evaluate(c, screen) for c in val)
        elif key == "any":
            ok = any(evaluate(c, screen) for c in val)
        elif key == "not":
            ok = not evaluate(val, screen)
        elif key == "foreground":
            ok = screen.package == val
        elif key == "node":
            ok = any(matches(n, val) for n in screen.app_nodes)
        else:
            ok = False
        if not ok:
            return False
    return True
