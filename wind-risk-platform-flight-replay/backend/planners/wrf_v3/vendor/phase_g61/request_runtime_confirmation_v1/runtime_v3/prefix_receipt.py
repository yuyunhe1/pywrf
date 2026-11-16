"""Strict publication schema and durable cutoff for both independent roles."""
import math


def validate(value,publication,req,role,file_sha256,observed,deadline,Stop,Limit):
    expected={'result_file_sha256','persisted_readback_perf','prepare_deadline','timely','role'}
    if not isinstance(publication,dict) or set(publication)!=expected:raise Stop('PREFIX_PUBLICATION_SCHEMA')
    if not isinstance(value,dict) or value.get('diagnostic_only') is not False or value.get('role')!=role:
        raise Stop('PREFIX_RESULT_ROLE_OR_DIAGNOSTIC')
    if publication['role']!=role or publication['result_file_sha256']!=file_sha256 or publication['prepare_deadline']!=deadline:
        raise Stop('PREFIX_VALIDATOR_RECEIPT_IDENTITY')
    numbers=[value.get('published_perf'),publication['persisted_readback_perf'],publication['prepare_deadline'],observed,deadline]
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in numbers):raise Stop('PREFIX_PUBLICATION_CLOCK_SCHEMA')
    published,persisted=numbers[:2]
    if published<req['owner_start'] or persisted<published:raise Stop('PREFIX_PUBLICATION_CLOCK_ORDER')
    if type(publication['timely']) is not bool or publication['timely']!=(persisted<deadline):raise Stop('PREFIX_PUBLICATION_TIMELINESS_SCHEMA')
    if not publication['timely'] or published>=deadline or observed>=deadline:raise Limit('LATE_PREFIX_'+role)
    return True
