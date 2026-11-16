"""Frozen WRF spherical LCC and explicitly bounded local research frame."""
import math
from dataclasses import dataclass

FRAME_ID = 'WRF_LCC_LOCAL_METRIC_PLANE_V1'
HEIGHT_DATUM = 'WRF_MODEL_MSL_GEOPOTENTIAL'


@dataclass(frozen=True)
class ROI:
    xmin: float
    xmax: float
    ymin: float
    ymax: float

    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.xmin, self.xmax, self.ymin, self.ymax)):
            raise ValueError('ROI bounds must be finite')
        if self.xmin >= self.xmax or self.ymin >= self.ymax:
            raise ValueError('ROI must have positive area')

    def contains(self, x, y):
        return self.xmin <= x <= self.xmax and self.ymin <= y <= self.ymax

    def as_dict(self):
        return dict(xmin=self.xmin, xmax=self.xmax, ymin=self.ymin, ymax=self.ymax, units='m')


class WRFCoordinates:
    """Conversions do not assert physical trajectory validity over the full map."""
    frame_id = FRAME_ID
    radius_m = 6370000.0
    origin_lat_lon = (31.05935, 118.55255)

    def __init__(self, attributes, anchor_lat_lon, shape, *, roi):
        if not isinstance(roi, ROI):
            raise ValueError('An explicit ROI is required')
        self.roi = roi
        attrs = {k: float(attributes[k]) for k in
                 ('MAP_PROJ', 'GRID_ID', 'TRUELAT1', 'TRUELAT2', 'STAND_LON', 'MOAD_CEN_LAT', 'DX', 'DY')}
        if not all(math.isfinite(v) for v in attrs.values()):
            raise ValueError('Nonfinite projection metadata')
        if attrs['MAP_PROJ'] != 1 or attrs['GRID_ID'] != 2:
            raise ValueError('Only the frozen Lambert d02 contract is supported')
        if not (0 < attrs['TRUELAT1'] < attrs['TRUELAT2'] < 90):
            raise ValueError('Unsupported Lambert standard parallels')
        if not -90 < attrs['MOAD_CEN_LAT'] < 90 or not -180 <= attrs['STAND_LON'] <= 180:
            raise ValueError('Invalid projection origin')
        if attrs['DX'] <= 0 or attrs['DY'] <= 0:
            raise ValueError('Invalid grid spacing')
        self.attributes = attrs
        self.dx, self.dy = attrs['DX'], attrs['DY']
        self.ny, self.nx = shape
        if self.nx < 2 or self.ny < 2:
            raise ValueError('A bilinear mass grid requires at least 2 by 2 nodes')
        p1, p2 = math.radians(attrs['TRUELAT1']), math.radians(attrs['TRUELAT2'])
        tangent = lambda p: math.tan(math.pi / 4 + p / 2)
        self.n = math.log(math.cos(p1) / math.cos(p2)) / math.log(tangent(p2) / tangent(p1))
        self.f = math.cos(p1) * tangent(p1) ** self.n / self.n
        self.lon0 = math.radians(attrs['STAND_LON'])
        self.rho0 = self.radius_m * self.f / tangent(math.radians(attrs['MOAD_CEN_LAT'])) ** self.n
        lat, lon = self.origin_lat_lon
        self.origin_projected = self.lonlat_to_projected(lon, lat)
        self.m0 = self.map_factor(lat)
        self.anchor_projected = self.lonlat_to_projected(anchor_lat_lon[1], anchor_lat_lon[0])

    def lonlat_to_projected(self, lon, lat):
        lon, lat = float(lon), float(lat)
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 < lat < 90):
            raise ValueError('Invalid lon/lat degrees')
        rho = self.radius_m * self.f / math.tan(math.pi / 4 + math.radians(lat) / 2) ** self.n
        theta = self.n * (math.radians(lon) - self.lon0)
        return rho * math.sin(theta), self.rho0 - rho * math.cos(theta)

    def projected_to_lonlat(self, px, py):
        px, py = float(px), float(py)
        if not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError('Nonfinite projected meters')
        rho = math.hypot(px, self.rho0 - py)
        if rho == 0:
            raise ValueError('Projection pole is outside the supported domain')
        lat = 2 * math.atan((self.radius_m * self.f / rho) ** (1 / self.n)) - math.pi / 2
        lon = self.lon0 + math.atan2(px, self.rho0 - py) / self.n
        return math.degrees(lon), math.degrees(lat)

    def local_to_projected(self, x, y):
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError('Nonfinite local coordinates')
        return self.origin_projected[0] + self.m0*x, self.origin_projected[1] + self.m0*y

    def projected_to_local(self, px, py):
        if not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError('Nonfinite projected coordinates')
        return (px-self.origin_projected[0])/self.m0, (py-self.origin_projected[1])/self.m0

    def lonlat_to_local(self, lon, lat):
        return self.projected_to_local(*self.lonlat_to_projected(lon, lat))

    def local_to_lonlat(self, x, y):
        return self.projected_to_lonlat(*self.local_to_projected(x, y))

    def local_to_grid(self, x, y):
        px, py = self.local_to_projected(x, y)
        return (px-self.anchor_projected[0])/self.dx, (py-self.anchor_projected[1])/self.dy

    def grid_to_local(self, i, j):
        return self.projected_to_local(self.anchor_projected[0]+i*self.dx, self.anchor_projected[1]+j*self.dy)

    def map_factor(self, lat):
        phi = math.radians(float(lat))
        if not -math.pi/2 < phi < math.pi/2:
            raise ValueError('Latitude outside projection domain')
        rho = self.radius_m*self.f/math.tan(math.pi/4+phi/2)**self.n
        return self.n*rho/(self.radius_m*math.cos(phi))

    def summary(self):
        samples = []
        for y in (self.roi.ymin, (self.roi.ymin+self.roi.ymax)/2, self.roi.ymax):
            for x in (self.roi.xmin, (self.roi.xmin+self.roi.xmax)/2, self.roi.xmax):
                lon, lat = self.local_to_lonlat(x, y)
                samples.append({'x': x, 'y': y, 'lon': lon, 'lat': lat,
                                'map_factor': self.map_factor(lat),
                                'convergence_rad': self.n*(math.radians(lon)-self.lon0),
                                'grid_i_j': list(self.local_to_grid(x, y))})
        return {'frame_id': self.frame_id, 'roi': self.roi.as_dict(),
                'origin_lat_lon_deg': list(self.origin_lat_lon), 'origin_projected_m': list(self.origin_projected),
                'anchor_projected_m': list(self.anchor_projected), 'local_scale_m0': self.m0,
                'map_factor_samples': samples,
                'sampled_max_relative_metric_error': max(abs(s['map_factor']/self.m0-1) for s in samples),
                'sampled_convergence_range_rad': max(s['convergence_rad'] for s in samples)-min(s['convergence_rad'] for s in samples),
                'metric_scope': 'NINE_DETERMINISTIC_ROI_PROBES_NOT_A_CONTINUOUS_ERROR_CERTIFICATE',
                'geometry_model': 'LOCAL_PLANAR_APPROXIMATION', 'full_domain_validated': False}
