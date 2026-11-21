"""앞단 서버: 앱의 안내 요청을 검증된 매뉴얼로 먼저 답하고, 답하지 못하면 artemis 서버에 넘긴다.

    python manual/front.py            # http://127.0.0.1:8770

앱은 안내 서버 주소만 이쪽으로 바꾼다. 요청과 응답의 형식은 artemis 서버와 같다(docs/interface.md).
매뉴얼은 `MANUALS_DIR`의 JSON 파일이고, 그 가운데 status가 verified인 것만 쓴다. 파일이 바뀌면 다시 읽는다.
여기서는 아무것도 등록하지 않는다. 규칙을 만드는 일은 저작 서버(author.py)가 에뮬레이터에서 한다.

환경 변수: FRONT_HOST(127.0.0.1), FRONT_PORT(8770), ARTEMIS_URL(http://127.0.0.1:8765), MANUALS_DIR(../manuals),
RECORD_DIR(없으면 기록하지 않음).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from guide import Guide  # noqa: E402
from server import make_server  # noqa: E402
from store import Manuals  # noqa: E402


def main() -> None:
    host = os.environ.get("FRONT_HOST", "127.0.0.1")
    port = int(os.environ.get("FRONT_PORT", "8770"))
    manuals = Manuals(os.environ.get("MANUALS_DIR", HERE.parent / "manuals"))
    guide = Guide(manuals, os.environ.get("ARTEMIS_URL", "http://127.0.0.1:8765"), authoring=False,
                  record_dir=os.environ.get("RECORD_DIR") or None)
    verified = [m["id"] for m in manuals.all() if m.get("status") == "verified"]
    print(f"[front] http://{host}:{port}  manuals: {', '.join(verified) or 'none verified'}  fallback: {guide.artemis_url}", flush=True)
    make_server(host, port, "front", guide, manuals).serve_forever()


if __name__ == "__main__":
    main()
