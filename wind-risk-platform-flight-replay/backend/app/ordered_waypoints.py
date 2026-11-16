"""A directed, layered native graph: visit every anchor before changing legs.

Time labels remain independent inside each layer. This is one search, not a
sequence of greedy searches with a single irrevocable arrival at each anchor.
"""
from .route_service import haversine_km
from .planning_control import checkpoint


class OrderedGraph:
    def __init__(self, weather, anchors, bbox):
        from .segmented_routing import NativeGraph
        self.weather, self.bbox = weather, bbox
        self.required = tuple(map(tuple, anchors))
        self.leg_count = len(self.required)-1
        first = NativeGraph(weather, self.required[0], self.required[1], bbox)
        self.legs = {0:first}
        self.start, self.goal = ('anchor', 0), ('anchor', len(self.required)-1)
        self.anchors = {('anchor', i): p for i, p in enumerate(self.required)}
        self.rows, self.cols = first.rows, first.cols
        self.lengths = [haversine_km(a, b) for a, b in zip(self.required, self.required[1:])]
        self.offsets = [0.]
        for point, length in zip(self.required[1:], self.lengths):
            checkpoint()
            if not min(weather.grid.lons)<=point[0]<=max(weather.grid.lons) or not min(weather.grid.lats)<=point[1]<=max(weather.grid.lats):
                raise ValueError('必经航点超出风场空间覆盖范围')
            self.offsets.append(self.offsets[-1]+length)

    def leg(self, index):
        if index not in self.legs:
            from .segmented_routing import NativeGraph
            checkpoint()
            self.legs[index] = NativeGraph(self.weather, self.required[index], self.required[index+1],
                                          self.bbox, domain=(self.rows,self.cols))
        return self.legs[index]

    def pack(self, leg, node):
        graph = self.leg(leg)
        if node == graph.start:
            return ('anchor', leg)
        if node == graph.goal:
            return ('anchor', leg+1)
        return ('leg', leg, node)

    def point(self, node):
        if node in self.anchors:
            return self.anchors[node]
        if node[0] == 'range_boundary':
            return node[2]
        return self.leg(node[1]).point(node[2])

    def neighbours(self, node):
        if node[0] == 'anchor':
            leg = node[1]
            if leg >= self.leg_count:
                return []
            graph = self.leg(leg)
            base = graph.start
        else:
            leg, base = node[1:]
            graph = self.leg(leg)
        return [self.pack(leg, other) for other in graph.neighbours(base) if other != graph.start]

    def predecessors(self, node):
        if node[0] == 'anchor':
            leg = node[1]-1
            if leg < 0:
                return []
            graph = self.leg(leg)
            base = graph.goal
        else:
            leg, base = node[1:]
            graph = self.leg(leg)
        return [self.pack(leg, other) for other in graph.predecessors(base) if other != graph.goal]

    def boundary(self, point, pending):
        leg = pending[1]-1 if pending[0] == 'anchor' else pending[1]
        return ('range_boundary', leg, point)

    def forward_leg(self, node):
        return node[1]

    def backward_leg(self, node):
        return node[1]-1 if node[0] == 'anchor' else node[1]

    def compatible(self, forward, backward):
        return forward == backward or self.forward_leg(forward) == self.backward_leg(backward)

    def forward_bound(self, node, point):
        leg = self.forward_leg(node)
        if leg >= self.leg_count:
            return 0.
        return haversine_km(point, self.required[leg+1]) + self.offsets[-1]-self.offsets[leg+1]

    def backward_bound(self, node, point):
        leg = self.backward_leg(node)
        if leg < 0:
            return 0.
        return self.offsets[leg] + haversine_km(self.required[leg], point)

    def forward_target(self, node):
        return self.required[min(self.forward_leg(node)+1, self.leg_count)]

    def forward_tail(self, node):
        return self.required[self.forward_leg(node)+1:]

    def backward_head(self, node):
        return self.required[:self.backward_leg(node)+1]

    def clear(self):
        self.anchors = {('anchor', i): p for i, p in enumerate(self.required)}
        for graph in self.legs.values():
            graph.virtual.clear()
            graph.chains.clear()
