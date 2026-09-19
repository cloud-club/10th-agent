"""로컬 테스트 앱: python app.py → http://localhost:8765

표준 라이브러리 http.server만 사용한다. 화면에서 딜·회차를 고르고 녹취를 붙여넣어
에이전트를 실행하면 도구 호출 과정과 생성된 회의록을 그대로 보여준다.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.loop import run_agent
from agent.tools import DEALS_DIR, OUTPUT_DIR, list_deals, read_transcript

ROOT = Path(__file__).resolve().parent
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 요청 로그를 조용히
        pass

    # ---- 응답 도우미 ----
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    # ---- 라우팅 ----
    def do_GET(self):
        if self.path == "/":
            self._send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/deals":
            self._json(list_deals())
        elif self.path.startswith("/api/transcript"):
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&"))
            try:
                self._json({"text": read_transcript(q["deal_id"], int(q["meeting_no"]))})
            except Exception as e:
                self._json({"error": str(e)}, 404)
        elif self.path.startswith("/api/minutes"):
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&"))
            p = OUTPUT_DIR / q["deal_id"] / f"minutes_{int(q['meeting_no']):02d}.md"
            if p.exists():
                self._json({"markdown": p.read_text(encoding="utf-8")})
            else:
                self._json({"error": "아직 생성된 회의록이 없습니다."}, 404)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode("utf-8"))
        deal_id, meeting_no = req["deal_id"], int(req["meeting_no"])

        # 화면에서 수정한 녹취가 있으면 파일에 반영한 뒤 실행한다(에이전트는 항상 파일을 읽는다)
        if req.get("transcript") is not None:
            (DEALS_DIR / deal_id / f"transcript_{meeting_no:02d}.txt").write_text(req["transcript"], encoding="utf-8")

        # 진행 상황을 단계마다 흘려보낸다(Server-Sent Events). 화면은 이 줄을 받는 즉시 그린다.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        emit({"type": "start", "message": "에이전트 시작 — 모델에 첫 요청을 보냈습니다"})
        try:
            r = run_agent(deal_id, meeting_no, on_step=lambda e: emit({"type": "step", "entry": e}))
        except Exception as e:
            emit({"type": "error", "message": str(e)})
            return
        p = OUTPUT_DIR / deal_id / f"minutes_{meeting_no:02d}.md"
        emit({
            "type": "done",
            "final_answer": r.final_answer,
            "saved_path": r.saved_path,
            "markdown": p.read_text(encoding="utf-8") if p.exists() else "",
        })


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"회의록 에이전트 로컬 앱: {url}  (종료: Ctrl+C)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
