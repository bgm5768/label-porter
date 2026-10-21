"""Render bundled Mermaid fences before packaging; the EXE needs no JS/Node.

Install dev tooling: $env:PUPPETEER_SKIP_DOWNLOAD='true'; npm ci --prefix tools/mermaid --ignore-scripts
Run: python scripts/build_mermaid.py --browser 'C:/Program Files/Google/Chrome/Application/chrome.exe'
Check packaged assets: python scripts/build_mermaid.py --check
"""
import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from markdown_it import MarkdownIt
from label_porter.mermaid_assets import diagram_id

DOCUMENTS = ('AI_규칙작성_가이드.md', '사용설명.md', '후처리_지원범위.md', '아키텍처_및_규칙엔진.md')


def diagrams():
    found = {}
    for name in DOCUMENTS:
        for token in MarkdownIt().parse((ROOT/name).read_text(encoding='utf-8')):
            if token.type == 'fence' and token.info.strip().split(' ', 1)[0] == 'mermaid':
                identity = diagram_id(token.content)
                found[identity] = {'source': token.content, 'document': name}
    return found


def check_assets():
    target = ROOT/'assets/mermaid'
    manifest = json.loads((target/'manifest.json').read_text(encoding='utf-8'))['diagrams']
    if set(diagrams()) != set(manifest):
        raise ValueError('Mermaid 원문이 변경되었습니다. python scripts/build_mermaid.py로 도식을 다시 생성하세요.')
    for identity in diagrams():
        info = manifest[identity]
        for suffix in ('png', 'svg'):
            raw = (target/f'{identity}.{suffix}').read_bytes()
            if hashlib.sha256(raw).hexdigest() != info[f'{suffix}_sha256']:
                raise ValueError(f'도식이 바뀌었습니다. 다시 생성하세요: {identity}.{suffix}')
    return len(diagrams())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', default='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        print(f'{check_assets()} Mermaid diagrams verified.')
        return
    cli = ROOT/'tools/mermaid/node_modules/@mermaid-js/mermaid-cli/src/cli.js'
    if not cli.is_file():
        raise SystemExit('First run npm ci --prefix tools/mermaid --ignore-scripts')
    target = ROOT/'assets/mermaid'; target.mkdir(parents=True, exist_ok=True)
    manifest = {'renderer': '@mermaid-js/mermaid-cli 12.0.0', 'diagrams': {}}
    with tempfile.TemporaryDirectory(prefix='label-porter-mermaid-') as temp:
        temp = Path(temp)
        puppeteer = temp/'puppeteer.json'
        puppeteer.write_text(json.dumps({'executablePath': args.browser}), encoding='utf-8')
        config = temp/'mermaid.json'
        config.write_text(json.dumps({'securityLevel': 'strict', 'theme': 'base', 'markdownAutoWrap': False,
            'flowchart': {'htmlLabels': True, 'curve': 'basis', 'wrappingWidth': 420},
            'themeVariables': {'fontFamily': 'Malgun Gothic, sans-serif', 'fontSize': '18px', 'primaryColor': '#eff3fa',
                              'primaryTextColor': '#253b55', 'primaryBorderColor': '#94aac7', 'lineColor': '#657f9d'}}), encoding='utf-8')
        for identity, item in diagrams().items():
            source = temp/'diagram.mmd'; source.write_text(item['source'], encoding='utf-8')
            for extension in ('svg', 'png'):
                command = ['node', str(cli), '-i', str(source), '-o', str(target/f'{identity}.{extension}'),
                           '-p', str(puppeteer), '-c', str(config), '-b', 'white', '--size', '1600', '-s', '2', '--no-font-embed']
                rendered = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', cwd=ROOT,
                                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                if rendered.returncode:
                    raise RuntimeError(rendered.stdout + rendered.stderr)
            png = (target/f'{identity}.png').read_bytes()
            width, height = struct.unpack('>II', png[16:24])
            title = re.search(r'^\s*accTitle:\s*(.+)$', item['source'], re.M)
            manifest['diagrams'][identity] = {'document': item['document'], 'title': title[1] if title else '아키텍처 다이어그램',
                'width': width, 'height': height,
                'png_sha256': hashlib.sha256(png).hexdigest(),
                'svg_sha256': hashlib.sha256((target/f'{identity}.svg').read_bytes()).hexdigest()}
    (target/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    for path in target.iterdir():
        if path.suffix in ('.png', '.svg') and re.fullmatch(r'[0-9a-f]{64}', path.stem) and path.stem not in manifest['diagrams']:
            path.unlink()
    print(f'{check_assets()} Mermaid diagrams built.')


if __name__ == '__main__': main()
