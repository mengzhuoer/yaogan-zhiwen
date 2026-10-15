# 遥感指纹 · gee-raster-fingerprint

> **EN — TL;DR**: Batch-downloaded rasters often arrive as `hash_getPixels.tiff`
> with zero metadata about which year/version each file contains. Guessing by
> download order silently corrupts time series. This tool claims file identity
> with **multi-point value fingerprints**: sample a handful of pixel-center
> points, exact-match the vectors against references recomputed from the
> data source under an identical grid. Unique hit → rename; no hit → hard
> error (never "close enough").

> **中文一句话**：批量下载的栅格只剩哈希文件名、文件里没有任何“哪一年/哪个
> 版本”的信息时，用少数几个点的**取值向量全等匹配**把文件身份认回来——
> 唯一命中才认领，认不出就明确报错，绝不“凑合认领”。

---

## 一、痛点：文件在手，身份成谜

以 Google Earth Engine `getDownloadURL` 批量下载 22 年年度产品为例（本方法的真实出处）：

1. **文件名是哈希**。`filename` 参数被服务端忽略，回来的全是
   `097d4855ff5e049311ee21ffcb97347b-fc6fc39c..._getPixels.tiff`——21 个文件
   长得一模一样，谁也不知道哪个是 2001 年、哪个是 2017 年。
2. **文件里没有年份元数据**。翻遍 TIFF 标签：只有压缩方式、分辨率、CRS，
   没有一个字写着“2017”。
3. **常规线索全部失效**：
   - 下载顺序 ≠ 保存顺序（并发下载互相超车）；
   - 文件修改时间挤在同一分钟，排序无意义；
   - `ls` 按名字排序 = 按哈希排序 = 随机顺序。
4. **猜错的代价是无声的**。把 2017 当成 2015，时间序列整体错位，Sen 斜率、
   MK 显著性、趋势分级**照常产出一张看起来很专业的图**——但结论是错的，
   而且没有任何报错会提醒你。
5. 还会混进**不同投影的同名下载**（从资产下的是 MODIS Sinusoidal、从源码
   下的是 EPSG:4326），直接叠栈就错得更隐蔽。

**这个方法解决的就是第 1—4 条：给已经落盘、身份未知的文件补上可信的身份。**
第 5 条（网格不一致）由工具主动检测并拒绝操作。

---

## 二、解决什么 / 不解决什么

### 解决

| 输入 | 输出 |
|---|---|
| 一批哈希名（或任何无元数据）的同网格栅格 | `文件 → 年份/标签` 的**唯一**映射表 |
| 生成端（GEE 等）按**同一参数**重算的能力 | 参考指纹（控制台单行打印） |
| | 可选：按模板批量改名 |
| | 反向发现：参考里有、本地没有的年份（= 漏下载的文件） |

**保证**：比对是 0 容差的**全等匹配**。命中 = 向量逐位相等且唯一；
不命中会明确暴露（说明网格或口径漂移了），不存在“最相似就算认领”的模糊地带。

### 不解决

- ❌ 文件传输完整性校验（那是 checksum/md5 的事）；
- ❌ 生成端不可重算的黑盒下载；
- ❌ 多次加工、无法按原参数复现的衍生文件；
- ❌ 浮点结果在不同实现间可能有尾差的场景（本方法要求整数/定点值域）。

---

## 三、原理

**身份指纹 = 一组预先约定地理位置上，该栅格的数值向量。**

- 同一批点 + 同一网格 + 同一口径 ⇒ 同一个数据源在生成端可以**确定性重算**出
  与文件逐位相同的向量；
- 每个年份/版本给出一条参考向量；本地每个文件算一条本地向量；
- **全等 + 唯一** ⇒ 身份确认。

为什么几个点就够：8 个点、值域数千，随机向量全等的概率可忽略；且比对失败
会**明确暴露**而不是误认（见「局限」一节的排障顺序）。

与校验和的区别：它证明的是“**与参考参数一致**”，不是“传输无损”。

### 成立的四个前提（缺一不可）

1. **网格可复刻**：参考端 `reproject` 到与文件完全相同的 CRS + geotransform
   （六元组直接抄本地 tiff）；
2. **口径可复刻**：掩膜 / 累加 / 筛选逻辑与下载脚本一字不差（同一函数，不重写）；
3. **整数值域**：避免浮点尾差破坏全等比较；
4. **生成端可确定性重算**。

---

## 四、快速上手

```bash
pip install -r requirements.txt     # numpy + rasterio（--boundary 另需 geopandas）

# ① 本地选点 + 给每个文件算指纹（点位会打印成可粘贴的 GEE 配置）
python fingerprint.py compute --dir ./downloads -n 8 --boundary shanxi.geojson

# ② 把打印的 var POINTS 和 GRID_TRANSFORM 粘进 gee/fingerprint_reference.js，
#    填好你自己的数据构造函数，Code Editor 里 Run，
#    把控制台 "2017 => [7309, 5823, ...]" 那些行整段复制存成 console.txt

# ③ 解析 GEE 打印 → 参考指纹
python fingerprint.py parse --in console.txt --out reference.json

# ④ 全等比对 → 映射表；全部唯一命中才允许批量改名
python fingerprint.py match --local local_fingerprints.json --reference reference.json --out mapping.json
python fingerprint.py match --local local_fingerprints.json --reference reference.json \
    --rename --template "annual_gpp_{label}.tif" --dir ./downloads

# （可选）补缺下载后的复核：单文件按既有点位取值
python fingerprint.py sample --file ./downloads/新增文件.tif --local local_fingerprints.json
```

`match` 退出码：`0` 全部唯一命中；`2` 存在未命中/歧义（CI 里可直接当断言用）。

GEE 端模板见 [`gee/fingerprint_reference.js`](gee/fingerprint_reference.js)，
JSON 格式示例见 [`examples/`](examples/)。

---

## 五、实测案例（方法出处）

山西省 2001—2022 年 MODIS GPP 年度数据，21 条哈希名链接批量下载：

| 项 | 结果 |
|---|---|
| 待认领文件 | 20 个（全部哈希名、零元数据） |
| 指纹点 | 8 个像元中心（省内、全部文件有效、空间分散） |
| GEE 参考指纹 | 21 行（同网格 `reproject` 后逐点打印） |
| 比对 | **20/20 逐位全等、唯一命中** |
| 反向发现 | 参考有、本地无 → 精确定位**漏下载的是 2017 年** |
| 混网格检测 | 另一个 Sinusoidal 投影文件被网格检查直接拦截 |
| 补缺 | 2017、2022 各单链接下载 → `sample` 复核 → 落名 |
| 终检 | 22 年连续、同 CRS/transform/尺寸/dtype，100% 覆盖 |

---

## 六、什么时候**不要**用它（常规方案优先）

| 优先级 | 方案 | 说明 |
|---|---|---|
| 1 | `Export.image.toDrive` + `fileNamePrefix` | 文件到云盘就带名字（GEE 社区主流） |
| 2 | Python API + `requests` 脚本化下载 | 程序自己知道在下什么，存盘即命名 |
| 3 | 下载时维护 manifest（映射 CSV） | 最朴素也最可靠 |
| 4 | 单对象单独下载 | 只缺一两个文件时，身份天然无歧义 |
| 5 | **本工具（指纹认领）** | 文件已落盘成哈希名、元数据为空、生成端可重算 |

**先走 1—4，走不通（文件已经下砸了）再用 5。**

---

## 七、局限与排障顺序

- 网格差一位 → 全不命中。这是特性（暴露不一致），排障顺序固定为：
  **transform → 口径（掩膜/累加）→ 点位坐标 → 最后才怀疑数据**；
- 任一端 nodata 规则不同（None vs 0）会造成向量错位 —— `compute` 选点时
  就要求“全部文件该点有效”来绕开；
- 禁止“取最相似匹配”：0 命中时修参考端，不要放宽容差；
- 指纹不等于校验和，两者解决不同问题。

---

## License

MIT — 见 [LICENSE](LICENSE)。使用本方法产出的研究建议在论文方法节注明
“文件身份经多点取值指纹核验”。
