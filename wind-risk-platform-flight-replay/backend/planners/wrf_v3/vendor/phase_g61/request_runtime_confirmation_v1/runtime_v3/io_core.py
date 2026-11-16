"""Pure immutable-code I/O primitives; no request directory or wind work on import."""
import hashlib,json,os,tempfile
from pathlib import Path
from phase_g61.common import ROOT
CODE=Path(__file__).resolve().parent

def sha(path):
    with native(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def content(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('DUPLICATE_JSON_KEY:'+key)
        result[key]=value
    return result
def nonfinite(value):raise ValueError('NONFINITE_JSON:'+value)
def read(path):return json.loads(native(path).read_text(encoding='utf-8-sig'),object_pairs_hook=unique,parse_constant=nonfinite)
def logical(value):
    text=os.fspath(value)
    if os.name=='nt' and text.startswith('\\\\?\\UNC\\'):text='\\\\'+text[8:]
    elif os.name=='nt' and text.startswith('\\\\?\\'):text=text[4:]
    return Path(text).resolve()
def native(value):
    path=logical(value)
    if os.name!='nt':return path
    text=str(path)
    return Path('\\\\?\\UNC\\'+text[2:] if text.startswith('\\\\') else '\\\\?\\'+text)
def absolute(value):
    if not isinstance(value,str) or not value or not Path(value).is_absolute():raise ValueError('ABSOLUTE_PATH_REQUIRED')
    return logical(value)
def inside(path,root):
    p=logical(path)
    if not p.is_relative_to(logical(root)):raise ValueError('OUTPUT_SCOPE_ESCAPE')
    return p
def put(path,value,root,replace=False):
    p=native(inside(path,root));p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists() and not replace:raise ValueError('REFUSE_OVERWRITE:'+str(p))
    data=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    fd,temporary=tempfile.mkstemp(prefix='.'+p.name+'.',dir=p.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            if stream.write(data)!=len(data):raise ValueError('SHORT_WRITE')
            stream.flush();os.fsync(stream.fileno())
        if replace:os.replace(temporary,p)
        else:os.link(temporary,p);os.unlink(temporary)
        if p.read_bytes()!=data:raise ValueError('FULL_BODY_READBACK')
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


# Logical identities must not depend on how importlib spells __file__.
ROOT=logical(ROOT)
CODE=logical(CODE)
