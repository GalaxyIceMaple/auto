# app.py
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import sqlite3, json, csv, io, time, os
from queue import Queue
from threading import Lock
from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+

DB = "gantt.db"
TZ = ZoneInfo("Asia/Singapore")

app = Flask(__name__)
CORS(app)

listeners = set()
lst_lock = Lock()

# ---------- Utils ----------
def get_conn():
    return sqlite3.connect(DB)

def today_str():
    return datetime.now(TZ).date().isoformat()  # 'YYYY-MM-DD'

def replace_date_part(ts: str, to_date: str) -> str:
    """
    数据库存的是模板日期（如 2024-08-06 08:00），返回把“日期部分”替换成 to_date 的同一时刻。
    支持 'HH:MM' 或 'HH:MM:SS'。
    """
    ts = ts.strip()
    _t = ts.split(" ", 1)[1] if " " in ts else ts
    try:
        t_obj = datetime.strptime(_t.strip(), "%H:%M:%S").time()
        t_str = t_obj.strftime("%H:%M:%S")
    except ValueError:
        t_obj = datetime.strptime(_t.strip(), "%H:%M").time()
        t_str = t_obj.strftime("%H:%M")
    return f"{to_date} {t_str}"

def mr_truth(v: str) -> bool:
    return str(v).strip().lower() in ("是","true","1","y","yes")

# ---------- DB init ----------
def init_db():
    with get_conn() as con:
        cur = con.cursor()
        # 固定任务模板：起止时间存一份“基准日期”的时刻表，must_review 仅此处维护
        cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            start TEXT NOT NULL,     -- 'YYYY-MM-DD HH:MM'
            end   TEXT NOT NULL,
            predecessor TEXT,        -- JSON array string
            must_review TEXT NOT NULL DEFAULT '否'
        )""")
        # 每日状态：仅 completed / reviewed
        cur.execute("""
        CREATE TABLE IF NOT EXISTS task_state(
            date TEXT NOT NULL,      -- 'YYYY-MM-DD'
            task_id INTEGER NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0,
            reviewed  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(date, task_id),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )""")
        con.commit()

def seed_if_empty():
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM tasks")
        if cur.fetchone()[0] == 0:
            # 注意：这里的日期无所谓，都是模板，会在返回时投影为请求的日期
            sample = [
                (1,'T1','2024-08-06 08:00','2024-08-06 09:00', json.dumps([]), '否'),
                (2,'T2','2024-08-06 08:10','2024-08-06 08:45', json.dumps([]), '否'),
                (3,'T3','2024-08-06 08:15','2024-08-06 08:55', json.dumps([]), '是'),
                (4,'T4','2024-08-06 09:20','2024-08-06 09:50', json.dumps(["T1","T3","T2"]), '否'),
                (5,'T5','2024-08-06 10:00','2024-08-06 10:40', json.dumps(["T4"]), '是'),
            ]
            cur.executemany(
                "INSERT INTO tasks(id,name,start,end,predecessor,must_review) VALUES (?,?,?,?,?,?)",
                sample
            )
            # 给一个示例日的状态（随便哪天）
            cur.executemany("""
            INSERT OR REPLACE INTO task_state(date,task_id,completed,reviewed)
            VALUES (?,?,?,?)
            """, [
                ('2024-08-06',1,1,1),
                ('2024-08-06',2,1,0),
                ('2024-08-06',3,0,0),
                ('2024-08-06',4,1,1),
                ('2024-08-06',5,0,0),
            ])
            con.commit()

# ---------- SSE ----------
def broadcast(ev: dict):
    with lst_lock:
        dead = []
        for q in list(listeners):
            try:
                q.put_nowait(ev)
            except Exception:
                dead.append(q)
        for q in dead:
            listeners.discard(q)

@app.get("/api/state/stream")
def sse_stream():
    date = request.args.get("date") or today_str()
    q = Queue(maxsize=1000)
    with lst_lock:
        listeners.add(q)

    def gen():
        yield f": connected date={date}\n\n"
        try:
            while True:
                ev = q.get()
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except GeneratorExit:
            pass
        finally:
            with lst_lock:
                listeners.discard(q)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp

# ---------- APIs ----------
@app.get("/")
def index_page():
    return send_from_directory(".", "index.html")

@app.get("/api/tasks")
def api_tasks():
    # 带 date 参数；不传则用新加坡“今天”
    req_date = request.args.get("date") or today_str()
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT id,name,start,end,predecessor,must_review FROM tasks ORDER BY id")
        rows = cur.fetchall()

    out = []
    for id_, name, start, end, predecessor, mr in rows:
        start2 = replace_date_part(start, req_date)
        end2   = replace_date_part(end,   req_date)
        try:
            pred = json.loads(predecessor) if predecessor else []
        except Exception:
            pred = []
        out.append({
            "id": id_,
            "name": name,
            "start": start2,
            "end": end2,
            "predecessor": pred,
            "must_review": mr
        })
    return jsonify(out)

@app.get("/api/state")
def api_state_get():
    date = request.args.get("date") or today_str()
    with get_conn() as con:
        cur = con.cursor()
        # 只统计“模板中存在的任务”即可（你也可以 join tasks 过滤）
        cur.execute("""
        SELECT t.id, t.name, t.must_review,
               COALESCE(s.completed,0), COALESCE(s.reviewed,0)
        FROM tasks t
        LEFT JOIN task_state s
          ON s.task_id = t.id AND s.date = ?
        ORDER BY t.id
        """, (date,))
        rows = cur.fetchall()

    lst = [{"id":tid,"name":name,"mustReview":mr,
            "completed":bool(comp),"reviewed":bool(rev)} for tid,name,mr,comp,rev in rows]

    stats = {
        "total": len(lst),
        "doneReviewed": sum(1 for x in lst if x["completed"] and x["reviewed"]),
        "doneUnreviewed": sum(1 for x in lst if x["completed"] and not x["reviewed"]),
        "undone": sum(1 for x in lst if not x["completed"]),
        "mustReviewUndone": sum(1 for x in lst if (mr_truth(x["mustReview"]) and not x["completed"] and not x["reviewed"]))
    }
    return jsonify({"date": date, "list": lst, "stats": stats})

@app.post("/api/state")
def api_state_post():
    data = request.get_json(silent=True) or {}
    date = data.get("date") or today_str()
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"error":"need task_id"}), 400
    completed = 1 if data.get("completed") else 0
    reviewed  = 1 if data.get("reviewed")  else 0

    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,))
        if not cur.fetchone():
            return jsonify({"error":f"task {task_id} not exists"}), 404
        cur.execute("""
        INSERT OR REPLACE INTO task_state(date,task_id,completed,reviewed)
        VALUES (?,?,?,?)
        """, (date, task_id, completed, reviewed))
        con.commit()

    # 广播
    ev = {"date": date, "changes": [{
        "task_id": task_id, "completed": completed, "reviewed": reviewed
    }], "seq": int(time.time()*1000)}
    broadcast(ev)
    return jsonify({"ok":True})

#（可选）导出 CSV
@app.get("/api/export/fixed.csv")
def export_fixed():
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("SELECT id,name,start,end,predecessor,must_review FROM tasks ORDER BY id")
        rows = cur.fetchall()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["id","name","start","end","predecessor","must_review"])
    for r in rows: w.writerow(r)
    out = buf.getvalue()
    return Response(out, mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=fixed_tasks.csv"})

@app.get("/api/export/state.csv")
def export_state():
    date = request.args.get("date") or today_str()
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""
        SELECT task_id, completed, reviewed
        FROM task_state WHERE date=? ORDER BY task_id
        """, (date,))
        rows = cur.fetchall()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["date","task_id","completed","reviewed"])
    for r in rows: w.writerow([date, r[0], r[1], r[2]])
    out = buf.getvalue()
    return Response(out, mimetype="text/csv",
                    headers={"Content-Disposition":f"attachment; filename=state_{date}.csv"})

if __name__ == "__main__":
    init_db()
    seed_if_empty()
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=True, use_reloader=False)
