"""Hash-bound runtime/inputs; usable both before and after final seal."""
from .common import D, ROOT, read, sha, StructuralStop

def verify_sources(require_freeze=True,policy='FULL_AUDIT'):
    p=D/'RUNTIME_FREEZE.json'
    if not p.exists():
        if require_freeze:raise StructuralStop('RUNTIME_NOT_FROZEN')
        return {'status':'PREFREEZE_STRUCTURAL_ONLY'}
    if not (D/'RUNTIME_FREEZE_SEAL.json').exists() or sha(p)!=read(D/'RUNTIME_FREEZE_SEAL.json')['sha256']:
        raise StructuralStop('RUNTIME_FREEZE_SEAL')
    frozen=read(p)
    if policy not in ('FULL_AUDIT','REQUEST_SEAL_ONLY'):
        raise StructuralStop('UNKNOWN_SOURCE_GUARD_POLICY')
    if policy=='REQUEST_SEAL_ONLY':
        return frozen
    for row in frozen['runtime_sources']:
        if not (D/row['path']).is_file() or sha(D/row['path'])!=row['sha256']:
            raise StructuralStop('RUNTIME_SOURCE_CHANGED:'+row['path'])
    for row in frozen['inherited_sources']:
        if not (ROOT/row['path']).is_file() or sha(ROOT/row['path'])!=row['sha256']:
            raise StructuralStop('INHERITED_SOURCE_CHANGED:'+row['path'])
    return frozen
