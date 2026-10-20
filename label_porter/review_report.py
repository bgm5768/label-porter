"""Standalone, offline review pages with complete comparison evidence."""
import html
import json

from .formats import atomic_text


def dump(value):
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2), quote=True)


def render_review(review_id, detail, *, back="../report.html", download=True):
    from .reports import STATUS
    e = lambda value: html.escape(str(value), quote=True)
    issues = detail.get("issues", [])
    findings = []
    for number, issue in enumerate(issues, 1):
        evidence = {k: v for k, v in issue.items() if k not in ("message", "code", "source_path")}
        comparisons = []
        for key, label in (("source", "기준값 / 원본"), ("platform", "플랫폼 최종값"), ("expected", "반입 당시 값"), ("comparison", "조건 검사값")):
            value = issue.get(key)
            if isinstance(value, dict) and "raw_present" in value:
                raw = dump(value.get("raw")) if value["raw_present"] else "필드 없음 / 읽기 실패"
                evaluated = dump(value.get("evaluated")) if value["evaluated_present"] else e(value.get("evaluated_error", "계산 실패"))
                comparisons.append(f'<tr><th>{label}</th><td><pre>{raw}</pre></td><td><pre>{evaluated}</pre></td></tr>')
        values = (f'<table><thead><tr><th>비교 대상</th><th>원래 값</th><th>변환 후 검사값</th></tr></thead><tbody>{"".join(comparisons)}</tbody></table>' if comparisons else "")
        for key, label in (("item_id", "연결 ID"), ("source_item", "원본 항목"), ("result_item", "플랫폼 최종 항목"),
                           ("previous_item", "이전 구간 후보"), ("candidate_item", "현재 항목 후보"), ("overlap", "겹치는 시간"), ("duration", "전체 길이")):
            if key in issue:
                values += f'<div class="value"><b>{label}</b><pre>{dump(issue[key])}</pre></div>'
        findings.append(f'''<article><div class="issue-head"><b>{number}. {e(issue['message'])}</b>
<code>{e(issue.get('source_path') or '레코드 전체')}</code></div>
{values}<details><summary>검사식·전체 근거 펼치기</summary><pre>{dump(evidence)}</pre></details></article>''')
    panes = []
    for key, title, caption in (
        ("source_record", "원본 JSON", "보완 전 원본 레코드"),
        ("platform_record", "보완 데이터 JSON", "플랫폼 또는 별도로 보완한 레코드 · 수정값 출처"),
        ("final_record", "실제 출력 JSON", "미리보기에서는 데이터 파일을 생성하지 않습니다." if detail.get("preview") else "실제 출력값입니다. 전체 반영을 보류한 레코드는 원본 그대로입니다."),
        ("candidate_record", "반영 예정 / 후보 JSON", detail.get("candidate_note", "실제 저장값이 아닙니다.")),
    ):
        if key in detail:
            panes.append(f'<section><h2>{title}</h2><p class="muted">{e(caption)}</p><pre>{dump(detail[key])}</pre></section>')
    if "proposed_changes" in detail:
        panes.append(f'<section><h2>계산 가능한 변경 후보 · 저장되지 않음</h2><pre>{dump(detail["proposed_changes"])}</pre></section>')
    # Index/parse errors may have several ambiguous records, or only raw text.
    extra = {k: v for k, v in detail.items() if k not in {
        "issues", "source_record", "platform_record", "final_record", "candidate_record", "candidate_note",
        "changes", "proposed_changes", "source_file", "record_path", "key", "result_file", "result_row", "status", "review_id"}}
    if extra:
        panes.append(f'<section><h2>추가 연결·입력 정보</h2><pre>{dump(extra)}</pre></section>')
    title = "검토 항목" if issues else "레코드 처리 상세"
    download_link = f'<a href="{review_id}.json">전체 근거 JSON</a>' if download else "전체 원문은 보고서의 레코드 상세 JSONL에서도 확인할 수 있습니다."
    changes = detail.get("changes", [])
    change_rows = "".join(f'<tr><td><code>{e(c["path"])}</code></td><td><pre>{dump(c.get("before"))}</pre></td><td><pre>{dump(c.get("after"))}</pre></td></tr>' for c in changes)
    change_table = f'<h2>변경 필드 {len(changes):,}개</h2><table><thead><tr><th>필드 경로</th><th>변경 전</th><th>변경 후</th></tr></thead><tbody>{change_rows}</tbody></table>' if changes else ""
    findings_section = f'<h2>검토 사유 {len(issues):,}개</h2>' + "".join(findings) if issues else ""
    body = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} {e(review_id)} · {e(detail.get('source_file') or detail.get('key', ''))}</title>
<style>body{{margin:0;background:#f5f6f8;color:#20242c;font:14px/1.7 'Malgun Gothic',sans-serif}}
main{{max-width:1400px;margin:auto;padding:32px}}a{{color:#285cc7}}h1{{font-size:24px}}h2{{font-size:17px}}
.muted{{color:#677181}}.meta{{background:#e9edf3;padding:16px;border-radius:10px;overflow-wrap:anywhere}}
article,section{{background:white;border:1px solid #dfe4eb;border-radius:10px;padding:20px;margin:16px 0;min-width:0}}
.issue-head{{display:flex;flex-direction:column;gap:6px}}code{{color:#7b4c14;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #e3e7ed;padding:8px;overflow-wrap:anywhere}}summary{{cursor:pointer;color:#285cc7;padding:10px 0}}
pre{{font:13px/1.65 Consolas,'Malgun Gothic',monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f8fa;padding:14px;max-height:650px;overflow:auto}}
.comparison{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}.comparison section{{margin:0}}
@media(max-width:800px){{.comparison{{grid-template-columns:1fr}}main{{padding:16px}}}}
</style><main><a id="back-to-list" href="{back}">← 처리 결과서 목록</a><h1>{title} {e(review_id)}</h1>
<div class="meta"><b>처리 상태: {e(STATUS.get(detail.get("status"), detail.get("status", "")))}</b><br>원본: {e(detail.get('source_file',''))} · {e(detail.get('record_path',''))}<br>
연결키: {e(detail.get('key',''))}<br>결과: {e(detail.get('result_file',''))} · 레코드 {e(detail.get('result_row',''))}<br>
{download_link}</div>
<p>{e(detail.get("reason", ""))}</p>{change_table}{findings_section}<h2>원본 · 보완 데이터 · 처리 결과</h2>
<div class="comparison">{''.join(panes)}</div></main>
<script type="text/javascript">document.getElementById('back-to-list').href = '{back}' + location.hash;</script></html>'''
    return body


def write_review(directory, review_id, detail):
    body = render_review(review_id, detail)
    atomic_text(directory / "review" / f"{review_id}.json", json.dumps(detail, ensure_ascii=False, indent=2))
    atomic_text(directory / "review" / f"{review_id}.html", body)
