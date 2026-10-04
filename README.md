# 公交时刻换乘规划台

基于 **Python Flask + 原生 JavaScript / HTML / CSS** 的公交时刻换乘规划工具。
用户录入站点、带发车时刻的线路班次、站内（站间）步行换乘时间与临时停运区间，
选择起终点、最早出发时刻和「最快 / 少换乘 / 少步行」偏好，系统在**时间依赖图**上
搜索可行行程，并逐段展示班次、等待与换乘细节。

## 一条命令启动

```bash
docker compose up --build
```

然后浏览器访问：

> **http://localhost:8000**

- 端口：宿主机 **8000** → 容器 **8000**（在 `docker-compose.yml` 的 `ports` 中修改）

停止：`Ctrl+C`（后台模式用 `docker compose down`）。

### 不用 Docker 时

```bash
pip install -r requirements.txt
python app.py
# 同样访问 http://localhost:8000
```

## 使用说明

页面左侧为四块可编辑输入，右侧为规划与结果：

1. **站点**：站点 ID（如 `S1`）与名称。
2. **线路班次**：班次 ID、线路名与逐站经停时刻（每行 `站点ID HH:MM`，时刻须严格递增）。
   每个班次即一趟车，只可在不早于到站时刻的车站上车，沿停站顺序单向乘坐。
3. **步行换乘**：两站间步行分钟数，双向生效；换乘时必须先走完该步行时间才能在邻站乘车。
4. **临时停运区间**（三种，可混用）：
   - **站点停运**：时间窗内该站不能上、下车，途经班次自动**跳站通过**；
   - **线路停运**：时间窗内发车的该线路**整个班次被跳过**；
   - **线路局部停运（站段）**：填写线路名与该线路**经停顺序中的起点站、终点站**及时段。
     班次穿过该站段的时刻（到达起点站→到达终点站）与停运窗（含边界）重叠时，
     **不整班删除，也不得穿越封锁**：只保留封锁前能下车的乘车段（可在起点站下车，
     前提是该站未被站点停运），后续由步行或其他班次继续；封锁区间内部各站既不能上、
     也不能下；**封锁终点站及其之后各站仍可正常上车**，封锁起点站之前各站仍可下车。

在「⑤ 行程规划」中选择起点、终点、最早出发时刻与偏好后点击 **规划行程**。
**修改任何输入后可再次点击直接重算**，页面会提示“结果待重算”。
工具栏支持载入示例、清空、导入 / 导出 JSON。

### 偏好与字典序取解

| 偏好 | 字典序（从主到次） |
|---|---|
| 最快到达 | 到达时刻 → 换乘次数 → 总步行分钟 |
| 最少换乘 | 换乘次数 → 到达时刻 → 总步行分钟 |
| 最少步行 | 总步行分钟 → 到达时刻 → 换乘次数 |

搜索采用多标签（帕累托）时间依赖 Dijkstra：标签在每个站点按所选三元组互相支配剪枝，
三元组完全相同的标签只保留最先产生的一个，因此同分时结果稳定、可复现。
「换乘次数」= 乘坐的班次数 − 1（只坐一趟车即为 0 次换乘）。

### 无解与“首个阻塞约束”

不存在可行行程时，系统返回搜索中遇到的**首个阻塞约束**，按事件发生时刻（最早为先）
排序，消息会具体指出原因，例如：

- 起点站在出发时刻即处于停运时段，无法出发；
- 某班次被线路停运区间覆盖，整班被跳过；
- 拟在局部停运站段的封锁区间内部上车，或班次在封锁起点处被截断、不能穿越；
- 步行到达邻站时该站恰好封站，不能进入换乘；
- 班次到达某站时该站封站，只能跳站通过；
- 最早出发时刻之后已无班次，或起终点之间不存在连通路径。

截断的乘车段会在结果时间线以 ⛔ 标记，注明被封锁的线路、站段与时间窗；
时间线其余部分（等 / 走 / 乘）保持原有结构。

结果区逐段（`等 / 走 / 乘`）展示时间线：站内候车区间、步行换乘起讫与耗时、
每个班次的线路、班次号、上/下车站点及发车、到达时刻。

## 接口

- `GET /` — 页面
- `GET /api/sample` — 内置示例数据
- `POST /api/plan` — 规划

`/api/plan` 请求体：

```json
{
  "query": {"origin": "S1", "destination": "S6",
            "earliest": "08:00", "preference": "fastest"},
  "data": {
    "stations": [{"id": "S1", "name": "东郊枢纽"}],
    "trips": [{"id": "K1", "route": "1路",
               "stops": [{"station": "S1", "time": "08:05"}]}],
    "transfers": [{"from": "S2", "to": "S1", "minutes": 4}],
    "suspensions": [
      {"kind": "station", "ref": "S7",
       "start": "00:00", "end": "23:59", "label": "施工封站"},
      {"kind": "segment", "ref": "2路", "from": "S4", "to": "S6",
       "start": "08:00", "end": "09:30", "label": "区间甩段"}
    ]
  }
}
```

`preference` 取值：`fastest` / `transfers` / `walking`；
停运 `kind` 取值：

| kind | 含义 | 必填字段 |
|---|---|---|
| `station` | 站点停运（跳站通过） | `ref`＝站点 ID |
| `route` | 线路停运（跳过整班） | `ref`＝线路名 |
| `segment` | 线路局部停运（站段截断） | `ref`＝线路名、`from`/`to`＝该线路经停顺序中的起止站点 |

三类停运都含 `start` / `end`（`HH:MM`，结束必须晚于开始）与可选 `label`。
`segment` 的线路名也兼容写在 `route` 字段；`from`/`to` 两站必须存在，且该线路
至少有一个班次按顺序同时经过两站，否则返回 400。
时间均为当日 `HH:MM`，不含跨夜班次。

成功返回 `{"status": "ok", "itinerary": {...}}`；成功行程中被截断的乘车段
（`type: "ride"`）会额外带 `"truncated": true` 与
`"segment": {"route", "from", "to", "window": {"start","end","label"}}`，
其余字段与既有响应一致。
无解返回 `{"status": "blocked", "blocker": {...}}`，局部停运导致的首个阻塞约束
`kind` 为 `segment_suspended`，并带 `route`、`seg_from`、`seg_to` 指向实际线路站段；
输入不合法返回 HTTP 400 与 `{"status": "error", "message": "..."}`。

### 调用示例（curl）

```bash
# 线路局部停运：2路 S4->S6 早高峰封锁，班次只能截到 S4，再步行或换乘
curl -s http://localhost:8000/api/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {"origin": "S1", "destination": "S6",
              "earliest": "08:00", "preference": "fastest"},
    "data": {
      "stations": [{"id": "S1"}, {"id": "S4"}, {"id": "S6"}],
      "trips": [
        {"id": "K2", "route": "2路", "stops": [
          {"station": "S1", "time": "08:08"},
          {"station": "S4", "time": "08:25"},
          {"station": "S6", "time": "08:35"}]},
        {"id": "K3", "route": "3路", "stops": [
          {"station": "S4", "time": "08:45"},
          {"station": "S6", "time": "08:52"}]}
      ],
      "transfers": [],
      "suspensions": [
        {"kind": "segment", "ref": "2路", "from": "S4", "to": "S6",
         "start": "08:00", "end": "09:30", "label": "路口施工"}
      ]
    }
  }'
```

成功响应（节选；`K2` 乘段在 `S4` 截断，换乘 `K3` 继续，未穿越封锁站段）：

```json
{
  "status": "ok",
  "itinerary": {
    "arrival": 532, "rides": 2, "transfers": 1,
    "legs": [
      {"type": "ride", "trip_id": "K2", "route": "2路",
       "from": "S1", "to": "S4", "depart": 488, "arrive": 505,
       "minutes": 17, "truncated": true,
       "segment": {"route": "2路", "from": "S4", "to": "S6",
                   "window": {"start": 480, "end": 570, "label": "路口施工"}}},
      {"type": "ride", "trip_id": "K3", "route": "3路",
       "from": "S4", "to": "S6", "depart": 525, "arrive": 532, "minutes": 7}
    ]
  }
}
```

若截断后既无步行也无其他班次可继续，则返回
`{"status": "blocked", "blocker": {"kind": "segment_suspended", ...}}`，
消息具体指出是哪条线路的哪个站段在何时截断。

## 目录结构

```
.
├── app.py               # Flask 路由与静态页面
├── planner.py           # 时间依赖图、停运判定、多标签搜索、阻塞约束归因
├── static/
│   ├── index.html       # 页面结构
│   ├── style.css        # 样式
│   └── app.js           # 原生 JS：录入、重算、结果渲染
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
