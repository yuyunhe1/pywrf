"""Independent C: physical DOP853 replay, no solver propagation or A imports."""
import math
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA, R0
from phase3a_implementation.coordinates import FRAME_ID, HEIGHT_DATUM

THRESHOLDS = {'position_over_R0':1e-6,'heading_rad':1e-7,
              'control_over_umax':1e-8,'interior_node_scaled':1e-6}


def validate(trajectory, control, time, provider, *, max_step=.1):
    base = {'valid':False,'validation_status':'FAILED','time':None,'trajectory':None,
            'position_error':None,'heading_error':None,'control_violation':None,
            'terminal_residual':None,'constraint_residual':None,'failures':[],
            'domain_status':'NOT_CHECKED','wind_history':[],'beta_history':[],
            'thresholds':dict(THRESHOLDS),'method':'INDEPENDENT_DOP853',
            'domain_coverage':'ODE_STAGE_AND_SAMPLED_POINTS_NOT_CONTINUOUS_CERTIFICATE'}
    try:
        q0,qf = [np.asarray(trajectory[k],float) for k in ('state_start','state_goal')]
        us = np.asarray(control,float)
        ds = np.asarray(trajectory['durations'],float)
        z,total = float(trajectory['z_layer_msl_m']),float(time)
        nodes = np.asarray(trajectory['states'],float) if 'states' in trajectory else None
        if q0.shape != (3,) or qf.shape != (3,) or us.ndim != 1 or ds.shape != us.shape:
            raise ValueError('State/control/duration shape')
        if nodes is not None and nodes.shape != (len(us)+1,3):
            raise ValueError('Stored nodes shape')
        if trajectory['type'] != 'piecewise_constant' or trajectory['control_interpolation'] != 'ZOH':
            raise ValueError('Piecewise constant ZOH required')
        if trajectory['parameters'] != {'Va':VA,'R0':R0}:
            raise ValueError('Frozen parameters mismatch')
        if not np.array_equal(us,np.asarray(trajectory['controls'],float)):
            raise ValueError('Trajectory/control mismatch')
        if getattr(provider,'frame_id',None) != FRAME_ID or getattr(provider,'height_datum',None) != HEIGHT_DATUM:
            raise ValueError('Provider frame/datum mismatch')
        arrays = [q0,qf,us,ds,np.array([z,total,max_step])]
        if nodes is not None: arrays.append(nodes)
        if not all(np.isfinite(a).all() for a in arrays) or np.any(ds<0) or total<0 or max_step<=0:
            raise ValueError('Nonfinite, negative time or invalid step')
        summed = math.fsum(ds)
        if abs(total-summed)>64*np.finfo(float).eps*max(1.,total,summed):
            raise ValueError('TIME_MISMATCH')
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        return {**base,'failures':['INVALID_INPUT'],'detail':str(exc)}
    violation = float(max(0.,np.max(np.abs(us)/(VA/R0)-1.,initial=0.)))
    if violation > THRESHOLDS['control_over_umax']:
        return {**base,'control_violation':violation*(VA/R0),'failures':['CONTROL_BOUND']}
    state,t0 = q0.copy(),0.
    actual_nodes = [[0.,*state.tolist()]]
    histories,betas = [],[]
    count = 0
    beta_min,beta_max = math.inf,-math.inf
    last_query = None
    node_error = float(np.linalg.norm((nodes[0]-q0)/[R0,R0,1.])) if nodes is not None else 0.

    class QueryError(Exception): pass

    def query(t,q,record=False):
        nonlocal count,beta_min,beta_max,last_query
        s = provider.wind_query(float(q[0]),float(q[1]),z,max(0.,float(t)))
        count += 1
        last_query = {'t':float(t),'x':float(q[0]),'y':float(q[1]),'sample':s}
        if not s.get('valid',False): raise QueryError(s.get('status','WIND_QUERY_FAILED'))
        w = [float(s[k]) for k in ('wx','wy','wz')]
        if not all(math.isfinite(v) for v in w): raise QueryError('NONFINITE_WIND')
        beta = math.hypot(*w[:2])/VA
        last_query['beta'] = beta
        if beta >= 1.: raise QueryError('CONNECTOR_OUT_OF_DOMAIN')
        beta_min,beta_max = min(beta_min,beta),max(beta_max,beta)
        if record:
            histories.append({'t':float(t),'x':float(q[0]),'y':float(q[1]),'z':z,
                              'wx':w[0],'wy':w[1],'wz':w[2]})
            betas.append({'t':float(t),'beta':beta})
        return w
    try:
        query(0.,state,True)
        for i,(u,d) in enumerate(zip(us,ds)):
            if d>0:
                def rhs(t,q):
                    w=query(t,q)
                    return [VA*math.cos(q[2])+w[0],VA*math.sin(q[2])+w[1],float(u)]
                sol = solve_ivp(rhs,(t0,t0+float(d)),state,method='DOP853',rtol=1e-11,
                                atol=[R0*1e-12,R0*1e-12,1e-12],max_step=max_step,dense_output=True)
                if not sol.success: raise QueryError('INTEGRATION_FAILURE')
                # Histories sample the accepted solution, never rejected RK trial states.
                for t in np.linspace(t0,t0+float(d),max(2,math.ceil(float(d)/.25)+1))[1:]:
                    query(t,sol.sol(t),True)
                state=sol.y[:,-1]
            t0 += float(d)
            actual_nodes.append([t0,*state.tolist()])
            if nodes is not None:
                node_error=max(node_error,float(np.linalg.norm((nodes[i+1]-state)/[R0,R0,1.])))
    except QueryError as exc:
        return {**base,'failures':[str(exc)],'domain_status':str(exc),'wind_history':histories,
                'beta_history':betas,'failure_query':last_query,'query_count':count,
                'diagnostic_nodes':actual_nodes,'diagnostic_elapsed_s':t0,
                'diagnostic_final_state':state.tolist()}
    pos=float(np.linalg.norm(state[:2]-qf[:2]))
    head=abs(math.atan2(math.sin(state[2]-qf[2]),math.cos(state[2]-qf[2])))
    failures=[]
    if pos/R0>THRESHOLDS['position_over_R0']: failures.append('POSITION_RESIDUAL')
    if head>THRESHOLDS['heading_rad']: failures.append('HEADING_RESIDUAL')
    if node_error>THRESHOLDS['interior_node_scaled']: failures.append('INTERIOR_NODE_RESIDUAL')
    return {**base,'valid':not failures,'validation_status':'PASSED' if not failures else 'FAILED',
            'position_error':pos,'heading_error':head,'control_violation':violation*(VA/R0),
            'terminal_residual':{'position_over_R0':pos/R0,'heading_rad':head,
                                 'ground_position_m':(state[:2]-qf[:2]).tolist()},
            'constraint_residual':{'control_over_umax':violation,'interior_node_scaled':node_error},
            'domain_status':'IN_DOMAIN_AT_EVALUATED_POINTS','wind_history':histories,'beta_history':betas,
            'beta_range':[beta_min,beta_max],'query_count':count,'diagnostic_nodes':actual_nodes,
            'diagnostic_final_state':state.tolist(),'diagnostic_elapsed_s':t0,'failures':failures,
            'time':t0 if not failures else None,
            'trajectory':{'switch_nodes_t_x_y_psi':actual_nodes,'z_layer_msl_m':z} if not failures else None}
