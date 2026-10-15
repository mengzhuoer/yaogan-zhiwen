/**************************************************************
 * fingerprint_reference.js —— GEE 端「参考指纹」打印模板
 *
 * 作用：按【与下载文件完全相同的网格与口径】重算各年份（版本）在
 * 指纹点上的取值，打印成单行 "label => [v1, v2, ...]"，交给
 *     python fingerprint.py parse --in console.txt --out reference.json
 *
 * 使用步骤：
 *   1. 本地先跑 `fingerprint.py compute`，把它打印的
 *      var POINTS = [...] 和 GRID_TRANSFORM 粘到下面的 CONFIG；
 *   2. 把 buildReference(label) 换成你项目真实的数据构造逻辑
 *      —— 必须复用【下载时完全相同】的函数（掩膜/累加/筛选不改一个字）；
 *   3. Run → 复制 "=> " 各行存为 console.txt。
 *
 * 关键约束（详见仓库 README「四个前提」）：
 *   - GRID_TRANSFORM 六元组直接抄任意一个已下载 tiff 的 geotransform；
 *   - reproject 到该网格后再 reduceRegions，保证与文件逐像元一致；
 *   - 点位与本地完全一致，任何一个坐标都不能动。
 **************************************************************/

// ==================== CONFIG（按项目填写） ====================

var START_YEAR = 2001;
var END_YEAR = 2021;

// 下载文件的 CRS（从任意一个本地 tiff 读出）
var GRID_CRS = 'EPSG:4326';

// 下载文件的 geotransform 六元组 [xres, xskew, xorigin, yskew, yres, yorigin]
// 示例（本方法出处项目的真实值）：
var GRID_TRANSFORM = [
  0.004491576420597608, 0, 110.22777693788589,
  0, -0.004491576420597608, 40.77902232260568
];

// 由 `fingerprint.py compute` 打印，原样粘贴，勿增删点、勿改坐标
var POINTS = [
  // [113.841250168, 39.613458241],
  // [110.575874110, 34.749080978],
  // ...
];

var pointsFC = ee.FeatureCollection(POINTS.map(function (p, idx) {
  return ee.Feature(ee.Geometry.Point(p), {point_index: idx});
}));

// ==================== 数据构造（替换为你的项目） ====================
//
// 要求：与生成下载文件的脚本【同一个函数】。示例为「8 天 GPP 按年累加」，
// 与下载侧 prepareGPP / buildAnnualRaw 逐字对应。
// 若你的项目已有下载脚本，把那里的构造函数整段复制过来即可。

var source = ee.ImageCollection('MODIS/061/MOD17A2HGF')
  .filterBounds(ee.Geometry.Point([112.549, 37.871]));

function prepareGPP(image) {
  image = ee.Image(image);
  var raw = image.select('Gpp');
  return raw
    .updateMask(raw.gte(0).and(raw.lte(3000)))
    .rename('raw')
    .toInt32()
    .copyProperties(image, ['system:time_start']);
}

function buildReference(label) {
  var year = ee.Number.parse(label);
  var start = ee.Date.fromYMD(year, 1, 1);
  var yearSource = source.filterDate(start, start.advance(1, 'year'));
  var prepared = yearSource.map(prepareGPP);
  var nImages = yearSource.size();
  var native = ee.Image(yearSource.first()).select('Gpp').projection();

  var validCount = prepared.select('raw').count()
    .setDefaultProjection(native);
  var yearIsComplete = ee.Image.constant(nImages.eq(46))
    .setDefaultProjection(native);
  var mask = validCount.eq(nImages).and(yearIsComplete);

  return prepared.select('raw').sum()
    .rename('value')
    .toInt32()
    .updateMask(mask);
}

// ==================== 打印（一般无需修改） ====================

for (var year = START_YEAR; year <= END_YEAR; year++) {
  var label = String(year);

  var snap = buildReference(label)
    .reproject(GRID_CRS, GRID_TRANSFORM);

  var res = snap.reduceRegions({
    collection: pointsFC,
    reducer: ee.Reducer.first()
  });

  // 单行输出，直接整段复制给 fingerprint.py parse
  print(label + ' => [' + String(res.aggregate_array('first').getInfo()) + ']');
}
