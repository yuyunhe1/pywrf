"""Fixed map-derived seeds with pre-registered control reserve, no route oracle."""
import copy,math
from phase3a_implementation.domain import VA,R0
RESERVE_FRACTION=.90

def reserve_seed(route,fraction=RESERVE_FRACTION):
    if not 0<fraction<=1:raise ValueError('RESERVE_FRACTION_DOMAIN')
    tr=copy.deepcopy(route)
    tr['controls']=[float(u)*fraction if u else 0. for u in route['controls']]
    tr['durations']=[float(d)/fraction if u else float(d) for u,d in zip(route['controls'],route['durations'])]
    return tr

def waypoint_order(case):
    if len(case['rectangles'])!=1:raise ValueError('FROZEN_SINGLE_RECTANGLE_REQUIRED')
    x0,x1,y0,y1=case['rectangles'][0];cx,cy=(x0+x1)/2,(y0+y1)/2
    clearance=2*(R0/RESERVE_FRACTION)+case['radius_m']+1.
    points=[(cx,y1+clearance),(cx,y0-clearance),(x0-clearance,cy),(x1+clearance,cy)]
    start,goal=case['route']['state_start'],case['route']['state_goal'];out=[]
    for x,y in points:
        psi=math.atan2(goal[1]-y,goal[0]-x);lift=psi+2*math.pi*math.floor((start[2]-psi)/(2*math.pi)+.5)
        out.append([x,y,lift])
    return out
