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
4. **临时停运区间**：
   - **站点停运**：时间窗内该站不能上、下车，途经班次自动**跳站通过**；
   - **线路整线停运**：时间窗内发车的该线路**整个班次被跳过**；
   - **线路区段停运**：给出线路名、经停顺序中的起止站点与时间窗，
     该线路起止站之间的站段在窗内封锁。班次跨过封锁站段时**不整班取消、
     也不穿越封锁**：乘车段最远生成到封锁段起点站（结果时间线会标注
     「在此终止」），之后由步行或其他班次接续；封锁区段之外的上、下车
     不受影响（例如可在区段终点之后重新上车，甚至步行绕过封锁后
     在更靠后的经停站重新登上同一班次）。

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
- 班次前方站段被线路区段停运封锁，只能提前下车（消息会指出具体线路、
  受阻站段与最远可下车的车站）；
- 步行到达邻站时该站恰好封站，不能进入换乘；
- 班次到达某站时该站封站，只能跳站通过；
- 最早出发时刻之后已无班次，或起终点之间不存在连通路径。

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
    "suspensions": [{"kind": "station", "ref": "S7",
                     "start": "00:00", "end": "23:59", "label": "施工封站"},
                    {"kind": "segment", "route": "2路", "from": "S4",
                     "to": "S6", "start": "08:50", "end": "09:10",
                     "label": "区间临时管制"}]
  }
}
```

`preference` 取值：`fastest` / `transfers` / `walking`；
停运 `kind` 取值：
- `station` — 站点停运，`ref` 为站点 ID；
- `route` — 线路整线停运，`ref` 为线路名；
- `segment` — 线路区段停运，字段为 `route`（线路名）、`from` / `to`
  （经停顺序中的起止站点，`from` 必须排在 `to` 之前）、`start` / `end`
  （时间窗）。站段运行区间与时间窗（闭区间）相交即视为封锁。

时间均为当日 `HH:MM`，不含跨夜班次。

成功返回 `{"status": "ok", "itinerary": {...}}`，
无解返回 `{"status": "blocked", "blocker": {"message": "首个阻塞约束…"}}`，
输入不合法返回 HTTP 400 与 `{"status": "error", "message": "..."}`。

### 调用示例

```bash
# 内置示例数据
curl http://localhost:8000/api/sample

# 规划：2 路 S4→S6 站段 08:50–09:10 区段停运，K2b 只能在 S4 前下车
curl -X POST http://localhost:8000/api/plan \
  -H "Content-Type: application/json" \
  -d '{
    "query": {"origin": "S1", "destination": "S6",
              "earliest": "08:30", "preference": "fastest"},
    "data": {
      "stations": [{"id": "S1", "name": "东郊枢纽"},
                   {"id": "S4", "name": "人民广场"},
                   {"id": "S5", "name": "大学城"},
                   {"id": "S6", "name": "火车北站"}],
      "trips": [{"id": "K2b", "route": "2路", "stops": [
                   {"station": "S1", "time": "08:38"},
                   {"station": "S4", "time": "08:55"},
                   {"station": "S6", "time": "09:05"}]},
                 {"id": "K4", "route": "4路", "stops": [
                   {"station": "S5", "time": "09:05"},
                   {"station": "S6", "time": "09:12"}]}],
      "transfers": [{"from": "S4", "to": "S5", "minutes": 7}],
      "suspensions": [{"kind": "segment", "route": "2路",
                       "from": "S4", "to": "S6",
                       "start": "08:50", "end": "09:10",
                       "label": "区间临时管制"}]
    }
  }'
```

响应（节选）：K2b 在 S4 截断并标注原因，随后步行至 S5 换乘 K4——

```json
{"status": "ok", "itinerary": {"arrival": 552, "legs": [
  {"type": "ride", "trip_id": "K2b", "route": "2路", "from": "S1", "to": "S4",
   "depart": 518, "arrive": 535, "truncated": true,
   "truncation": "受 2路 区段停运（人民广场→火车北站 08:50–09:10）影响，本次乘车在 人民广场 终止，需步行或换乘其他班次继续。"},
  {"type": "walk", "from": "S4", "to": "S5", "depart": 535, "arrive": 542},
  {"type": "ride", "trip_id": "K4", "route": "4路", "from": "S5", "to": "S6",
   "depart": 545, "arrive": 552}]}}
```

若无解（例如把上例的步行换乘删掉），返回的首个阻塞约束会指向实际线路站段：

```json
{"status": "blocked", "blocker": {"kind": "segment_suspended",
  "route": "2路", "seg_from": "S4", "seg_to": "S6", "trip_id": "K2b",
  "time": 535,
  "message": "班次 K2b（2路）于 08:55 到达 人民广场 后，前方 人民广场→火车北站 站段被线路区段停运封锁（2路 人民广场→火车北站 08:50–09:10，区间临时管制）…"}}
```

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
