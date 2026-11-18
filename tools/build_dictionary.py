"""Add exact, verified reference outlines. Usage: python tools/build_dictionary.py FONT..."""
import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from html_to_epub.fonts import Decoder, DATA

parser = argparse.ArgumentParser(description="从正确参考字体生成字形哈希字典，不保存字体轮廓。")
parser.add_argument("fonts", nargs="+")
args = parser.parse_args()
decoder = Decoder(args.fonts)
DATA.mkdir(parents=True, exist_ok=True)
path = DATA / "glyphs.json.gz"
with gzip.open(path, "wt", encoding="utf-8") as stream:
    json.dump(decoder.glyphs, stream, ensure_ascii=False, separators=(",", ":"))
print(f"已保存 {len(decoder.glyphs)} 个字形指纹：{path}")
