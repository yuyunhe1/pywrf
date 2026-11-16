"""Phase2B.4 family engine: CSC/SC_2PI/CC_2PI/CCC, rank C-valid candidates.

Geometry derives from the frozen ground dynamics. Root audits explicitly retain
the difference between numerical enumeration and certified completeness.
"""
import math
import time
import numpy as np
from .roots import TWO_PI, EPS, partition_roots, csc_roots, unique
from phase2b4.validator import validate_case as validate


def e(h):
    return np.array([math.cos(h), math.sin(h)])


def j(v):
    return np.array([-v[1],v[0]])


def wrap(h):
    return math.atan2(math.sin(h),math.cos(h))


def angle(h):
    # Numerical representation of exact modulo boundaries; never enlarge C limits.
    out = h % TWO_PI
    return 0. if min(out,TWO_PI-out) <= 32*EPS*max(1.,abs(h)) else out


def propagate(heading, controls, durations, beta):
    point = np.zeros(2)
    nodes = [[0.,0.,heading]]
    for u,duration in zip(controls,durations):
        if u == 0:
            point += duration*e(heading)
        else:
            last = heading+u*duration
            half = u*duration/2
            sinc = math.sin(half)/half if half != 0 else 1.
            point += duration*sinc*e(heading+half)
            heading = last
        point += beta*duration
        nodes.append([*point,heading])
    return point,heading,nodes


class GeneralWindSolver:
    def __init__(self, case):
        self.case = case
        self.q0, self.qf = np.asarray(case['q0'],float),np.asarray(case['qf'],float)
        self.va,self.radius = float(case['Va']),float(case['R0'])
        if self.q0.shape != (3,) or self.qf.shape != (3,) or np.asarray(case['wind']).shape != (2,):
            raise ValueError('State shape must be (3,), wind shape (2,)')
        if self.va != 10. or self.radius <= 0:
            raise ValueError('Phase2B.4 requires Va=10 and R0>0')
        self.beta = np.asarray(case['wind'],float)/self.va
        if not np.isfinite(np.r_[self.q0,self.qf,self.beta,self.radius]).all():
            raise ValueError('Nonfinite input')
        if np.linalg.norm(self.beta) >= 1:
            raise ValueError('Frozen scope requires |wind|<Va')
        self.p = (self.qf[:2]-self.q0[:2])/self.radius
        self.h0,self.hf = self.q0[2],self.qf[2]
        self.candidates,self.rejected,self.audit = [],[],[]
        self.roots, self.events, self.family_execution = [], [], {}

    def register_root(self, family, value, branch, source='EQUATION_ROOT'):
        root_id = len(self.roots)
        self.roots.append({'root_id': root_id, 'family': family, 'value': float(value),
                           'branch': branch, 'source': source, 'status': 'PENDING'})
        return root_id

    def mark(self, event, status, reason=None):
        event.update({'status': status, 'reason': reason})
        rid = event['root_id']
        if rid is not None:
            root = self.roots[rid]
            if root['status'] != 'ACCEPTED' or status == 'ACCEPTED':
                root.update({'status': status, 'reason': reason})

    def reject_root(self, root_id, reason, parameters):
        root = self.roots[root_id]
        root.update({'status': 'REJECTED', 'reason': reason})
        self.rejected.append({'family': root['family'], 'root_id': root_id, 'reason': reason,
                              'parameters': parameters, 'valid': False, 'trajectory': None, 'time': None,
                              'validation_status': 'NOT_VALIDATED'})

    def accept(self,family,controls,durations,parameters,root_id=None,source='ROOT_RECOVERY'):
        event = {'family': family, 'root_id': root_id, 'source': source, 'status': 'PENDING'}
        self.events.append(event)
        duration = np.asarray(durations,float)
        if not np.isfinite(duration).all() or np.min(duration,initial=0) < -64*EPS:
            self.rejected.append({'family':family,'reason':'PARAMETER_DOMAIN',
                                  'parameters':parameters,'trajectory':None,'time':None,'valid':False,
                                  'root_id':root_id,'validation_status':'NOT_VALIDATED'})
            self.mark(event, 'REJECTED', 'PARAMETER_DOMAIN')
            return
        duration = np.maximum(duration,0.)
        point,heading,nodes = propagate(self.h0,controls,duration,self.beta)
        pos = float(np.linalg.norm(point-self.p))
        head = abs(wrap(heading-self.hf))
        # Strict pre-C substitution checks, tighter than inherited C limits.
        if pos > 1e-8 or head > 1e-9:
            self.rejected.append({'family':family,'reason':'BACK_SUBSTITUTION',
                                  'parameters':parameters,'position_over_R0':pos,'heading_rad':head,
                                  'trajectory':None,'time':None,'valid':False,
                                  'root_id':root_id,'validation_status':'NOT_VALIDATED'})
            self.mark(event, 'REJECTED', 'BACK_SUBSTITUTION')
            return
        physical_nodes = [[self.q0[0]+self.radius*n[0], self.q0[1]+self.radius*n[1],n[2]]
                          for n in nodes]
        trajectory = {'type':'piecewise_constant','controls':(np.asarray(controls)*self.va/self.radius).tolist(),
                      'durations':(duration*self.radius/self.va).tolist(),
                      'states':physical_nodes,'control_interpolation':'ZOH',
                      'state_start':self.case['q0'],'state_goal':self.case['qf'],
                      'parameters':{'Va':self.va,'R0':self.radius},
                      'units':{'state':'m,m,rad_air','control':'rad/s','duration':'s'}}
        c_result = validate(self.case,trajectory)
        record = {'family':family,'parameters':parameters,'time':float(sum(duration)*self.radius/self.va),
                  'trajectory':trajectory,'valid':c_result['valid'],'C':c_result,
                  'root_id':root_id,'validation_status':c_result['validation_status'],
                  'back_substitution':{'position_over_R0':pos,'heading_rad':head}}
        if not record['valid']:
            record.update({'reason':'C_REJECTION','trajectory':None,'time':None})
            self.rejected.append(record)
            self.mark(event, 'REJECTED', 'C_REJECTION')
            return
        # Keep different controls/families and arbitrarily close different roots.
        key = (family,tuple(controls),tuple(duration))
        if not any(x.get('_key') == key for x in self.candidates):
            record['_key'] = key
            self.candidates.append(record)
        self.mark(event, 'ACCEPTED')

    def sc2pi(self):
        velocity = e(self.h0)+self.beta
        remainder = self.p-TWO_PI*self.beta
        ell = float(remainder@velocity/(velocity@velocity))
        self.audit.append({'family':'SC_2PI','method':'vector_projection','ell':ell,
                           'equation':'(velocity dot velocity)*ell - remainder dot velocity = 0',
                           'roots':[ell], 'root_solving':'closed_form_linear_equation',
                           'ground_speed_squared':float(velocity@velocity),
                           'full_vector_residual':float(np.linalg.norm(remainder-ell*velocity))})
        root_id = self.register_root('SC_2PI', ell, 'SHARED_STRAIGHT_PARAMETER')
        for sigma in (1,-1):
            self.accept('SC_2PI',[0,sigma],[ell,TWO_PI],
                        {'pattern':'SL_2PI' if sigma==1 else 'SR_2PI','ell':ell,'circle':TWO_PI}, root_id)

    def cc2pi(self):
        b2 = float(self.beta@self.beta)
        for sigma in (1,-1):
            d = self.p-sigma*j(e(self.h0))-TWO_PI*self.beta
            coefficients = [float(d@d-1),float(-2*d@self.beta),b2]
            def f(a):
                return (coefficients[2]*a+coefficients[1])*a+coefficients[0]
            if b2 == 0:
                roots = [angle(sigma*(self.hf-self.h0))]
                audit = {'method':'zero_wind_heading_recovery',
                         'degeneracy':'CONSTANT_POLYNOMIAL','constant':coefficients[0]}
            else:
                vertex = -coefficients[1]/(2*b2)
                points = [0.,TWO_PI]+([vertex] if 0<vertex<TWO_PI else [])
                roots,audit = partition_roots(f,points,sum(abs(c)*TWO_PI**i for i,c in enumerate(coefficients)))
                audit['method'] = 'quadratic_derivative_partition_brentq'
            audit.update({'family':'CC_2PI','sigma':sigma,'coefficients_ascending':coefficients,
                          'roots':roots,'domain':[0.,TWO_PI],'upper_exclusive':True})
            heading_alpha = angle(sigma*(self.hf-self.h0))
            derivative = abs(2*b2*heading_alpha+coefficients[1])
            coefficient_error = 128*EPS*max(1.,sum(abs(c)*TWO_PI**i for i,c in enumerate(coefficients)))
            if b2 != 0 and derivative < coefficient_error/1e-9 and abs(f(heading_alpha)) <= coefficient_error:
                # Preserve nonzero coefficients/search. Near zero wind amplifies input
                # rounding in the eliminated equation; recover phase from heading and
                # require the same strict vector substitution and C, not a looser root.
                audit['conditioning'] = 'HEADING_RECOVERY_AFTER_ILL_CONDITIONED_POLYNOMIAL_SEARCH'
                audit['heading_recovery_alpha'] = heading_alpha
                roots = unique([*roots,heading_alpha])
            self.audit.append(audit)
            for a in roots:
                source = ('EXPLICIT_HEADING_RECOVERY' if audit.get('heading_recovery_alpha') == a
                          else ('ZERO_WIND_HEADING_RECOVERY' if b2 == 0 else 'EQUATION_ROOT'))
                root_id = self.register_root('CC_2PI', a, {'sigma':sigma}, source)
                if 0 <= a < TWO_PI:
                    self.accept('CC_2PI',[sigma,-sigma],[a,TWO_PI],
                                {'pattern':'LR_2PI' if sigma==1 else 'RL_2PI',
                                 'sigma':sigma,'alpha':a,'full_circle':TWO_PI,'scalar_residual':f(a)},root_id)
                else:
                    self.reject_root(root_id,'ROOT_DOMAIN',{'alpha':a,'sigma':sigma})

    def ccc(self):
        b2 = float(self.beta@self.beta)
        delta = wrap(self.hf-self.h0)
        for sigma in (1,-1):
            # Physical lift interval follows a-b+c with each arc in [0,2pi).
            for k in range(-3,4):
                hk = sigma*(delta+TWO_PI*k)
                if not -TWO_PI < hk < 2*TWO_PI:
                    continue
                d0 = self.p-self.beta*hk+sigma*(j(e(self.hf))-j(e(self.h0)))
                dot = float(d0@self.beta)
                constant = float(d0@d0-8)
                def f(b):
                    return 4*b2*b*b-4*dot*b+8*math.cos(b)+constant
                def df(b):
                    return 8*b2*b-4*dot-8*math.sin(b)
                inflection = math.acos(b2)
                stationary,da = partition_roots(df,[0.,inflection,TWO_PI-inflection,TWO_PI],
                                                8*b2*TWO_PI+4*abs(dot)+8)
                roots,audit = partition_roots(f,[0.,*stationary,TWO_PI],
                                             4*b2*TWO_PI**2+4*abs(dot)*TWO_PI+8+abs(constant))
                audit.update({'family':'CCC','sigma':sigma,'k':k,'h_k':hk,'roots':roots,
                              'coefficients':[4*b2,-4*dot,8.,constant],
                              'method':'analytic_G_second_derivative_then_brentq',
                              'first_derivative_search':da})
                self.audit.append(audit)
                for b in roots:
                    root_id = self.register_root('CCC', b, {'sigma':sigma,'k':k})
                    if b == 0:
                        # Merge equal-turn arcs. Cycles represented by SC/CC, not hidden here.
                        if 0 <= hk < TWO_PI:
                            self.accept('CCC',[sigma],[hk],{'pattern':'C_DEGENERATE',
                                        'sigma':sigma,'k':k,'a':hk,'b':0.,'c':0.}, root_id)
                        else:
                            self.reject_root(root_id,'ZERO_MIDDLE_LIFT_DOMAIN',{'h_k':hk})
                        continue
                    if not 0 < b < TWO_PI:
                        self.reject_root(root_id,'ROOT_DOMAIN',{'b':b})
                        continue
                    d = d0-2*b*self.beta
                    # Direct integration gives D=+4*sin(b/2)*e(mid), not minus.
                    a = angle(sigma*(math.atan2(d[1],d[0])-self.h0)+b/2)
                    c = b+hk-a
                    if c < -64*EPS or c >= TWO_PI or a >= TWO_PI:
                        self.reject_root(root_id,'HEADING_LIFT_DOMAIN',
                                         {'a':a,'b':b,'c':c,'k':k,'sigma':sigma})
                        continue
                    self.accept('CCC',[sigma,-sigma,sigma],[a,b,c],
                                {'pattern':'LRL' if sigma==1 else 'RLR','sigma':sigma,'k':k,
                                 'a':a,'b':b,'c':c,'middle_branch':'SHORT' if b<math.pi else
                                 ('PI_BOUNDARY' if b==math.pi else 'LONG'),'scalar_residual':f(b)},root_id)

    def csc(self):
        boundaries = unique([0.,angle(self.h0),angle(self.hf),TWO_PI])
        for sigma in (1,-1):
            for kappa in (1,-1):
                m = sigma-kappa
                def recover(h,root_id=None,source='BOUNDARY_CHECK'):
                    a = angle(sigma*(h-self.h0))
                    c = angle(kappa*(self.hf-h))
                    # Use original arc displacements for back substitution, including boundary branches.
                    arcs,_,_ = propagate(self.h0,[sigma,kappa],[a,c],np.zeros(2))
                    v = e(h)+self.beta
                    remainder = self.p-arcs-self.beta*(a+c)
                    ell = float(remainder@v/(v@v))
                    self.accept('CSC',[sigma,0,kappa],[a,ell,c],
                                {'pattern':('L' if sigma==1 else 'R')+'S'+('L' if kappa==1 else 'R'),
                                 'a':a,'ell':ell,'c':c,'middle_heading':h},root_id,source)
                for boundary in boundaries[:-1]:
                    recover(boundary)
                for lo,hi in zip(boundaries[:-1],boundaries[1:]):
                    midpoint = (lo+hi)/2
                    t0 = angle(sigma*(midpoint-self.h0))+angle(kappa*(self.hf-midpoint))-m*midpoint
                    K = self.p-sigma*j(e(self.h0))+kappa*j(e(self.hf))-self.beta*t0
                    coeff = [float(K[0]-m*self.beta[1]),float(-K[1]-m*self.beta[0]),
                             float(m*self.beta[1]),float(-m*self.beta[0]),
                             float(K[0]*self.beta[1]-K[1]*self.beta[0]-m)]
                    roots,audit = csc_roots(coeff,lo,hi)
                    audit.update({'family':'CSC','sigma':sigma,'kappa':kappa,'branch':[lo,hi],
                                  'arc_time_intercept':t0,'roots':roots})
                    if audit.get('degeneracy') == 'IDENTICALLY_ZERO':
                        # Typically zero-wind coincident circles: one arc suffices.
                        # Do not claim enumerating a feasible continuum via endpoints.
                        audit['continuum'] = True
                        audit['numerical_unresolved'] = 'CSC_CONTINUUM_NOT_ENUMERATED'
                    self.audit.append(audit)
                    for h in roots:
                        root_id = self.register_root('CSC', h, {'sigma':sigma,'kappa':kappa,'branch':[lo,hi]})
                        if lo < h < hi:
                            recover(h,root_id,'EQUATION_ROOT')
                        else:
                            self.roots[root_id].update({'status':'REDUNDANT_BOUNDARY',
                                                        'reason':'BOUNDARY_HANDLED_SEPARATELY'})

    def run(self,families=None):
        started = time.perf_counter()
        self.candidates,self.rejected,self.audit = [],[],[]
        self.roots,self.events,self.family_execution = [],[],{}
        families = families or ('CC_2PI','CCC','SC_2PI','CSC')
        methods = {'CC_2PI':self.cc2pi,'CCC':self.ccc,'SC_2PI':self.sc2pi,'CSC':self.csc}
        # Only exact input identity receives the contractual T=0 candidate.
        if 'CSC' in families and np.array_equal(self.q0[:2],self.qf[:2]) and abs(wrap(self.hf-self.h0)) <= 1e-14:
            self.accept('CSC',[],[],{'pattern':'IDENTICAL_STATE'},source='EXACT_IDENTITY')
        for family in families:
            before = time.perf_counter()
            try:
                methods[family]()
                self.family_execution[family] = {'status':'READY','error':None}
            except (ArithmeticError, ValueError, RuntimeError) as exc:
                # Explicit partial execution, never return early or substitute another family.
                self.family_execution[family] = {'status':'FAILED','error':repr(exc)}
            self.family_execution[family]['elapsed_s'] = time.perf_counter()-before
        self.candidates.sort(key=lambda x:x['time'])
        for candidate in self.candidates:
            candidate.pop('_key',None)
        best = self.candidates[0] if self.candidates else None
        metrics = {}
        for family in families:
            roots = [r for r in self.roots if r['family'] == family]
            unresolved = [a for a in self.audit if a['family'] == family and a.get('numerical_unresolved')]
            pending = [r for r in roots if r['status'] == 'PENDING']
            execution = self.family_execution[family]
            status = execution['status']
            if status == 'READY' and (unresolved or pending):
                status = 'PARTIAL'
            metrics[family] = {**execution,'status':status,'executed':True,
                'candidate_count':sum(c['family']==family for c in self.candidates),
                'root_count':len(roots),
                'rejected_root_count':sum(r['status']=='REJECTED' for r in roots),
                'accepted_root_count':sum(r['status']=='ACCEPTED' for r in roots),
                'redundant_boundary_root_count':sum(r['status']=='REDUNDANT_BOUNDARY' for r in roots),
                'pending_root_count':len(pending),
                'rejected_candidate_attempt_count':sum(r['family']==family for r in self.rejected),
                'unresolved':unresolved,'no_candidate_is_not_a_solver_failure':True}
        implementation_status = ('FAILED' if not best else
                                 ('PARTIAL' if any(m['status'] != 'READY' for m in metrics.values()) else 'READY'))
        return {'status':'SUCCESS' if best else 'NO_VALID_CANDIDATE',
                'implementation_status':implementation_status,
                'valid':best is not None,'validation_status':'PASSED' if best else 'NO_VALID_TRAJECTORY',
                'family':best['family'] if best else None,
                'time':best['time'] if best else None,'trajectory':best['trajectory'] if best else None,
                'candidate_count':len(self.candidates),'candidates':self.candidates,
                'root_count':len(self.roots),'rejected_root_count':sum(r['status']=='REJECTED' for r in self.roots),
                'family_metrics':metrics,'root_records':self.roots,'candidate_attempts':self.events,
                'rejected':self.rejected,'root_audit':self.audit,
                'elapsed_s':time.perf_counter()-started,
                'coverage':{'implemented_families':list(families),'all_roots_certified':False,
                            'global_optimality_guaranteed':False,
                            'unresolved':[a for a in self.audit if a.get('numerical_unresolved')]}}


def solve(case,families=None):
    return GeneralWindSolver(case).run(families)
