"""Preserve ordinary bounded failures; never relabel worker/schema faults no-route."""
import math
from .work import content
from .initial_contract import admit_initial as inherited_admission
class InitialOrdinaryFailure(ValueError):pass
class InitialStructuralFailure(RuntimeError):pass

def admit_initial(req,value,observed_perf,*,coverage_receipt=None):
    if not isinstance(value,dict):raise InitialStructuralFailure('INITIAL_WORKER_SCHEMA')
    if value.get('request_sha256')!=content(req) or value.get('nonce')!=req.get('nonce'):raise InitialStructuralFailure('INITIAL_REQUEST_IDENTITY')
    published=value.get('published_perf')
    if isinstance(published,bool) or not isinstance(published,(int,float)) or not math.isfinite(published):raise InitialStructuralFailure('INITIAL_PUBLICATION_CLOCK_SCHEMA')
    if isinstance(observed_perf,bool) or not isinstance(observed_perf,(int,float)) or not math.isfinite(observed_perf):raise InitialStructuralFailure('INITIAL_OBSERVATION_CLOCK_SCHEMA')
    if not published<req['deadline_perf'] or not observed_perf<req['deadline_perf']:raise InitialOrdinaryFailure('INITIAL_WORKER_LATE')
    status=value.get('status')
    if status=='INITIAL_TIME_DOMAIN_REJECT':
        from phase_g61.request_runtime_confirmation_v1.runtime_v3.time_coverage_guard import verify_receipt
        try:
            verify_receipt(req,coverage_receipt)
            if value.get('context_id')!=coverage_receipt['context_id']:raise ValueError('INITIAL_CONTEXT_IDENTITY')
            if value.get('result') is not None or value.get('mathematical_search_entered') is not False or type(value.get('new_physical_executions')) is not int or value['new_physical_executions']!=0:raise ValueError('INITIAL_REJECT_MUST_PRECEDE_SEARCH_AND_FLIGHT')
        except Exception as exc:raise InitialStructuralFailure('INITIAL_TIME_COVERAGE_RECEIPT:'+str(exc)) from exc
        raise InitialOrdinaryFailure(status)
    if status in ('INITIAL_NO_VALID_ROUTE','INITIAL_PLANNING_TIMEOUT'):raise InitialOrdinaryFailure(status)
    if status=='INITIAL_PLANNING_ERROR':
        errors=value.get('errors')
        if isinstance(errors,list) and len(errors)==1 and errors[0].get('detail')=='INITIAL_START_DOMAIN_REJECT':raise InitialOrdinaryFailure('INITIAL_DOMAIN_REJECT')
        raise InitialStructuralFailure('INITIAL_WORKER_REPORTED_ERROR:'+str(errors))
    if status!='INITIAL_PLAN_FOUND' or not isinstance(value.get('result'),dict):raise InitialStructuralFailure('INITIAL_STATUS_OR_FOUND_PAYLOAD_SCHEMA')
    try:return inherited_admission(req,value,observed_perf)
    except (ValueError,KeyError,TypeError,IndexError) as exc:raise InitialStructuralFailure('INITIAL_ADMISSION_CONTRACT:'+str(exc)) from exc
