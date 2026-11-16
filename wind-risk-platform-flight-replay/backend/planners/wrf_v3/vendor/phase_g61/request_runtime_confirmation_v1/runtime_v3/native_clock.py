"""Native UTC cells for current-position time derivatives; no wind cache."""
import bisect
import datetime as dt
import hashlib
import json
import math
from phase4t.amendment_v1.model import StructuralStop,DomainFail


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def utc(text):
    try:instant=dt.datetime.fromisoformat(text)
    except (TypeError,ValueError) as e:raise StructuralStop('NATIVE_CLOCK_UTC_SCHEMA') from e
    if instant.tzinfo is None or instant.utcoffset()!=dt.timedelta(0):raise StructuralStop('NATIVE_CLOCK_EXPLICIT_UTC_REQUIRED')
    return instant


def clock_cells(source,max_gap):
    if isinstance(max_gap,bool) or not isinstance(max_gap,(int,float)) or not math.isfinite(max_gap) or max_gap<=0:raise StructuralStop('NATIVE_CLOCK_GAP_SCHEMA')
    epoch=utc(source['mission_epoch_utc']);native=source['native_utc'];offset=source.get('physical_offset_s',0.)
    if not isinstance(native,(list,tuple)) or len(native)<2:raise StructuralStop('NATIVE_CLOCK_SOURCE_COUNT')
    if isinstance(offset,bool) or not isinstance(offset,(int,float)) or not math.isfinite(offset) or offset<0:raise StructuralStop('NATIVE_CLOCK_OFFSET_SCHEMA')
    instants=[utc(v) for v in native]
    if any(b<=a for a,b in zip(instants,instants[1:])):raise StructuralStop('NATIVE_CLOCK_ORDER')
    if any((b-a).total_seconds()>max_gap for a,b in zip(instants,instants[1:])):raise DomainFail('TIME_GAP_EXCEEDS_CONTRACT')
    knots=tuple((instant-epoch).total_seconds()-float(offset) for instant in instants)
    if not knots[0]<=0.<knots[-1]:raise DomainFail('OUT_OF_TIME_DOMAIN')
    return knots,(0.,knots[-1])


def coverage_from_spec(spec):
    native=[dt.datetime.strptime(row['expected_time'],'%Y-%m-%d_%H:%M:%S').replace(tzinfo=dt.timezone.utc).isoformat() for row in spec['records']]
    return list(clock_cells(dict(mission_epoch_utc=spec['epoch_utc'],native_utc=native),spec['max_gap_s'])[1])


class NativeTimeCells:
    def __init__(self,provider,declared_interval):
        if provider.kind!='WRF_TIME_LINEAR_SNAPSHOTS':raise StructuralStop('NATIVE_CLOCK_PROVIDER_KIND')
        source=provider.source
        self.provider=provider;self.signature=fingerprint(source)
        self.knots,self.coverage=clock_cells(source,float(provider.max_gap_s))
        if declared_interval is None or tuple(declared_interval)!=self.coverage:
            raise StructuralStop('DECLARED_NATIVE_TIME_COVERAGE_MISMATCH')

    def bracket(self,t):
        if isinstance(t,bool) or not isinstance(t,(int,float)) or not math.isfinite(t):raise StructuralStop('NATIVE_DERIVATIVE_TIME_SCHEMA')
        if not self.coverage[0]<=t<=self.coverage[1]:raise DomainFail('OUT_OF_TIME_DOMAIN')
        if fingerprint(self.provider.source)!=self.signature:raise StructuralStop('NATIVE_CLOCK_BINDING_CHANGED')
        # Right derivative at an internal native time; left at the final endpoint.
        index=min(len(self.knots)-2,max(0,bisect.bisect_right(self.knots,float(t))-1))
        left=max(0.,self.knots[index]);right=self.knots[index+1]
        if not left<right or not left<=t<=right:raise StructuralStop('NATIVE_DERIVATIVE_EMPTY_OR_WRONG_CELL')
        return left,right
