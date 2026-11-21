"""규칙 재생: 녹화한 세션으로 rules/를 시험한다. 서버도 모델도 폰도 필요 없다. 파이썬만 있으면 돈다.

    python manual/replay.py                      # manual/tests/sessions/의 모든 세션
    python manual/replay.py a.jsonl b.jsonl      # 고른 세션만
    python manual/replay.py --show               # 배운 단계의 조건을 보여 준다
    python manual/replay.py --detail             # 못 찾았거나 틀린 화면을 하나씩 보여 준다
    python manual/replay.py --json out.json      # 결과를 파일로

하는 일: 세션 하나에서 배우고(review → learn), 같은 매뉴얼의 세션마다 화면을 차례로 넣어 본다(find).
화면 하나의 결과는 셋 중 하나다.
    맞음      녹화에서 사람이 해낸 동작과 같은 동작을 찾았다 (모델을 부르지 않아도 된다)
    못 찾음   찾은 것이 없다 (artemis 서버에 묻게 된다. 느릴 뿐 틀리지는 않다)
    틀림      다른 동작을 찾았다 (틀린 안내가 나간다). 하나라도 있으면 종료 코드 1

"같은 세션"은 배운 세션에 그대로 넣어 본 것이고(등록이 되는지), "다른 세션"은 다른 날, 다른 시작 상태의 세션에 넣어 본 것이다
(다시 왔을 때 알아보는지). 규칙을 고치는 목표는 틀림을 0으로 둔 채 다른 세션의 맞음을 늘리는 것이다.

녹화는 저작 서버가 한다. 저작을 한 번 돌리면 runs/<매뉴얼>-<작업>/sessions/에 세션마다 파일이 하나 생긴다.
앞단 서버는 RECORD_DIR을 주고 띄우면 남긴다. 파일의 첫 줄은 매뉴얼이고, 그 뒤는 안내 요청과 응답이 한 줄씩이다(스크린샷은 뺀다).
sample-로 시작하는 파일은 손으로 쓴 화면이다(manual/tests/fake.py).

서버와 다른 점: 서버는 find의 결과에 보호 규칙 두 가지(방금 한 단계가 또 나오면 artemis에 묻기, 한 세션에서 같은 단계는 두 번까지)를
더 걸고, 읽을 수 없는 화면에서는 잠깐 기다린다.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rules as cache  # noqa: E402
from screen import Screen  # noqa: E402

SESSIONS = HERE / "tests" / "sessions"
GUIDED = ("model", "saved", "manual")


# ---------------------------------------------------------------- 녹화 읽기

def load(path: Path) -> dict:
    """{"name", "manual", "manual_id", "device", "exchanges": [{"request", "response", "screen": Screen}]}"""
    manual, exchanges = None, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "manual" in rec:
            manual = rec["manual"]
        elif "request" in rec and rec.get("status", 200) == 200:
            rec["screen"] = Screen.parse(rec["request"].get("screen") or {})
            exchanges.append(rec)
    first = exchanges[0]["request"] if exchanges else {}
    return {"name": Path(path).stem, "manual": manual, "manual_id": str(first.get("manual_id") or (manual or {}).get("id") or "?"),
            "device": str(first.get("device_model") or "unknown"), "exchanges": exchanges}


def _completed(action: dict) -> bool:
    return action.get("name") == "report_task_status" and action.get("status") == "completed"


def finished(session: dict) -> bool:
    ex = session["exchanges"]
    return bool(ex) and _completed(ex[-1]["response"].get("action") or {})


def confirmed(session: dict, i: int) -> bool:
    """Whether the person did what exchange i guided: the next request says so, or it is the model's own "done"."""
    ex = session["exchanges"]
    resp = ex[i]["response"]
    if resp.get("source") not in GUIDED:
        return False  # ended by the phone's setting: nothing was guided on this screen
    if i + 1 < len(ex):
        return ex[i + 1]["request"].get("event") == "met"
    return _completed(resp.get("action") or {})


# ---------------------------------------------------------------- 배우기

def trace(session: dict) -> list[dict]:
    """What the person did in a finished session, in the shape the guide server hands to cache.review."""
    if not finished(session):
        return []  # the server registers nothing from a session that never reached the end
    return [{"source": "model", "screen": e["screen"], "reply": e["response"], "step": None}
            for i, e in enumerate(session["exchanges"]) if confirmed(session, i)]


def teach(session: dict, suggest=None) -> dict:
    """{"steps": the steps this session would register, "done": steps the person did, "kept": steps the review kept}"""
    q = {"manual_id": session["manual_id"], "device_model": session["device"]}
    done = trace(session)
    kept = cache.review(q, done)
    steps, earlier = [], []
    for e in kept:
        step = cache.learn({**q, "screen": e["screen"], "earlier": list(earlier), "suggest": suggest}, e["reply"])
        if step is not None:
            steps.append({**step, "id": len(steps) + 1, "seq": len(steps) + 1, "device_model": session["device"]})
        earlier.append(e["screen"])
    return {"steps": steps, "done": len(done), "kept": len(kept)}


# ---------------------------------------------------------------- 넣어 보기

def same(got: dict, want: dict) -> bool:
    """Whether the action found from the saved steps is the action the person was guided to and did."""
    if got.get("name") != want.get("name"):
        return False
    name = got.get("name")
    if name in cache.TARGET_ACTIONS:
        if name == "input_text" and got.get("text") != want.get("text"):
            return False
        for point, box in ((got.get("target_point_px"), want.get("target_bounds_px")), (want.get("target_point_px"), got.get("target_bounds_px"))):
            if point and box and box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]:
                return True
        return False
    if name == "report_task_status":
        return got.get("status") == want.get("status")
    return all(got.get(k) == want.get(k) for k in ("key", "direction", "app_name"))


def label(action: dict) -> str:
    what = action.get("target_text") or action.get("key") or action.get("direction") or action.get("app_name") or action.get("status") or ""
    return f"{action.get('name', '?')} {what}".strip()


def mark(screen: Screen) -> tuple:
    """Two observations with the same mark are the same screen in the same state."""
    return screen.package, frozenset((n.text, n.desc, n.id, n.checked if n.checkable else None) for n in screen.app_nodes)


def test(steps: list[dict], session: dict) -> list[dict]:
    """One row per screen of the session where the person did what was guided: what the saved steps find there.

    The reference is what the person did the last time they were on that screen. On the first visit of a wrong turn
    they were guided the wrong way and did it; the right answer for that screen is what got them to the end later.
    """
    last: dict[tuple, dict] = {}
    for i, e in enumerate(session["exchanges"]):
        if confirmed(session, i):
            last[mark(e["screen"])] = e["response"].get("action") or {}
    rows = []
    for i, e in enumerate(session["exchanges"]):
        if not confirmed(session, i):
            continue
        want = last[mark(e["screen"])]
        hit = cache.find({"manual_id": session["manual_id"], "device_model": session["device"], "screen": e["screen"]}, steps)
        verdict = "못 찾음" if hit is None else ("맞음" if same(hit["action"], want) else "틀림")
        rows.append({"step": e["request"].get("step"), "want": label(want), "got": label(hit["action"]) if hit else "",
                     "saved_seq": hit["step"]["seq"] if hit else None, "verdict": verdict})
    return rows


# ---------------------------------------------------------------- 표

def _pad(s: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - shown)


def run(paths: list[Path], show: bool = False, detail: bool = False) -> dict:
    sessions = [load(p) for p in paths]
    by_manual: dict[str, list[dict]] = {}
    for s in sessions:
        by_manual.setdefault(s["manual_id"], []).append(s)
    pairs = []
    for manual_id, group in by_manual.items():
        title = next((s["manual"].get("title") or s["manual"].get("task", "") for s in group if s["manual"]), "")
        print(f"\n매뉴얼 {manual_id}  {title}")
        print("  " + _pad("배운 세션", 26) + _pad("등록/해낸", 11) + _pad("넣어 본 세션", 26) + "화면  맞음  못 찾음  틀림")
        for a in group:
            if not finished(a):
                print("  " + _pad(a["name"], 26) + "끝까지 가지 못한 세션이라 배우지 않습니다")
                continue
            taught = teach(a)
            if show:
                print(f"  {a['name']}에서 배운 단계")
                for st in taught["steps"]:
                    target = next(iter((st["done_condition"].get("target") or {}).values()), "")
                    print(f"      #{st['seq']} {label({**st['done_condition'], 'target_text': target})}"
                          f"  조건 {json.dumps(st['screen_condition'], ensure_ascii=False)}")
            for b in group:
                rows = test(taught["steps"], b)
                n = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("맞음", "못 찾음", "틀림")}
                pairs.append({"manual": manual_id, "taught_by": a["name"], "tested_on": b["name"], "registered": len(taught["steps"]),
                              "done": taught["done"], "kept": taught["kept"], "screens": len(rows), "right": n["맞음"],
                              "missed": n["못 찾음"], "wrong": n["틀림"], "rows": rows})
                print("  " + _pad(a["name"], 26) + _pad(f"{len(taught['steps'])}/{taught['done']}", 11) + _pad(b["name"], 26)
                      + f"{len(rows):4d}  {n['맞음']:4d}  {n['못 찾음']:7d}  {n['틀림']:4d}")
                if detail:
                    for r in rows:
                        if r["verdict"] != "맞음":
                            got = f"찾은 것: {r['got']} (#{r['saved_seq']})" if r["got"] else "찾은 것 없음"
                            print(f"      {r['verdict']}  {r['step']}번째 화면, 해낸 동작: {r['want']}, {got}")

    def total(rows: list[dict]) -> dict:
        t = {k: sum(r[k] for r in rows) for k in ("screens", "right", "missed", "wrong")}
        t["right_rate"] = round(t["right"] / t["screens"], 3) if t["screens"] else None
        return t

    summary = {"same_session": total([p for p in pairs if p["taught_by"] == p["tested_on"]]),
               "other_session": total([p for p in pairs if p["taught_by"] != p["tested_on"]])}
    print("\n요약")
    for name, key in (("같은 세션", "same_session"), ("다른 세션", "other_session")):
        t = summary[key]
        rate = "-" if t["right_rate"] is None else f"{t['right_rate'] * 100:.0f}%"
        print("  " + _pad(name, 12) + f"화면 {t['screens']:4d}   맞음 {t['right']:4d} ({rate})   못 찾음 {t['missed']:4d}   틀림 {t['wrong']:4d}")
    return {"summary": summary, "pairs": pairs}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="녹화한 세션으로 rules/를 시험한다")
    ap.add_argument("sessions", nargs="*", type=Path, help="세션 파일(.jsonl). 없으면 manual/tests/sessions/ 전부")
    ap.add_argument("--show", action="store_true", help="배운 단계의 조건을 보여 준다")
    ap.add_argument("--detail", action="store_true", help="못 찾았거나 틀린 화면을 하나씩 보여 준다")
    ap.add_argument("--json", type=Path, help="결과를 이 파일에 쓴다")
    args = ap.parse_args()
    paths = args.sessions or sorted(SESSIONS.glob("*.jsonl"))
    if not paths:
        print(f"세션이 없습니다. {SESSIONS}에 녹화를 넣거나 python manual/tests/fake.py로 견본을 만드세요.")
        return 1
    result = run(paths, args.show, args.detail)
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if result["summary"]["same_session"]["wrong"] or result["summary"]["other_session"]["wrong"] else 0


if __name__ == "__main__":
    sys.exit(main())
