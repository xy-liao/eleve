import struct
import math
import collections
import logging

import plyvel

NaN = float('nan')
PACKER = struct.Struct('<Lf')
NORMALIZATION_PACKER = struct.Struct('<ff')

SEPARATOR = b'\x00'
SEPARATOR_PLUS_ONE = bytes((SEPARATOR[0]+1,))

def to_bytes(o):
    return o if type(o) == bytes else str(o).encode()

def ngram_to_key(ngram):
    return bytes([len(ngram)]) + b''.join([SEPARATOR + to_bytes(i) for i in ngram])

class Node:
    def __init__(self, db, key, data=None):
        self.db = db
        self.key = key

        if data is None:
            data = db.get(key)

        self.count, self.entropy = PACKER.unpack(data) if data else (0, NaN)

    def childs(self):
        start = bytes([self.key[0] + 1]) + self.key[1:] + SEPARATOR
        stop = start[:-1] + SEPARATOR_PLUS_ONE
        for key, value in self.db.iterator(start=start, stop=stop):
            yield Node(self.db, key, value)

    def save(self, db=None):
        if db is None:
            db = self.db
        value = PACKER.pack(self.count, self.entropy)
        db.put(self.key, value)

    def update_entropy(self, terminals):
        if self.count == 0:
            self.entropy = NaN
            return

        entropy = 0
        sum_counts = 0
        for child in self.childs():
            if child.count == 0:
                continue
            sum_counts += child.count
            if child.key.split(SEPARATOR)[-1] in terminals:
                entropy += (child.count / self.count) * math.log2(self.count)
            else:
                entropy -= (child.count / self.count) * math.log2(child.count / self.count)
        assert entropy >= 0

        if not sum_counts:
            entropy = NaN
        else:
            assert sum_counts == self.count

        if self.entropy != entropy and not(math.isnan(self.entropy) and math.isnan(entropy)):
            self.entropy = entropy
            self.save()
    
class LevelTrie:
    def __init__(self, path="/tmp/level_trie", terminals=['^', '$']):
        self.terminals = set(to_bytes(i) for i in terminals)

        self.db = plyvel.DB(path,
                create_if_missing=True,
                write_buffer_size=32*1024**2,
                #block_size=16*1024,
                #lru_cache_size=512*1024**2,
                #bloom_filter_bits=8,
        )

        self.dirty = True
        i = 0
        while True:
            ndata = self.db.get(b'\xff' + bytes((i,)))
            if ndata is None:
                break
            self.normalization.append(NORMALIZATION_PACKER.unpack(ndata))
            self.dirty = False
            i += 1
        
    def clear(self):
        for key in self.db.iterator(include_value=False):
            self.db.delete(key)
        self.dirty = True

    def _update_stats_rec(self, parent_entropy, depth, node):
        node.update_entropy(self.terminals)

        if not math.isnan(node.entropy) and (node.entropy or parent_entropy):
            ev = node.entropy - parent_entropy

            while len(self.normalization) <= depth:
                self.normalization.append((0., 0., 0))
            mean, stdev, count = self.normalization[depth]
            old_mean = mean
            count += 1
            mean += (ev - old_mean) / count
            stdev += (ev - old_mean)*(ev - mean)
            self.normalization[depth] = mean, stdev, count

        for child in node.childs():
            self._update_stats_rec(node.entropy, depth + 1, child)

    def update_stats(self):
        if not self.dirty:
            return

        self.normalization = []

        self._update_stats_rec(NaN, 0, Node(self.db, b'\x00'))

        for k, (mean, stdev, count) in enumerate(self.normalization):
            stdev = math.sqrt(stdev / (count or 1))
            self.normalization[k] = (mean, stdev)
            self.db.put(b'\xff' + bytes((k,)),  NORMALIZATION_PACKER.pack(mean, stdev))

        self.db.compact_range()

        self.dirty = False

    def _check_dirty(self):
        if self.dirty:
            self.update_stats()

    def node(self, ngram):
        return Node(self.db, ngram_to_key(ngram))

    def add_ngram(self, ngram, freq=1):
        if not self.dirty:
            self.dirty = True
            self.db.delete(b'\xff\x00')

        b = bytearray(b'\x00')
        w = self.db.write_batch()

        # shortcut : if we encounter a node with a counter to zero, we will
        #            set data to False and avoid doing queries for the following nodes.
        create = False

        node = Node(self.db, b'\x00')
        node.count += freq
        node.save(w)

        for i in range(1, len(ngram) + 1):
            b[0] = i
            b.extend(SEPARATOR + str(ngram[i - 1]).encode())
            node = Node(self.db, bytes(b), data=(False if create else None))
            if node.count == 0:
                create = True
            node.count += freq
            node.save(w)

        w.write()

    def query_count(self, ngram):
        return self.node(ngram).count

    def query_entropy(self, ngram):
        self._check_dirty()
        return self.node(ngram).entropy

    def query_ev(self, ngram):
        self._check_dirty()
        if not ngram:
            return NaN
        node = self.node(ngram)
        if math.isnan(node.entropy):
            return NaN
        parent = self.node(ngram[:-1])
        if node.entropy != 0 or parent.entropy != 0:
            return node.entropy - parent.entropy
        return NaN

    def query_autonomy(self, ngram):
        self._check_dirty()

        ev = self.query_ev(ngram)
        if math.isnan(ev):
            return NaN

        try:
            mean, stdev = self.normalization[len(ngram)]
            return (ev - mean) / stdev
        except (ZeroDivisionError, IndexError):
            return NaN

