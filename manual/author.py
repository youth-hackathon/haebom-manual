"""저작 서버: 에뮬레이터에서 요청을 실제로 따라 해 보면서 매뉴얼의 규칙을 만들고, 다시 돌려 검증한다.

    python manual/author.py serve                                       # http://127.0.0.1:8780 (POST /author)
    python manual/author.py run "설정에서 블루투스 화면 열어줘" --id bluetooth   # 한 번 저작하고 끝낸다

한 번의 저작은 이렇게 돈다. 기기에는 해봄 앱이 깔려 있고 접근성 서비스가 켜져 있어야 한다.

    1. 기기의 앱이 이 서버를 안내 서버로 보게 한다(adb reverse, DEBUG_CONFIG). 끝나면 원래대로 돌린다.
    2. 시작 상태를 만든다: cold(최근 쓴 앱을 모두 끄고 홈에서), warm(홈에서, 앱은 쓰던 자리 그대로), done(이미 다 된 화면에서).
    3. 사람 역할(human.py)이 화면의 안내를 따라 누른다. 안내는 이 서버가 답한다: 매뉴얼에 맞는 규칙이 있으면
       그것으로, 없으면 artemis 서버에 넘긴다(guide.py). 누르는 것은 사람 역할이고 앱이 아니다.
    4. 끝까지 갔으면, artemis가 답한 단계 가운데 사람이 해낸 것을 검토해 규칙으로 등록한다(rules.review, learn).
       규칙으로 안내했는데 막힌 단계는 그 규칙을 지운다.
    5. 같은 시작 상태에서 다시 돌린다. 모델을 한 번도 부르지 않고 끝까지 가면 그 시작 상태는 통과다.
    6. 모든 시작 상태가 통과하면 매뉴얼의 status를 verified로 바꾼다. 앞단 서버는 그때부터 이 매뉴얼을 쓴다.

환경 변수: AUTHOR_HOST(127.0.0.1), AUTHOR_PORT(8780), ARTEMIS_URL(http://127.0.0.1:8765), MANUALS_DIR(../manuals),
RUNS_DIR(../runs), DEVICE_SERIAL(127.0.0.1:6520, device/의 Cuttlefish), APP_APK(앱이 없을 때 설치할 APK).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from guide import Guide  # noqa: E402
from server import make_server  # noqa: E402
from store import Manuals  # noqa: E402

ADB = os.environ.get("ADB") or shutil.which("adb") or str(
    Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ".") / "platform-tools" / "adb")
PKG = "ai.toolus.mobileguide"
SERVICE = f"{PKG}/{PKG}.GuideAccessibilityService"
TAG = "ToolusGuide"
STARTS = ("cold", "warm", "done")
DEFAULT_SERIAL = "127.0.0.1:6520"  # the Cuttlefish device of device/
START_WAIT_S = 30  # the app's first request comes a few seconds after a run starts; none by then means the run did not start


# ---------------------------------------------------------------- the device

class Device:
    def __init__(self, serial: str, port: int, apk: str = ""):
        self.serial, self.port, self.apk = serial, port, apk
        self.before: str | None = None  # the guide server the app pointed at before authoring

    def adb(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run([ADB, "-s", self.serial, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

    def model(self) -> str:
        return self.adb("shell", "getprop", "ro.product.model").stdout.strip() or "unknown"

    def home_package(self) -> str:
        out = self.adb("shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME").stdout.strip().splitlines()
        return out[-1].split("/")[0] if out and "/" in out[-1] else ""

    def recent_packages(self) -> set[str]:
        """The apps with a task in the recents list. A cold start stops them all, so each opens at its first screen
        (a new manual does not know yet which apps its path goes through)."""
        out = self.adb("shell", "dumpsys", "activity", "recents").stdout
        return set(re.findall(r"mActivityComponent=([A-Za-z0-9_.]+)/", out))

    def config(self, **extras: str) -> str:
        """Send DEBUG_CONFIG and return the line the app logs back ("CONFIG guide=... mode=... tts=... engine=...")."""
        for _ in range(8):
            self.adb("logcat", "-c")
            args = ["shell", "am", "broadcast", "-a", f"{PKG}.DEBUG_CONFIG", "-p", PKG]
            for k, v in extras.items():
                args += ["--es", k, f"'{v}'"]
            self.adb(*args)
            for _ in range(6):
                time.sleep(0.5)
                hits = [l for l in self.adb("logcat", "-d", "-s", f"{TAG}:I").stdout.splitlines() if "CONFIG " in l]
                if hits:
                    return hits[-1].split(f"{TAG}:", 1)[-1].strip()
        return ""

    def check(self) -> None:
        """The device is there: nothing is created for a job whose device cannot be reached."""
        if re.fullmatch(r"[^:\s]+:\d+", self.serial):  # a device reached over the network (Cuttlefish in a container)
            subprocess.run([ADB, "connect", self.serial], capture_output=True, text=True, timeout=30)
        state = self.adb("get-state")
        if state.stdout.strip() != "device":
            raise RuntimeError(f"adb does not see {self.serial} as a device ({(state.stdout or state.stderr).strip()})")

    def prepare(self, log) -> None:
        self.check()
        if "package:" not in self.adb("shell", "pm", "path", PKG).stdout:
            if not self.apk or not Path(self.apk).exists():
                raise RuntimeError(f"the app ({PKG}) is not on {self.serial}; install it, or give its APK with --apk / APP_APK")
            r = self.adb("install", "-r", self.apk, timeout=300)
            log(f"install: {(r.stdout.strip().splitlines() or [r.stderr.strip()])[-1]}")
        enabled = self.adb("shell", "settings", "get", "secure", "enabled_accessibility_services").stdout.strip()
        if SERVICE not in enabled:
            value = SERVICE if enabled in ("", "null") else f"{enabled}:{SERVICE}"
            self.adb("shell", "settings", "put", "secure", "enabled_accessibility_services", value)
            self.adb("shell", "settings", "put", "secure", "accessibility_enabled", "1")
            time.sleep(3)
            enabled = self.adb("shell", "settings", "get", "secure", "enabled_accessibility_services").stdout.strip()
            if SERVICE not in enabled:
                raise RuntimeError("the accessibility service could not be turned on over adb; turn it on by hand on the device")
        self.adb("reverse", f"tcp:{self.port}", f"tcp:{self.port}")
        self.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        line = self.config()
        m = re.search(r"guide=(\S+)", line)
        if not m:
            raise RuntimeError("the app did not answer DEBUG_CONFIG (is its accessibility service on?)")
        mine = f"http://127.0.0.1:{self.port}"
        if m.group(1) != mine:
            self.before = m.group(1)
        line = self.config(guide_base=mine)
        if f"guide={mine}" not in line:
            raise RuntimeError(f"the app did not take the authoring server as its guide server: {line}")
        log(f"device {self.serial} ({self.model()}): app points at {mine} (was {self.before or mine})")

    def restore(self, log) -> None:
        if self.before:
            line = self.config(guide_base=self.before)
            log(f"device {self.serial}: app points back at {self.before}" if f"guide={self.before}" in line else f"device {self.serial}: could not restore the guide server ({line})")

    def reset(self, start: str, packages: set[str]) -> None:
        """Bring the device to a start state. The guide session of the last run is ended first."""
        self.adb("shell", "am", "broadcast", "-a", f"{PKG}.DEBUG_STOP", "-p", PKG)
        if start == "cold":
            home = self.home_package()
            if not home:
                raise RuntimeError("could not tell which app is the home screen; not stopping any app")
            keep = {PKG, "com.android.systemui", home, ""}
            for p in sorted((packages | self.recent_packages()) - keep):
                self.adb("shell", "am", "force-stop", p)
        time.sleep(0.8)


# ---------------------------------------------------------------- one authoring job

class Author:
    def __init__(self, manuals: Manuals, guide: Guide, port: int, runs_dir: Path):
        self.manuals, self.guide, self.port, self.runs_dir = manuals, guide, port, runs_dir
        self.busy = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def run(self, job: dict) -> dict:
        """Author one manual on one device. Fills job["log"] and job["report"]; returns the report."""
        def log(msg: str) -> None:
            line = f"[{time.strftime('%H:%M:%S')}] {msg}"
            job["log"].append(line)
            print(f"[author {job['id']}] {msg}", flush=True)

        task, starts, rounds = job["task"], job["starts"], job["rounds"]
        dev = Device(job["serial"], self.port, job.get("apk", ""))
        dev.check()
        self.manuals.reload()
        manual = (self.manuals.get(job["manual"]) if job.get("manual") else None) or self.manuals.match(task, verified_only=False)
        if manual is None:
            manual = self.manuals.create(task, job.get("manual"))
            log(f"new manual {manual['id']}: {manual['task']}")
        else:
            log(f"manual {manual['id']} ({manual.get('status')}, {len(manual['steps'])} step(s)): {manual['task']}")
        mid = manual["id"]
        job["manual"] = mid
        out = self.runs_dir / f"{mid}-{job['id']}"
        report = {"manual": mid, "task": manual["task"], "device": None, "serial": dev.serial, "starts": {}, "runs": [], "status": "draft"}
        job["report"] = report
        dev.prepare(log)
        device_model = dev.model()
        report["device"] = self.guide.device_model = device_model
        self.guide.record_dir = out / "sessions"
        if job.get("fresh"):
            log(f"dropped {self.manuals.forget(mid, device_model)} earlier step(s) of {device_model}")
        # The apps this manual goes through, as far as its steps say: a cold start stops them first.
        packages: set[str] = _foregrounds(manual["steps"])
        saved_after_cold = False
        try:
            for start in starts:
                clean, failures = False, 0
                for n in range(1, rounds + 1):
                    run = self._once(dev, manual["task"], start, packages, out / f"{start}-{n}", log)
                    report["runs"].append(run)
                    packages |= set(run["packages"])
                    if run["completed"] and run["manual_turns"] == 0 and run["model_turns"] > 0:
                        m = self.manuals.get(mid)
                        if not m.get("first_run"):  # the way artemis alone did it, kept for comparison
                            m["first_run"] = _brief(run)
                            self.manuals.save(m)
                    if run["new_steps"] and start != "cold":
                        saved_after_cold = True
                    if run["clean"]:
                        clean = True
                        break
                    if not run["completed"]:
                        failures += 1
                        if failures >= 2:
                            log(f"{start}: two runs did not reach the end; leaving this start state")
                            break
                report["starts"][start] = {"clean": clean, "runs": n}
            # Steps saved for the other start states are new to the cold path: it must still go through untouched.
            if saved_after_cold and "cold" in starts and report["starts"]["cold"]["clean"]:
                run = self._once(dev, manual["task"], "cold", packages, out / "cold-final", log)
                report["runs"].append(run)
                report["starts"]["cold"]["clean"] = run["clean"]
        finally:
            dev.restore(log)

        ok = bool(report["starts"]) and all(s["clean"] for s in report["starts"].values())
        m = self.manuals.get(mid)
        first = m.get("first_run")
        last = next((r for r in reversed(report["runs"]) if r["clean"] and r["start"] == "cold"), None) or \
            next((r for r in reversed(report["runs"]) if r["clean"]), None)
        m["status"] = report["status"] = "verified" if ok else "draft"
        m["verified"] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "device_model": device_model, "starts": report["starts"], "runs": len(report["runs"]),
            "model_turns_total": sum(r["model_turns"] for r in report["runs"]),
            "clean_run": _brief(last),
        }
        self.manuals.save(m)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"manual {mid}: {m['status']}, {len(m['steps'])} step(s). " + ", ".join(f"{k} {'ok' if v['clean'] else 'NOT clean'} ({v['runs']} run(s))" for k, v in report["starts"].items()))
        if first and last:
            log(f"artemis alone: {first['seconds']}s with {first['model_turns']} model turn(s) → from the manual ({last['start']}): {last['seconds']}s with 0")
        return report

    def _once(self, dev: Device, task: str, start: str, packages: set[str], out: Path, log) -> dict:
        """One run from one start state: the stand-in human follows the guide to the end, then the session is settled."""
        cmd = [sys.executable, "-X", "utf8", "-I", str(HERE / "human.py"), task, "--serial", dev.serial, "--out", str(out),
               "--timeout", "300", "--max-steps", "25"] + ([] if start == "done" else ["--home"])
        code, text, sid, seconds = 1, "", None, 0.0
        for attempt in (1, 2, 3):
            dev.reset(start, packages)
            before = self.guide.last_sid
            t0 = time.time()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    env={**os.environ, "ADB": ADB})
            while proc.poll() is None and self.guide.last_sid == before and time.time() - t0 < START_WAIT_S:
                time.sleep(0.5)
            if proc.poll() is None and self.guide.last_sid == before:
                # Seen once: the app took the start but never asked for a guide. Nothing was guided, so start over.
                proc.kill()
                proc.wait()
                log(f"{start}: the app sent no request within {START_WAIT_S}s of the start (attempt {attempt}); starting this run again")
                continue
            text = proc.communicate()[0] or ""
            code, seconds = proc.returncode, round(time.time() - t0, 1)
            sid = self.guide.last_sid if self.guide.last_sid != before else None
            break
        s = self.guide.summary(sid) or {"completed": False, "manual_turns": 0, "model_turns": 0, "events": [], "dropped": [], "packages": [],
                                        "manual_latency_s": [], "model_latency_s": []}
        learned = self.guide.register(sid) if sid and s["completed"] and s["model_turns"] > 0 else {"new": 0, "not_saved": []}
        completed = bool(s["completed"]) and code == 0
        run = {
            "start": start, "session": sid, "seconds": seconds, "completed": completed,
            "manual_turns": s["manual_turns"], "model_turns": s["model_turns"], "waits": s.get("waits", 0),
            "clean": completed and s["model_turns"] == 0 and not s["dropped"],
            "new_steps": learned["new"], "not_saved": learned["not_saved"], "dropped": s["dropped"],
            "manual_latency_s": s["manual_latency_s"], "model_latency_s": s["model_latency_s"],
            "events": s["events"], "packages": s["packages"], "out": str(out),
        }
        tail = text.strip().splitlines()[-1:] or ["no output"]
        log(f"{start}: {'reached the end' if completed else 'did NOT reach the end'} in {seconds}s, manual {run['manual_turns']} / model {run['model_turns']} turn(s)"
            + (f", saved {run['new_steps']} new step(s)" if run["new_steps"] else "")
            + (f", not saved: {run['not_saved']}" if run["not_saved"] else "")
            + (f", removed {len(run['dropped'])} step(s)" if run["dropped"] else "")
            + ("" if completed else f" | human: {tail[0]}"))
        return run

    # ---------------------------------------------------------------- jobs over HTTP

    def start(self, body: dict) -> tuple[int, dict]:
        task = str(body.get("task") or "").strip()
        if not task:
            return 400, {"error": "task is required"}
        starts = body.get("starts") or list(STARTS)
        if any(s not in STARTS for s in starts):
            return 400, {"error": f"starts must be among {list(STARTS)}"}
        if not self.busy.acquire(blocking=False):
            return 409, {"error": "another authoring job is running on the device"}
        job = self.new_job(task, body.get("id"), str(body.get("serial") or os.environ.get("DEVICE_SERIAL", DEFAULT_SERIAL)),
                           starts, int(body.get("rounds") or 3), bool(body.get("fresh")), os.environ.get("APP_APK", ""))

        def work() -> None:
            try:
                self.run(job)
                job["status"] = "done"
            except Exception as exc:  # noqa: BLE001
                job["status"], job["error"] = "failed", str(exc)
                job["log"].append(f"failed: {exc}")
                print(f"[author {job['id']}] failed: {exc!r}", flush=True)
            finally:
                self.busy.release()

        threading.Thread(target=work, daemon=True).start()
        return 202, {"job": job["id"], "status": "running"}

    def new_job(self, task: str, manual: str | None, serial: str, starts: list[str], rounds: int, fresh: bool, apk: str) -> dict:
        job = {"id": time.strftime("%m%d-%H%M%S"), "task": task, "manual": manual, "serial": serial, "starts": list(starts),
               "rounds": rounds, "fresh": fresh, "apk": apk, "status": "running", "log": [], "report": None}
        self.jobs[job["id"]] = job
        return job

    def http(self, method: str, path: str, body):
        if method == "POST" and path == "/author":
            return self.start(body if isinstance(body, dict) else {})
        if method == "GET" and path == "/author":
            return 200, [{"job": j["id"], "task": j["task"], "manual": j["manual"], "status": j["status"]} for j in self.jobs.values()]
        if method == "GET" and path.startswith("/author/"):
            j = self.jobs.get(path.split("/")[2])
            return (200, {k: v for k, v in j.items() if k != "apk"}) if j else (404, {"error": "no such job"})
        if method == "DELETE" and re.fullmatch(r"/manuals/[^/]+/steps", path):
            return 200, {"dropped": self.manuals.forget(path.split("/")[2])}
        return None


def _foregrounds(steps: list[dict]) -> set[str]:
    out: set[str] = set()

    def walk(c) -> None:
        if isinstance(c, dict):
            for k, v in c.items():
                if k == "foreground" and isinstance(v, str):
                    out.add(v)
                else:
                    walk(v)
        elif isinstance(c, list):
            for x in c:
                walk(x)

    for s in steps:
        walk(s.get("screen_condition"))
    return out


def _brief(run: dict | None) -> dict | None:
    if run is None:
        return None
    return {"start": run["start"], "seconds": run["seconds"], "manual_turns": run["manual_turns"], "model_turns": run["model_turns"]}


# ---------------------------------------------------------------- command line

def main() -> int:
    ap = argparse.ArgumentParser(description="매뉴얼 저작 서버")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="저작 서버를 띄운다 (POST /author)")
    r = sub.add_parser("run", help="매뉴얼 하나를 저작하고 끝낸다")
    r.add_argument("task", help="요청 문장")
    r.add_argument("--id", default=None, help="매뉴얼 id (없으면 무작위)")
    r.add_argument("--serial", default=os.environ.get("DEVICE_SERIAL", DEFAULT_SERIAL))
    r.add_argument("--starts", default=",".join(STARTS), help="시작 상태: cold,warm,done 가운데")
    r.add_argument("--rounds", type=int, default=3, help="시작 상태마다 최대 몇 번 돌릴지")
    r.add_argument("--fresh", action="store_true", help="이 기기의 예전 규칙을 지우고 처음부터")
    r.add_argument("--apk", default=os.environ.get("APP_APK", ""), help="앱이 없을 때 설치할 APK")
    a = ap.parse_args()

    host = os.environ.get("AUTHOR_HOST", "127.0.0.1")
    port = int(os.environ.get("AUTHOR_PORT", "8780"))
    manuals = Manuals(os.environ.get("MANUALS_DIR", HERE.parent / "manuals"))
    guide = Guide(manuals, os.environ.get("ARTEMIS_URL", "http://127.0.0.1:8765"), authoring=True)
    author = Author(manuals, guide, port, Path(os.environ.get("RUNS_DIR", HERE.parent / "runs")))
    server = make_server(host, port, "author", guide, manuals, extra=author.http)
    print(f"[author] http://{host}:{port}  fallback: {guide.artemis_url}  manuals: {manuals.folder}", flush=True)
    if a.cmd == "serve":
        server.serve_forever()
        return 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    starts = [s.strip() for s in a.starts.split(",") if s.strip()]
    if any(s not in STARTS for s in starts):
        ap.error(f"--starts must be among {','.join(STARTS)}")
    job = author.new_job(a.task, a.id, a.serial, starts, a.rounds, a.fresh, a.apk)
    try:
        report = author.run(job)
    except RuntimeError as exc:
        print(f"[author] {exc}", flush=True)
        return 2
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
