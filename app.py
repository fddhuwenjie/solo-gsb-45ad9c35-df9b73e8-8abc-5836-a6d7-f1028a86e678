"""公交时刻换乘规划台 —— Flask 后端。"""

from flask import Flask, jsonify, request, send_from_directory

from planner import (
    PREFERENCE_LABELS,
    PlanError,
    SAMPLE_DATA,
    Planner,
    normalize,
    parse_time,
)

app = Flask(__name__, static_folder="static", static_url_path="")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/meta")
def meta():
    return jsonify({"preferences": [
        {"id": pid, "label": label}
        for pid, label in PREFERENCE_LABELS.items()
    ]})


@app.get("/api/sample")
def sample():
    return jsonify(SAMPLE_DATA)


@app.post("/api/plan")
def plan():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error",
                        "message": "请求体必须是 JSON 对象"}), 400

    query = payload.get("query") or {}
    data = payload.get("data") or {}

    try:
        network = normalize(data)
        origin = str(query.get("origin", "")).strip()
        destination = str(query.get("destination", "")).strip()
        earliest = parse_time(query.get("earliest", "00:00"), "最早出发时刻")
        preference = str(query.get("preference", "fastest")).strip()
        result = Planner(network).plan(origin, destination, earliest,
                                       preference)
    except PlanError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
