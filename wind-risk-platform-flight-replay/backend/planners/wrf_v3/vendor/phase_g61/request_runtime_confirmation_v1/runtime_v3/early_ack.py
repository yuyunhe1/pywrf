"""One-use, same-request/context input verification inside the original8s.

Semantic verification is unchanged; only its scheduling precedes PREPARED.
ACK still rereads every pinned input byte and all new route/C/collision data.
"""
import math,time
from pathlib import Path
from .work import read,sha,content


def input_paths(req,root,tx,common):
    rootpath=Path(req['root_request_path']).resolve();anchor=common.unpack(root,'ACTUAL_ANCHOR',read(tx/'anchor.json'))
    base=rootpath.parent
    def within(relative):
        p=(base/relative).resolve()
        if not p.is_relative_to(base):raise common.Stop('EARLY_ACK_PATH_ESCAPE')
        return p
    paths=[rootpath,tx/'request.json',tx/'anchor.json',within(anchor['actual_prefix_file']),
        within(anchor['reference_file']),within(anchor['journal_snapshot_dir'])/'prefix_identity.json',
        within(anchor['journal_snapshot_dir'])/'prefix.jsonl',common.D/'RUNTIME_FREEZE.json',common.D.parent/'ROOT_INPUT_MANIFEST_V2.json']
    source=root['case']['initial_plan_source']
    paths.extend(common.ROOT/source[k] for k in ['route_file','parent_receipt_file','pipeline_request_file'])
    paths.append(within(req['group_origin_file']))
    from .transaction_contract import prior_input_paths
    paths.extend(prior_input_paths(req,root,base,common))
    if req.get('prior_attempt_index') is not None:
        previous=tx.parent/f"{req['prior_attempt_index']:02d}"
        paths.extend(previous/name for name in ['request.json','anchor.json','actual_prefix.json','invalidation.json'])
        paths.extend([previous/'anchor_snapshot/prefix_identity.json',previous/'anchor_snapshot/prefix.jsonl'])
    return sorted(set(p.resolve() for p in paths),key=str)


def capture(paths,verify,clock,Stop):
    clock.check();before={str(p):sha(p) for p in paths};clock.check()
    verify();clock.check();after={str(p):sha(p) for p in paths};clock.check()
    if before!=after:raise Stop('EARLY_ACK_INPUT_CHANGED_DURING_VERIFICATION')
    return after


def prepare(req,root,tx,common,contract):
    clock=common.Clock(req['prepare_deadline']);clock.check()
    if time.perf_counter()<req['owner_start']:raise common.Stop('EARLY_ACK_CLOCK_ORDER')
    begin=time.perf_counter();common.source_check()
    paths=input_paths(req,root,tx,common)
    hashes=capture(paths,lambda:contract.verify_transaction(req),clock,common.Stop)
    # This object is request-owned, created now, and may only be consumed once.
    return dict(schema='ONE_USE_SAME_REQUEST_CONTEXT_EARLY_ACK_INPUTS_V1',
        request_sha256=content(req),root_sha256=content(root),case_sha256=content(req['case']),
        parent_context_id=req['parent_context_id'],nonce=req['nonce'],epoch=req['epoch'],
        deadline=req['prepare_deadline'],inputs=hashes,verified_perf=time.perf_counter(),
        verification_inclusive_s=time.perf_counter()-begin,used=False)


def consume(proof,req,root,tx,common):
    clock=common.Clock(req['prepare_deadline']);clock.check()
    if not isinstance(proof,dict) or proof.get('schema')!='ONE_USE_SAME_REQUEST_CONTEXT_EARLY_ACK_INPUTS_V1' or proof.get('used') is not False:
        raise common.Stop('EARLY_ACK_SCHEMA_OR_ALREADY_USED')
    proof['used']=True
    expected=dict(request_sha256=content(req),root_sha256=content(root),case_sha256=content(req['case']),
        parent_context_id=req['parent_context_id'],nonce=req['nonce'],epoch=req['epoch'],deadline=req['prepare_deadline'])
    if any(proof.get(k)!=v for k,v in expected.items()):raise common.Stop('EARLY_ACK_REQUEST_CONTEXT_IDENTITY')
    verified=proof.get('verified_perf')
    if isinstance(verified,bool) or not isinstance(verified,(int,float)) or not math.isfinite(verified) or not req['owner_start']<=verified<req['prepare_deadline']:
        raise common.Stop('EARLY_ACK_VERIFICATION_CLOCK')
    paths=input_paths(req,root,tx,common)
    if {str(p) for p in paths}!=set(proof['inputs']):raise common.Stop('EARLY_ACK_INPUT_SET_CHANGED')
    # Full streaming readback, not a stat/mtime shortcut or a skipped material.
    after={str(p):sha(p) for p in paths};clock.check()
    if after!=proof['inputs']:raise common.Stop('EARLY_ACK_INPUT_HASH_CHANGED')
    return True
