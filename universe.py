"""
AlphaEdge Universe — shared symbol/sector config loader.
Used by scanner.py, market_intel.py, dip_scanner.py, morning_brief.py
"""
import os
from pathlib import Path

try:
    import yaml
except ImportError:
    raise ImportError("pyyaml required. Run: pip install pyyaml")

CONFIG_PATH = Path(__file__).parent / 'symbols.yaml'


class Universe:
    """Single source of truth for all symbol/sector config."""

    def __init__(self, config_path=None):
        self.path = Path(config_path) if config_path else CONFIG_PATH
        if not self.path.exists():
            raise FileNotFoundError(f"symbols.yaml not found at {self.path}")
        with open(self.path, 'r', encoding='utf-8') as f:
            self._raw = yaml.safe_load(f)

    # ─── Bucket accessors ───
    def _syms(self, bucket):
        return [x['symbol'] for x in self._raw.get(bucket, []) or []]

    @property
    def crypto(self):
        return self._syms('crypto')

    @property
    def extended_hours(self):
        return self._syms('extended_hours')

    @property
    def regular_hours(self):
        return self._syms('regular_hours')

    @property
    def dip_extras(self):
        return self._syms('dip_extras')

    @property
    def all_symbols(self):
        """Core watchlist for main signal scanner."""
        return self.crypto + self.extended_hours + self.regular_hours

    @property
    def dip_universe(self):
        """Larger universe for dip scanner."""
        seen = set()
        out = []
        for sym in self.all_symbols + self.dip_extras:
            if sym not in seen:
                out.append(sym)
                seen.add(sym)
        return out

    @property
    def monitor_list(self):
        """For market_intel.py — all actively monitored."""
        return self.all_symbols

    # ─── Lookups ───
    @property
    def emoji_map(self):
        out = {}
        for bucket in ('crypto', 'extended_hours', 'regular_hours', 'dip_extras'):
            for item in self._raw.get(bucket, []) or []:
                out[item['symbol']] = item.get('emoji', '📈')
        return out

    @property
    def sector_map(self):
        """{symbol: sector}"""
        out = {}
        for bucket in ('crypto', 'extended_hours', 'regular_hours', 'dip_extras'):
            for item in self._raw.get(bucket, []) or []:
                out[item['symbol']] = item.get('sector', 'Other')
        return out

    @property
    def sectors(self):
        """{sector: [symbols]} — built from sector_map, only 1+ symbols."""
        groups = {}
        for sym, sector in self.sector_map.items():
            if sym in self.monitor_list:  # only sectors in monitor list
                groups.setdefault(sector, []).append(sym)
        return groups

    @property
    def correlation_groups(self):
        """Sectors with 2+ symbols (useful for correlation alerts)."""
        return {k: v for k, v in self.sectors.items() if len(v) >= 2}

    # ─── Settings ───
    def setting(self, module, key, default=None):
        return self._raw.get('settings', {}).get(module, {}).get(key, default)

    # ─── Introspection ───
    def summary(self):
        return (f"Universe loaded: {len(self.all_symbols)} core "
                f"({len(self.crypto)} crypto, {len(self.extended_hours)} ext, "
                f"{len(self.regular_hours)} reg) + {len(self.dip_extras)} dip-extras")


# ─── Singleton pattern (load once) ───
_instance = None

def get_universe():
    global _instance
    if _instance is None:
        _instance = Universe()
    return _instance


if __name__ == "__main__":
    u = get_universe()
    print(u.summary())
    print(f"\nSectors with 2+ symbols:")
    for sec, syms in u.correlation_groups.items():
        print(f"  {sec}: {', '.join(syms)}")
