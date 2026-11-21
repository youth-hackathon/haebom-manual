"""안내 한 번: 매뉴얼의 규칙이 먼저 답하고, 답하지 못하는 화면은 artemis 서버에 넘긴다.

앱이 보내는 요청과 받는 응답은 artemis 서버와 같다(docs/interface.md). 앱은 주소만 이쪽으로 바꾸면 된다.

    1. 요청 문장으로 매뉴얼을 고른다(같은 문장일 때만). 없으면 요청을 그대로 artemis 서버에 넘긴다.
    1-1. 매뉴얼에 완료 조건(done)이 있으면 폰이 보낸 설정값(facts)으로 먼저 본다. 글자 크기가 세션을 시작할 때보다
         커졌으면, 다음 알람이 08:00이면 바로 "다 됐다"를 답한다. 화면을 보고 판단하지 않고 모델도 부르지 않는다.
    2. 매뉴얼의 단계 가운데 지금 화면에 조건이 맞는 것을 찾는다. 여럿이면 가장 뒤의 것(rules.find).
       그 대상이 화면에 하나만 있으면 바로 답한다. 모델을 부르지 않는다.
    3. 없으면 받은 요청을 artemis 서버에 넘기고 그 답을 그대로 돌려준다.

저작할 때(authoring=True)는 여기에 두 가지가 더 붙는다. 사람이 해낸 단계를 세션에 적어 두었다가 끝까지 간 세션에서만
규칙으로 등록하고(register), 규칙으로 안내했는데 막히면 그 규칙을 지운다. 사용자의 세션에서는 아무것도 등록하지 않는다.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import rules
from screen import Screen
from store import Manuals

MAX_SERVES = 2      # a step guided this often in one session is not guided from the manual again
MAX_WAITS = 5       # an app window that cannot be read yet is looked at again this many times before artemis is asked
WAIT_MS = 600
SETTLE_WAITS = 2    # looks at the screen a manual step led to before artemis is asked about it
# The guide app leaves its own windows out of the tree it sends. With that app in front the tree has no app node,
# and no amount of waiting changes that: such a screen is judged at once (the entry step answers "go home").
GUIDE_APP = "ai.toolus.mobileguide"
MAX_SESSIONS = 300  # kept in memory


def completed(action: dict) -> bool:
    return action.get("name") == "report_task_status" and action.get("status") == "completed"


def done_by_facts(done: dict | None, before: dict, now: dict) -> bool:
    """Whether the phone's own settings say the task is done.

    `done` is {"fact": name, "equals": value} or {"fact": name, "change": "increased" | "decreased" | "on" | "off"}.
    A change is counted from the settings the session started with. A phone that does not report the setting never
    says done this way.
    """
    if not isinstance(done, dict) or done.get("fact") not in now:
        return False
    value, first = now[done["fact"]], before.get(done["fact"])
    if "equals" in done:
        return str(value) == str(done["equals"])
    change = done.get("change")
    number = isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(first, (int, float)) and not isinstance(first, bool)
    if change == "increased":
        return number and value > first
    if change == "decreased":
        return number and value < first
    if change == "on":
        return value is True
    if change == "off":
        return value is False
    return False


def label(action: dict) -> str:
    what = action.get("target_text") or action.get("key") or action.get("direction") or action.get("app_name") or action.get("status") or ""
    typed = f' "{action["text"]}"' if action.get("name") == "input_text" and action.get("text") else ""  # the manual's own text
    return f"{action.get('name', '?')} {what}".strip() + typed


class Guide:
    def __init__(self, manuals: Manuals, artemis_url: str, authoring: bool = False, record_dir: str | Path | None = None,
                 device_model: str = "unknown"):
        self.manuals = manuals
        self.artemis_url = artemis_url.rstrip("/")
        self.authoring = authoring
        self.record_dir = Path(record_dir) if record_dir else None
        self.device_model = device_model  # authoring: the device the manual is being written on
        self.last_sid: str | None = None
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._record_lock = threading.Lock()

    # ---------------------------------------------------------------- one request

    def next(self, req: dict) -> tuple[int, dict]:
        started = time.time()
        sid = str(req.get("session_id", "default"))
        event = str(req.get("event", ""))
        self.manuals.reload()
        with self._lock:
            st = self._sessions.get(sid)
            if st is None or event == "start":
                manual = self._pick(req)
                st = self._sessions[sid] = {
                    "manual_id": manual["id"] if manual else None, "task": str(req.get("task", "")).split("\n\n", 1)[0],
                    "device": str(req.get("device_model") or self.device_model), "pending": None, "trace": [], "end": None,
                    "manual": 0, "model": 0, "served": {}, "skip": set(), "events": [], "dropped": [], "packages": set(),
                    "last": None, "waits": 0, "waited": 0, "facts0": dict(req.get("facts") or {}), "by_facts": False,
                    "completed": False, "registered": False, "started": started, "latency": [],
                }
                self.last_sid = sid
                while len(self._sessions) > MAX_SESSIONS:
                    self._sessions.pop(next(iter(self._sessions)))
                if manual:
                    self._record(sid, {"manual": {"id": manual["id"], "task": manual["task"]}}, fresh=True)
            prev, st["pending"] = st["pending"], None
        mid = st["manual_id"]
        screen = Screen.parse(req.get("screen") or {})
        if screen.package:
            st["packages"].add(screen.package)
        if prev is not None and prev["source"] != "wait":
            self._settle(sid, st, prev, event, str(req.get("event_detail", "")), screen)
        last = st["last"]  # the step guided last (a wait in between is not a step)

        # 1. no manual for this request: artemis alone
        if mid is None:
            code, body = self._artemis(req)
            return self._answered(sid, st, req, screen, code, body, started, prev=None)

        # 1-1. the phone's own settings say the task is done: no screen is judged and no model is asked
        manual = self.manuals.get(mid) or {}
        facts = req.get("facts") if isinstance(req.get("facts"), dict) else {}
        if done_by_facts(manual.get("done"), st["facts0"], facts) and "replay" not in req:
            done = manual["done"]
            said = str(done.get("say") or "다 됐어요.")
            with self._lock:
                st["completed"] = st["by_facts"] = True
                st["pending"] = {"source": "wait"}  # nothing was guided: there is nothing to settle
            body = {"memo": "", "explain": said, "source": "check",
                    "action": {"name": "report_task_status", "status": "completed", "text": said},
                    "meta": {"source": "check", "manual": mid, "fact": done["fact"], "value": facts.get(done["fact"]),
                             "latency_s": round(time.time() - started, 3), "model_calls": 0,
                             "manual_turns": st["manual"], "model_turns": st["model"]}}
            print(f"[guide] {sid} step {req.get('step')} {event} -> done by the phone's setting {done['fact']}: "
                  f"{st['facts0'].get(done['fact'])} -> {facts.get(done['fact'])}", flush=True)
            self._record(sid, {"request": _no_shot(req, mid, st["device"]), "status": 200, "response": body})
            return 200, body

        # 2. the manual's steps
        # The app window cannot be read yet (the app is still drawing its screen): that is not a screen to judge, and
        # a rule answers faster than an app opens. Look again in a moment instead of asking the model about nothing.
        if not screen.app_nodes and screen.package != GUIDE_APP and st["waits"] < MAX_WAITS and "replay" not in req:
            return self._wait(sid, st, req, mid, started, f"no readable app window, {st['waits'] + 1}/{MAX_WAITS}")
        hit = None
        try:
            steps = [s for s in self.manuals.steps(mid) if s["id"] not in st["skip"]]
            hit = rules.find({"manual_id": mid, "device_model": st["device"], "screen": screen}, steps)
        except Exception as exc:  # noqa: BLE001 - a broken rule only costs a model call
            print(f"[guide] {sid} rules.find failed: {exc}", flush=True)
        # The step the person just did still holds (the screen looks the same after it): the model decides what is next.
        if hit and last and last["source"] == "manual" and last["step"]["id"] == hit["step"]["id"] and event not in ("retry", "user_message"):
            print(f"[guide] {sid} step #{hit['step']['seq']} still holds after '{event}': asking artemis", flush=True)
            hit = None
        # The same step again and again in one session is a loop between steps, not progress.
        if hit and st["served"].get(hit["step"]["id"], 0) >= MAX_SERVES:
            print(f"[guide] {sid} step #{hit['step']['seq']} already guided {MAX_SERVES} times: asking artemis", flush=True)
            hit = None
        if hit:
            step, action = hit["step"], hit["action"]
            with self._lock:
                st["manual"] += 1
                st["served"][step["id"]] = st["served"].get(step["id"], 0) + 1
                st["pending"] = st["last"] = {"source": "manual", "step": step, "action": action, "screen": screen, "req": _light(req)}
                st["waits"] = 0
                if completed(action):
                    st["completed"] = True
            latency = round(time.time() - started, 3)
            st["latency"].append(("manual", latency))
            back = bool(last and last["source"] == "manual" and step["seq"] < last["step"]["seq"])
            if back:
                st["rewinds"] = st.get("rewinds", 0) + 1
                print(f"[guide] {sid} back from #{last['step']['seq']} to #{step['seq']}: what an earlier step set no longer holds", flush=True)
            said = ("앞에서 맞춘 것이 바뀌었어요. 다시 맞춰 주세요. " if back else "") + step["instruction"]
            body = {"memo": "", "explain": said, "action": action, "source": "manual",
                    "meta": {"source": "manual", "manual": mid, "step": step["seq"], "latency_s": latency, "model_calls": 0,
                             "elements": len(screen.nodes), "manual_turns": st["manual"], "model_turns": st["model"]}}
            print(f"[guide] {sid} step {req.get('step')} {event} -> manual {mid} #{step['seq']} {label(action)} [{latency}s]", flush=True)
            self._record(sid, {"request": _no_shot(req, mid, st["device"]), "status": 200, "response": body})
            return 200, body

        # The screen a manual step led to, and no step knows it (or only the step just done still holds): it may still
        # be loading (a list not filled in yet, a dialog not up yet). A look costs under a second; the model costs ten.
        if last and last["source"] == "manual" and event in ("met", "wait_done") and st["waits"] < SETTLE_WAITS and "replay" not in req:
            return self._wait(sid, st, req, mid, started, f"the screen after a manual step matches no step yet, {st['waits'] + 1}/{SETTLE_WAITS}")

        # 3. artemis
        areq = {k: v for k, v in req.items() if k not in ("manual_id", "device_model")}
        if manual.get("task"):
            # The person may have asked with another sentence of this manual (an alias) that says less ("알람 8시"):
            # artemis gets the manual's own sentence, with whatever note the app put after it.
            head, sep, rest = str(req.get("task", "")).partition(chr(10) * 2)
            areq["task"] = manual["task"] + sep + rest
        if last and last["source"] == "manual" and event != "start" and "replay" not in areq:
            # artemis never saw the step the manual guided. Tell it what that step was, the way a replay does, so its
            # own history says the step before this screen was done.
            areq["replay"] = {"previous": {
                "step": last["req"]["step"], "elapsed_s": last["req"]["elapsed_s"], "package": last["screen"].package,
                "width": last["screen"].width, "height": last["screen"].height,
                "reply": {"action": last["action"], "memo": ""},
            }}
        if last and last["source"] == "manual" and event != "start":
            # artemis keeps only its own turns. What the manual guided before this goes in as words, in the field it
            # reads as the outcome of the step before, so its judgement (above all "is it done?") has the whole session.
            done = [e for e in st["events"] if e["source"] == "manual"]
            if done:
                told = "; ".join(f"{i}) {e['action']} ({e['outcome']})" for i, e in enumerate(done, 1))
                note = f"steps of this session the person already did, guided from a saved manual: {told}"
                areq["event_detail"] = "; ".join(x for x in (str(req.get("event_detail", "")), note) if x)
        code, body = self._artemis(areq)
        return self._answered(sid, st, req, screen, code, body, started, prev)

    def _wait(self, sid: str, st: dict, req: dict, mid: str, started: float, why: str) -> tuple[int, dict]:
        """Answer "look again in a moment". A wait is not a step: it is not settled, reviewed or saved."""
        with self._lock:
            st["waits"] += 1
            st["waited"] += 1
            st["pending"] = {"source": "wait"}
        body = {"memo": "", "explain": "화면이 준비되기를 기다립니다.", "action": {"name": "wait_for_delay", "time_in_ms": WAIT_MS},
                "source": "wait", "meta": {"source": "wait", "manual": mid, "latency_s": round(time.time() - started, 3), "model_calls": 0}}
        print(f"[guide] {sid} step {req.get('step')} {req.get('event')} -> wait ({why})", flush=True)
        self._record(sid, {"request": _no_shot(req, mid, st["device"]), "status": 200, "response": body})
        return 200, body

    def _answered(self, sid: str, st: dict, req: dict, screen: Screen, code: int, body: dict, started: float, prev) -> tuple[int, dict]:
        """An answer that came from the artemis server: count it, note it for the review, hand it on as it is."""
        if code != 200 or not isinstance(body, dict):
            print(f"[guide] {sid} step {req.get('step')} artemis answered {code}: {str(body)[:160]}", flush=True)
            self._record(sid, {"request": _no_shot(req, st["manual_id"], st["device"]), "status": code, "response": body})
            return code, body
        action = body.get("action") or {}
        latency = round(time.time() - started, 2)
        with self._lock:
            st["model"] += 1
            st["pending"] = st["last"] = {"source": "model", "reply": {"explain": body.get("explain", ""), "action": action},
                                          "action": action, "screen": screen, "req": _light(req)}
            st["waits"] = 0
            if completed(action):
                st["completed"] = True
                st["end"] = {"source": "model", "screen": screen, "reply": {"explain": body.get("explain", ""), "action": action}, "step": None}
        st["latency"].append(("model", latency))
        body["source"] = "model"
        body["meta"] = {**(body.get("meta") or {}), "source": "model", "manual": st["manual_id"],
                        "manual_turns": st["manual"], "model_turns": st["model"]}
        print(f"[guide] {sid} step {req.get('step')} {req.get('event')} -> artemis {label(action)} [{latency}s]", flush=True)
        self._record(sid, {"request": _no_shot(req, st["manual_id"], st["device"]), "status": 200, "response": body})
        return 200, body

    # ---------------------------------------------------------------- what the next request says about the last step

    def _settle(self, sid: str, st: dict, prev: dict, event: str, detail: str, screen: Screen | None = None) -> None:
        action = prev["action"]
        seq = prev["step"]["seq"] if prev["source"] == "manual" else None
        st["events"].append({"step": prev["req"]["step"], "source": prev["source"], "seq": seq, "action": label(action), "outcome": event})
        # A swipe that opened another surface (home to the app list) ends with the swiped area gone. The app reports that
        # as "unaimable: target-lost ... the screen changed after the swipe": the swipe was done and it worked.
        swiped = action.get("name") == "swipe" and event == "unaimable" and "the screen changed after the swipe" in detail
        if swiped:
            event = "met"
            st["events"][-1]["outcome"] = "met (the swiped area gave way to another screen)"
        # The target is gone because the screen is another one now (the person left, or tapped something else): the step
        # did not mislead. It did only when its kernel still holds on this screen and the app could not aim it.
        left = (event == "unaimable" and prev["source"] == "manual" and screen is not None
                and not rules.kernel_holds(prev["step"], screen))
        if left:
            st["events"][-1]["outcome"] = "left (the screen is another one now)"
        if prev["source"] == "manual" and event in ("declared_stuck", "unaimable") and not left:
            # The step misled on this screen. This session does not get it again. While authoring, a step that never
            # worked is removed; one that has worked before stays (its condition was met by a screen it is not for).
            worked = int(prev["step"].get("met") or 0)
            st["skip"].add(prev["step"]["id"])
            st["dropped"].append({"seq": seq, "action": label(action), "why": event + (f": {detail}" if detail else ""), "kept": worked > 0})
            if self.authoring and st["manual_id"] and not worked:
                self.manuals.delete_step(st["manual_id"], prev["step"]["id"])
            print(f"[guide] {sid} manual step #{seq} {'removed' if self.authoring and not worked else 'set aside'} after '{event}'"
                  + (f" (it has worked {worked} time(s) before)" if worked else ""), flush=True)
        if event == "met" and prev["source"] == "manual" and self.authoring and st["manual_id"]:
            self.manuals.count_met(st["manual_id"], prev["step"]["id"])
        if event == "met":
            # Only what the person actually did goes on record, and only for the review at the end of the session.
            st["trace"].append({"source": prev["source"], "screen": prev["screen"], "reply": prev.get("reply"), "step": prev.get("step")})

    # ---------------------------------------------------------------- authoring: register after a finished session

    def register(self, sid: str) -> dict:
        """Review what the person did in a finished session and save the steps artemis had to find.

        Nothing is saved from a session that did not reach the end: a half-followed path must not be guided to anyone.
        """
        st = self._sessions.get(sid)
        if st is None or not st["manual_id"] or not st["completed"] or st["registered"]:
            return {"done": 0, "kept": 0, "new": 0, "not_saved": []}
        st["registered"] = True
        mid, device = st["manual_id"], st["device"]
        trace = list(st["trace"]) + ([st["end"]] if st["end"] else [])
        kept = rules.review({"manual_id": mid, "device_model": device}, trace)
        last_seq, earlier, new, not_saved = 0, [], 0, []
        for e in kept:
            if e["source"] == "manual":
                if e["step"].get("device_model") == device:
                    last_seq = self.manuals.step_seq(mid, e["step"]["id"]) or last_seq
            else:
                name = (e["reply"].get("action") or {}).get("name")
                if name == "report_task_status" and (self.manuals.get(mid) or {}).get("done"):
                    earlier.append(e["screen"])
                    continue  # this manual's "done" is the phone's setting, not what a screen looks like
                try:
                    step = rules.learn({"manual_id": mid, "device_model": device, "screen": e["screen"], "earlier": list(earlier)}, e["reply"])
                except Exception as exc:  # noqa: BLE001
                    print(f"[guide] {sid} rules.learn failed: {exc}", flush=True)
                    step = None
                if step is None:
                    not_saved.append(label(e["reply"].get("action") or {}))
                    print(f"[guide] {sid} not saved: {name} (no condition tells this screen apart)", flush=True)
                else:
                    seq = self.manuals.reserve_seq(mid, device, last_seq)
                    if self.manuals.add_step(mid, device, seq, step) is not None:
                        last_seq, new = seq, new + 1
                        print(f"[guide] {sid} saved #{seq} {name} when {json.dumps(step['screen_condition'], ensure_ascii=False)}", flush=True)
            earlier.append(e["screen"])
        print(f"[guide] {sid} session complete: {len(trace)} step(s) done, {len(kept)} kept by the review, {new} newly saved", flush=True)
        return {"done": len(trace), "kept": len(kept), "new": new, "not_saved": not_saved}

    def chain(self, sid: str | None) -> dict:
        """Authoring: read the requirement chain from a run that went from the start to the end on the manual alone.

        Every step of that run gets `requires`: what the steps before it changed and its own screen still showed.
        A run that artemis helped with, or that did not end, says nothing here.
        """
        st = self._sessions.get(sid or "")
        if st is None or not st["manual_id"] or not st["completed"] or st["model"]:
            return {"steps": 0, "requirements": 0}
        mid = st["manual_id"]
        kept = rules.review({"manual_id": mid, "device_model": st["device"]}, list(st["trace"]))
        path = [(e["step"], e["screen"]) for e in kept if e["source"] == "manual" and e.get("step")]
        requires = rules.chain(path)
        for step_id, conditions in requires.items():
            self.manuals.set_requires(mid, step_id, conditions)
        total = sum(len(v) for v in requires.values())
        print(f"[guide] {sid} requirement chain over {len(path)} step(s): {total} requirement(s)", flush=True)
        # the general first step: off the manual's apps altogether, go home (then the path's own steps lead back in)
        packages = sorted({p for s in self.manuals.steps(mid) if s.get("device_model") == st["device"] and not s.get("entry")
                           for p in _foregrounds(s["screen_condition"])})
        self.manuals.set_entry(mid, st["device"], packages)
        for step, _screen in path:
            if requires.get(step["id"]):
                print(f"[guide] {sid}   #{step['seq']} {label(rules.resolve(step['done_condition'], _screen) or step['done_condition'])} "
                      f"requires {json.dumps(requires[step['id']], ensure_ascii=False)}", flush=True)
        return {"steps": len(path), "requirements": total}

    def reject(self, sid: str | None) -> int | None:
        """Authoring: the session ended as "done" but the device says the task is not done.

        Nothing of the session is saved. When it was a step of the manual that said "done", that step is removed:
        its condition holds on a screen where the task is not done. Returns that step's place, or None.
        """
        st = self._sessions.get(sid or "")
        if st is None:
            return None
        st["registered"] = True
        last = st["last"]
        if self.authoring and st["manual_id"] and last and last["source"] == "manual" and completed(last["action"]):
            self.manuals.delete_step(st["manual_id"], last["step"]["id"])
            print(f"[guide] {sid} step #{last['step']['seq']} said done where the task was not done: removed", flush=True)
            return last["step"]["seq"]
        return None

    # ---------------------------------------------------------------- views

    def summary(self, sid: str | None) -> dict | None:
        st = self._sessions.get(sid or "")
        if st is None:
            return None
        lat = st["latency"]
        return {
            "session": sid, "manual": st["manual_id"], "task": st["task"], "device": st["device"], "completed": st["completed"],
            "by_facts": st["by_facts"], "rewinds": st.get("rewinds", 0),
            "manual_turns": st["manual"], "model_turns": st["model"], "waits": st["waited"], "events": list(st["events"]),
            "dropped": list(st["dropped"]), "packages": sorted(st["packages"]),
            "manual_latency_s": [t for s, t in lat if s == "manual"], "model_latency_s": [t for s, t in lat if s == "model"],
        }

    def recent(self, n: int = 30) -> list[dict]:
        return [self.summary(sid) for sid in list(self._sessions)[-n:]]

    # ---------------------------------------------------------------- plumbing

    def _pick(self, req: dict) -> dict | None:
        mid = req.get("manual_id")
        if mid:
            m = self.manuals.get(str(mid))
            return m if m and (self.authoring or m.get("status") == "verified") else None
        return self.manuals.match(str(req.get("task", "")), verified_only=not self.authoring)

    def _artemis(self, req: dict) -> tuple[int, dict]:
        data = json.dumps(req).encode("utf-8")
        r = urllib.request.Request(self.artemis_url + "/v1/guide/next", data=data, headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(r, timeout=118) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": raw[:300]}
        except Exception as exc:  # noqa: BLE001 - the app shows "could not prepare the guide" and offers a retry
            return 502, {"error": f"artemis server at {self.artemis_url}: {exc}"}

    def _record(self, sid: str, line: dict, fresh: bool = False) -> None:
        """One file per session: the manual on the first line, then every request (without its screenshot) and answer."""
        if self.record_dir is None:
            return
        try:
            with self._record_lock:
                self.record_dir.mkdir(parents=True, exist_ok=True)
                with open(self.record_dir / f"{sid}.jsonl", "w" if fresh else "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"[guide] record failed: {exc}", flush=True)


def _foregrounds(condition) -> set[str]:
    out: set[str] = set()
    if isinstance(condition, dict):
        for k, v in condition.items():
            if k == "foreground" and isinstance(v, str):
                out.add(v)
            else:
                out |= _foregrounds(v)
    elif isinstance(condition, list):
        for c in condition:
            out |= _foregrounds(c)
    return out


def _light(req: dict) -> dict:
    return {"step": req.get("step"), "elapsed_s": int(req.get("elapsed_s") or 0)}


def _no_shot(req: dict, manual_id, device: str) -> dict:
    out = {k: v for k, v in req.items() if k != "screen"}
    out["screen"] = {k: v for k, v in (req.get("screen") or {}).items() if k != "screenshot_jpeg_b64"}
    out["manual_id"], out["device_model"] = manual_id, device
    return out
