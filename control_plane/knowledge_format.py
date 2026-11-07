"""Versioned, deterministic text-only indexing contract shared with the RAG service."""
import hashlib
import json
import re

FORMAT = 'zh-text-512-v1'
DIMENSIONS = 512
CHARS = 400
OVERLAP = 60


def prepare(files, max_files=10, max_bytes=1024*1024, max_chunks=4000):
    if not isinstance(files, list) or not 1 <= len(files) <= max_files:
        raise ValueError('knowledge_file_limit')
    names, chunks, originals = set(), [], 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {'name', 'content'}:
            raise ValueError('knowledge_invalid_file')
        name, content = item['name'], item['content']
        if (not isinstance(name, str) or not re.fullmatch(r'[^/\\\x00-\x1f]{1,120}\.(md|txt)', name, re.I)
                or name in names or not isinstance(content, str) or not content.strip()
                or '\x00' in content):
            raise ValueError('knowledge_invalid_file')
        size = len(content.encode('utf-8'))
        if size > max_bytes:
            raise ValueError('knowledge_file_too_large')
        names.add(name)
        originals += size
        for start in range(0, len(content), CHARS-OVERLAP):
            text = content[start:start+CHARS]
            chunks.append({'file': name, 'text': text, 'ordinal': len(chunks),
                           'line': content.count('\n', 0, start)+1})
            if len(chunks) > max_chunks:
                raise ValueError('knowledge_chunk_limit')
            if start+CHARS >= len(content):
                break
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(FORMAT.encode()+b'\0'+canonical).hexdigest()
    # Logical bytes: original UTF-8 + chunk UTF-8/metadata + float32 vectors.
    # Database pages, replicas and model weights are platform overhead, not purchased user space.
    index_bytes = sum(len(json.dumps(c, ensure_ascii=False, separators=(',', ':')).encode())+
                      DIMENSIONS*4 for c in chunks)
    return digest, originals, index_bytes, chunks
