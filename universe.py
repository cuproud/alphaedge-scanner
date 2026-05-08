"""
Shared symbol universe loader.
Used by scanner.py, market_intel.py, dip_scanner.py, morning_brief.py
"""
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / 'symbols.yaml'

class Universe:
    def __init__(self, config_path=CONFIG_PATH):
        with open(config_path, 'r', encoding='utf-8') as f:
            self._raw = yaml.safe_load(f)

    def _flatten(self, key):
        return self._raw.get('watchlists', {}).get(key, [])

    @property
    def crypto(self):
        return [x['symbol'] for x in self._flatten('crypto')]

    @property
    def extended_hours(self):
        return [x['symbol'] for x in self._flatten('extended_hours')]

    @property
    def regular_hours(self):
        return [x['symbol'] for x in self._flatten('regular_hours')]

    @property
    def all_symbols(self):
        return self.crypto + self.extended_hours + self.regular_hours

    @property
    def emoji_map(self):
        out = {}
        for bucket in ('crypto', 'extended_hours', 'regular_hours'):
            for item in self._flatten(bucket):
                out[item['symbol']] = item.get('emoji', '📈')
        return out

    @property
    def sector_map(self):
        """Returns {symbol: sector}"""
        out = {}
        for bucket in ('crypto', 'extended_hours', 'regular_hours'):
            for item in self._flatten(bucket):
                out[item['symbol']] = item.get('sector', 'Other')
        return out

    @property
    def correlation_groups(self):
        """Returns {sector: [symbols]} derived from sector_map"""
        groups = {}
        for sym, sector in self.sector_map.items():
            groups.setdefault(sector, []).append(sym)
        # Only keep sectors with 2+ symbols
        return {k: v for k, v in groups.items() if len(v) >= 2}

    def dip_universe(self):
        base = self.all_symbols
        extras = [x['symbol'] for x in self._raw.get('dip_scanner_extras', [])]
        return list(dict.fromkeys(base + extras))  # dedup, preserve order


# Singleton
_instance = None
def get_universe():
    global _instance
    if _instance is None:
        _instance = Universe()
    return _instance
