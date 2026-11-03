"""Hamming-distance candidate index; exact hashes use a stable representative.

BK trees prune disjoint distance ranges. Worst-case dense candidates still require
a configured bound and a visible failed task, rather than a truncated success.
"""
class HammingIndex:
    def __init__(self):
        self.root = None
        self.comparisons = 0

    def add(self, code, value):
        if self.root is None:
            self.root = [code, [value], {}]
            return
        node = self.root
        while True:
            distance = (node[0] ^ code).bit_count()
            if distance == 0:
                node[1].append(value)
                return
            child = node[2].get(distance)
            if child is None:
                node[2][distance] = [code, [value], {}]
                return
            node = child

    def search(self, code, radius, *, max_comparisons=None, heartbeat=None):
        stack = [self.root] if self.root else []
        while stack:
            self.comparisons += 1
            if max_comparisons is not None and self.comparisons>max_comparisons:
                raise RuntimeError('近重复检索超过计算预算，请降低距离阈值或联系管理员；未生成完整去重报告')
            if heartbeat and self.comparisons%256==0:heartbeat()
            node = stack.pop()
            distance = (node[0] ^ code).bit_count()
            if distance <= radius:
                for value in node[1]:yield value, distance
            stack.extend(child for edge,child in node[2].items() if distance-radius <= edge <= distance+radius)
