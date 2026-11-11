"""Đọc cấu hình .env cơ bản, giữ ưu tiên biến đã có trong môi trường."""
import os
from pathlib import Path


def load_env(path=None):
    path = Path(path) if path is not None else Path(__file__).resolve().parent / '.env'
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not key.isidentifier():
            raise ValueError(f'.env: dòng {number} không phải phép gán hợp lệ.')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)
