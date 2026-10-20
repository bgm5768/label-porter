from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="라벨 포터: 원본을 보존하는 파일 기반 라벨 반영")
    sub = parser.add_subparsers(dest="command")
    cmd = sub.add_parser("run", help="명령행에서 반영 또는 미리보기")
    cmd.add_argument("--source", required=True, help="원본 폴더")
    cmd.add_argument("--result", nargs="+", required=True, help="어드민 다운로드 JSON/CSV 파일")
    cmd.add_argument("--output", required=True, help="새 실행 폴더를 만들 상위 폴더")
    cmd.add_argument("--config", required=True, help="프로젝트 규칙 JSON")
    cmd.add_argument("--preview", action="store_true", help="검증 보고서만 생성; 데이터 복사/수정 없음")
    cmd.add_argument("--no-split", action="store_true", help="변경/미변경 분리본 없이 전체 data만 생성")
    check = sub.add_parser("validate-config", help="설정 문법 검사")
    check.add_argument("config")
    sub.add_parser("gui", help="데스크톱 화면 실행")
    args = parser.parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from .gui import main as gui_main
            gui_main()
            return 0
        from .config import load_config
        if args.command == "validate-config":
            print(json.dumps({"valid": True, "name": load_config(Path(args.config))["name"]}, ensure_ascii=False))
            return 0
        from .engine import run
        def progress(event):
            print(event["message"], file=sys.stderr, flush=True)
        result = run(args.source, args.result, args.output, load_config(Path(args.config)), args.preview, progress, split_output=not args.no_split)
        print(json.dumps(result.summary, ensure_ascii=False, indent=2))
        # 0: clean; 2: finished with review issues; 1: failed/cancelled.
        return 0 if result.summary["status"] == "completed" else (2 if result.summary["status"] == "completed_with_issues" else 1)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
