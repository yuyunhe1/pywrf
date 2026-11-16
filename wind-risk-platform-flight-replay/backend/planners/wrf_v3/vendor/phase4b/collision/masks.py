"""Static obstacle mask protocol: conservative distance to occupied geometry."""
import math
import numpy as np
from phase4a_design.mock_contract import content_hash


class RectangleMask:
    def __init__(self, rectangles):
        self.rectangles = tuple(tuple(map(float, rect)) for rect in rectangles)
        if any(len(r)!=4 or not all(math.isfinite(v) for v in r) or r[0]>=r[1] or r[2]>=r[3]
               for r in self.rectangles):
            raise ValueError('Finite positive-area rectangles (xmin,xmax,ymin,ymax) required')
        self.source = dict(kind='STATIC_RECTANGLE_UNION', rectangles=self.rectangles,
                           sha256=content_hash(self.rectangles))

    def distance_lower_bound(self, x, y):
        return min((math.hypot(max(r[0]-x,0.,x-r[1]),max(r[2]-y,0.,y-r[3]))
                    for r in self.rectangles), default=math.inf)


class EmptyMask(RectangleMask):
    def __init__(self):
        super().__init__([])


class GridObstacleMask(RectangleMask):
    """True raster cells represent their entire closed rectangles, not centers."""
    def __init__(self, occupied, *, origin_xy, resolution_xy):
        a = np.asarray(occupied)
        if a.ndim!=2 or a.dtype!=bool:
            raise ValueError('Boolean 2D mask required')
        x0,y0 = map(float,origin_xy);dx,dy = map(float,resolution_xy)
        if not all(math.isfinite(v) for v in (x0,y0,dx,dy)) or min(dx,dy)<=0:
            raise ValueError('Finite origin and positive grid resolution required')
        super().__init__([(x0+i*dx,x0+(i+1)*dx,y0+j*dy,y0+(j+1)*dy)
                          for j,i in np.argwhere(a)])
        self.source = dict(kind='STATIC_BOOLEAN_GRID_CLOSED_CELLS', shape=list(a.shape),
                           origin_xy=[x0,y0], resolution_xy=[dx,dy],
                           sha256=content_hash(dict(cells=a.tolist(),origin=[x0,y0],resolution=[dx,dy])))
