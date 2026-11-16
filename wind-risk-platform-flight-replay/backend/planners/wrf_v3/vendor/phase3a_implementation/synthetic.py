"""Explicit synthetic provider for unchanged Phase2B input regression."""
import hashlib
import json
import math
from .coordinates import FRAME_ID, HEIGHT_DATUM, ROI


class ConstantWindProvider:
    frame_id = FRAME_ID
    height_datum = HEIGHT_DATUM
    kind = 'SYNTHETIC_CONSTANT'

    def __init__(self, wx, wy, wz, *, roi):
        self.wind = tuple(float(v) for v in (wx,wy,wz))
        if not all(math.isfinite(v) for v in self.wind) or not isinstance(roi,ROI):
            raise ValueError('Finite wind and explicit ROI required')
        self.roi = roi
        self.source = {'snapshot_id':'SYNTHETIC_CONSTANT', 'source_time':'STATIC', 'record_index':0,
                       'source_sha256':hashlib.sha256(json.dumps(self.wind).encode()).hexdigest()}

    def wind_query(self,x,y,z,t):
        out = {'wx':None,'wy':None,'wz':None,'valid':False,'status':'INVALID_INPUT',
               'frame_id':self.frame_id,'height_datum':self.height_datum,
               'time_mode':'BOUND_STATIC_SNAPSHOT','source_indices':None,**self.source}
        try:
            x,y,z,t = map(float,(x,y,z,t))
            if not all(math.isfinite(v) for v in (x,y,z,t)) or t < 0:
                return out
            if not self.roi.contains(x,y):
                return {**out,'status':'OUT_OF_ROI'}
            return {**out,'wx':self.wind[0],'wy':self.wind[1],'wz':self.wind[2],'valid':True,'status':'OK'}
        except (TypeError,ValueError,OverflowError):
            return out

    __call__ = wind_query
