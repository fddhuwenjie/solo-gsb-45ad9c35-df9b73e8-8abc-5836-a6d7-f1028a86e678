"""公交时刻换乘规划核心。

网络为“时间依赖图”：
- 站点为节点；
- 每个班次按发车时刻依次经停若干站点，只有在不早于到达时刻才能在该站上车；
- 站点之间可配置站内/站间步行换乘边（双向），换乘必须先走完步行时间；
- 临时停运分三类：站点停运（某站在时间窗内不能上/下车，班次只能跳站通过）、
  线路停运（时间窗内发车的班次整个班次被跳过）与线路局部停运（指定线路按经停顺序
  的某一站段在时间窗内封闭，班次不整班删除，只在封锁站段之前截断：封锁前各站仍可
  下车、封锁终点及以后各站仍可上车，区间内既不能上也不能下，不得穿越封锁站段）。

搜索采用多标签时间依赖 Dijkstra，标签按所选偏好对应的三元组做帕累托支配剪枝；
无解时汇总搜索过程中遇到的全部阻塞约束，返回时间最早的一个。
"""

from __future__ import annotations

import heapq
from itertools import count
from typing import Any

DAY_MINUTES = 24 * 60

# 三种偏好对应的字典序键序：
# 到达时刻、换乘次数(=乘坐班次数-1)、总步行分钟
PREFERENCE_FIELDS = {
    "fastest": ("arrival", "transfers", "walking"),
    "transfers": ("transfers", "arrival", "walking"),
    "walking": ("walking", "arrival", "transfers"),
}
PREFERENCE_LABELS = {
    "fastest": "最快到达",
    "transfers": "最少换乘",
    "walking": "最少步行",
}


class PlanError(ValueError):
    """输入数据不合法。"""


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def to_hhmm(value: int | None) -> str:
    if value is None:
        return "--:--"
    value = int(value)
    return f"{value // 60:02d}:{value % 60:02d}"


def parse_time(value: Any, field: str) -> int:
    """把 'HH:MM' 或分钟数解析为 0..1439 的整数分钟。"""
    if isinstance(value, bool):
        raise PlanError(f"{field} 时间格式不合法：{value!r}")
    if isinstance(value, (int, float)):
        minutes = int(value)
    elif isinstance(value, str):
        text = value.strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise PlanError(f"{field} 时间格式应为 HH:MM：{value!r}")
        try:
            hours, mins = int(parts[0]), int(parts[1])
        except ValueError:
            raise PlanError(f"{field} 时间格式应为 HH:MM：{value!r}")
        minutes = hours * 60 + mins
    else:
        raise PlanError(f"{field} 时间格式不合法：{value!r}")
    if not 0 <= minutes < DAY_MINUTES:
        raise PlanError(f"{field} 时间超出当日范围：{to_hhmm(minutes)}")
    return minutes


def _uid(prefix: str, value: Any, index: int) -> str:
    text = str(value).strip() if value is not None else ""
    return text or f"{prefix}{index + 1}"


# --------------------------------------------------------------------------- #
# 数据规范化
# --------------------------------------------------------------------------- #
def normalize(data: dict) -> dict:
    """校验并规范化前端提交的数据。"""
    if not isinstance(data, dict):
        raise PlanError("请求体必须是 JSON 对象")

    # ---- 站点 ----
    raw_stations = data.get("stations", [])
    if not isinstance(raw_stations, list) or not raw_stations:
        raise PlanError("至少需要录入一个站点")
    stations: list[str] = []
    station_names: dict[str, str] = {}
    seen: set[str] = set()
    for i, item in enumerate(raw_stations):
        if isinstance(item, str):
            sid, name = item.strip(), item.strip()
        elif isinstance(item, dict):
            sid = str(item.get("id", "")).strip() or f"S{i + 1}"
            name = str(item.get("name", "")).strip() or sid
        else:
            raise PlanError(f"站点 #{i + 1} 格式不合法")
        if sid in seen:
            raise PlanError(f"站点编号重复：{sid}")
        seen.add(sid)
        stations.append(sid)
        station_names[sid] = name

    # ---- 班次 ----
    raw_trips = data.get("trips", [])
    if not isinstance(raw_trips, list):
        raise PlanError("trips 必须是数组")
    trips: list[dict] = []
    trip_ids: set[str] = set()
    for i, item in enumerate(raw_trips):
        if not isinstance(item, dict):
            raise PlanError(f"班次 #{i + 1} 格式不合法")
        tid = _uid("T", item.get("id"), i)
        if tid in trip_ids:
            raise PlanError(f"班次编号重复：{tid}")
        trip_ids.add(tid)
        route = str(item.get("route", tid)).strip() or tid
        raw_stops = item.get("stops", [])
        if not isinstance(raw_stops, list) or len(raw_stops) < 2:
            raise PlanError(f"班次 {tid} 至少需要两个经停站")
        stops: list[dict] = []
        prev_time = -1
        for j, stop in enumerate(raw_stops):
            if not isinstance(stop, dict):
                raise PlanError(f"班次 {tid} 的经停站 #{j + 1} 格式不合法")
            sid = str(stop.get("station", "")).strip()
            if sid not in seen:
                raise PlanError(f"班次 {tid} 引用了不存在的站点：{sid}")
            when = parse_time(stop.get("time"), f"班次 {tid} 站点 {sid} 时刻")
            if when <= prev_time:
                raise PlanError(
                    f"班次 {tid} 在 {sid} 的时刻 {to_hhmm(when)} 不晚于前一站，"
                    "各班次时刻必须严格递增"
                )
            prev_time = when
            stops.append({"station": sid, "time": when})
        trips.append({"id": tid, "route": route, "stops": stops})

    # ---- 步行换乘（双向）----
    raw_transfers = data.get("transfers", [])
    if not isinstance(raw_transfers, list):
        raise PlanError("transfers 必须是数组")
    walk_minutes: dict[tuple[str, str], int] = {}

    def add_walk(a: str, b: str, minutes: int) -> None:
        key = (a, b)
        old = walk_minutes.get(key)
        if old is None or minutes < old:
            walk_minutes[key] = minutes

    for i, item in enumerate(raw_transfers):
        if not isinstance(item, dict):
            raise PlanError(f"步行换乘 #{i + 1} 格式不合法")
        a = str(item.get("from", "")).strip()
        b = str(item.get("to", "")).strip()
        if a not in seen or b not in seen:
            raise PlanError(f"步行换乘 #{i + 1} 引用了不存在的站点：{a}->{b}")
        if a == b:
            raise PlanError(f"步行换乘 #{i + 1} 的起终点相同：{a}")
        try:
            minutes = int(item.get("minutes"))
        except (TypeError, ValueError):
            raise PlanError(f"步行换乘 {a}->{b} 的步行分钟数不合法")
        if minutes < 1:
            raise PlanError(f"步行换乘 {a}->{b} 的步行时间必须为正整数")
        add_walk(a, b, minutes)
        add_walk(b, a, minutes)

    walk_edges: dict[str, list[tuple[str, int]]] = {s: [] for s in stations}
    for (a, b), minutes in walk_minutes.items():
        walk_edges[a].append((b, minutes))
    for sid in walk_edges:
        walk_edges[sid].sort(key=lambda x: (x[1], x[0]))

    # ---- 临时停运 ----
    raw_susp = data.get("suspensions", [])
    if not isinstance(raw_susp, list):
        raise PlanError("suspensions 必须是数组")
    station_windows: dict[str, list[tuple[int, int, str | None]]] = {
        s: [] for s in stations
    }
    route_windows: dict[str, list[tuple[int, int, str | None]]] = {}
    # 线路局部停运：班次 -> [(from下标, to下标, start, end, label, from站, to站, 线路)]
    trip_segments: dict[str, list[tuple]] = {}
    # 线路名 -> 班次列表，供局部停运校验站段归属
    trips_by_route: dict[str, list[dict]] = {}
    for trip in trips:
        trips_by_route.setdefault(trip["route"], []).append(trip)

    for i, item in enumerate(raw_susp):
        if not isinstance(item, dict):
            raise PlanError(f"停运记录 #{i + 1} 格式不合法")
        kind = str(item.get("kind", "")).strip()
        # ref 为标准字段；历史/外部数据可能把线路名放在 route 字段，两者都接受
        ref = str(item.get("ref", "")).strip()
        if not ref:
            ref = str(item.get("route", "")).strip()
        label = item.get("label")
        seg_from = seg_to = ""
        if kind == "station":
            if ref not in seen:
                raise PlanError(f"停运记录 #{i + 1} 引用了不存在的站点：{ref}")
        elif kind in ("route", "segment"):
            if not ref:
                raise PlanError(f"停运记录 #{i + 1} 缺少线路名")
        else:
            raise PlanError(
                f"停运记录 #{i + 1} 类型必须是 station / route / segment"
            )
        if kind == "segment":
            seg_from = str(item.get("from", "")).strip()
            seg_to = str(item.get("to", "")).strip()
            if not seg_from or not seg_to:
                raise PlanError(
                    f"停运记录 #{i + 1}（线路 {ref} 局部停运）必须填写经停顺序中的"
                    "起止站点"
                )
            if seg_from not in seen:
                raise PlanError(
                    f"停运记录 #{i + 1}（{ref}）引用了不存在的站点：{seg_from}"
                )
            if seg_to not in seen:
                raise PlanError(
                    f"停运记录 #{i + 1}（{ref}）引用了不存在的站点：{seg_to}"
                )
            if seg_from == seg_to:
                raise PlanError(
                    f"停运记录 #{i + 1}（{ref}）的起止站点相同：{seg_from}"
                )
            route_trips = trips_by_route.get(ref, ())
            matched: list[tuple[dict, int, int]] = []
            for trip in route_trips:
                pos = {stop["station"]: k for k, stop in enumerate(trip["stops"])}
                if seg_from in pos and seg_to in pos \
                        and pos[seg_from] < pos[seg_to]:
                    matched.append((trip, pos[seg_from], pos[seg_to]))
            if not matched:
                raise PlanError(
                    f"停运记录 #{i + 1}：线路 {ref} 没有班次按经停顺序同时经过 "
                    f"{seg_from} 与 {seg_to}，无法定义局部停运站段"
                )
        start = parse_time(item.get("start"), f"停运记录 #{i + 1} 开始")
        end = parse_time(item.get("end"), f"停运记录 #{i + 1} 结束")
        if end <= start:
            raise PlanError(f"停运记录 #{i + 1} 的结束时刻必须晚于开始时刻")
        window = (start, end, str(label) if label else None)
        if kind == "station":
            station_windows[ref].append(window)
        elif kind == "route":
            route_windows.setdefault(ref, []).append(window)
        else:
            for trip, p_from, p_to in matched:
                trip_segments.setdefault(trip["id"], []).append(
                    (p_from, p_to, start, end,
                     str(label) if label else None, seg_from, seg_to, ref))

    # 局部停运站段按（from 下标, to 下标, 窗口起点, 窗口终点）排序，保证多段重叠时
    # 归因与截断稳定可复现
    for tid in trip_segments:
        trip_segments[tid].sort(key=lambda s: (s[0], s[1], s[2], s[3]))

    # 站点 -> 可乘坐的 (班次, 经停下标)，按到站时刻排序
    departures: dict[str, list[tuple[str, int]]] = {s: [] for s in stations}
    for trip in trips:
        for idx, stop in enumerate(trip["stops"]):
            departures[stop["station"]].append((trip["id"], idx))
    for sid in departures:
        departures[sid].sort(key=lambda x: (
            next(t["stops"][x[1]]["time"] for t in trips if t["id"] == x[0]),
            x[0],
        ))

    return {
        "stations": stations,
        "station_names": station_names,
        "trips": {t["id"]: t for t in trips},
        "trip_order": [t["id"] for t in trips],
        "walk_edges": walk_edges,
        "departures": departures,
        "station_windows": station_windows,
        "route_windows": route_windows,
        "trip_segments": trip_segments,
    }


SAMPLE_DATA: dict = {
    "stations": [
        {"id": "S1", "name": "东郊枢纽"},
        {"id": "S2", "name": "东郊路"},
        {"id": "S3", "name": "会展中心"},
        {"id": "S4", "name": "人民广场"},
        {"id": "S5", "name": "大学城"},
        {"id": "S6", "name": "火车北站"},
        {"id": "S7", "name": "临江道口"},
        {"id": "S8", "name": "西城枢纽"},
    ],
    "trips": [
        {"id": "K1", "route": "1路", "stops": [
            {"station": "S1", "time": "08:05"},
            {"station": "S3", "time": "08:23"},
            {"station": "S4", "time": "08:40"},
            {"station": "S5", "time": "09:02"},
        ]},
        {"id": "K2", "route": "2路", "stops": [
            {"station": "S1", "time": "08:08"},
            {"station": "S4", "time": "08:25"},
            {"station": "S6", "time": "08:35"},
        ]},
        {"id": "K2b", "route": "2路", "stops": [
            {"station": "S1", "time": "08:38"},
            {"station": "S4", "time": "08:55"},
            {"station": "S6", "time": "09:05"},
        ]},
        {"id": "K3", "route": "3路", "stops": [
            {"station": "S4", "time": "08:45"},
            {"station": "S6", "time": "08:52"},
            {"station": "S7", "time": "09:10"},
            {"station": "S8", "time": "09:22"},
        ]},
        {"id": "K4", "route": "4路", "stops": [
            {"station": "S5", "time": "09:05"},
            {"station": "S6", "time": "09:12"},
            {"station": "S7", "time": "09:20"},
            {"station": "S8", "time": "09:35"},
        ]},
    ],
    "transfers": [
        {"from": "S2", "to": "S1", "minutes": 4},
        {"from": "S4", "to": "S5", "minutes": 7},
        {"from": "S3", "to": "S6", "minutes": 30},
    ],
    "suspensions": [
        # 示例：临江道口当日全天封站，途经班次须跳站通过（仍可乘到西城枢纽）
        {"kind": "station", "ref": "S7", "start": "00:00", "end": "23:59",
         "label": "道口施工，全天封站"},
        # 示例：3 路午间区间停运
        {"kind": "route", "ref": "3路", "start": "12:00", "end": "13:00",
         "label": "3路午间停运"},
        # 示例：2 路早高峰人民广场→火车北站 站段封闭，班次只截到封锁前各站，
        # 不得穿越；下车步行或换乘 3 路继续
        {"kind": "segment", "ref": "2路", "from": "S4", "to": "S6",
         "start": "08:00", "end": "09:30",
         "label": "人民广场路口施工，2路区间甩段"},
    ],
}


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #
class Planner:
    def __init__(self, network: dict):
        self.n = network
        self.names = network["station_names"]

    def sname(self, sid: str) -> str:
        return self.names.get(sid, sid)

    def station_open(self, sid: str, when: int) -> tuple[bool, dict | None]:
        for start, end, label in self.n["station_windows"].get(sid, ()):  # type: ignore[arg-type]
            if start <= when <= end:
                return False, {"start": start, "end": end, "label": label}
        return True, None

    def route_open(self, route: str, when: int) -> tuple[bool, dict | None]:
        for start, end, label in self.n["route_windows"].get(route, ()):
            if start <= when <= end:
                return False, {"start": start, "end": end, "label": label}
        return True, None

    def segment_for_board(self, trip_id: str, idx: int,
                          when: int) -> tuple | None:
        """在经停下标 idx、时刻 when 上车时，禁止上车的局部停运站段（区间内上车）。

        封锁区间内部为下标 [p_from, p_to)：终点站 p_to 属于区间外，仍可上车。
        """
        for seg in self.n["trip_segments"].get(trip_id, ()):
            p_from, p_to, start, end, label, s_from, s_to, route = seg
            if p_from <= idx < p_to and start <= when <= end:
                return seg
        return None

    def segment_cut(self, trip_id: str, idx: int, dep: int) -> tuple | None:
        """从下标 idx 上车（时刻 dep）后，第一个截断本班次的局部停运站段。

        班次穿过站段的时间窗 [到达 p_from, 到达 p_to] 与停运窗（含边界）重叠，
        且上车点在 p_from 之前时，本班在 p_from 处截断：不能穿越，只能在 p_from
        及之前下车；p_to 及以后属于区间外，另从其后方站上车不受影响。
        """
        stops = self.n["trips"][trip_id]["stops"]
        best = None
        for seg in self.n["trip_segments"].get(trip_id, ()):
            p_from, p_to, start, end, label, s_from, s_to, route = seg
            if p_from <= idx:
                continue  # 上车点不在封锁站段之前（区间内上车另有拦截）
            t_from = stops[p_from]["time"]
            t_to = stops[p_to]["time"]
            # 穿越时间窗 [t_from, t_to] 与停运窗 [start, end] 含边界重叠即截断
            if t_from <= end and start <= t_to:
                if best is None or (p_from, p_to, start, end) < (
                        best[0], best[1], best[2], best[3]):
                    best = seg
        return best

    @staticmethod
    def _window_text(window: dict | None) -> str:
        if not window:
            return "停运时段"
        base = f"{to_hhmm(window['start'])}–{to_hhmm(window['end'])}"
        if window.get("label"):
            base += f"（{window['label']}）"
        return base

    def plan(self, origin: str, destination: str, earliest: int,
             preference: str) -> dict:
        if origin not in self.names:
            raise PlanError(f"起点站不存在：{origin}")
        if destination not in self.names:
            raise PlanError(f"终点站不存在：{destination}")
        if preference not in PREFERENCE_FIELDS:
            raise PlanError(f"未知偏好：{preference}")

        # 起点自身在出发时刻即停运：任何方案都不可能成立
        open_at_start, window = self.station_open(origin, earliest)
        if not open_at_start:
            return self._blocked({
                "kind": "origin_closed",
                "action": "start",
                "station": origin,
                "time": earliest,
                "window": window,
                "message": (
                    f"起点站 {self.sname(origin)} 在 {to_hhmm(earliest)} 处于临时停运时段"
                    f"{self._window_text(window)}，无法出发。"
                ),
            })

        if origin == destination:
            return {"status": "ok", "itinerary": self._itinerary(
                origin, destination, earliest, preference, (), 0)}

        blockers: dict[tuple, dict] = {}

        def record(blocker: dict) -> None:
            key = (blocker["kind"], blocker.get("action"),
                   blocker.get("station"), blocker.get("time"),
                   blocker.get("trip_id"), blocker.get("route"),
                   blocker.get("seg_from"), blocker.get("seg_to"))
            blockers.setdefault(key, blocker)

        order = PREFERENCE_FIELDS[preference]

        def vec(label: dict) -> tuple:
            values = {
                "arrival": label["arrival"],
                "transfers": max(0, label["rides"] - 1),
                "walking": label["walk"],
            }
            return tuple(values[f] for f in order)

        def dominates(a: dict, b: dict) -> bool:
            va, vb = list(vec(a)), list(vec(b))
            return all(x <= y for x, y in zip(va, vb)) and va != vb

        def key_tuple(label: dict) -> tuple:
            return tuple(vec(label))

        # 标签：站点、到达时刻、乘坐班次数、累计步行分钟、上次班次、链
        start_label = {
            "uid": 0, "station": origin, "arrival": earliest,
            "rides": 0, "walk": 0, "last_trip": None, "chain": (),
        }
        buckets: dict[str, list[dict]] = {s: [] for s in self.n["stations"]}
        buckets[origin].append(start_label)

        heap: list[tuple] = []
        counter = count(1)
        heapq.heappush(heap, (earliest, 0, start_label["uid"], start_label))
        uid_of = {0: start_label}
        explored = 0

        def try_extend(label: dict, station: str, arrival: int, rides: int,
                       walk: int, last_trip: str | None, step: dict) -> None:
            new_label = {
                "uid": next(counter), "station": station, "arrival": arrival,
                "rides": rides, "walk": walk, "last_trip": last_trip,
                "chain": label["chain"] + (step,),
            }
            bucket = buckets[station]
            if any(dominates(existing, new_label) or
                   key_tuple(existing) == key_tuple(new_label)
                   for existing in bucket):
                return
            for existing in list(bucket):
                if dominates(new_label, existing):
                    bucket.remove(existing)
            bucket.append(new_label)
            uid_of[new_label["uid"]] = new_label
            heapq.heappush(heap, (arrival, new_label["uid"],
                                  new_label["uid"], new_label))

        while heap:
            _, _, uid, label = heapq.heappop(heap)
            if not any(b["uid"] == uid for b in buckets[label["station"]]):
                continue  # 已被更优标签支配
            explored += 1
            station = label["station"]
            ready = label["arrival"]

            # ---- 步行换乘边（时间无关，走完后才允许在邻站乘车）----
            for neighbor, minutes in self.n["walk_edges"].get(station, ()):
                arrival = ready + minutes
                open_now, win = self.station_open(neighbor, arrival)
                if not open_now:
                    record({
                        "kind": "station_closed", "action": "walk",
                        "station": neighbor, "time": arrival,
                        "trip_id": None, "window": win,
                        "message": (
                            f"步行 {self.sname(station)}→{self.sname(neighbor)} 于 "
                            f"{to_hhmm(arrival)} 到达 {self.sname(neighbor)}，但该站处于"
                            f"临时停运时段{self._window_text(win)}，无法进入换乘。"
                        ),
                    })
                    continue
                try_extend(label, neighbor, arrival, label["rides"],
                           label["walk"] + minutes, label["last_trip"],
                           {"type": "walk", "from": station, "to": neighbor,
                            "depart": ready, "arrive": arrival,
                            "minutes": minutes})

            # ---- 班次边（时间依赖：只能赶上 ready 之后的发车）----
            for trip_id, idx in self.n["departures"].get(station, ()):
                trip = self.n["trips"][trip_id]
                dep = trip["stops"][idx]["time"]
                if dep < ready:
                    continue  # 已发车
                if trip_id == label["last_trip"]:
                    continue  # 同班次不重复乘坐

                # 本站上车时站点必须开放
                open_board, win_board = self.station_open(station, dep)
                if not open_board:
                    record({
                        "kind": "station_closed", "action": "board",
                        "station": station, "time": dep,
                        "trip_id": trip_id, "window": win_board,
                        "message": (
                            f"拟于 {to_hhmm(dep)} 在 {self.sname(station)} 乘坐班次 "
                            f"{trip_id}（{trip['route']}），但该站此时处于临时停运时段"
                            f"{self._window_text(win_board)}，不能上车。"
                        ),
                    })
                    continue
                # 线路在该发车时刻处于停运区间：跳过整个班次
                open_route, win_route = self.route_open(trip["route"], dep)
                if not open_route:
                    record({
                        "kind": "route_suspended", "action": "board",
                        "station": station, "time": dep,
                        "trip_id": trip_id, "window": win_route,
                        "route": trip["route"],
                        "message": (
                            f"班次 {trip_id}（{trip['route']}）原定于 {to_hhmm(dep)} 从 "
                            f"{self.sname(station)} 发车，但线路在临时停运区间"
                            f"{self._window_text(win_route)}内，该班次被跳过。"
                        ),
                    })
                    continue

                # 上车点正处于某条局部停运站段的封锁区间内：不能上车（区间外仍可用）
                seg_here = self.segment_for_board(trip_id, idx, dep)
                if seg_here:
                    _, _, seg_start, seg_end, seg_label, sf, st, sroute = seg_here
                    record({
                        "kind": "segment_suspended", "action": "board",
                        "station": station, "time": dep,
                        "trip_id": trip_id, "route": sroute,
                        "seg_from": sf, "seg_to": st,
                        "window": {"start": seg_start, "end": seg_end,
                                   "label": seg_label},
                        "message": (
                            f"拟于 {to_hhmm(dep)} 在 {self.sname(station)} 乘坐班次 "
                            f"{trip_id}（{sroute}），但该站处于线路局部停运站段 "
                            f"{self.sname(sf)}→{self.sname(st)} 的"
                            f"{self._window_text({'start': seg_start, 'end': seg_end, 'label': seg_label})}"
                            "区间内，不能上车；封锁站段终点之后各站仍可上车。"
                        ),
                    })
                    continue

                # 本班次是否在前方被某条局部停运站段截断（不删整班）
                cut = self.segment_cut(trip_id, idx, dep)
                cut_idx = cut[0] if cut else -1

                # 延伸到后续每个（到站时开放的）经停站；停运站跳站通过；
                # 遇到局部停运站段的起点即截断：只保留封锁前能下车的乘段，不穿越
                for j in range(idx + 1, len(trip["stops"])):
                    target = trip["stops"][j]["station"]
                    arrive_at = trip["stops"][j]["time"]

                    if cut is not None and j == cut_idx:
                        _, _, c_start, c_end, c_label, c_sf, c_st, c_route = cut
                        win_cut = {"start": c_start, "end": c_end,
                                   "label": c_label}
                        # 封锁起点站本身能否下车，仍受站点停运约束
                        open_cut, win_alight = self.station_open(target,
                                                                 arrive_at)
                        if open_cut:
                            try_extend(
                                label, target, arrive_at, label["rides"] + 1,
                                label["walk"], trip_id,
                                {"type": "ride", "trip_id": trip_id,
                                 "route": trip["route"], "from": station,
                                 "to": target, "depart": dep,
                                 "arrive": arrive_at,
                                 "minutes": arrive_at - dep,
                                 "truncated": True,
                                 "segment": {"route": c_route,
                                             "from": c_sf, "to": c_st,
                                             "window": win_cut}})
                        else:
                            record({
                                "kind": "station_closed", "action": "alight",
                                "station": target, "time": arrive_at,
                                "trip_id": trip_id, "window": win_alight,
                                "message": (
                                    f"班次 {trip_id}（{trip['route']}）于 "
                                    f"{to_hhmm(arrive_at)} 到达 {self.sname(target)}，"
                                    f"该站正处于临时停运时段"
                                    f"{self._window_text(win_alight)}，班次跳站通过，"
                                    "不能下车。"
                                ),
                            })
                        record({
                            "kind": "segment_suspended", "action": "cross",
                            "station": target, "time": arrive_at,
                            "trip_id": trip_id, "route": c_route,
                            "seg_from": c_sf, "seg_to": c_st,
                            "window": win_cut,
                            "message": (
                                f"班次 {trip_id}（{c_route}）在 {to_hhmm(arrive_at)} "
                                f"到达 {self.sname(c_sf)} 后进入局部停运站段 "
                                f"{self.sname(c_sf)}→{self.sname(c_st)}（"
                                f"{self._window_text(win_cut)}），乘车段在此截断，"
                                "不能穿越封锁；请在封锁前各站下车，改由步行或其他班次"
                                "继续，封锁终点之后各站仍可乘车。"
                            ),
                        })
                        break  # 封锁站段及以后不再由本班继续

                    open_alight, win_alight = self.station_open(target, arrive_at)
                    if not open_alight:
                        record({
                            "kind": "station_closed", "action": "alight",
                            "station": target, "time": arrive_at,
                            "trip_id": trip_id, "window": win_alight,
                            "message": (
                                f"班次 {trip_id}（{trip['route']}）于 {to_hhmm(arrive_at)} "
                                f"到达 {self.sname(target)}，该站正处于临时停运时段"
                                f"{self._window_text(win_alight)}，班次跳站通过，不能下车。"
                            ),
                        })
                        continue
                    try_extend(
                        label, target, arrive_at, label["rides"] + 1,
                        label["walk"], trip_id,
                        {"type": "ride", "trip_id": trip_id,
                         "route": trip["route"], "from": station,
                         "to": target, "depart": dep, "arrive": arrive_at,
                         "minutes": arrive_at - dep})

        candidates = buckets[destination]
        if not candidates:
            return {"status": "blocked",
                    "blocker": self._first_blocker(blockers, origin,
                                                   destination, earliest)}

        best = min(candidates, key=lambda lb: (
            tuple([{"arrival": lb["arrival"],
                    "transfers": max(0, lb["rides"] - 1),
                    "walking": lb["walk"]}[f] for f in order]),
            lb["uid"]))
        return {"status": "ok", "explored": explored,
                "itinerary": self._itinerary(origin, destination, earliest,
                                             preference, best["chain"],
                                             best["rides"], best["walk"],
                                             best["arrival"])}

    # ------------------------------------------------------------------ #
    def _first_blocker(self, blockers: dict, origin: str,
                       destination: str, earliest: int) -> dict:
        items = list(blockers.values())

        # 结构性阻塞：起终点根本没有班次服务
        if not self.n["departures"].get(origin):
            return {
                "kind": "no_service", "action": "start", "station": origin,
                "time": earliest, "trip_id": None, "window": None,
                "message": f"起点站 {self.sname(origin)} 没有任何经停班次，无法出发。",
            }
        later_exists = any(
            self.n["trips"][tid]["stops"][idx]["time"] >= earliest
            for tid, idx in self.n["departures"].get(origin, ())
        )
        if not later_exists and not any(
                b.get("station") == origin for b in items):
            return {
                "kind": "no_service_after", "action": "start",
                "station": origin, "time": earliest, "trip_id": None,
                "window": None,
                "message": (
                    f"在 {to_hhmm(earliest)} 之后已无从 {self.sname(origin)} "
                    "发车的班次，当日运营结束。"
                ),
            }
        if not self.n["departures"].get(destination):
            return {
                "kind": "no_service", "action": "end", "station": destination,
                "time": earliest, "trip_id": None, "window": None,
                "message": f"终点站 {self.sname(destination)} 没有任何经停班次，无法到达。",
            }
        if items:
            # 先按事件发生时刻（最早为先）；同一时刻优先归因到具体线路站段
            # （segment_suspended），再按线路、站段、站点、班次稳定加细排序，
            # 保证同一输入下首个阻塞约束可复现
            kind_rank = {"origin_closed": 0, "segment_suspended": 1,
                         "station_closed": 2, "route_suspended": 3,
                         "no_service_after": 4}
            first = min(items, key=lambda b: (
                b.get("time", earliest), kind_rank.get(b["kind"], 9),
                str(b.get("route", "")),
                str(b.get("seg_from", "")), str(b.get("seg_to", "")),
                str(b.get("station", "")), str(b.get("trip_id", ""))))
            return first
        return {
            "kind": "unreachable", "action": "end", "station": destination,
            "time": earliest, "trip_id": None, "window": None,
            "message": (
                f"在给定的班次、步行换乘与停运条件下，{self.sname(origin)} 无法到达 "
                f"{self.sname(destination)}（两区间不存在连通路径）。"
            ),
        }

    def _blocked(self, blocker: dict) -> dict:
        return {"status": "blocked", "blocker": blocker}

    # ------------------------------------------------------------------ #
    def _itinerary(self, origin: str, destination: str, earliest: int,
                   preference: str, chain: tuple, rides: int,
                   walk: int = 0, arrival: int | None = None) -> dict:
        legs: list[dict] = []
        cursor = earliest
        total_wait = 0
        total_walk = 0
        for step in chain:
            if step["depart"] > cursor:
                wait = step["depart"] - cursor
                total_wait += wait
                legs.append({"type": "wait", "station": step["from"],
                             "start": cursor, "end": step["depart"],
                             "minutes": wait})
            legs.append(dict(step))
            if step["type"] == "walk":
                total_walk += step["minutes"]
            cursor = step["arrive"]
        if arrival is None:
            arrival = cursor
        return {
            "origin": origin,
            "destination": destination,
            "earliest": earliest,
            "preference": preference,
            "preference_label": PREFERENCE_LABELS[preference],
            "arrival": arrival,
            "rides": rides,
            "transfers": max(0, rides - 1),
            "walking_minutes": walk or total_walk,
            "waiting_minutes": total_wait,
            "legs": legs,
        }
