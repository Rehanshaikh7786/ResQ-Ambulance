"""Run ONCE while online to cache the demo-area map, so it also works offline:   python tools/prefetch_tiles.py [--sat]"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sos import geo, tiles

styles = ("streets", "sat") if "--sat" in sys.argv else ("streets",)
print("Caching demo-area map tiles (zoom 11-15) – about 110 tiles per style, please wait…")
r = tiles.prefetch(geo.BOUNDS, styles=styles)
print(r, "→", tiles.tile_dir())
