"""Small durable attribution receipts; never an alternative success certificate."""
import time
from .work import content

def persist_phase(put,base,root,phase,started,inclusive,value):
    put(base/('phase_'+phase+'.json'),dict(phase=phase,started_perf=started,
        inclusive_work_s=inclusive,observed_perf=time.perf_counter(),root_request_sha256=content(root),
        body_sha256=content(value),valid=value.get('valid'),status=value.get('status'),
        complete=value.get('complete'),position_error=value.get('position_error'),
        independent_original_body_required_for_success=True,not_success_certificate=True))
