"""Sequential original C/collision statements with independent private states.

No future controls, shared plant state or altered numerical tolerances. Only
the outer control-row loop is made resumable. Full schema is checked again at
every snapshot. Original final whole-actual evaluators remain mandatory.
"""
import ast,copy,inspect,math,time
import numpy as np
from phase3b import validator as original_C
from phase4b.collision.checker import EdgeCollisionChecker


def function(source,name,args,body):
    return ast.FunctionDef(name=name,args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=a) for a in args],
        kwonlyargs=[],kw_defaults=[],defaults=[]),body=body,decorator_list=[])


class NodeLookup(ast.NodeTransformer):
    def visit_Subscript(self,node):
        if isinstance(node.value,ast.Name) and node.value.id=='nodes' and isinstance(node.slice,ast.BinOp):
            return ast.copy_location(ast.Name(id='observed_node',ctx=ast.Load()),node)
        return self.generic_visit(node)


def compile_C():
    node=ast.parse(inspect.getsource(original_C.validate)).body[0]
    run_index=next(i for i,n in enumerate(node.body) if isinstance(n,ast.Try) and any(isinstance(x,ast.For) for x in n.body))
    state_index=next(i for i,n in enumerate(node.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Tuple) and any(isinstance(x,ast.Name) and x.id=='state' for x in t.elts) for t in n.targets))
    schema=copy.deepcopy(node);schema.name='schema_only'
    schema.body=copy.deepcopy(node.body[:state_index])+[ast.parse("return {'schema_ok':True,'violation':violation}").body[0]]
    generator=copy.deepcopy(node);generator.name='C_rows'
    statement=generator.body[run_index];loop=next(n for n in statement.body if isinstance(n,ast.For))
    loopbody=[NodeLookup().visit(copy.deepcopy(n)) for n in loop.body]
    receive=ast.parse('incoming = yield locals()\ni,u,d,observed_node = incoming').body
    statement.body=[statement.body[0],ast.While(test=ast.Constant(True),body=receive+loopbody,orelse=[])]
    generator.body=generator.body[:run_index+1]
    names=['state','qf','node_error','base','violation','beta_min','beta_max','histories','betas','count','actual_nodes','t0','z']
    finish=function('', 'finish_C',names,copy.deepcopy(node.body[run_index+1:]))
    module=ast.fix_missing_locations(ast.Module(body=[schema,generator,finish],type_ignores=[]))
    namespace=dict(vars(original_C));exec(compile(module,'<ORIGINAL_C_RESUMABLE_ROW_LOOP>','exec'),namespace)
    return namespace['schema_only'],namespace['C_rows'],namespace['finish_C'],names,ast.unparse(module)


def compile_collision():
    import textwrap
    node=ast.parse(textwrap.dedent(inspect.getsource(EdgeCollisionChecker.check))).body[0]
    run_index=next(i for i,n in enumerate(node.body) if isinstance(n,ast.Try))
    generator=copy.deepcopy(node);generator.name='collision_rows'
    statement=generator.body[run_index];loop=next(n for n in statement.body if isinstance(n,ast.For))
    receive=ast.parse('incoming = yield locals()\nu,duration = incoming').body
    statement.body=[statement.body[0],ast.While(test=ast.Constant(True),body=receive+copy.deepcopy(loop.body),orelse=[])]
    # Original exception/failure return stays after the suspended loop.
    finishbody=[copy.deepcopy(node.body[run_index].body[-1])]+copy.deepcopy(node.body[run_index+1:])
    finish=function('', 'finish_collision',['result','state','minimum'],finishbody)
    module=ast.fix_missing_locations(ast.Module(body=[generator,finish],type_ignores=[]))
    namespace=dict(EdgeCollisionChecker.check.__globals__);exec(compile(module,'<ORIGINAL_COLLISION_RESUMABLE_ROW_LOOP>','exec'),namespace)
    return namespace['collision_rows'],namespace['finish_collision'],ast.unparse(module)


SCHEMA,C_ROWS,C_FINISH,C_NAMES,C_GENERATED=compile_C()
COLLISION_ROWS,COLLISION_FINISH,COLLISION_GENERATED=compile_collision()


class StreamRejected(RuntimeError):
    def __init__(self,role,value):self.role,self.value=role,value;super().__init__(role+':'+str(value))


class PrefixStream:
    def __init__(self,template,c_context,collision_context):
        self.Cctx=c_context;self.COctx=collision_context
        self.trace=copy.deepcopy(template);self.trace.update(controls=[],durations=[],states=[list(template['state_start'])])
        self.immutable={k:copy.deepcopy(v) for k,v in self.trace.items() if k not in ('controls','durations','states')}
        self.wall=dict(C_s=0.,COLLISION_s=0.,snapshots_s=0.);self.rows=0;self.failed=None
        self.cg=C_ROWS(self.trace,[],0.,c_context.provider)
        self.cog=COLLISION_ROWS(collision_context.checker,self.trace)
        try:
            with c_context.evaluation():self.ce=next(self.cg)
        except StopIteration as exc:raise StreamRejected('C',exc.value) from exc
        try:
            with collision_context.evaluation():self.coe=next(self.cog)
        except StopIteration as exc:raise StreamRejected('COLLISION',exc.value) from exc
        self.initial_C_state=np.array(self.ce['state'],copy=True)
        self.initial_collision_state=np.array(self.coe['state'],copy=True)

    def add(self,u,d,observed_node):
        if self.failed is not None:raise RuntimeError('PREFIX_STREAM_ALREADY_FAILED')
        if any(isinstance(x,bool) for x in [u,d]) or not math.isfinite(u) or not math.isfinite(d) or d<=0 or abs(u)>10/8.513422179807296+1e-12:
            raise RuntimeError('PREFIX_STREAM_CONTROL_SCHEMA')
        node=np.asarray(observed_node,float)
        if node.shape!=(3,) or not np.isfinite(node).all():raise RuntimeError('PREFIX_STREAM_NODE_SCHEMA')
        i=self.rows;self.trace['controls'].append(float(u));self.trace['durations'].append(float(d));self.trace['states'].append(node.tolist())
        started=time.perf_counter()
        try:
            with self.Cctx.evaluation():self.ce=self.cg.send((i,float(u),float(d),node))
        except StopIteration as exc:
            self.failed=('C',exc.value);raise StreamRejected('C',exc.value) from exc
        finally:self.wall['C_s']+=time.perf_counter()-started
        started=time.perf_counter()
        try:
            with self.COctx.evaluation():self.coe=self.cog.send((float(u),float(d)))
        except StopIteration as exc:
            self.failed=('COLLISION',exc.value);raise StreamRejected('COLLISION',exc.value) from exc
        finally:self.wall['COLLISION_s']+=time.perf_counter()-started
        self.rows+=1

    def snapshot(self):
        begin=time.perf_counter()
        if self.failed is not None:raise RuntimeError('PREFIX_STREAM_FAILED_SNAPSHOT')
        self.Cctx.assert_bound();self.COctx.assert_bound()
        if any(self.trace.get(k)!=v for k,v in self.immutable.items()):raise RuntimeError('PREFIX_STREAM_IMMUTABLE_METADATA')
        schema=SCHEMA(self.trace,self.trace['controls'],math.fsum(self.trace['durations']),self.Cctx.provider)
        if schema.get('schema_ok') is not True:raise RuntimeError('PREFIX_STREAM_FINAL_SCHEMA:'+str(schema))
        ce=dict(self.ce);ce['violation']=schema['violation']
        c=C_FINISH(**{name:ce[name] for name in C_NAMES})
        collision=COLLISION_FINISH(copy.deepcopy(self.coe['result']),self.coe['state'],self.coe['minimum'])
        value=copy.deepcopy(dict(C=c,collision=collision))
        self.wall['snapshots_s']+=time.perf_counter()-begin
        return value

    def statistics(self):
        return dict(rows=self.rows,wall_s=dict(self.wall),C_queries=self.ce['count'],collision_geometry_queries=self.coe['result']['query_count'],
            collision_intervals=self.coe['result']['interval_count'],future_controls_used=False,plant_state_used_as_integration_restart=False,
            independent_C_start=self.initial_C_state.tolist(),independent_collision_start=self.initial_collision_state.tolist(),
            stream_C_provider_queries=self.Cctx.raw.query_stats(),stream_collision_provider_queries=self.COctx.raw.query_stats())
