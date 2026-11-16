"""Registered deadline wrapper for the independent temporal partition backend."""
from phase_g61.dynamic_wind.partition import PartitionReplay as TemporalPartition,POLICY
from phase_g61.dynamic_wind.provider import TemporalWRF,MissionSuffixView
from .common import Timed,Stop

class PartitionReplay(TemporalPartition):
    def __init__(self,provider,*args,**kwargs):
        if type(provider) is Timed:
            source=provider.original
            if not isinstance(source,(TemporalWRF,MissionSuffixView)):raise Stop('UNREGISTERED_TIMED_PARTITION_SOURCE')
            source.assert_bound();provider.clock.check()
            super().__init__(source,*args,**kwargs)
            # Geometry detection uses the registered immutable source; every
            # actual RHS query retains this evaluator's original deadline.
            self.provider=provider
        else:super().__init__(provider,*args,**kwargs)
