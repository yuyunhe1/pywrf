"""Conservative footprint check on the independently integrated continuous curve.

The platform currently has no user obstacle masks. Terrain and native data
coverage are separately enclosed by the wind/flight certificate.
"""
from .native import R0


def check_geometry(curve,roi,check=lambda:None,radius_m=2.):
    minimum=float("inf")
    boxes=0
    def interval(index,a,b,depth=0):
        nonlocal minimum,boxes
        check()
        boxes+=1
        x,y,*_=curve.bounds(index,a,b)
        clearance=min(x.lo-roi.xmin,roi.xmax-x.hi,y.lo-roi.ymin,roi.ymax-y.hi)-radius_m
        if clearance>R0*1e-6:
            minimum=min(minimum,clearance)
            return True
        if depth>=16 or b-a<=1e-5:
            return False
        mid=(a+b)/2
        return interval(index,a,mid,depth+1) and interval(index,mid,b,depth+1)
    valid=all(interval(i,float(curve.breaks[i]),float(curve.breaks[i+1])) for i in range(len(curve.coefficients)))
    return {"valid":valid,"method":"OUTWARD_CONTINUOUS_POLYNOMIAL_FOOTPRINT",
            "status":"CLEAR_POLYNOMIAL_TUBE" if valid else "COLLISION_OR_ROI_FOOTPRINT_UNCONFIRMED",
            "radius_m":radius_m,"ode_margin_m":R0*1e-6,"interval_count":boxes,
            "sampled_min_clearance_m":minimum if minimum!=float("inf") else None,"obstacle_mask":[]}
