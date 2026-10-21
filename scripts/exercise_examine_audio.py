"""Create isolated audio fixtures and verify all four examine exports end to end.

Inputs are read only. The output directory must not exist. No real data belongs in git.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from label_porter.config import load_config, validate_config
from label_porter.engine import run
from label_porter.formats import read_document, records
from label_porter.rules import apply_record
from scripts.verify_run import verify


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def extract(path, spec):
    return [row for _, _, row in records(read_document(path), spec)]


def exercise(source, result, rule_path, destination):
    source, result, rule_path, destination = [p.resolve() for p in (source, result, rule_path, destination)]
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('테스트 저장 위치는 존재하지 않는 별도 폴더여야 합니다.')
    rule_config = load_config(rule_path)
    incoming = extract(result, rule_config['result'])
    counts = Counter(row.get('sound_id') for row in incoming if isinstance(row, dict))
    by_key = {row['sound_id']: row for row in incoming if isinstance(row, dict) and counts[row.get('sound_id')] == 1}
    chosen = []
    for path in sorted(source.glob(rule_config['source'].get('glob', '**/*.json'))):
        try:
            rows = extract(path, rule_config['source'])
            if len(rows) != 1:
                continue
            original = rows[0]
            final = by_key[original['sound_id']]
            if not final['Segmentation']['eojeols']:
                continue
            _, _, _, status = apply_record(original, final, rule_config, {})
            if status not in ('updated', 'unchanged'):
                continue
        except (ValueError, KeyError, TypeError, IndexError):
            continue
        chosen.append((path, original, final))
        if len(chosen) == 30:
            break
    if len(chosen) != 30 or len({item[1]['sound_id'] for item in chosen}) != 30:
        raise ValueError('유일하게 연결되고 검증을 통과하는 오디오 레코드 30건이 필요합니다.')
    preserved = {str(path): digest(path) for path in [result, rule_path, *(p for p, _, _ in chosen)]}
    destination.mkdir(parents=True, exist_ok=False)
    expectations = []
    groups = ['정상 반영'] * 5 + ['review'] * 5 + ['excluded'] * 5 + ['unmatched 원본만'] * 5 + ['unmatched 보안만'] * 5 + ['file_errors 양쪽'] * 5
    for number, ((path, original, _), group) in enumerate(zip(chosen, groups), 1):
        expectations.append({'sample': number, 'sound_id': original['sound_id'], 'source_file': str(path), 'category': group})
    save(destination / '변형내역.json', expectations)
    save(destination / '원본보존해시.json', preserved)
    outcomes = []
    for combined_source in (False, True):
        name = '02_통합원본_개별보안' if combined_source else '01_개별원본_통합보안'
        case = destination / name
        original_dir, final_dir = case / '입력/원본', case / '입력/보안'
        original_dir.mkdir(parents=True)
        final_dir.mkdir(parents=True)
        config = copy.deepcopy(rule_config)
        config['name'] = rule_config['name'] + ' · examine 인위 오류 검증 전용'
        config['source'].update(records_path='', record_mode='array' if combined_source else 'object')
        config['result'].update(records_path='', record_mode='array')
        # Only the fixture rule excludes these IDs. Production rules are unchanged.
        config['include_when'] = [{'left': '/sound_id', 'op': 'ne', 'value': row['sound_id']} for _, row, _ in chosen[10:15]]
        validate_config(config)
        save(case / '규칙_검증전용.json', config)
        original_rows, final_rows, final_paths = [], [], []
        for number, (path, original, final) in enumerate(chosen):
            relative = path.relative_to(source)
            if number < 20:
                if combined_source:
                    original_rows.append(copy.deepcopy(original))
                else:
                    target = original_dir / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
            if number < 15 or 20 <= number < 25:
                revised = copy.deepcopy(final)
                if number < 5:
                    revised['RecordingMetadata']['orthographic'] += ' [저장 검증용]'
                elif number < 10:
                    revised['Segmentation']['eojeols'][0]['end'] = revised['Segmentation']['eojeols'][0]['start'] - 0.25
                if combined_source:
                    target = final_dir / relative
                    save(target, [revised])
                    final_paths.append(target)
                else:
                    final_rows.append(revised)
            if number >= 25:
                # Break the JSON only in copies. Each side has five unreadable files.
                for target, value in ((original_dir / relative, [original] if combined_source else original),
                                      (final_dir / '파일오류' / relative, [final])):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    text = json.dumps(value, ensure_ascii=False, indent=2)
                    target.write_text(text[:len(text) // 2], encoding='utf-8')
                final_paths.append(final_dir / '파일오류' / relative)
        if combined_source:
            save(original_dir / '통합원본.json', original_rows)
        else:
            combined = final_dir / result.name
            save(combined, final_rows)
            final_paths.insert(0, combined)
        fixture_hashes = {str(p): digest(p) for p in [*original_dir.rglob('*.json'), *final_paths, case / '규칙_검증전용.json']}
        print(f'Running {name}', flush=True)
        preview = run(original_dir, final_paths, case / '처리결과', config, preview=True)
        assert preview.summary['status'] == 'completed_with_issues', preview.summary
        assert not (preview.directory / 'examine').exists()
        outcome = run(original_dir, final_paths, case / '처리결과', config)
        assert outcome.summary['status'] == 'completed_with_issues', outcome.summary
        expected_counts = {'unmatched': 10, 'review': 5, 'excluded': 5, 'file_errors': 10}
        assert outcome.summary['examine_category_counts'] == expected_counts, outcome.summary
        assert preview.summary['examine_category_counts'] == expected_counts
        assert outcome.summary['source_status_counts'] == {'updated': 5, 'review_required': 5, 'excluded': 5, 'no_result': 5}
        verify(outcome.directory)
        saved_counts, saved_records, seen = {}, {}, set()
        for category, expected in expected_counts.items():
            for side in ('원본', '보안'):
                files = [p for p in (outcome.directory / 'examine' / category / side).rglob('*') if p.is_file()]
                saved_counts[category + '/' + side] = len(files)
                assert len(files) == 5, (category, side, files)
                assert not (outcome.directory / 'examine' / category / ('labels' if side == '원본' else 'results')).exists()
            details = list((outcome.directory / 'examine' / category / 'details').glob('*.json'))
            assert len(details) == expected
            for detail_path in details:
                detail = json.loads(detail_path.read_text(encoding='utf-8'))
                # Each known side is present once; missing/undecodable partners are not guessed.
                expected_sides = (1, 1) if category in ('review', 'excluded') else ((1, 0) if detail['scope'].startswith('source') else (0, 1))
                assert (len(detail['examine_sources']), len(detail['examine_results'])) == expected_sides
                for side, links in (('원본', detail['examine_sources']), ('보안', detail['examine_results'])):
                    input_file = original_dir / detail['source_file'] if side == '원본' else Path(detail['result_file'])
                    spec = config['source'] if side == '원본' else config['result']
                    for relative in links:
                        saved = outcome.directory / relative
                        assert saved.is_file() and relative not in seen, relative
                        seen.add(relative)
                        if category == 'file_errors':
                            assert saved.read_bytes() == input_file.read_bytes()
                            assert saved.name == input_file.name
                            continue
                        rows = extract(input_file, spec)
                        exported = extract(saved, spec)
                        assert len(exported) == 1
                        matching = [r for r in rows if r['sound_id'] == detail['key']]
                        assert len(matching) == 1 and exported == matching
                        saved_records[relative] = exported[0]['sound_id']
                        if len(rows) == 1:
                            assert saved.name == input_file.name
                            assert saved.read_bytes() == input_file.read_bytes()
                        else:
                            assert len(rows) == 20 and isinstance(json.loads(saved.read_text(encoding='utf-8')), list)
                            assert saved.parent.name == input_file.name
        assert len(seen) == 40
        assert all(digest(Path(p)) == sha for p, sha in fixture_hashes.items())
        outcomes.append({'scenario': name, 'directory': str(outcome.directory), 'report': str(outcome.directory / 'reports/report.html'),
                         'counts': outcome.summary['examine_category_counts'], 'saved_files': saved_counts,
                         'source_records': outcome.summary['source_records'], 'result_records': outcome.summary['result_records'],
                         'normal_updated': 5, 'examine_data_files': len(seen), 'one_record_split': True,
                         'individual_bytes_preserved': True, 'missing_side_not_created': True, 'input_unchanged': True})
    assert all(digest(Path(p)) == sha for p, sha in preserved.items())
    result_summary = {'verified': True, 'original_inputs_unchanged': True, 'real_pairs_sampled': 30,
                      'scope': '실제 자료에서 추출한 30쌍으로 만든 인위 오류 테스트. 전체 19,934건의 업무 검수가 아님.',
                      'created_at': datetime.now().isoformat(timespec='seconds'), 'scenarios': outcomes}
    save(destination / '검증결과.json', result_summary)
    lines = ['# examine 저장 검증 결과', '', '**두 시나리오 모두 통과했습니다. 실제 입력 파일은 변경하지 않았습니다.**', '',
             '실제 자료에서 정상 연결되는 30쌍을 추출해 별도 복사본만 변형했습니다. 각 시나리오는 정상 반영 5쌍, 검토 5쌍, 조건 제외 5쌍, 원본만 5건, 보안만 5건, 양쪽의 문법 오류 파일 각 5개를 포함합니다.', '',
             '| 시나리오 | unmatched | review | excluded | file_errors | 정상 반영 |', '| --- | ---: | ---: | ---: | ---: | ---: |']
    for item in outcomes:
        lines.append(f"| {item['scenario']} | 10 | 5 | 5 | 10 | 5 |")
    lines += ['', 'file_errors 10건은 원본 오류 파일 5개와 보안 오류 파일 5개입니다. JSON을 읽을 수 없으므로 KEY로 짝을 추정하지 않고 각 입력 파일을 보관합니다.', '',
              '## 변형한 내용', '', '- review: 보안본 첫 어절의 종료 시각을 시작 시각보다 0.25 작게 설정했습니다.',
              '- excluded: 검증 전용 규칙에 지정한 sound_id 5개를 제외하는 조건을 추가했습니다. 실제 사용 규칙은 변경하지 않았습니다.',
              '- unmatched: 원본만 또는 보안만 입력한 레코드를 각각 5건씩 만들었습니다.',
              '- file_errors: 복사한 JSON 텍스트를 중간에서 잘라 각 입력에 문법 오류 파일을 5개씩 만들었습니다.',
              '- 정상 반영: 보안 전사에 검증 표기를 추가하여 나머지 정상 5건이 실제로 반영되는지 함께 확인했습니다.', '',
              '## 저장 내용 검증', '', '- 분류마다 원본 5개·보안 5개씩, 시나리오당 라벨 파일 40개가 저장되었습니다.',
              '- 개별 입력은 파일명과 하위 경로를 유지하고 입력 바이트와 일치합니다. 새 래퍼를 넣지 않았습니다.',
              '- 통합 입력은 파일명 하위에 건별 파일로 분리되며 각 파일에는 해당 레코드 1건만 있습니다.',
              '- 짝이 없는 쪽의 빈 파일이나 대체 데이터를 생성하지 않았습니다.',
              '- 실제 입력과 테스트 입력의 해시가 실행 전후 일치합니다.',
              '- 전체 복사본의 변경은 changes.jsonl에 기록한 변경과 일치하며 독립 검증 도구도 통과했습니다.',
              '- 미리보기와 실제 저장의 분류 건수가 일치하며 미리보기는 examine 데이터 파일을 만들지 않습니다.', '', '## 처리 결과서', '']
    for item in outcomes:
        report = Path(item['report']).relative_to(destination).as_posix()
        lines.append(f"- [{item['scenario']} 결과서](<{report}>)")
    lines += ['', '재현 시 각 시나리오의 `규칙_검증전용.json`, `입력/원본`, `입력/보안`을 선택하세요. 입력 폴더에 포함된 문법 오류는 테스트를 위한 의도적인 변형입니다.', '',
              '전체 19,934건에 대한 실제 업무 검수 결과가 아니라, 저장 기능의 분류·형태·보존을 검증한 결과입니다.', '']
    (destination / '검증결과.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'verified': True, 'directory': str(destination), 'scenarios': len(outcomes)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    exercise(args.source, args.result, args.config, args.output)
