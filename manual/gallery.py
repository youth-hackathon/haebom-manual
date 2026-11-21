"""안내를 따라간 실행들의 스크린샷을 한 페이지로 모은다.

    python manual/gallery.py                 # runs/gallery/index.html
    python manual/gallery.py runs/front-1    # 고른 실행만

사람 역할(human.py)은 안내가 뜰 때마다 화면을 찍어 그 실행의 폴더에 stepNN.png로 남기고, 무엇을 봤고 무엇을 했는지를
transcript.txt에 남긴다. 이 스크립트는 그 둘을 읽어 실행마다 단계 순서대로 늘어놓는다. 그림은 복사하지 않고 제자리의 것을 가리킨다.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE.parent / "runs"
OUT = RUNS / "gallery"

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>해봄 안내 스크린샷</title>
<style>
  :root {{ color-scheme: light dark; --line: #8884; --dim: #6b7280; --fast: #0f9d6c; --slow: #b45309; }}
  body {{ font: 15px/1.5 system-ui, "Malgun Gothic", sans-serif; margin: 0; padding: 20px 24px 60px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .lead {{ color: var(--dim); margin: 0 0 22px; max-width: 70ch; }}
  nav {{ display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 0 0 26px; }}
  section {{ border-top: 1px solid var(--line); padding: 18px 0 8px; }}
  h2 {{ font-size: 17px; margin: 0; }}
  .meta {{ color: var(--dim); margin: 2px 0 12px; }}
  .steps {{ display: flex; gap: 14px; overflow-x: auto; padding-bottom: 10px; }}
  figure {{ margin: 0; flex: 0 0 216px; }}
  figure img {{ width: 216px; height: 480px; object-fit: cover; display: block; border-radius: 10px; border: 1px solid var(--line); background: #0001; }}
  figcaption {{ padding: 6px 2px 0; font-size: 13px; }}
  .what {{ font-weight: 600; }}
  .took {{ color: var(--dim); font-variant-numeric: tabular-nums; }}
  .fast {{ color: var(--fast); }} .slow {{ color: var(--slow); }}
</style></head><body>
<h1>해봄 안내 스크린샷</h1>
<p class="lead">사람 역할 스크립트가 안내를 따라가며 단계마다 찍은 화면입니다. 그림 아래의 시간은 그 단계의 안내를 서버가 답하는 데 걸린 시간입니다.
<span class="fast">수십 ms</span>는 매뉴얼의 규칙이 답한 것이고 <span class="slow">수 초</span>는 artemis가 답한 것입니다. 그림을 누르면 원래 크기로 열립니다.</p>
<nav>{nav}</nav>
{sections}
</body></html>
"""


def read_run(folder: Path) -> dict | None:
    """One followed run: its screenshots in order, each with what the guide said and how long the answer took."""
    shots = sorted(folder.glob("step*.png"))
    if not shots:
        return None
    transcript = folder / "transcript.txt"
    lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines() if transcript.exists() else []
    titles: dict[int, str] = {}
    took: dict[int, int] = {}
    task = ""
    for line in lines:
        m = re.search(r"ARM step=(\d+) verb=\S+ src=\S+ title=(.*?)(?: text=| key=|$)", line)
        if m:
            titles[int(m.group(1))] = m.group(2).strip()
        m = re.search(r"\] step=(\d+) action=(\S+) .*-> \w+ \((\d+)ms\)", line)
        if m:
            took[int(m.group(1))] = int(m.group(3))
    end = next((l for l in reversed(lines) if l.startswith(("done in", "stopped after"))), "")
    if not end:
        last = next((l for l in reversed(lines) if "-> Terminal" in l), "")
        end = "끝까지 감" if last else ""
    steps = []
    for p in shots:
        m = re.match(r"step(\d+)(_again)?", p.stem)
        n = int(m.group(1))
        steps.append({"file": p, "n": n, "again": bool(m.group(2)), "title": titles.get(n, ""), "ms": took.get(n)})
    final = folder / "final.png"
    return {"folder": folder, "steps": steps, "final": final if final.exists() else None, "end": end, "task": task}


def section(run: dict, idx: int) -> str:
    rel = run["folder"].relative_to(RUNS).as_posix()
    figs = []
    for s in run["steps"]:
        src = "../" + s["file"].relative_to(RUNS).as_posix()
        ms = s["ms"]
        took = "" if ms is None or s["again"] else f'<span class="took {"fast" if ms < 1000 else "slow"}">{ms}ms</span>'
        what = html.escape(s["title"] or "안내") + (" (다시 따라 함)" if s["again"] else "")
        figs.append(f'<figure><a href="{src}"><img loading="lazy" src="{src}" alt="{html.escape(what)}"></a>'
                    f'<figcaption><span class="what">{s["n"]}. {what}</span><br>{took}</figcaption></figure>')
    if run["final"]:
        src = "../" + run["final"].relative_to(RUNS).as_posix()
        figs.append(f'<figure><a href="{src}"><img loading="lazy" src="{src}" alt="끝난 화면"></a>'
                    f'<figcaption><span class="what">끝난 화면</span></figcaption></figure>')
    return (f'<section id="r{idx}"><h2>{html.escape(rel)}</h2><p class="meta">{html.escape(run["end"])}</p>'
            f'<div class="steps">{"".join(figs)}</div></section>')


def main() -> int:
    picked = [Path(a).resolve() for a in sys.argv[1:]]
    folders = picked or sorted({p.parent for p in RUNS.rglob("step*.png")}, key=lambda p: p.stat().st_mtime)
    runs = [r for r in (read_run(f) for f in folders) if r]
    if not runs:
        print("스크린샷이 있는 실행이 없습니다.")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    nav = "".join(f'<a href="#r{i}">{html.escape(r["folder"].relative_to(RUNS).as_posix())}</a>' for i, r in enumerate(runs))
    page = PAGE.format(nav=nav, sections="\n".join(section(r, i) for i, r in enumerate(runs)))
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(f"{len(runs)}개 실행, 그림 {sum(len(r['steps']) for r in runs)}장 → {OUT / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
