"""Spatial presentation policy, separate from meteorological feasibility.

Repeated grid cells and later arrival labels are NOT pruned. Only actual
retracing, geometric cycles and concentrated repeated turning are assessed.
"""
import math
from .planning_control import checkpoint
from .route_service import haversine_km

MODEL = 'NO_BACKTRACKING_OR_LOCAL_CIRCLING_V1'
MIN_LEG_KM = .05


def assess_route_quality(points, speed=10.):
    from .route_refinement import clean_points, intersections
    points = clean_points(points)
    if len(points)<2: return dict(model=MODEL,acceptable=False,reason_codes=['invalid_geometry'])
    lengths = [haversine_km(a,b) for a,b in zip(points,points[1:])]
    offsets = [0.]
    for length in lengths: offsets.append(offsets[-1]+length)
    headings = [math.atan2((b[0]-a[0])*math.cos(math.radians((a[1]+b[1])/2)),b[1]-a[1])
                for a,b in zip(points,points[1:])]
    turns = [abs((b-a+math.pi)%(2*math.pi)-math.pi)*180/math.pi for a,b in zip(headings,headings[1:])]
    reversals = [i+1 for i,angle in enumerate(turns)
                 if angle>=150 and min(lengths[i:i+2])>=MIN_LEG_KM]
    cycles=[]
    for i,(a,b) in enumerate(zip(points,points[1:])):
        checkpoint()
        for j in range(i+1,len(points)-1):
            c,d=points[j:j+2]
            if max(a[0],b[0])+1e-9<min(c[0],d[0]) or max(c[0],d[0])+1e-9<min(a[0],b[0]): continue
            if max(a[1],b[1])+1e-9<min(c[1],d[1]) or max(c[1],d[1])+1e-9<min(a[1],b[1]): continue
            for t,u in intersections(a,b,c,d):
                removed=offsets[j]+u*lengths[j]-offsets[i]-t*lengths[i]
                if removed>=MIN_LEG_KM:
                    cycles.append((i,j,removed));break
    local=[]
    diameter_limit=min(20.,max(1.,float(speed)*600/1000*2))
    for i in range(len(points)-3):
        west=east=points[i][0];south=north=points[i][1];turn=0.
        for j in range(i+1,len(points)):
            west=min(west,points[j][0]);east=max(east,points[j][0])
            south=min(south,points[j][1]);north=max(north,points[j][1])
            diameter=haversine_km((west,south),(east,north))
            if diameter>diameter_limit: break
            if j>=i+2 and min(lengths[j-2:j])>=MIN_LEG_KM: turn+=turns[j-2]
            travelled=offsets[j]-offsets[i]
            if turn>=540 and travelled>=max(.1,2*diameter) and travelled>=3*max(.05,haversine_km(points[i],points[j])):
                local.append((i,j));break
    reasons=[]
    if reversals: reasons.append('u_turn')
    if cycles: reasons.append('spatial_cycle')
    if local: reasons.append('local_circling')
    return dict(model=MODEL,acceptable=not reasons,reason_codes=reasons,u_turn_count=len(reversals),
                cycle_count=len(cycles),local_circling_count=len(local),
                max_cycle_distance_km=max((value[2] for value in cycles),default=0.),
                reversal_indices=reversals[:12],local_spans=local[:12])


def quality_shortcuts(points, speed=10.):
    """Propose shorter geometry; caller must retime and recheck ALL edges."""
    quality=assess_route_quality(points,speed)
    proposals=[]
    for index in quality['reversal_indices']:
        proposals.append(points[:index]+points[index+1:])
    for begin,end in quality['local_spans']:
        proposals.append(points[:begin+1]+points[end:])
    return proposals


def simple_geometry_seed(points, speed=10.):
    """Geometry guidance only; it is NOT certified at any departure time."""
    from .route_refinement import clean_points, cycle_proposals
    points=clean_points(points)
    for _ in range(len(points)):
        checkpoint()
        if assess_route_quality(points,speed)['acceptable']: return points
        cycles=cycle_proposals(points)
        proposals=cycles or quality_shortcuts(points,speed)
        if not proposals: return None
        points=proposals[0]
    return points if assess_route_quality(points,speed)['acceptable'] else None
