"""公交时刻换乘规划核心。

网络为“时间依赖图”：
- 站点为节点；
- 每个班次按发车时刻依次经停若干站点，只有在不早于到达时刻才能在该站上车；
- 站点之间可配置站内/站间步行换乘边（双向），换乘必须先走完步行时间；
- 临时停运分三类：站点停运（某站在时间窗内不能上/下车，班次只能跳站通过）、
  线路停运（时间窗内发车的班次整个班次被跳过）与线路区段停运（某线路在经停
  顺序中的起止站点之间的站段于时间窗内封锁：班次不整班取消，也不穿越封锁，
  乘车段最远生成到封锁段起点站，之后由步行或其他班次接续；封锁区段之外的
  上、下车不受影响）。

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


def _add_segment_closure(segment_closures: dict, trips: list[dict],
                         item: dict, index: int, start: int, end: int,
                         label: str | None, station_ids: set[str]) -> None:
    """校验一条线路区段停运记录，并按班次经停顺序登记被封锁的站段区间。

    记录格式：{"kind": "segment", "route": 线路名, "from": 起点站, "to": 终点站,
               "start": ..., "end": ..., "label": ...}
    起止站点必须都出现在该线路至少一个班次的经停序列中且顺序一致；
    对不含起止站点的班次该记录不生效。
    """
    no = index + 1
    route = str(item.get("route") or item.get("ref") or "").strip()
    if not route:
        raise PlanError(f"停运记录 #{no} 缺少线路名")
    seg_from = str(item.get("from", "")).strip()
    seg_to = str(item.get("to", "")).strip()
    if not seg_from or not seg_to:
        raise PlanError(f"停运记录 #{no} 缺少经停顺序中的起止站点")
    for sid in (seg_from, seg_to):
        if sid not in station_ids:
            raise PlanError(f"停运记录 #{no} 引用了不存在的站点：{sid}")
    if seg_from == seg_to:
        raise PlanError(f"停运记录 #{no} 的起止站点相同：{seg_from}")

    route_trips = [t for t in trips if t["route"] == route]
    if not route_trips:
        raise PlanError(f"停运记录 #{no} 引用了不存在的线路：{route}")

    matched = False
    reversed_seen = False
    for trip in route_trips:
        seq = [s["station"] for s in trip["stops"]]
        if seg_from not in seq or seg_to not in seq:
            continue  # 该班次不经停此区段，记录对其不生效
        from_idx, to_idx = seq.index(seg_from), seq.index(seg_to)
        if from_idx >= to_idx:
            reversed_seen = True
            continue
        matched = True
        segment_closures.setdefault(trip["id"], []).append({
            "route": route, "from": seg_from, "to": seg_to,
            "from_idx": from_idx, "to_idx": to_idx,
            "start": start, "end": end, "label": label,
        })
    if not matched:
        if reversed_seen:
            raise PlanError(
                f"停运记录 #{no} 的起止站点与线路 {route} 的经停顺序相反："
                f"{seg_from}→{seg_to}")
        raise PlanError(
            f"停运记录 #{no}：线路 {route} 的经停站中不存在 "
            f"{seg_from}→{seg_to} 区段")


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
    # 线路区段停运：班次 ID -> 该班次经停序列中被封锁的站段区间列表
    segment_closures: dict[str, list[dict]] = {}
    for i, item in enumerate(raw_susp):
        if not isinstance(item, dict):
            raise PlanError(f"停运记录 #{i + 1} 格式不合法")
        kind = str(item.get("kind", "")).strip()
        ref = str(item.get("ref", "")).strip()
        label = item.get("label")
        if kind == "station":
            if ref not in seen:
                raise PlanError(f"停运记录 #{i + 1} 引用了不存在的站点：{ref}")
        elif kind == "route":
            if not ref:
                raise PlanError(f"停运记录 #{i + 1} 缺少线路名")
        elif kind == "segment":
            pass  # 线路名与起止站点在时间窗解析之后统一校验
        else:
            raise PlanError(
                f"停运记录 #{i + 1} 类型必须是 station、route 或 segment")
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
            _add_segment_closure(
                segment_closures, trips, item, i, start, end,
                str(label) if label else None, seen)

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
        "segment_closures": segment_closures,
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
        {"from": "S3", "to": "S6", "minutes": 12},
    ],
    "suspensions": [
        # 示例：临江道口当日全天封站，途经班次须跳站通过（仍可乘到西城枢纽）
        {"kind": "station", "ref": "S7", "start": "00:00", "end": "23:59",
         "label": "道口施工，全天封站"},
        # 示例：3 路午间区间停运
        {"kind": "route", "ref": "3路", "start": "12:00", "end": "13:00",
         "label": "3路午间停运"},
        # 示例：2 路 S4→S6 站段早高峰局部停运——跨段班次不取消、不穿越，
        # 只能在人民广场（S4）前下车，再步行或换乘其他班次前往火车北站
        {"kind": "segment", "route": "2路", "from": "S4", "to": "S6",
         "start": "08:50", "end": "09:10", "label": "2路 S4→S6 区间临时管制"},
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

    def segment_closure(self, trip_id: str, seg_idx: int) -> dict | None:
        """班次 trip_id 的第 seg_idx 个站段（stops[k]→stops[k+1]）是否被
        线路区段停运封锁：站段运行区间与停运时间窗（闭区间）相交即封锁。"""
        trip = self.n["trips"][trip_id]
        depart = trip["stops"][seg_idx]["time"]
        arrive = trip["stops"][seg_idx + 1]["time"]
        for clo in self.n["segment_closures"].get(trip_id, ()):
            if (clo["from_idx"] <= seg_idx < clo["to_idx"]
                    and depart <= clo["end"] and clo["start"] <= arrive):
                return clo
        return None

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
                   blocker.get("trip_id"))
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

        # 标签：站点、到达时刻、乘坐班次数、累计步行分钟、上次班次及其下车下标、链
        start_label = {
            "uid": 0, "station": origin, "arrival": earliest,
            "rides": 0, "walk": 0, "last_trip": None, "last_alight": -1,
            "chain": (),
        }
        buckets: dict[str, list[dict]] = {s: [] for s in self.n["stations"]}
        buckets[origin].append(start_label)

        heap: list[tuple] = []
        counter = count(1)
        heapq.heappush(heap, (earliest, 0, start_label["uid"], start_label))
        uid_of = {0: start_label}
        explored = 0

        def try_extend(label: dict, station: str, arrival: int, rides: int,
                       walk: int, last_trip: str | None, last_alight: int,
                       step: dict) -> None:
            new_label = {
                "uid": next(counter), "station": station, "arrival": arrival,
                "rides": rides, "walk": walk, "last_trip": last_trip,
                "last_alight": last_alight,
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
                           label["last_alight"],
                           {"type": "walk", "from": station, "to": neighbor,
                            "depart": ready, "arrive": arrival,
                            "minutes": minutes})

            # ---- 班次边（时间依赖：只能赶上 ready 之后的发车）----
            for trip_id, idx in self.n["departures"].get(station, ()):
                trip = self.n["trips"][trip_id]
                dep = trip["stops"][idx]["time"]
                if dep < ready:
                    continue  # 已发车
                if (trip_id == label["last_trip"]
                        and idx <= label["last_alight"]):
                    continue  # 同班次已乘过的区段不重复乘坐；但因区段停运
                    # 提前下车、步行绕行后，仍可在更靠后的经停站重新上车

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

                # 延伸到后续每个（到站时开放的）经停站；停运站跳站通过。
                # 线路区段停运的封锁站段不可穿越：乘车段最远生成到封锁段
                # 起点站（班次不整班取消），之后由步行或其他班次接续。
                for j in range(idx + 1, len(trip["stops"])):
                    closure = self.segment_closure(trip_id, j - 1)
                    if closure is not None:
                        seg_from = trip["stops"][j - 1]["station"]
                        seg_to = trip["stops"][j]["station"]
                        detail = (
                            f"{trip['route']} {self.sname(closure['from'])}→"
                            f"{self.sname(closure['to'])} "
                            f"{to_hhmm(closure['start'])}–"
                            f"{to_hhmm(closure['end'])}")
                        if closure["label"]:
                            detail += f"，{closure['label']}"
                        record({
                            "kind": "segment_suspended", "action": "ride",
                            "station": seg_from,
                            "time": trip["stops"][j - 1]["time"],
                            "trip_id": trip_id, "route": trip["route"],
                            "seg_from": seg_from, "seg_to": seg_to,
                            "window": {"start": closure["start"],
                                       "end": closure["end"],
                                       "label": closure["label"]},
                            "message": (
                                f"班次 {trip_id}（{trip['route']}）于 "
                                f"{to_hhmm(trip['stops'][j - 1]['time'])} 到达 "
                                f"{self.sname(seg_from)} 后，前方 "
                                f"{self.sname(seg_from)}→{self.sname(seg_to)} 站段"
                                f"被线路区段停运封锁（{detail}），车辆无法穿越；"
                                f"本次乘车最远可在 {self.sname(seg_from)} 下车，"
                                "之后需步行或换乘其他班次。"
                            ),
                        })
                        break
                    target = trip["stops"][j]["station"]
                    arrive_at = trip["stops"][j]["time"]
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
                        label["walk"], trip_id, j,
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
            kind_rank = {"origin_closed": 0, "station_closed": 1,
                         "segment_suspended": 2, "route_suspended": 3,
                         "no_service_after": 4}
            first = min(items, key=lambda b: (
                b.get("time", earliest), str(b.get("station", "")),
                str(b.get("trip_id", "")), kind_rank.get(b["kind"], 9)))
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

    def _truncation_note(self, leg: dict) -> str | None:
        """若乘车段恰因线路区段停运而在封锁段起点站终止，返回说明文字。"""
        trip = self.n["trips"].get(leg["trip_id"])
        if not trip:
            return None
        stops = trip["stops"]
        alight_idx = next(
            (k for k, st in enumerate(stops)
             if st["station"] == leg["to"] and st["time"] == leg["arrive"]),
            None)
        if alight_idx is None or alight_idx >= len(stops) - 1:
            return None
        clo = self.segment_closure(leg["trip_id"], alight_idx)
        if clo is None:
            return None
        return (
            f"受 {trip['route']} 区段停运（{self.sname(clo['from'])}→"
            f"{self.sname(clo['to'])} {to_hhmm(clo['start'])}–"
            f"{to_hhmm(clo['end'])}）影响，本次乘车在 "
            f"{self.sname(leg['to'])} 终止，需步行或换乘其他班次继续。")

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
            leg = dict(step)
            if leg["type"] == "ride":
                note = self._truncation_note(leg)
                if note:
                    leg["truncated"] = True
                    leg["truncation"] = note
            legs.append(leg)
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
