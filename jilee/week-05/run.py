"""터미널 실행: python run.py KR0001 P001 1 [--confirm]   (고객코드 프로젝트코드 회차)"""
import sys

from agent.loop import run_agent


def main() -> None:
    if len(sys.argv) < 4:
        print("사용법: python run.py <고객코드> <프로젝트코드> <회차>")
        sys.exit(1)
    customer_id, project_id, meeting_no = sys.argv[1], sys.argv[2], int(sys.argv[3])

    def show(entry: dict) -> None:
        if entry["type"] == "tool":
            mark = "OK" if entry["ok"] else "FAIL"
            print(f"[{entry['step']}] tool {entry['name']}({entry['args']}) -> {mark}")
            if not entry["ok"]:
                print("    " + entry["output_preview"].replace("\n", "\n    "))
        elif entry["type"] == "route":
            print(f"[라우팅] {entry['content']}")
        elif entry["type"] in ("nudge", "info"):
            print(f"[{entry['step']}] {entry['content']}")
        else:
            print(f"[{entry['step']}] 최종 보고:\n{entry['content']}")

    r = run_agent(customer_id, project_id, meeting_no, on_step=show)
    if r.saved_path:
        print("\n" + r.saved_path)
        if "--confirm" in sys.argv:  # 여러 회차를 이어 돌릴 때: 회의록을 바로 확정해 다음 회차의 문을 연다
            from agent import tools
            g = tools.confirm_minutes(customer_id, project_id, meeting_no, "CLI")
            print(f"회의록 확정 → {g['state']} ({g['todo']})")
        else:
            print("회의록은 초안입니다. 화면에서 확정하거나 --confirm 을 붙여 실행하세요.")


if __name__ == "__main__":
    main()
