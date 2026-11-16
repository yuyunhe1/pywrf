"""Wind-aware queue ordering; never a feasibility mask or pruning bound.

The point's optimistic passage time selects its ten-minute frame. Actual search
labels and full-voyage validation still use their own accumulated flight time.
"""
import heapq
import math
from time import monotonic

import numpy as np

from .planning_control import checkpoint
from .route_service import haversine_km
from .routing import NEIGHBOURS
from .wrf_native_schema import NATIVE_WRF_TIME_MODEL

MODEL='TEN_MINUTE_PASSAGE_WIND_ORDER_V1'


class WindGuidance:
    def __init__(self,weather,start,rows,cols):
        self.weather,self.grid=weather,weather.grid
        self.rows,self.cols=set(rows),set(cols)
        self.potentials={}
        self.seconds=0.
        began=monotonic()
        lon,lat=np.meshgrid(self.grid.lons,self.grid.lats)
        h=np.sin(np.radians(lat-start[1])/2)**2+math.cos(math.radians(start[1]))*np.cos(np.radians(lat))*np.sin(np.radians(lon-start[0])/2)**2
        distance=12742*np.arcsin(np.sqrt(np.clip(h,0,1)))
        passage=weather.request.departure_at.timestamp()+distance*1000/weather.speed
        stamps=np.floor(passage/600)*600
        times=np.asarray([value for value,_ in weather.dataset.frames])
        left=np.clip(np.searchsorted(times,stamps,side='right')-1,0,len(times)-1)
        u,v=np.empty(lon.shape),np.empty(lon.shape)
        for index in np.unique(left):
            checkpoint()
            mask=left==index
            a=weather.dataset.frames[index][1]
            ua,va=weather.full_fields[a.valid_time]
            u[mask],v[mask]=ua[mask],va[mask]
            if weather.dataset.time_model!=NATIVE_WRF_TIME_MODEL and index+1<len(times) and abs(times[index+1]-times[index]-3600)<.001:
                b=weather.dataset.frames[index+1][1]
                ub,vb=weather.full_fields[b.valid_time]
                alpha=np.clip((stamps[mask]-times[index])/3600,0,1)
                u[mask]+=alpha*(ub[mask]-ua[mask]);v[mask]+=alpha*(vb[mask]-va[mask])
        magnitude=np.hypot(u,v)
        weights,thresholds=weather.config.weights,weather.config.thresholds
        risk=np.clip((magnitude-thresholds.safe_wind_speed)/max(1e-9,thresholds.max_wind_speed-thresholds.safe_wind_speed),0,1)
        exposure=np.clip(magnitude/max(1e-9,thresholds.max_wind_speed),0,1)**2
        # Missing/nominally dangerous cells retain a finite priority. They may
        # become safe at another arrival time, and must remain searchable.
        self.density=weights.alpha_distance+weights.beta_wind*np.nan_to_num(risk,nan=1)+weights.epsilon_wind_exposure*np.nan_to_num(exposure,nan=1)
        self.seconds=monotonic()-began

    def point(self,node):
        return float(self.grid.lons[node[1]]),float(self.grid.lats[node[0]])

    def potential(self,target):
        if target in self.potentials: return self.potentials[target]
        began=monotonic()
        values,queue={},[]
        rows=sorted(self.rows,key=lambda row:abs(float(self.grid.lats[row])-target[1]))[:2]
        cols=sorted(self.cols,key=lambda col:abs(float(self.grid.lons[col])-target[0]))[:2]
        for row in rows:
            for col in cols:
                node=(row,col)
                cost=haversine_km(self.point(node),target)*self.density[node]
                values[node]=cost;heapq.heappush(queue,(cost,node))
        count=0
        while queue:
            cost,node=heapq.heappop(queue)
            if cost!=values[node]: continue
            count+=1
            if count%256==0: checkpoint()
            row,col=node
            for dr,dc in NEIGHBOURS:
                other=(row+dr,col+dc)
                if other[0] not in self.rows or other[1] not in self.cols: continue
                density=max(self.density[node],self.density[other])
                if dr and dc: density=max(density,self.density[row,other[1]],self.density[other[0],col])
                candidate=cost+haversine_km(self.point(node),self.point(other))*density
                if candidate<values.get(other,math.inf):
                    values[other]=candidate;heapq.heappush(queue,(candidate,other))
        self.potentials[target]=values
        self.seconds+=monotonic()-began
        return values

    def hint(self,point,target):
        values=self.potential(target)
        row=int(np.abs(self.grid.lats-point[1]).argmin())
        col=int(np.abs(self.grid.lons-point[0]).argmin())
        return values.get((row,col),haversine_km(point,target)*self.weather.config.weights.alpha_distance)
