"""Lossless derived native-array material, not sampled/approximate wind values.

Original native file hashes are checked on construction entry and exit. The
original TemporalWRF constructor still enforces time/grid/lineage/coordinate
bindings and builds the same immutable adapters and original interpolation.
"""
import copy,json,threading
from pathlib import Path
from unittest.mock import patch
import numpy as np
from phase3a_implementation.adapter import StaticWRFWindAdapter,AdapterError,file_sha256
from phase3a_implementation.coordinates import WRFCoordinates
from phase_g61.dynamic_wind import provider as original
from phase_g61.wind_column_diagnostic_v1.column_v3.paired_columns import PrunedColumnsTemporal
from .work import D,read,sha,BOUND
from .layout import assert_same

_construction_lock=threading.Lock()
FIELDS=original.ARRAY_FIELDS


def checked_manifest():
    assert_same(BOUND)
    path=Path(BOUND['material_manifest_file'])
    if sha(path)!=BOUND['material_manifest_sha256']:raise AdapterError('NATIVE_MATERIAL_MANIFEST_CHANGED')
    doc=read(path)
    if doc.get('schema')!='LOSSLESS_ORIGINAL_NATIVE_ARRAYS_V1':raise AdapterError('NATIVE_MATERIAL_SCHEMA')
    return doc


def _entry(doc,path,index,expected):
    matches=[r for r in doc['records'] if r['source_sha256']==expected and r['record_index']==index]
    if len(matches)!=1:raise AdapterError('NATIVE_MATERIAL_NOT_REGISTERED')
    row=matches[0]
    if Path(path).resolve()!=Path(row['source']['path']).resolve():raise AdapterError('NATIVE_MATERIAL_SOURCE_PATH')
    return row


def build_adapter(row,*,roi,expected_time,case_id):
    if row['source']['source_time']!=expected_time or row['case_id']!=case_id:
        raise AdapterError('NATIVE_MATERIAL_TIME_OR_CASE')
    path=(Path(BOUND['material_root'])/row['arrays_file']).resolve()
    if not path.is_relative_to(Path(BOUND['material_root'])) or path.is_relative_to(D):raise AdapterError('NATIVE_MATERIAL_PATH_SCOPE')
    if sha(path)!=row['arrays_file_sha256']:raise AdapterError('NATIVE_MATERIAL_BODY_CHANGED')
    with np.load(path,allow_pickle=False) as saved:
        if set(saved.files)!=set(FIELDS):raise AdapterError('NATIVE_MATERIAL_ARRAY_SCHEMA')
        arrays={name:np.array(saved[name],copy=True) for name in FIELDS}
    for name,a in arrays.items():
        expected=row['arrays'][name]
        if a.dtype.str!=expected['dtype'] or list(a.shape)!=expected['shape'] or __import__('hashlib').sha256(a.tobytes()).hexdigest()!=expected['sha256']:
            raise AdapterError('NATIVE_MATERIAL_ARRAY_IDENTITY',name)
    # Values are original constructor outputs, with original float32
    # destagger/addition semantics already evaluated by the original adapter.
    raw=StaticWRFWindAdapter.__new__(StaticWRFWindAdapter)
    raw._source=copy.deepcopy(row['source']);raw.roi=roi
    coordinates=WRFCoordinates.__new__(WRFCoordinates)
    for key,value in row['coordinate_values'].items():setattr(coordinates,key,copy.deepcopy(value))
    coordinates.roi=roi;coordinates.attributes=copy.deepcopy(row['coordinate_attributes'])
    raw.coordinates=coordinates
    for name,a in arrays.items():a.setflags(write=False);setattr(raw,name,a)
    return raw


class PreparedNativeColumns(PrunedColumnsTemporal):
    @classmethod
    def from_records(cls,records,*args,**kwargs):
        doc=checked_manifest();used=[]
        # Only constructor dependencies are supplied; the source modules and
        # original query/interpolation functions are never edited.
        def metadata(path,index,expected_sha256):
            row=_entry(doc,path,index,expected_sha256)
            if file_sha256(path)!=expected_sha256:raise AdapterError('SOURCE_CHANGED')
            return copy.deepcopy(row['grid']),copy.deepcopy(row['lineage'])
        def from_material(adapter_cls,path,*,expected_sha256,expected_time,case_id,roi,record_index=0):
            row=_entry(doc,path,record_index,expected_sha256)
            result=build_adapter(row,roi=roi,expected_time=expected_time,case_id=case_id)
            used.append(dict(source_sha256=expected_sha256,material_sha256=row['arrays_file_sha256']))
            return result
        if not _construction_lock.acquire(blocking=False):raise AdapterError('NATIVE_MATERIAL_CONCURRENT_CONSTRUCTION')
        try:
            with patch.object(original,'_metadata',metadata),patch.object(StaticWRFWindAdapter,'from_wrfout',classmethod(from_material)):
                obj=super().from_records(records,*args,**kwargs)
        finally:_construction_lock.release()
        object.__setattr__(obj,'_native_material_evidence',dict(manifest_sha256=sha(BOUND['material_manifest_file']),records=used,
            values='FULL_ORIGINAL_NATIVE_ARRAYS_DTYPE_SHAPE_BYTES',wind_value_cache=False,
            full_native_source_hashes='ENTRY_METADATA_AND_ORIGINAL_CONSTRUCTOR_EXIT',
            grid_lineage_metadata='HASH_BOUND_DERIVATION;RAW_SOURCE_FULL_SHA_RECHECKED_AT_EACH_LOAD'))
        return obj

    def query_stats(self):
        out=super().query_stats();out['native_preparation_material']=copy.deepcopy(self._native_material_evidence);return out
