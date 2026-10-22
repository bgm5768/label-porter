"""An offline processing receipt: one shared searchable, paginated catalogue."""
import html
import json

from .detail_store import script_json
from .formats import atomic_text
from .examine import CATEGORIES as EXAMINE_CATEGORIES
from .report_paging import CONTROLS, PAGER, SCRIPT, STYLE


CATEGORIES = {
    "output": ("작업 결과 레코드", "원본을 기준으로 반영·보존된 레코드"),
    "completed": ("처리 완료", "검토 사유 없이 반영 완료 또는 변경 없음"),
    "review": ("검토 / 오류", "검증 실패·중복·입력 오류·별도 검토"),
    "unmatched": ("짝이 없는 데이터", "보완 결과가 없는 원본 + 원본이 없는 보완 결과"),
    "changes": ("변경 필드", "필드 경로별 변경 전후 값 · 목록 교체는 1개"),
    "source": ("원본 레코드", "처리 상태가 확인된 원본의 레코드"),
    "result": ("보완 레코드", "플랫폼 / 별도로 보완한 입력의 레코드"),
    "excluded": ("조건 제외", "규칙의 대상 조건에 해당하지 않아 보존"),
    "files": ("파일 단위 오류", "해석할 수 없어 레코드 수를 알 수 없는 파일"),
}

CSS = """
:root{color-scheme:light;--ink:#152b42;--muted:#63748a;--line:#dfe6ee;--bg:#f4f6fa;--accent:#244fc6}
*{box-sizing:border-box}body{margin:0;color:var(--ink);background:var(--bg);font:14px/1.6 'Malgun Gothic',sans-serif}
main{max-width:1480px;margin:auto;padding:36px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.eyebrow{font-size:12px;letter-spacing:.12em;color:var(--muted)}h1{font-size:30px;letter-spacing:-1px;margin:8px 0}h2{font-size:20px;margin:0 0 12px}p{margin:8px 0}
.hero{display:flex;justify-content:space-between;gap:20px;align-items:start}.muted,small{color:var(--muted)}
.badge{display:inline-block;padding:7px 12px;border-radius:8px;background:#e5f2ed;color:#246347;font-weight:bold;white-space:nowrap}
.warn{background:#fff0d4;color:#825b14}.error{background:#fbe4e4;color:#a53434}
.run-info{margin:18px 0 24px;padding:14px 18px;border:1px solid var(--line);border-radius:10px;background:#fff;display:flex;gap:24px;flex-wrap:wrap}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.metric{display:block;background:white;border:1px solid var(--line);border-radius:12px;padding:18px;color:var(--ink)}
.metric:hover,.metric[aria-current=true]{border-color:var(--accent);box-shadow:0 2px 9px #244fc612;text-decoration:none}
.metric strong{display:block;font-size:30px;line-height:1.3;margin:8px 0}.metric small{display:block;font-size:12px}.metric.attention strong{color:#9a6712}
.panel{background:white;border:1px solid var(--line);border-radius:12px;padding:24px;margin:22px 0}.section-head{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
.tabs{display:flex;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:18px}
.tabs a{border:1px solid var(--line);border-radius:7px;padding:8px 12px;color:var(--muted)}.tabs a[aria-current=true]{color:var(--accent);background:#eff3ff;border-color:#b8c8f4}
.filters{display:flex;gap:12px;flex-wrap:wrap}.filters label{display:flex;flex-direction:column;gap:5px}
.field-filter{flex:1;min-width:220px;max-width:620px}.field-filter select{width:100%;min-width:0}
select,input,button{font:inherit;color:inherit;border:1px solid #cbd4df;background:white;border-radius:6px;padding:7px 10px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:13px 12px;border-bottom:1px solid var(--line);overflow-wrap:anywhere;vertical-align:top}
th{background:#f6f8fb;color:#52657c;font-size:12px}tbody tr:hover{background:#fafbfe}
#review-table{min-width:1080px}#review-table th:nth-child(1){width:12%}#review-table th:nth-child(2),#review-table th:nth-child(3){width:16%}
#label-counts table{min-width:1000px}#label-counts td:first-child{max-width:300px}
#review-table th:nth-child(4){width:10%}#review-table th:nth-child(5){width:10%}#review-table th:nth-child(6){width:26%}#review-table th:nth-child(7){width:10%}
#review-table td:nth-child(2),#review-table td:nth-child(3){font-size:12px}#review-table td:nth-child(6){max-height:200px}
.notice{border-left:3px solid #cb9c36;padding:12px 16px;background:#fff9e9;margin:16px 0}.downloads{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.downloads a{padding:16px;border:1px solid var(--line);border-radius:8px}.downloads small{display:block;margin-top:6px}
summary{cursor:pointer;font-weight:bold;font-size:17px}details[open] summary{margin-bottom:18px}
.path{font:12px/1.6 Consolas,monospace;overflow-wrap:anywhere}.storage td:first-child{font-family:Consolas,monospace}
footer{color:var(--muted);font-size:12px;margin:24px 0}
@media(max-width:1000px){main{padding:20px}.cards{grid-template-columns:repeat(2,1fr)}.downloads{grid-template-columns:1fr 1fr}}
@media(max-width:600px){main{padding:12px}.hero{display:block}.cards,.downloads{grid-template-columns:1fr}.panel{padding:16px}}
"""


def label_count_panel(summary):
    e = lambda value: html.escape(str(value), quote=True)
    rows = summary.get("label_counts", [])
    if not rows:
        return '<section class="panel"><h2>라벨 수 집계</h2><p class="muted">이번 규칙에서 집계할 라벨 배열을 찾지 못했습니다. 문자열·숫자 필드와 레코드 수는 라벨 수로 세지 않습니다.</p></section>'

    def cell(counts):
        problems = [("경로 누락", counts["missing"]), ("배열 아님", counts["non_array"]),
                    ("자동 집계 불가", counts["unavailable"])]
        notes = " · ".join(f"{label} {n:,}" for label, n in problems if n)
        number = f'{counts["items"]:,}' if not notes or counts["arrays"] else "—"
        return f'<td><b>{number}</b><br><small>레코드 {counts["records"]:,}건{(" · 확인된 항목만 집계<br>" + notes) if notes else ""}</small></td>'

    body = ""
    for row in rows:
        count = row["counts"]
        delta = "확인 필요" if row["delta"] is None else f'{row["delta"]:+,}'
        path = e(row["path"])
        incoming = e(row["result_path"]) if row["result_path"] is not None else "변환·다중 연결·참조 규칙: 입력 배열 자동 집계 불가"
        body += f'<tr><td><b class="path">{path}</b><br><small class="path">보완: {incoming}</small></td>'
        body += "".join(cell(count[side]) for side in ("source", "result", "output", "completed", "examine"))
        body += f'<td><b>{delta}</b><br><small>처리 전 {count["output_before"]["items"]:,}개 기준</small></td></tr>'
    return f'''<section class="panel" id="label-counts"><div class="section-head"><h2>라벨 수 집계 · 목록별 항목 수</h2><a href="label_counts.csv">라벨 집계 CSV 열기 ↗</a></div>
<p>규칙이 다루는 배열의 항목을 1개씩 셉니다. 어절·발음 태그 등 서로 다른 종류와 상위·하위 목록은 합산하지 않습니다. 변경 필드 수와도 별개입니다.</p>
<div class="table-scroll"><table><thead><tr><th>라벨 목록 / 연결 경로</th><th>원본 입력</th><th>보완 입력</th><th>{'반영 예정' if summary['preview'] else '처리 후'}</th><th>정상 처리</th><th>examine 대상</th><th>증감</th></tr></thead><tbody>{body}</tbody></table></div>
<p class="muted">입력은 읽은 레코드 전체(짝 없음·중복·키 오류 포함), 처리 후는 처리 상태가 확정된 원본 레코드 기준입니다. 정상 처리 + examine 대상 = 처리 후입니다.
증감은 같은 처리 대상의 원본과 출력 항목 수 차이이며, 값이 바뀐 라벨 수가 아닙니다. 검토·조건 제외·짝 없는 원본은 보존된 값으로 셉니다.
{'미리보기는 검증 통과한 후보의 저장 예정 개수이며 파일을 저장하지 않습니다.' if summary['preview'] else '전체 data 복사본 기준이며 examine에 보관한 보완 입력을 출력에 더하지 않습니다.'}
빈 배열은 0개, 누락·잘못된 형식은 확인 필요로 표시합니다. 변환식 등 입력 경로를 확정할 수 없는 경우 보완 개수를 추측하지 않습니다.
파일 해석 실패는 집계에서 제외되며 중단·실패 시에는 확인된 부분만 표시합니다. 규칙에 없는 목록은 집계하지 않습니다.</p></section>'''


def write_dashboard(directory, summary, catalog, examples):
    from .reports import STATUS
    e = lambda value: html.escape(str(value), quote=True)
    counts = summary["category_counts"]
    preview = summary["preview"]
    rule_name = summary.get("rule", summary.get("profile", ""))
    rule_file = "profile.json" if not (directory.parent / "rule.json").is_file() and (directory.parent / "profile.json").is_file() else "rule.json"
    mode = "전체 처리 완료 · 임시 보관 (최종 저장 전)" if summary.get("prepared") else "검증 미리보기 · 데이터 파일 미생성" if preview else "복사본 저장 결과"
    state = {"completed": "처리 완료", "completed_with_issues": "처리 완료 · 확인 필요",
             "failed": "실패 · 부분 결과", "cancelled": "중단 · 부분 결과"}.get(summary["status"], "진행 중")
    badge = "error" if summary["status"] in ("failed", "cancelled") else "warn" if summary["status"] == "completed_with_issues" else ""
    cards = ""
    for group in ("source", "result", "output", "completed", "changes", "review", "unmatched", "files"):
        label, hint = CATEGORIES[group]
        number = summary["source_records"] if group == "source" else summary["result_records"] if group == "result" else counts.get(group, 0)
        if preview and group == "completed":
            label, hint = "검증 통과", "저장 전 예시 · 실제 반영은 복사본 저장 시 진행"
        cards += f'<a class="metric {"attention" if group in ("review","unmatched","files") else ""}" data-group="{group}" href="#group={group}">{label}<strong>{number:,}</strong><small>{hint}</small></a>'
    tabs = "".join(f'<a data-group="{key}" href="#group={key}">{label} <b>{counts.get(key,0):,}</b></a>' for key, (label, _) in CATEGORIES.items())
    options = "".join(f'<option value="{key}">{label}</option>' for key, (label, _) in CATEGORIES.items())
    statuses = "".join(f'<option value="{key}">{e(STATUS.get(key,key))}</option>' for key in sorted({r["status"] for r in catalog}))
    partition = summary.get("partition_records", {})
    files = summary.get("partition_files", {})
    partition_state = {"completed": "저장 완료", "planned": "분리 예정", "in_progress": "미완료", "pending": "미실행"}.get(summary.get("partition_status"), "미실행")
    storage = ""
    for folder, hint, count in (
        ("data/", "전체 복사본 · 검토로 보류한 레코드는 원본 유지", summary.get("output_records", 0)),
        ("changed/labels/", "정상 반영 후 값이 달라진 레코드", partition.get("changed", 0)),
        ("unchanged/labels/", "정상적으로 값이 같은 레코드", partition.get("unchanged", 0)),
        *[(f"examine/{category}/", label + " · 원본/보안은 입력 형태 유지 · 통합 파일은 건별 분리 · details는 사유",
           summary.get("examine_category_counts", {}).get(category, 0)) for category, label in EXAMINE_CATEGORIES.items()],
    ):
        if not summary.get("split_output") and folder.startswith(("changed", "unchanged")):
            continue
        # Link only real files/directories; a preview creates reports alone.
        name = e(folder) if preview or not (directory.parent / folder).exists() else f'<a href="../{folder}">{e(folder)}</a>'
        storage += f"<tr><td>{name}</td><td>{hint}</td><td>{count:,}</td></tr>"
    downloads = (
        ("records.csv", "처리 상태 CSV", "원본 처리 상태와 연결 오류 · 엑셀용"),
        ("catalog.json", "전체 목록 JSON", "원본·보완·변경 필드 등 모든 분류의 검색 목록"),
        ("record_details.jsonl", "레코드 상세 JSONL", "원본·보완·출력 JSON과 상세 처리 내용"),
        ("changes.jsonl", "변경 전후 JSONL", "필드별 변경 전후 값 전체"),
        ("review_details.jsonl", "검토 근거 JSONL", "검토 항목의 모든 사유와 근거"),
        ("summary.json", "처리 집계 JSON", "범위별 집계와 실행 정보"),
        ("label_counts.csv", "라벨 수 집계 CSV", "목록별 원본·보완·처리 후 개수와 증감"),
        ("../" + rule_file, "사용한 규칙", "이번 실행에 적용한 규칙"),
        ("../manifest.json", "파일 확인 명세", "입력·결과 경로와 파일 해시"),
        ("held_references.jsonl", "별도 보관 태그", "규칙에서 별도 검토로 지정한 태그"),
    )
    links = "".join(f'<a href="{path}">{title}<small>{hint}</small></a>' for path, title, hint in downloads)
    notice = summary.get("error") or "원본 파일은 변경하지 않았습니다. 검토·연결 누락 대상은 examine에서 확인하세요. 데이터 내용의 정답 여부는 별도 검수가 필요합니다."
    controls = CONTROLS.replace("검토 항목 검색", "선택한 분류에서 검색").replace("전체 항목에서 파일명, ID, 사유 검색", "파일명 · KEY · 필드 경로 · 처리 사유")
    doc = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>처리 결과서 · {e(rule_name)}</title><style>{STYLE}{CSS}</style></head><body><main>
<header class="hero"><div><div class="eyebrow">LABEL PORTER / PROCESSING REPORT</div><h1>라벨 데이터 처리 결과서</h1><p>{e(rule_name)}</p></div><span class="badge {badge}">{state}</span></header>
<div class="run-info"><b>{mode}</b><span class="muted">{e(summary['started_at'])} → {e(summary.get('finished_at',''))}</span><span>원본 보존 · 로컬 처리</span></div>
<div class="notice">{e(notice)}</div><nav class="cards" aria-label="분류별 집계">{cards}</nav>
<p class="muted">카드를 누르면 해당 목록으로 이동합니다. 원본과 보완은 서로 다른 입력 집계이며, 처리 완료·검토·변경 필드 등은 서로 겹칠 수 있어 전체 카드를 합산하지 않습니다.
짝이 없는 데이터: 보완 결과 없는 원본 <b>{summary.get('unmatched_source_records',0):,}</b>건 / 원본 없는 보완 <b>{summary.get('unmatched_result_records',0):,}</b>건.
검토/오류는 입력 쪽별 기록이며 중복 KEY는 원본과 보완 양쪽에 표시됩니다. 해석 실패 파일은 레코드 수에 포함할 수 없습니다.</p>
{label_count_panel(summary)}
<section class="panel" id="records"><div class="section-head"><h2 id="list-title">작업 결과 레코드</h2><a href="records.csv">처리 상태 CSV 열기 ↗</a></div>
<nav class="tabs" id="report-categories" aria-label="처리 목록 분류">{tabs}</nav>
<div class="filters"><label>데이터 분류<select id="report-category">{options}</select></label><label>처리 상태<select id="report-status"><option value="">모든 상태</option>{statuses}</select></label><label class="field-filter">변경된 필드<select id="report-field" aria-describedby="report-field-hint"><option value="">모든 변경 필드</option></select></label></div>
<p class="muted" id="report-field-hint">변경 기록에서 감지된 필드만 표시합니다. 건수는 필드별 전체 원본 레코드 수이며, 선택하면 현재 분류·상태·검색과 함께 적용됩니다. 목록 전체 교체는 목록 경로로 표시합니다.{ ' 미리보기에서는 변경 예정 기준입니다.' if preview else ''}</p>
{controls}<div class="table-scroll"><table id="review-table"><thead><tr><th>처리 상태</th><th>원본 파일</th><th>보완 파일</th><th>KEY</th><th>레코드 / 필드 위치</th><th>처리 내용 · 변경 전후</th><th>상세</th></tr></thead><tbody id="review-list"></tbody></table></div>{PAGER}
<p class="muted">전체 목록을 검색하고 현재 페이지의 행만 표시합니다. ‘상세 비교’에서 원본·보완·출력 JSON과 변경 필드·검토 사유를 확인하세요. { '미리보기의 출력은 저장 전 상태이며, 후보 JSON이 반영 예정 값입니다.' if preview else ''}</p></section>
<section class="panel"><h2>저장 위치와 데이터 분류 · {partition_state}</h2><p class="path">{e(summary['run_directory'])}</p>
<div class="table-scroll"><table class="storage"><thead><tr><th>위치</th><th>담긴 내용</th><th>건수</th></tr></thead><tbody>{storage}</tbody></table></div>
<p class="muted">examine의 원본 레코드는 changed / unchanged와 분리됩니다. 전체 data/에는 모든 원본의 복사본이 유지됩니다. 검토 대상 중 일부 반영된 항목은 출력값과 원본을 상세에서 함께 확인하세요.
{ '미리보기이므로 이 표는 저장 예정 분류이며 실제 데이터 폴더는 생성하지 않았습니다.' if preview else ''}</p></section>
<details class="panel"><summary>내보내기 · 상세 파일</summary><div class="downloads">{links}</div><p class="muted">CSV는 UTF-8 BOM 형식입니다. ID 열은 텍스트로 여세요. JSONL은 한 줄에 한 건이며 전체 값이 생략 없이 포함됩니다.</p></details>
<details class="panel"><summary>실행 범위 · 집계 기준</summary><p>원본 위치</p><div class="path">{e(summary['source_directory'])}</div>
<p>작업 결과는 원본 기준 처리 상태가 확정된 레코드 수입니다. 처리 완료는 검토 사유 없이 반영되었거나 값이 같은 레코드이며, 조건 제외는 별도 분류입니다.
변경 필드는 실행 규칙에 기록된 변경 경로 수입니다. 어절 목록 전체 교체는 목록 경로 1개로 집계하며 상세에서 전체 목록 전후를 비교합니다.
중단·실패 시 아직 처리하지 않은 보완 레코드는 ‘처리 미완료’로 표시합니다. 원본 총수와 처리된 목록 수가 다를 수 있습니다.</p></details>
<footer>보고서를 공유할 때는 reports 폴더 전체를 함께 보관하세요. 인터넷 연결 없이 검색·페이지 이동·상세 비교를 사용할 수 있습니다.</footer>
</main><script id="review-data" type="application/json">{script_json(catalog)}</script><script type="text/javascript">{SCRIPT}</script></body></html>'''
    atomic_text(directory / "catalog.json", json.dumps(catalog, ensure_ascii=False))
    atomic_text(directory / "report.html", doc)
