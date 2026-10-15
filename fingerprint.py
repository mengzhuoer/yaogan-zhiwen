#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fingerprint.py —— 用「多点取值指纹」认领身份未知的下载栅格

痛点：批量下载（如 Google Earth Engine getDownloadURL）返回的文件
往往是哈希名，文件内没有任何"这是哪一年/哪个版本"的元数据；
靠下载顺序或时间戳猜，顺序一乱就得到一条被悄悄污染的时间序列。

解法：在一组预先约定的地理位置上取每个文件的数值向量当指纹，
与生成端（GEE 等，按同网格同口径重算）打印的参考指纹做**全等匹配**。
向量全等 -> 身份唯一命中；不命中会明确暴露，不存在"凑合认领"。

四个前提（缺一不可）：
  1. 网格可复刻 —— 参考端用与文件完全相同的 CRS + geotransform；
  2. 口径可复刻 —— 掩膜/累加/筛选逻辑与下载脚本一字不差；
  3. 整数/定点值域 —— 避免浮点尾差破坏全等比较；
  4. 生成端可确定性重算。

子命令：
  compute   选点 + 为目录内每个栅格计算指纹 -> local_fingerprints.json
            （并打印可直接粘进 GEE 的 var POINTS = [...]）
  parse     把 GEE 控制台打印的 "2001 => [1, 2, ...]" 文本
            解析为 reference_fingerprints.json
  match     本地指纹 vs 参考指纹 -> 年份映射表（全等 + 唯一命中）
            --rename 按模板改名（仅当全部唯一命中才执行）
  sample    按已存点位对单个文件取值（补缺下载后的复核）

示例：
  python fingerprint.py compute --dir ./downloads -n 8 --boundary shanxi.geojson
  python fingerprint.py parse --in console.txt --out reference.json
  python fingerprint.py match --local local.json --reference reference.json
  python fingerprint.py match --local local.json --reference reference.json \
      --rename --template "GPP_{label}.tif"

退出码：0 = 全部唯一命中；2 = 存在未命中/歧义（详见输出）。
"""

import argparse
import glob
import json
import os
import re
import sys

import numpy as np
import rasterio


# ---------------------------------------------------------------- 基础读取

def load_rasters(directory, pattern):
    files = sorted(glob.glob(os.path.join(directory, pattern)))
    if not files:
        sys.exit(f"错误：{directory} 下没有匹配 {pattern} 的文件")

    arrays, grid_ref, nodata, transform, crs = [], None, None, None, None
    for f in files:
        with rasterio.open(f) as ds:
            grid = (ds.width, ds.height, str(ds.crs),
                    tuple(round(v, 12) for v in tuple(ds.transform)))
            if grid_ref is None:
                grid_ref, nodata = grid, ds.nodata
                transform, crs = ds.transform, ds.crs
            elif grid != grid_ref:
                sys.exit(
                    f"错误：网格不一致，无法比对。\n"
                    f"  首个文件: {os.path.basename(files[0])}\n"
                    f"  本文件  : {os.path.basename(f)}\n"
                    f"提示：不同 CRS/投影/分辨率的文件不能混在一批里，"
                    f"先统一网格（检查来源与下载参数）。")
            arrays.append(ds.read(1))
    return files, arrays, nodata, transform, crs


def sample_vector(arr, transform, nodata, points):
    vals = []
    for x, y in points:
        r, c = rasterio.transform.rowcol(transform, x, y)
        v = arr[r, c]
        if nodata is not None and v == nodata:
            vals.append(None)
        else:
            vals.append(int(v))
    return vals


# ---------------------------------------------------------------- 选点

def pick_points(arrays, transform, nodata, n, boundary, seed, tries=20000):
    h, w = arrays[0].shape

    inside = None
    if boundary:
        try:
            import geopandas as gpd
            from shapely.geometry import Point
        except ImportError:
            sys.exit("错误：--boundary 需要 geopandas 与 shapely")
        gdf = gpd.read_file(boundary)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        poly = gdf.to_crs("EPSG:4326").union_all() \
            if hasattr(gdf, "union_all") else gdf.to_crs("EPSG:4326").unary_union

        def inside(x, y):
            return poly.contains(Point(x, y))

    def center(i, j):
        return (transform.c + (i + 0.5) * transform.a,
                transform.f + (j + 0.5) * transform.e)

    rng = np.random.default_rng(seed)

    # 有效 = 所有文件在该像元都不是 nodata
    ok = np.ones(arrays[0].shape, dtype=bool)
    for a in arrays:
        ok &= (a != nodata) if nodata is not None else True

    cand = []
    step = 0
    while len(cand) < max(n * 40, 200) and step < tries:
        step += 1
        i = int(rng.integers(10, w - 10))
        j = int(rng.integers(10, h - 10))
        if not ok[j, i]:
            continue
        x, y = center(i, j)
        if inside and not inside(x, y):
            continue
        cand.append((x, y))

    if len(cand) < n:
        sys.exit(f"错误：候选点不足（{len(cand)} < {n}）。"
                 f"请减少 -n、放宽 --boundary，或检查有效值掩膜。")

    # 贪心最大最小距离：保证空间分散
    chosen = [cand[int(rng.integers(0, len(cand)))]]
    while len(chosen) < n:
        best, bestd = None, -1.0
        for c in cand:
            d = min((c[0] - s[0]) ** 2 + (c[1] - s[1]) ** 2 for s in chosen)
            if d > bestd:
                best, bestd = c, d
        chosen.append(best)

    return [(round(x, 9), round(y, 9)) for x, y in chosen]


# ---------------------------------------------------------------- 子命令

def cmd_compute(args):
    files, arrays, nodata, T, crs = load_rasters(args.dir, args.pattern)
    points = pick_points(arrays, T, nodata, args.n, args.boundary, args.seed)

    result = {
        "grid": {
            "width": int(arrays[0].shape[1]),
            "height": int(arrays[0].shape[0]),
            "crs": str(crs),
            "transform": [round(v, 15) for v in tuple(T)[:6]],
            "nodata": nodata,
        },
        "points": [list(p) for p in points],
        "files": {os.path.basename(f): sample_vector(a, T, nodata, points)
                  for f, a in zip(files, arrays)},
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    print(f"文件 {len(files)} 个 | 点位 {len(points)} 个 | 输出 {args.out}")
    print("\n粘进 GEE 配置的点位（与本机一致，勿改动）：")
    print("var POINTS = [")
    for x, y in points:
        print(f"  [{x}, {y}],")
    print("];")
    print(f"\n网格（供 GEE reproject 用，抄给 GRID_TRANSFORM）：")
    print(f"  crs      = {result['grid']['crs']}")
    print(f"  transform= {result['grid']['transform']}")


def cmd_parse(args):
    line_re = re.compile(r"^\s*(\S+)\s*=>\s*\[([0-9, \t\-null]*)\]\s*$")
    ref = {}
    with open(args.inp, encoding="utf-8") as fh:
        for raw in fh:
            m = line_re.match(raw)
            if not m:
                continue
            label = m.group(1).strip()
            body = m.group(2).strip()
            vals = [None if t.strip() in ("null", "") else int(float(t))
                    for t in body.split(",")] if body else []
            ref[label] = vals
    if not ref:
        sys.exit("错误：没有解析到任何 'label => [..]' 行")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(ref, fh, ensure_ascii=False, indent=1)
    print(f"解析 {len(ref)} 行 -> {args.out}：{list(ref)[:5]}"
          f"{' ...' if len(ref) > 5 else ''}")


def cmd_match(args):
    with open(args.local, encoding="utf-8") as fh:
        local = json.load(fh)
    with open(args.reference, encoding="utf-8") as fh:
        ref = json.load(fh)

    ref_items = [(k, v) for k, v in ref.items()
                 if isinstance(v, list) and not k.startswith("_")]

    mapping, unmatched, ambiguous = {}, [], []
    print(f"{'文件':<46} {'结果'}")
    for fname, vec in local["files"].items():
        hits = [label for label, rvec in ref_items if rvec == vec]
        if len(hits) == 1:
            mapping[hits[0]] = fname
            print(f"{fname:<46} -> {hits[0]}  ✓")
        elif len(hits) > 1:
            ambiguous.append((fname, hits))
            print(f"{fname:<46} 歧义: {hits}  ✗")
        else:
            unmatched.append(fname)
            print(f"{fname:<46} 无参考命中  ✗")

    claimed_years = set(mapping)
    missing = [label for label, _ in ref_items if label not in claimed_years]
    print(f"\n唯一命中 {len(mapping)} | 未命中 {len(unmatched)} | "
          f"歧义 {len(ambiguous)} | 参考中无本地文件: {missing}")

    ok = not unmatched and not ambiguous
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"mapping": mapping, "unmatched": unmatched,
                       "ambiguous": [[f, h] for f, h in ambiguous],
                       "missing_locally": missing},
                      fh, ensure_ascii=False, indent=1)
        print(f"映射已写出: {args.out}")

    if ok and args.rename:
        for label, fname in mapping.items():
            new = args.template.format(label=label)
            src = os.path.join(args.dir, fname)
            dst = os.path.join(args.dir, new)
            if os.path.exists(dst):
                sys.exit(f"错误：目标已存在，未改名: {dst}")
            os.rename(src, dst)
            print(f"改名: {fname} -> {new}")
    elif ok and not mapping:
        print("（没有可匹配的文件）")
    else:
        if args.rename:
            print("\n存在未命中/歧义，**未执行改名**。"
                  "请先按排障顺序检查：transform -> 口径 -> 点位。")

    sys.exit(0 if ok else 2)


def cmd_sample(args):
    with open(args.local, encoding="utf-8") as fh:
        data = json.load(fh)
    points = [tuple(p) for p in data["points"]]
    if args.file:
        target = os.path.abspath(args.file)
        directory = os.path.dirname(target) or "."
        pattern = os.path.basename(target)
    else:
        directory, pattern = args.dir, "*"
    files, arrays, nodata, T, _ = load_rasters(directory, pattern)
    for f, a in zip(files, arrays):
        vec = sample_vector(a, T, nodata, points)
        print(f"{os.path.basename(f)} => {vec}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compute", help="选点并计算本地指纹")
    c.add_argument("--dir", required=True)
    c.add_argument("--pattern", default="*.tif*", help="文件匹配（默认兼容 .tif/.tiff）")
    c.add_argument("-n", "--n", type=int, default=8)
    c.add_argument("--boundary", help="研究区矢量（限省内选点，需 geopandas）")
    c.add_argument("--seed", type=int, default=2026)
    c.add_argument("-o", "--out", default="local_fingerprints.json")
    c.set_defaults(func=cmd_compute)

    p = sub.add_parser("parse", help="GEE 打印文本 -> 参考指纹 JSON")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", default="reference_fingerprints.json")
    p.set_defaults(func=cmd_parse)

    m = sub.add_parser("match", help="全等比对，输出年份映射")
    m.add_argument("--local", required=True)
    m.add_argument("--reference", required=True)
    m.add_argument("--out", help="映射 JSON 输出路径")
    m.add_argument("--rename", action="store_true",
                   help="全部唯一命中时按模板改名")
    m.add_argument("--template", default="{label}.tif")
    m.add_argument("--dir", default=".")
    m.set_defaults(func=cmd_match)

    s = sub.add_parser("sample", help="按已存点位对单文件取值（复核）")
    s.add_argument("--local", required=True,
                   help="compute 生成的 local_fingerprints.json（提供点位）")
    s.add_argument("--file", help="单个文件路径；缺省时用 --dir+pattern")
    s.add_argument("--dir", default=".")
    s.set_defaults(func=cmd_sample)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
