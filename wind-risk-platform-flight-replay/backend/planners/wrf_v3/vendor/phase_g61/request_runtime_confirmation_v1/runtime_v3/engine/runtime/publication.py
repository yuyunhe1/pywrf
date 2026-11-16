"""Validate durable solver publication without trusting a pre-write timestamp."""
import math
from .common import Stop
def validate_publication(value,req,expected_file_sha256):
    if not isinstance(value,dict) or set(value)!={'solver_result_file_sha256','persisted_readback_perf','prepare_deadline','timely'}:raise Stop('SOLVER_PUBLICATION_SCHEMA')
    if value['solver_result_file_sha256']!=expected_file_sha256 or value['prepare_deadline']!=req['prepare_deadline']:raise Stop('SOLVER_PUBLICATION_IDENTITY')
    t=value['persisted_readback_perf']
    if isinstance(t,bool) or not isinstance(t,(int,float)) or not math.isfinite(t) or t<req['owner_start']:raise Stop('SOLVER_PUBLICATION_CLOCK')
    if type(value['timely']) is not bool or value['timely']!=(t<req['prepare_deadline']):raise Stop('SOLVER_PUBLICATION_TIMELINESS')
    return value
