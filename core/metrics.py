# core/metrics.py
from collections import defaultdict
from typing import Dict

class Metrics:
    def __init__(self):
        self.counters: Dict[str, int] = defaultdict(int)
        self.intents: Dict[str, int] = defaultdict(int)

    def inc(self, key: str, n: int = 1):
        self.counters[key] += n

    def inc_intent(self, intent: str, n: int = 1):
        self.intents[intent] += n

    def snapshot(self):
        return {
            "counters": dict(self.counters),
            "intents": dict(self.intents),
        }

METRICS = Metrics()
