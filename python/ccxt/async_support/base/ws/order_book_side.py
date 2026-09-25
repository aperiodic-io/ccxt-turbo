# -*- coding: utf-8 -*-

import sys
from bisect import bisect_left

"""Author: Carlo Revelli"""
"""Fast bisect bindings"""
"""https://github.com/python/cpython/blob/master/Modules/_bisectmodule.c"""
"""Performs a binary search when inserting keys in sorted order"""


def _bulk_load(deltas, is_bid):
    # fast path used when a full, pre-sorted snapshot (a REST fetch, or a
    # full-book-per-message feed like hyperliquid's l2Book) is loaded in
    # one shot: a single linear pass instead of N binary-search inserts.
    # bails out (returns None) on anything that isn't a clean best-first
    # run of strictly increasing/decreasing prices (with only an
    # immediately-following zero-size delta allowed, to cancel the level
    # just appended) - callers fall back to the slower, always-correct
    # per-item store on a bail-out, so correctness never depends on
    # exchanges actually sending sorted/deduped data.
    result = []
    seen = set()
    for raw in deltas:
        delta = list(raw)
        price = delta[0]
        size = delta[1]
        if result and price == result[-1][0]:
            if size:
                result[-1] = delta
            else:
                result.pop()
                seen.discard(price)
            continue
        if not size:
            if price in seen:
                # cancels a level that isn't the most recent one - a mid-list
                # removal the fast path can't do cheaply; bail out
                return None
            continue
        if result:
            if is_bid:
                if price > result[-1][0]:
                    return None
            elif price < result[-1][0]:
                return None
        result.append(delta)
        seen.add(price)
    return result


class OrderBookSide(list):
    side = None  # set to True for bids and False for asks

    def __init__(self, deltas=[], depth=None):
        super(OrderBookSide, self).__init__()
        self._depth = depth or sys.maxsize
        # parallel to self
        self._index = []
        if deltas:
            self.merge_snapshot(deltas)

    def merge_snapshot(self, deltas):
        # fast path: bulk-load an already-sorted snapshot in one linear
        # pass (no per-item binary search), then rebuild the parallel
        # index in a single comprehension so subsequent incremental
        # storeArray() calls keep working exactly as before
        fast = _bulk_load(deltas, self.side)
        if fast is not None:
            self.extend(fast)
            self._index = [(-d[0] if self.side else d[0]) for d in fast]
        else:
            for delta in deltas:
                self.storeArray(list(delta))

    def store_array(self, delta):
        return self.storeArray(delta)

    def storeArray(self, delta):
        price = delta[0]
        size = delta[1]
        index_price = -price if self.side else price
        keys = self._index
        index = bisect_left(keys, index_price)
        if size:
            if index < len(keys) and keys[index] == index_price:
                self[index][1] = size
            else:
                keys.insert(index, index_price)
                self.insert(index, delta)
        elif index < len(keys) and keys[index] == index_price:
            del keys[index]
            del self[index]

    def store(self, price, size):
        self.storeArray([price, size])

    def limit(self):
        if len(self) > self._depth:
            del self[self._depth:]
            del self._index[self._depth:]

    def remove_index(self, order):
        pass

    # no __getitem__ override: list.__getitem__ already returns a plain list
    # when slicing a subclass, and overriding it made every access ~3x slower

    def __eq__(self, other):
        if isinstance(other, list):
            return list(self) == other
        return super(OrderBookSide, self).__eq__(other)

    def __repr__(self):
        return str(list(self))

    def copy(self):
        return self.__class__([delta[:] for delta in self], self._depth)

# -----------------------------------------------------------------------------
# overwrites absolute volumes at price levels
# or deletes price levels based on order counts (3rd value in a bidask delta)
# this class stores vector arrays of values indexed by price


class CountedOrderBookSide(OrderBookSide):
    def __init__(self, deltas=[], depth=None):
        super(CountedOrderBookSide, self).__init__(deltas, depth)

    def merge_snapshot(self, deltas):
        for delta in deltas:
            self.storeArray(list(delta))

    def storeArray(self, delta):
        price = delta[0]
        size = delta[1]
        count = delta[2]
        index_price = -price if self.side else price
        keys = self._index
        index = bisect_left(keys, index_price)
        if size and count:
            if index < len(keys) and keys[index] == index_price:
                order = self[index]
                order[1] = size
                order[2] = count
            else:
                keys.insert(index, index_price)
                self.insert(index, delta)
        elif index < len(keys) and keys[index] == index_price:
            del keys[index]
            del self[index]

    def store(self, price, size, count):
        self.storeArray([price, size, count])

    def limit(self):
        difference = len(self) - self._depth
        for _ in range(difference):
            self.remove_index(self.pop())
            self._index.pop()

# -----------------------------------------------------------------------------
# indexed by order ids (3rd value in a bidask delta)


class IndexedOrderBookSide(OrderBookSide):
    def __init__(self, deltas=[], depth=None):
        self._hashmap = {}
        super(IndexedOrderBookSide, self).__init__(deltas, depth)

    def merge_snapshot(self, deltas):
        for delta in deltas:
            self.storeArray(list(delta))

    def storeArray(self, delta):
        price = delta[0]
        if price is not None:
            index_price = -price if self.side else price
        else:
            index_price = None
        size = delta[1]
        order_id = delta[2]
        hashmap = self._hashmap
        keys = self._index
        if size:
            if order_id in hashmap:
                old_price = hashmap[order_id]
                index_price = index_price or old_price
                # in case the price is not defined
                delta[0] = abs(index_price)
                # matches if price is not defined or if price matches
                if index_price == old_price:
                    # just overwrite the old index
                    index = bisect_left(keys, index_price)
                    while self[index][2] != order_id:
                        index += 1
                    keys[index] = index_price
                    self[index] = delta
                    return
                else:
                    # remove old price level
                    old_index = bisect_left(keys, old_price)
                    while self[old_index][2] != order_id:
                        old_index += 1
                    del keys[old_index]
                    del self[old_index]
            # insert new price level
            hashmap[order_id] = index_price
            index = bisect_left(keys, index_price)
            length = len(keys)
            while index < length and keys[index] == index_price and self[index][2] < order_id:
                index += 1
            keys.insert(index, index_price)
            self.insert(index, delta)
        elif order_id in hashmap:
            old_price = hashmap[order_id]
            index = bisect_left(keys, old_price)
            while self[index][2] != order_id:
                index += 1
            del keys[index]
            del self[index]
            del hashmap[order_id]

    def limit(self):
        difference = len(self) - self._depth
        for _ in range(difference):
            self.remove_index(self.pop())
            self._index.pop()

    def remove_index(self, order):
        order_id = order[2]
        if order_id in self._hashmap:
            del self._hashmap[order_id]

    def store(self, price, size, order_id):
        self.storeArray([price, size, order_id])

# -----------------------------------------------------------------------------
# a more elegant syntax is possible here, but native inheritance is portable

class Asks(OrderBookSide): side = False                                     # noqa
class Bids(OrderBookSide): side = True                                      # noqa
class CountedAsks(CountedOrderBookSide): side = False                       # noqa
class CountedBids(CountedOrderBookSide): side = True                        # noqa
class IndexedAsks(IndexedOrderBookSide): side = False                       # noqa
class IndexedBids(IndexedOrderBookSide): side = True                        # noqa
