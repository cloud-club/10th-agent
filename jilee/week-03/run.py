"""터미널 실행: python run.py D001 1   (비교 실험: python run.py D001 1 --no-knowledge)"""
import sys

from agent.loop import run_agent


def main() -> None:
    if len(sys.argv) < 3:
        print("사용법: python run.py <딜코드> <회차>")
        sys.exit(1)
    deal_id, meeting_no = sys.argv[1], int(sys.argv[2])

    def show(entry: dict) -> None:
        if entry["type"] == "tool":
            mark = "OK" if entry["ok"] else "FAIL"
            print(f"[{entry['step']}] tool {entry['name']}({entry['args']}) -> {mark}")
        elif entry["type"] == "route":
            print(f"[라우팅] {entry['content']}")
        elif entry["type"] == "nudge":
            print(f"[{entry['step']}] {entry['content']}")
        else:
            print(f"[{entry['step']}] 최종 답변:\n{entry['content']}")

    use_knowledge = "--no-knowledge" not in sys.argv
    if not use_knowledge:
        # 비교 실험 산출물은 따로 둔다(정식 회의록을 덮어쓰지 않게). 직전 회의록도 이 폴더에서 읽는다.
        from agent import tools
        tools.OUTPUT_DIR = tools.ROOT / "experiments" / "no_knowledge"
        print("[비교 실험] 지식베이스 없이 실행 → experiments/no_knowledge/")
    r = run_agent(deal_id, meeting_no, on_step=show, use_knowledge=use_knowledge)
    if r.saved_path:
        print("\n" + r.saved_path)


if __name__ == "__main__":
    main()
