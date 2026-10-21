"""Only bundled, source-matched Mermaid diagrams are served to the help reader."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ASSET_PREFIX = 'https://label-porter.invalid/diagrams/'


def diagram_id(source):
    return hashlib.sha256((source.replace('\r\n', '\n').strip() + '\n').encode('utf-8')).hexdigest()


def asset_directory():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'assets' / 'mermaid'


@lru_cache(maxsize=1)
def manifest():
    try:
        return json.loads((asset_directory() / 'manifest.json').read_text(encoding='utf-8'))['diagrams']
    except (OSError, ValueError, KeyError):
        return {}


def diagram_info(source):
    identity = diagram_id(source)
    info = manifest().get(identity)
    return (identity, info) if info else (identity, None)


def read_diagram(url, data='', method='GET', encoding=None):
    """HtmlFrame's request callback: no filesystem/network paths from Markdown."""
    if method != 'GET' or data or not url.startswith(ASSET_PREFIX):
        raise ValueError('도움말은 내장 문서와 다이어그램만 표시합니다.')
    name = url[len(ASSET_PREFIX):]
    if not re.fullmatch(r'[0-9a-f]{64}\.png', name) or name[:-4] not in manifest():
        raise ValueError('등록되지 않은 다이어그램입니다.')
    raw = (asset_directory() / name).read_bytes()
    return url, raw, 'image/png', 200
