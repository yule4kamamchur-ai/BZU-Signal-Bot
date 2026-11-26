"""Atomic, non-executable calibration storage. Never touches production ledgers."""
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path

SCHEMA='bzu-replay-checkpoint-v1'


def digest(value):
    h=hashlib.sha256()
    for part in json.JSONEncoder(sort_keys=True,separators=(',',':'),allow_nan=False).iterencode(value):
        h.update(part.encode('utf-8'))
    return h.hexdigest()


def save(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as raw:
            with gzip.GzipFile(fileobj=raw,mode='wb',compresslevel=3,mtime=0) as stream:
                # One encoding, no pickle or untrusted code execution.
                stream.write(json.dumps(value,separators=(',',':'),allow_nan=False).encode('utf-8'))
            raw.flush(); os.fsync(raw.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def load(path):
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        return json.load(stream)


def cached_rows(cache, instrument, tf, begin, end, fetch):
    if cache is None: return fetch()
    request=[instrument,tf,begin,end]
    path=Path(cache)/(digest(request)+'.json.gz')
    if path.exists():
        try:
            obj=load(path)
            if obj['request']==request and obj['digest']==digest(obj['rows']):
                print(f'History cache hit {tf}',flush=True)
                return obj['rows']
        except (OSError,EOFError,ValueError,KeyError,TypeError):
            pass
    rows=fetch()
    save(path,{'request':request,'rows':rows,'digest':digest(rows)})
    return rows
