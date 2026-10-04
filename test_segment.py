"""线路区段停运（segment）行为验证脚本。"""

import json
import sys

sys.path.insert(0, "/workspace")

from planner import SAMPLE_DATA, PlanError, Planner, normalize


def make_data(**over):
    base = {
        "stations": [
            {"id": "A", "name": "甲站"},
            {"id": "B", "name": "乙站"},
            {"id": "C", "name": "丙站"},
            {"id": "D", "name": "丁站"},
            {"id": "E", "name": "戊站"},
        ],
        "trips": [
            {"id": "T1", "route": "1路", "stops": [
                {"station": "A", "time": "08:00"},
                {"station": "B", "time": "08:10"},
                {"station": "C", "time": "08:20"},
                {"station": "D", "time": "08:30"},
                {"station": "E", "time": "08:40"},
            ]},
        ],
        "transfers": [],
        "suspensions": [],
    }
    base.update(over)
    return base


def plan(data, origin, dest, earliest="07:00", pref="fastest"):
    net = normalize(data)
    hh, mm = map(int, earliest.split(":"))
    return Planner(net).plan(origin, dest, hh * 60 + mm, pref)


passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {extra}")


print("== 1. 跨过封锁站段：不整班删除、不穿越、最远坐到封锁前 ==")
data = make_data(suspensions=[
    {"kind": "segment", "route": "1路", "from": "B", "to": "D",
     "start": "00:00", "end": "23:59", "label": "区间施工"},
])
r = plan(data, "A", "B")
check("A→B 仍可到达（封锁段起点站下车）", r["status"] == "ok"
      and r["itinerary"]["arrival"] == 8 * 60 + 10, json.dumps(r, ensure_ascii=False))
ride = r["itinerary"]["legs"][-1]
check("乘车段在 B 终止且带截断标记", ride["to"] == "B" and ride.get("truncated"),
      json.dumps(ride, ensure_ascii=False))

r = plan(data, "A", "E")
check("A→E 不可直达穿越（blocked）", r["status"] == "blocked", json.dumps(r, ensure_ascii=False))
b = r.get("blocker", {})
check("首阻塞指向实际线路站段 segment_suspended@B",
      b.get("kind") == "segment_suspended" and b.get("seg_from") == "B"
      and b.get("route") == "1路" and b.get("time") == 8 * 60 + 10,
      json.dumps(b, ensure_ascii=False))

print("== 2. 区间外上、下车仍可用 ==")
r = plan(data, "D", "E")  # D 是封锁段终点，D→E 在区间外
check("D→E 可乘车（封锁段终点之后上车）", r["status"] == "ok"
      and r["itinerary"]["arrival"] == 8 * 60 + 40, json.dumps(r, ensure_ascii=False))
r = plan(data, "A", "B")
check("A→B 在封锁段起点下车", r["status"] == "ok")
r2 = plan(data, "B", "E")
check("B 上车无法出站（blocked，首阻塞为站段）", r2["status"] == "blocked"
      and r2["blocker"]["kind"] == "segment_suspended",
      json.dumps(r2, ensure_ascii=False))

print("== 3. 截断后由步行 / 其他班次继续 ==")
data2 = make_data(
    trips=[
        {"id": "T1", "route": "1路", "stops": [
            {"station": "A", "time": "08:00"},
            {"station": "B", "time": "08:10"},
            {"station": "C", "time": "08:20"},
            {"station": "D", "time": "08:30"},
            {"station": "E", "time": "08:40"},
        ]},
        {"id": "T2", "route": "2路", "stops": [
            {"station": "B", "time": "08:25"},
            {"station": "E", "time": "08:50"},
        ]},
    ],
    suspensions=[
        {"kind": "segment", "route": "1路", "from": "B", "to": "D",
         "start": "00:00", "end": "23:59"},
    ],
)
r = plan(data2, "A", "E")
rides = [l for l in r.get("itinerary", {}).get("legs", []) if l["type"] == "ride"]
check("A→E 经 B 换乘 2路 到达", r["status"] == "ok" and len(rides) == 2
      and rides[0]["to"] == "B" and rides[1]["trip_id"] == "T2",
      json.dumps(r, ensure_ascii=False))
check("第一段乘车带截断说明", rides and rides[0].get("truncated")
      and "区段停运" in rides[0].get("truncation", ""),
      json.dumps(rides[0] if rides else {}, ensure_ascii=False))

data3 = make_data(
    transfers=[{"from": "B", "to": "D", "minutes": 15}],
    suspensions=[
        {"kind": "segment", "route": "1路", "from": "B", "to": "D",
         "start": "00:00", "end": "23:59"},
    ],
)
r = plan(data3, "A", "E")
legs = r.get("itinerary", {}).get("legs", [])
walks = [l for l in legs if l["type"] == "walk"]
check("A→E 截断后步行 B→D 再乘同班后半段", r["status"] == "ok"
      and walks and walks[0]["from"] == "B" and walks[0]["to"] == "D",
      json.dumps(r, ensure_ascii=False))

print("== 4. 校验错误段 ==")
def expect_err(name, susp, frag):
    try:
        normalize(make_data(suspensions=[susp]))
    except PlanError as exc:
        check(name, frag in str(exc), str(exc))
        return
    check(name, False, "未报错")

expect_err("from/to 站点不存在",
           {"kind": "segment", "route": "1路", "from": "B", "to": "ZZ",
            "start": "08:00", "end": "09:00"}, "不存在的站点")
expect_err("起止站点相同",
           {"kind": "segment", "route": "1路", "from": "B", "to": "B",
            "start": "08:00", "end": "09:00"}, "起止站点相同")
expect_err("线路不存在",
           {"kind": "segment", "route": "9路", "from": "B", "to": "C",
            "start": "08:00", "end": "09:00"}, "不存在的线路")
expect_err("经停顺序相反",
           {"kind": "segment", "route": "1路", "from": "D", "to": "B",
            "start": "08:00", "end": "09:00"}, "经停顺序相反")
# 站点存在但不在该线路经停序列中 → 区段不存在
try:
    normalize(make_data(
        stations=[{"id": "A"}, {"id": "B"}, {"id": "C"}, {"id": "D"},
                  {"id": "E"}, {"id": "F"}],
        suspensions=[{"kind": "segment", "route": "1路", "from": "B",
                      "to": "F", "start": "08:00", "end": "09:00"}]))
    check("区段不存在报错", False, "未报错")
except PlanError as exc:
    check("区段不存在报错", "不存在" in str(exc), str(exc))
expect_err("时间窗 start>=end",
           {"kind": "segment", "route": "1路", "from": "B", "to": "C",
            "start": "09:00", "end": "09:00"}, "结束时刻必须晚于开始时刻")
expect_err("缺少线路名",
           {"kind": "segment", "from": "B", "to": "C",
            "start": "08:00", "end": "09:00"}, "缺少线路名")
expect_err("缺少起止站点",
           {"kind": "segment", "route": "1路",
            "start": "08:00", "end": "09:00"}, "起止站点")

print("== 5. 边界窗口：站段运行区间与时间窗相交才封锁 ==")
# B→C 站段运行 08:10–08:20
for name, win, expect_blocked in [
    ("窗结束=站段开始（相接即封锁）", ("08:00", "08:10"), True),
    ("窗开始=站段结束（相接即封锁）", ("08:20", "08:30"), True),
    ("窗完全在站段前（不封锁）", ("07:00", "08:09"), False),
    ("窗完全在站段后（不封锁）", ("08:21", "09:00"), False),
    ("窗在站段运行中途开始（封锁）", ("08:15", "08:16"), True),
]:
    d = make_data(suspensions=[
        {"kind": "segment", "route": "1路", "from": "B", "to": "C",
         "start": win[0], "end": win[1]}])
    rr = plan(d, "A", "E")
    got_blocked = rr["status"] == "blocked"
    check(name, got_blocked == expect_blocked,
          f"window={win} status={rr['status']}")

print("== 6. 旧格式（station / route）行为不变 ==")
r = plan(make_data(suspensions=[
    {"kind": "station", "ref": "C", "start": "00:00", "end": "23:59"}]),
    "A", "E")
check("站点停运：跳站通过仍可达 E", r["status"] == "ok"
      and r["itinerary"]["arrival"] == 8 * 60 + 40)
r = plan(make_data(suspensions=[
    {"kind": "route", "ref": "1路", "start": "00:00", "end": "23:59"}]),
    "A", "E")
check("整线停运：整班跳过 → blocked", r["status"] == "blocked"
      and r["blocker"]["kind"] == "route_suspended")
r = plan(make_data(), "A", "E")
check("无停运：直达", r["status"] == "ok" and r["itinerary"]["arrival"] == 8 * 60 + 40)

print("== 7. 示例数据 & 完全无解稳定归因 ==")
net = normalize(SAMPLE_DATA)
r = Planner(net).plan("S1", "S6", 8 * 60 + 30, "fastest")
legs = r.get("itinerary", {}).get("legs", [])
rides = [l for l in legs if l["type"] == "ride"]
walks = [l for l in legs if l["type"] == "walk"]
check("示例：08:30 出发 K2b 在 S4 截断，步行 S4→S5 换 K4",
      r["status"] == "ok" and len(rides) == 2
      and rides[0]["trip_id"] == "K2b" and rides[0]["to"] == "S4"
      and rides[0].get("truncated")
      and walks and walks[0]["from"] == "S4" and walks[0]["to"] == "S5"
      and rides[1]["trip_id"] == "K4"
      and r["itinerary"]["arrival"] == 9 * 60 + 12,
      json.dumps(r, ensure_ascii=False))
r = Planner(net).plan("S1", "S6", 8 * 60, "fastest")
check("示例：08:00 出发仍 K2 直达（窗口外不受影响）",
      r["status"] == "ok" and r["itinerary"]["arrival"] == 8 * 60 + 35,
      json.dumps(r, ensure_ascii=False))

# 完全无解：唯一通路被区段封锁，首阻塞须指向站段且按时间稳定
data4 = make_data(
    trips=[
        {"id": "T1", "route": "1路", "stops": [
            {"station": "A", "time": "08:00"},
            {"station": "B", "time": "08:10"},
            {"station": "C", "time": "08:20"},
            {"station": "D", "time": "08:30"},
            {"station": "E", "time": "08:40"},
        ]},
        {"id": "T0", "route": "1路", "stops": [
            {"station": "A", "time": "07:00"},
            {"station": "B", "time": "07:10"},
            {"station": "C", "time": "07:20"},
            {"station": "D", "time": "07:30"},
            {"station": "E", "time": "07:40"},
        ]},
    ],
    suspensions=[
        {"kind": "segment", "route": "1路", "from": "B", "to": "E",
         "start": "00:00", "end": "23:59", "label": "全日封锁"},
    ],
)
r = plan(data4, "A", "E", "06:00")
check("完全无解 → blocked", r["status"] == "blocked", json.dumps(r, ensure_ascii=False))
b = r.get("blocker", {})
check("首阻塞=时间最早的站段封锁（T0@07:10 B）",
      b.get("kind") == "segment_suspended" and b.get("trip_id") == "T0"
      and b.get("time") == 7 * 60 + 10 and b.get("seg_from") == "B",
      json.dumps(b, ensure_ascii=False))
check("阻塞消息含线路与站段", "1路" in b.get("message", "")
      and "乙站" in b.get("message", ""), b.get("message", ""))

print(f"\n通过 {passed}，失败 {failed}")
sys.exit(1 if failed else 0)
