import time


class T(time.struct_time):
    __slots__ = ("gmtoff",)

    def __new__(cls, seq, gmtoff):
        return super().__new__(cls, seq)

    def __init__(self, seq, gmtoff):
        self.gmtoff = gmtoff


t = T((2026, 1, 1, 23, 59, 58, 3, 1, 0), -5 * 3600)
print(t.gmtoff, t.tm_isdst, tuple(t), isinstance(t, time.struct_time))
