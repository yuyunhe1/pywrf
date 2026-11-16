"""Map frames sampled from the same native MSL provider as the trajectory."""
import math
import numpy as np
from .native import iso


def frame(provider,request):
    c=provider.coordinates
    points=[c.local_to_lonlat(x,y) for x in (provider.roi.xmin,provider.roi.xmax) for y in (provider.roi.ymin,provider.roi.ymax)]
    count=int(request.get("resolution",64))
    lons=np.linspace(min(p[0] for p in points),max(p[0] for p in points),count)
    lats=np.linspace(max(p[1] for p in points),min(p[1] for p in points),count)
    u,v=[],[]
    for lat in lats:
        for lon in lons:
            x,y=c.lonlat_to_local(float(lon),float(lat))
            q=provider.diagnostic_query(x,y,provider.spec["altitude_msl_m"],0.)
            if q.get("valid"):
                u.append(q["earth_diagnostic"]["east"])
                v.append(q["earth_diagnostic"]["north"])
            else:
                u.append(None)
                v.append(None)
    header={"nx":count,"ny":count,"lo1":float(lons[0]),"la1":float(lats[0]),
            "lo2":float(lons[-1]),"la2":float(lats[-1]),"dx":float(lons[1]-lons[0]),"dy":float(lats[0]-lats[1]),
            "refTime":iso(provider.epoch),"forecastTime":0,"parameterCategory":2,"parameterUnit":"m.s-1",
            "gridDefinitionTemplate":0,"scanMode":0}
    return {"available":True,"metadata":{"source":"WRF · V3 原生 MSL","cycle":provider.spec["cycle"],
            "level":f"{provider.spec['altitude_msl_m']:g}m MSL","dataset_id":provider.spec["dataset_id"],
            "valid_time":iso(provider.epoch),"forecast_hour":(provider.epoch-provider.times[0])/3600,
            "altitude_reference":"MSL","altitude_msl_m":provider.spec["altitude_msl_m"]},
            "velocity":[{"header":{**header,"parameterNumber":2,"parameterNumberName":"U-component_of_wind"},"data":u},
                        {"header":{**header,"parameterNumber":3,"parameterNumberName":"V-component_of_wind"},"data":v}]}
