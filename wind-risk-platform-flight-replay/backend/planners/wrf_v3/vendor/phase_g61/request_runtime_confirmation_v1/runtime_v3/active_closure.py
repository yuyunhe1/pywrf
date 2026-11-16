"""Conservative static project import closure, including explicit late entries."""
import ast,importlib.util
from pathlib import Path
from .io_core import ROOT,sha


def file_for(name):
    path=ROOT/Path(*name.split('.'))
    if path.with_suffix('.py').is_file():return path.with_suffix('.py')
    if (path/'__init__.py').is_file():return path/'__init__.py'
    return None


def collect(seeds):
    queue=list(seeds);files={};names=set()
    while queue:
        name=queue.pop()
        if name in names:continue
        names.add(name);path=file_for(name)
        if path is None:continue
        rel=path.relative_to(ROOT).as_posix();files[rel]=sha(path)
        parts=name.split('.')
        for i in range(1,len(parts)):
            init=ROOT/Path(*parts[:i])/'__init__.py'
            if init.is_file():queue.append('.'.join(parts[:i]))
        package=name if path.name=='__init__.py' else name.rsplit('.',1)[0]
        tree=ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):queue.extend(alias.name for alias in node.names)
            elif isinstance(node,ast.ImportFrom):
                try:base=importlib.util.resolve_name('.'*node.level+(node.module or ''),package) if node.level else node.module
                except (ImportError,ValueError):continue
                if base:
                    queue.append(base)
                    queue.extend(base+'.'+alias.name for alias in node.names if alias.name!='*')
            elif isinstance(node,ast.Constant) and isinstance(node.value,str):
                # Include literal dynamic project module entries. Variable
                # runtime entries are supplied explicitly by the caller.
                value=node.value
                if value.startswith('phase') and all(part.isidentifier() for part in value.split('.')):queue.append(value)
    return dict(sorted(files.items()))
