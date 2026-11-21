"""Emulator harness: plays the human who follows the on-screen guide.

It starts a guide session through the shell-only debug broadcast, then reads the app's `ARM` / `AIM`
log lines and does exactly what the overlay shows: taps the ring's centre, long-presses it, types the
requested text into it, swipes toward a chevron, or presses the system key a card-only press step asks
for. A typing step follows its `TYPEPHASE`: open (tap the field), type, submit (tap the ringed keyboard key).
Text adb cannot type (Hangul) is copied with the guide's copy icon and pasted, as a person may.
Every armed step is screenshotted. The app itself never acts; this script stands in for the person.

After each tap, long-press or swipe the script reads the accessibility tree again (the app's DEBUG_DUMP) and
says in its transcript whether the screen changed. A swipe is tried in several ways until the tree shows the
screen moved; when none does, the script stops with exit code 3 instead of telling the guide it is stuck.

usage: python -I scripts/follow.py "와이파이 설정 화면 열어줘" [--serial 127.0.0.1:6520] [--max-steps 15]
       python -I scripts/follow.py "설정에서 battery를 검색해줘" --home --deviate step2=back

--deviate STEP=KIND makes the human do something else the first time step STEP is shown, instead of following it:
home | back | app:<package> | tap:<label> | tapid:<view id> | send:<words> (ChatGPT: type one's own ASCII words
and send them) | swipe:<up|down|left|right> (the finger's direction, mid-screen). STEP is `step<N>`, the N-th
armed guide, or `at:<words>`, the first step whose title has those words (`at:확인=tap:오후`).
STEP:PHASE (open | type | submit) deviates when a typing step reaches that phase, e.g.
`step3:submit=back` hides the keyboard after the search words are in. The script then keeps following whatever
the guide shows and reports, per deviation, how long the guide took to show a step again and how many requests
to the guide server that took.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ADB = os.environ.get("ADB") or shutil.which("adb") or str(
    Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ".") / "platform-tools" / "adb")
TAG = "ToolusGuide"
PKG = "ai.toolus.mobileguide"
# Ways of dragging, tried in order until the screen moves: (start, end) as fractions of the swiped area, and how long.
# The first starts low: the AOSP launcher opens its app list only for a drag that starts on the bottom row of icons.
SWIPES = ((0.90, 0.15, 300), (0.83, 0.20, 400), (0.75, 0.15, 200), (0.90, 0.15, 150))


def adb(serial: str, *args: str, text: bool = True, timeout: int = 30):
    return subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=text, timeout=timeout,
                          encoding="utf-8" if text else None)


def latest_copybtn(serial: str):
    """The guide's copy icon for the step on screen (its COPYBTN line after the last ARM), waiting a moment for it."""
    for _ in range(6):
        for line in reversed(adb(serial, "logcat", "-d", "-s", f"{TAG}:I").stdout.splitlines()):
            if "COPYBTN" in line:
                return tuple(int(v) for v in line.split("COPYBTN", 1)[1].split()[:4])
            if "ARM step=" in line:
                break
        time.sleep(0.5)
    return None


def rect(s: str):
    if s in ("-", ""):
        return None
    l, t, r, b = map(int, s.split())
    return l, t, r, b


def swipe_for(chevron: str, vp, size, start: float = 0.83, end: float = 0.20):
    w, h = size
    l, t, r, b = vp if vp else (0, int(h * 0.12), w, int(h * 0.88))
    cx, cy = (l + r) // 2, (t + b) // 2
    # The chevron points where the hidden content is; the finger moves the other way.
    if chevron == "down":
        return cx, t + int((b - t) * start), cx, t + int((b - t) * end)
    if chevron == "up":
        return cx, t + int((b - t) * (1 - start)), cx, t + int((b - t) * (1 - end))
    if chevron == "right":
        return l + int((r - l) * start), cy, l + int((r - l) * end), cy
    return l + int((r - l) * (1 - start)), cy, l + int((r - l) * (1 - end)), cy


def tree(serial: str):
    """The screen as the accessibility service reads it (the app's DEBUG_DUMP): (foreground package, elements).

    An element is (class, label, view id, bounds). The status bar is left out: its clock changes by itself.
    This is how the script knows what its own tap or swipe did. It never judges that from a picture.
    """
    nonce = str(time.time_ns())
    adb(serial, "shell", f"log -t {TAG} -p i 'HARNESS tree {nonce}'")
    adb(serial, "shell", f"am broadcast -a {PKG}.DEBUG_DUMP -p {PKG}")
    best = None
    for _ in range(14):
        time.sleep(0.2)
        lines = adb(serial, "logcat", "-d", "-s", f"{TAG}:I", "-t", "500").stdout.splitlines()
        at = next((i for i in range(len(lines) - 1, -1, -1) if f"HARNESS tree {nonce}" in lines[i]), None)
        if at is None:
            continue
        dump = [x.split("DUMP ", 1)[1] for x in lines[at:] if "DUMP " in x]
        head = next((re.match(r"Screen (\d+)x(\d+), foreground package: (\S*)", x) for x in dump if x.startswith("Screen ")), None)
        if head is None:
            continue
        height = int(head.group(2))
        els = set()
        for x in dump:
            m = re.match(r"\[\d+\] (\S+) '(.*?)' (\S*) .*?\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]\s*$", x)
            if m and int(m.group(7)) > height * 0.06:
                els.add((m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6), m.group(7)))
        now = (head.group(3), frozenset(els))
        if best is not None and now == best:
            return now  # two reads in a row agree: the whole dump has arrived
        best = now
    return best or ("", frozenset())


def changed(serial: str, before, wait: float = 2.4):
    """Whether the screen is another one than `before` within `wait` seconds, and what it is now."""
    end = time.time() + wait
    now = before
    while time.time() < end:
        now = tree(serial)
        if now != before:
            return True, now
    return False, now


def tap_checked(serial: str, x: int, y: int, what: str, long: bool = False) -> str:
    """Tap (or long-press) and read from the tree whether anything happened; a tap that did nothing is done once more."""
    before = tree(serial)
    for attempt in (1, 2):
        if long:
            adb(serial, "shell", "input", "swipe", str(x), str(y), str(x), str(y), "900")
        else:
            adb(serial, "shell", "input", "tap", str(x), str(y))
        ok, _ = changed(serial, before)
        if ok:
            return what + (" (the screen changed)" if attempt == 1 else " (the screen changed on the second try)")
    return what + " (TWICE, and the screen did NOT change)"


def swipe_checked(serial: str, chevron: str, vp, size) -> tuple[str, bool]:
    """Drag the way the chevron asks until the tree shows the screen moved. Says so when no way of dragging moved it."""
    before = tree(serial)
    for start, end, ms in SWIPES:
        x1, y1, x2, y2 = swipe_for(chevron, vp, size, start, end)
        adb(serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))
        ok, _ = changed(serial, before)
        if ok:
            return f"swipe {chevron} ({x1},{y1} to {x2},{y2} in {ms}ms; the screen changed)", True
    return f"swipe {chevron}: the screen did NOT change after {len(SWIPES)} ways of dragging", False


def find_on_screen(serial: str, label: str = "", view_id: str = ""):
    """Centre of the first node the app's debug dump lists with this label or view id."""
    adb(serial, "shell", "am broadcast -a ai.toolus.mobileguide.DEBUG_DUMP -p ai.toolus.mobileguide")
    time.sleep(0.8)
    for line in reversed(adb(serial, "logcat", "-d", "-s", f"{TAG}:I").stdout.splitlines()):
        m = re.search(r"DUMP \[\d+\] \S+ '(.*?)' (\S*) .*?\[(\d+),(\d+)\]\[(\d+),(\d+)\]\s*$", line)
        if m and ((label and m.group(1) == label) or (view_id and m.group(2) == view_id)):
            l, t, r, b = map(int, m.groups()[2:])
            return (l + r) // 2, (t + b) // 2
    return None


def deviate(serial: str, kind: str) -> str:
    """Leave the plan. The timing marker goes into logcat right before the input itself."""
    mark = lambda: adb(serial, "shell", f"log -t {TAG} -p i 'HARNESS deviate {kind}'")  # noqa: E731
    at = None
    if kind.startswith(("tap:", "tapid:")):
        label, vid = (kind[4:], "") if kind.startswith("tap:") else ("", kind[6:])
        at = find_on_screen(serial, label, vid)
        if not at:
            return f"could not find {kind}"
    mark()
    if kind == "home":
        adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME"); return "pressed HOME"
    if kind == "back":
        adb(serial, "shell", "input", "keyevent", "KEYCODE_BACK"); return "pressed BACK"
    if kind.startswith("swipe:"):
        w, h = 1080, 2400
        m = re.search(r"Physical size: (\d+)x(\d+)", adb(serial, "shell", "wm", "size").stdout or "")
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        d = kind[6:]
        x1, y1, x2, y2 = {"up": (w // 2, int(h * 0.70), w // 2, int(h * 0.30)), "down": (w // 2, int(h * 0.30), w // 2, int(h * 0.70)),
                          "left": (int(w * 0.80), h // 2, int(w * 0.20), h // 2), "right": (int(w * 0.20), h // 2, int(w * 0.80), h // 2)}[d]
        adb(serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "400"); return f"swiped {d}"
    if kind.startswith("app:"):
        adb(serial, "shell", f"monkey -p {kind[4:]} -c android.intent.category.LAUNCHER 1"); return f"opened {kind[4:]}"
    if kind.startswith("send:"):
        # the person writes their own words (ASCII here) into ChatGPT's message box and sends them without the guide
        words = kind[5:]
        box = find_on_screen(serial, "ChatGPT에게 물어보세요") or find_on_screen(serial, "무엇이든 물어보세요")
        if not box:
            return "could not find the message box"
        adb(serial, "shell", "input", "tap", str(box[0]), str(box[1])); time.sleep(0.8)
        adb(serial, "shell", "input", "text", words.replace(" ", "%s")); time.sleep(1.0)
        send = find_on_screen(serial, "메시지 보내기")
        if not send:
            return f"typed '{words}', no send button found"
        adb(serial, "shell", "input", "tap", str(send[0]), str(send[1]))
        return f"typed '{words}' and sent it"
    if at:
        adb(serial, "shell", "input", "tap", str(at[0]), str(at[1])); return f"tapped {kind.split(':', 1)[1]} at {at[0]},{at[1]}"
    return f"unknown deviation {kind}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--deviate", action="append", default=[], help="STEP=KIND, see the module docstring")
    ap.add_argument("--serial", default="127.0.0.1:6520")
    ap.add_argument("--max-steps", type=int, default=15)
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--out", default="")
    ap.add_argument("--home", action="store_true", help="press HOME before starting")
    ap.add_argument("--pace", type=float, default=0.8, help="seconds the person looks at a new guide before acting (demo: 2.5)")
    a = ap.parse_args()

    out = Path(a.out or f"runs/{time.strftime('%Y%m%d-%H%M%S')}")
    out.mkdir(parents=True, exist_ok=True)
    size = tuple(map(int, re.search(r"(\d+)x(\d+)", adb(a.serial, "shell", "wm", "size").stdout).groups()))

    if a.home:
        adb(a.serial, "shell", "input", "keyevent", "KEYCODE_HOME")
        time.sleep(1)
    adb(a.serial, "logcat", "-c")
    # adb shell joins its arguments with spaces and the device shell re-splits them: quote the task once.
    quoted = "'" + a.task.replace("'", "'\\''") + "'"
    adb(a.serial, "shell", f"am broadcast -a ai.toolus.mobileguide.DEBUG_START -p ai.toolus.mobileguide --es task {quoted}")
    started = time.time()
    plans = dict(kv.split("=", 1) for kv in a.deviate)   # step<N> -> deviation kind, used once
    devs = []                                           # measurements per deviation

    seen = 0
    arm = None      # dict from the latest ARM line
    aim = None      # dict from the latest AIM line of that step
    acted_step = -1
    acted_at = 0.0
    swiped = False  # the last act followed a chevron: a person acts on the ring as soon as it comes into view
    idle = False    # the last look found nothing to follow
    copybtn = None  # where the guide's copy icon rests (COPYBTN), for text adb cannot type
    ready_at = None  # when the person, having looked at the current guide for a moment, acts
    tries = 0        # how many times the person followed the current step
    STUCK_TRIES = 3
    steps = 0
    transcript = []

    while time.time() - started < a.timeout:
        log = adb(a.serial, "logcat", "-v", "epoch", "-d", "-s", f"{TAG}:I").stdout.splitlines()
        new, seen = log[seen:], len(log)
        for line in new:
            msg = line.split(f"{TAG}: ", 1)[-1].strip()
            if not msg or msg.startswith("DUMP"):
                continue
            # device clock: deviation timings are differences of logcat timestamps, not of this poll loop
            stamp = re.match(r"\s*(\d+\.\d+)\s", line)
            if not stamp:
                continue
            ts = float(stamp.group(1))
            now = time.time()
            if msg.startswith("HARNESS deviate") and devs and devs[-1]["t"] is None:
                devs[-1]["t"] = ts
                continue
            if msg.startswith(("plan ", "step=", "outcome", "rebind", "ARM", "AIM", "TYPEPHASE", "COPY", "COVERED", "CHEVRON", "AMBIGUOUS", "TTS ")) or "unaimable" in msg:
                transcript.append(f"[{now - started:6.1f}s] {msg}")
                print(transcript[-1], flush=True)
            if devs and devs[-1]["t"] is not None and devs[-1].get("back") is None:
                d = devs[-1]
                now = ts
                if msg.startswith("plan "):
                    d["planner_calls"] += 1
                if msg.startswith("ARM"):
                    # the guide shows a step again: this deviation is over
                    d["back"] = now - d["t"]
                    d["first_title"] = re.search(r"title=(.*?)(?: text=| key=|$)", msg).group(1)
            if msg.startswith(("outcome", "plan ")):
                aim = None  # the step closed; while the next one is prepared the guide shows nothing to follow
            if msg.startswith("ARM"):
                kv = dict(re.findall(r"(\w+)=(\S+)", msg))
                title = re.search(r"title=(.*?)(?: text=| key=|$)", msg)
                typed = re.search(r" text=(.*?)(?: key=\S+)?$", msg)  # the text may contain spaces
                arm = {"step": int(kv["step"]), "verb": kv["verb"], "text": typed.group(1) if typed else None, "key": kv.get("key"),
                       "title": title.group(1) if title else ""}
                aim = None
                copybtn = None
                steps += 1
            elif msg.startswith("COPYBTN"):
                copybtn = tuple(int(v) for v in msg.split()[1:5])
            elif msg.startswith("TYPEPHASE") and arm:
                # the typing step now asks for something else (open the keyboard, type, press its action key): act again
                m = re.match(r"TYPEPHASE step=(\d+) phase=(\S+)", msg)
                if m and int(m.group(1)) == arm["step"]:
                    arm["phase"] = m.group(2)
                    acted_step = -1
            elif msg.startswith("AIM") and arm:
                m = re.match(r"AIM step=(\d+) ring=(.+?) chevron=(\S+) viewport=(.+)$", msg)
                if m and int(m.group(1)) == arm["step"]:
                    aim = {"ring": rect(m.group(2)), "chevron": None if m.group(3) == "-" else m.group(3),
                           "viewport": rect(m.group(4))}
            elif msg.startswith("step=") and "Terminal" in msg:
                time.sleep(1.5)
                (out / "final.png").write_bytes(adb(a.serial, "exec-out", "screencap", "-p", text=False).stdout)
                report(devs, transcript)
                (out / "transcript.txt").write_text("\n".join(transcript), encoding="utf-8")
                print(f"done in {time.time() - started:.1f}s, {steps} armed steps → {out}")
                return 0

        # Act once per armed step on what the guide shows ~0.8 s after it appeared (later AIM lines win);
        # act again if a chevron is still showing.
        # A person also retries a tap that seemed to do nothing after a while (then presses '막혔어요', see tries).
        due = arm and aim is not None and (acted_step != arm["step"] or (aim["chevron"] and time.time() - acted_at > 2.5)
                                           or ((swiped or idle) and aim["ring"] and time.time() - acted_at > 1.0)
                                           or time.time() - acted_at > 20)
        if not due:
            ready_at = None
        elif ready_at is None:
            ready_at = time.time() + a.pace
        if due and time.time() >= ready_at:
            ready_at = None
            shot = adb(a.serial, "exec-out", "screencap", "-p", text=False).stdout
            (out / f"step{arm['step']:02d}{'' if acted_step != arm['step'] else '_again'}.png").write_bytes(shot)
            verb, ring, chev = arm["verb"], aim["ring"], aim["chevron"]
            step_id = f"step{arm['step']}"
            at_phase = f"{step_id}:{arm['phase']}" if arm.get("phase") else None
            hit = at_phase if at_phase in plans else step_id if step_id in plans else None
            if hit is None:
                # `at:<words>`: the first step whose title has these words (step numbers move when the guide waits)
                hit = next((k for k in plans if k.startswith("at:") and k[3:] in arm.get("title", "")), None)
            if hit:
                kind = plans.pop(hit)
                devs.append({"kind": kind, "at_step": hit, "t": None, "planner_calls": 0})
                did = "DEVIATE " + deviate(a.serial, kind)
            elif verb == "type_guide" and arm.get("phase") in ("open", "submit") and ring:
                # open: tap the field so the keyboard comes up; submit: tap the keyboard key the ring is on
                x, y = (ring[0] + ring[2]) // 2, (ring[1] + ring[3]) // 2
                did = tap_checked(a.serial, x, y, f"tap {x},{y} ({arm['phase']})")
            elif verb == "type_guide" and arm.get("phase") == "submit-words":
                adb(a.serial, "shell", "input", "keyevent", "KEYCODE_ENTER"); did = "key KEYCODE_ENTER (the keyboard hides its keys)"
            elif ring and verb in ("click_guide", "launch_guide", "press_guide"):
                x, y = (ring[0] + ring[2]) // 2, (ring[1] + ring[3]) // 2
                did = tap_checked(a.serial, x, y, f"tap {x},{y}")
            elif ring and verb == "long_press_guide":
                x, y = (ring[0] + ring[2]) // 2, (ring[1] + ring[3]) // 2
                did = tap_checked(a.serial, x, y, f"long-press {x},{y}", long=True)
            elif ring and verb == "type_guide":
                x, y = (ring[0] + ring[2]) // 2, (ring[1] + ring[3]) // 2
                adb(a.serial, "shell", "input", "tap", str(x), str(y)); time.sleep(0.6)
                if "지우고" in arm.get("title", ""):
                    adb(a.serial, "shell", "input keycombination KEYCODE_CTRL_LEFT KEYCODE_A"); time.sleep(0.2)
                    adb(a.serial, "shell", "input", "keyevent", "KEYCODE_DEL"); time.sleep(0.3)
                typed = (arm["text"] or "").replace("\\n", "")
                if typed.isascii():
                    adb(a.serial, "shell", "input", "text", typed.replace(" ", "%s"))
                    shown = any(typed.replace(" ", "").lower() in el[1].replace(" ", "").lower() for el in changed(a.serial, None, 1.5)[1][1])
                    did = f"type '{typed}'" + (" (the field shows it)" if shown else " (the field does NOT show it)")
                else:
                    # adb cannot type Hangul: copy it with the guide's copy icon and paste it, as a person may
                    btn = latest_copybtn(a.serial)
                    if btn:
                        adb(a.serial, "shell", "input", "tap", str((btn[0] + btn[2]) // 2), str((btn[1] + btn[3]) // 2))
                        time.sleep(0.7)
                        adb(a.serial, "shell", "input", "keyevent", "KEYCODE_PASTE")
                        did = f"copy + paste '{typed}'"
                    else:
                        did = "no copy icon on screen: typed nothing"
                # a step that sends the text then rings the keyboard's action key (TYPEPHASE submit); that is followed then
            elif chev:
                did, moved = swipe_checked(a.serial, chev, aim["viewport"], size)
                if not moved:
                    # The script itself could not do what the guide asked. That is not the person being stuck, and
                    # saying so would send the guide looking for another way. Stop and say what failed.
                    transcript.append(f"[{time.time() - started:6.1f}s] HARNESS {did}")
                    print(transcript[-1], flush=True)
                    (out / "transcript.txt").write_text("\n".join(transcript), encoding="utf-8")
                    adb(a.serial, "shell", "am", "broadcast", "-a", f"{PKG}.DEBUG_STOP", "-p", PKG)
                    print(f"harness failure after {time.time() - started:.1f}s, {steps} armed steps → {out}")
                    return 3
            elif verb == "press_guide" and arm["key"]:
                code = {"BACK": "KEYCODE_BACK", "HOME": "KEYCODE_HOME", "APP_SWITCH": "KEYCODE_APP_SWITCH",
                        "ENTER": "KEYCODE_ENTER"}.get(arm["key"], "KEYCODE_BACK")
                adb(a.serial, "shell", "input", "keyevent", code); did = f"key {code} (card-only step)"
            else:
                did = "nothing to follow"
            tries = tries + 1 if acted_step == arm["step"] else 1
            # a wait step asks for nothing; a person keeps waiting (a download can take minutes)
            if tries > STUCK_TRIES and verb != "wait_guide":
                adb(a.serial, "shell", "am broadcast -a ai.toolus.mobileguide.DEBUG_DECLARE -p ai.toolus.mobileguide --es kind stuck")
                did = f"pressed '막혔어요' after following this step {STUCK_TRIES} times with no result"
            transcript.append(f"[{time.time() - started:6.1f}s] HUMAN {did}")
            print(transcript[-1], flush=True)
            acted_step, acted_at = arm["step"], time.time()
            swiped = did.startswith("swipe ")
            idle = did == "nothing to follow"  # the ring was not up yet: act as soon as it is
        if steps > a.max_steps:
            break
        time.sleep(0.25)

    report(devs, transcript)
    (out / "transcript.txt").write_text("\n".join(transcript), encoding="utf-8")
    print(f"stopped after {time.time() - started:.1f}s, {steps} armed steps → {out}")
    adb(a.serial, "shell", "am", "broadcast", "-a", "ai.toolus.mobileguide.DEBUG_STOP", "-p", "ai.toolus.mobileguide")
    return 1


def report(devs, transcript):
    for d in devs:
        f = lambda v: "-" if v is None else f"{v:.2f}s"  # noqa: E731
        line = (f"DEVIATION {d['kind']} at {d['at_step']}: next guide after {f(d.get('back'))} ({d.get('first_title')}), "
                f"requests to the guide server {d['planner_calls']}")
        transcript.append(line)
        print(line, flush=True)


if __name__ == "__main__":
    sys.exit(main())
