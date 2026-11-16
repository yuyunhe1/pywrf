"""Static WRF interfaces; the frozen Phase2B solver is an external dependency."""
from .adapter import StaticWRFWindAdapter, AdapterError
from .coordinates import ROI, WRFCoordinates
from .domain import wind_domain_validator
from .connector import StaticSnapshotConnector

__all__ = ['StaticWRFWindAdapter', 'AdapterError', 'ROI', 'WRFCoordinates',
           'wind_domain_validator', 'StaticSnapshotConnector']
