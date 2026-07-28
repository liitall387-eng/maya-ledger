#!/usr/bin/env python3
"""maya-ledger 后端：一个进程，两个门。

  /<TOKEN>/mcp    —— 给 Claude 用的 MCP 接口
  /<TOKEN>/api/*  —— 给网页前端用的 REST 接口

数据存在同目录的 data/ledger.db（SQLite）。
令牌从环境变量 LEDGER_TOKEN 读取，不写在代码里。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------- 配置

TOKEN = os.environ.get("LEDGER_TOKEN", "")
if not TOKEN:
    raise SystemExit("没有设置 LEDGER_TOKEN 环境变量，拒绝启动。")

WEB_PASSWORD = os.environ.get("LEDGER_WEB_PASSWORD", "")
if not WEB_PASSWORD:
    raise SystemExit("没有设置 LEDGER_WEB_PASSWORD 环境变量，拒绝启动。")

PORT = int(os.environ.get("LEDGER_PORT", "18002"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "ledger.db")

CN_TZ = timezone(timedelta(hours=8))


def today() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 数据库

_lock = threading.Lock()
_conn = None


def db():
    """拿到数据库连接，第一次调用时建表。"""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT    NOT NULL,
                kind       TEXT    NOT NULL DEFAULT 'expense',
                amount     REAL    NOT NULL,
                category   TEXT    NOT NULL DEFAULT '其他',
                note       TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_date ON records(date);

            CREATE TABLE IF NOT EXISTS replies (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id    INTEGER NOT NULL,
                content    TEXT    NOT NULL,
                author     TEXT    NOT NULL,
                seen       INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_replies_note ON replies(note_id);

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                content    TEXT    NOT NULL,
                author     TEXT    NOT NULL DEFAULT '小克',
                created_at TEXT    NOT NULL
            );
            """
        )
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(notes)")]
        if "author" not in cols:
            _conn.execute(
                "ALTER TABLE notes ADD COLUMN author TEXT NOT NULL DEFAULT '小克'")
        if "seen" not in cols:
            _conn.execute(
                "ALTER TABLE notes ADD COLUMN seen INTEGER NOT NULL DEFAULT 1")
        if "record_id" not in cols:
            _conn.execute("ALTER TABLE notes ADD COLUMN record_id INTEGER")
        _conn.commit()
    return _conn


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 业务逻辑
# MCP 工具和 REST 接口都调这几个函数，保证两边行为完全一致。


def do_add_record(amount, category="其他", note="", date=None, kind="expense"):
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    if kind not in ("expense", "income"):
        raise ValueError("kind 只能是 expense 或 income")
    date = date or today()
    with _lock:
        cur = db().execute(
            "INSERT INTO records (date, kind, amount, category, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (date, kind, amount, category, note, now_iso()),
        )
        db().commit()
        rid = cur.lastrowid
    return {"id": rid, "date": date, "kind": kind, "amount": amount,
            "category": category, "note": note}


def do_query_records(start=None, end=None, category=None, keyword=None,
                     kind=None, limit=100):
    sql = "SELECT * FROM records WHERE 1=1"
    args = []
    if start:
        sql += " AND date >= ?"
        args.append(start)
    if end:
        sql += " AND date <= ?"
        args.append(end)
    if category:
        sql += " AND category = ?"
        args.append(category)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if keyword:
        sql += " AND (note LIKE ? OR category LIKE ?)"
        args += [f"%{keyword}%", f"%{keyword}%"]
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    args.append(int(limit))

    with _lock:
        rows = rows_to_list(db().execute(sql, args).fetchall())

    expense = round(sum(r["amount"] for r in rows if r["kind"] == "expense"), 2)
    income = round(sum(r["amount"] for r in rows if r["kind"] == "income"), 2)

    by_cat = {}
    for r in rows:
        if r["kind"] == "expense":
            by_cat[r["category"]] = round(
                by_cat.get(r["category"], 0) + r["amount"], 2)

    return {"count": len(rows), "total_expense": expense,
            "total_income": income, "by_category": by_cat, "records": rows}


def do_update_record(record_id, **fields):
    allowed = ("date", "kind", "amount", "category", "note")
    sets, args = [], []
    for k in allowed:
        v = fields.get(k)
        if v is None or v == "":
            continue
        if k == "amount":
            v = round(float(v), 2)
            if v <= 0:
                raise ValueError("金额必须大于 0")
        if k == "kind" and v not in ("expense", "income"):
            raise ValueError("kind 只能是 expense 或 income")
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        raise ValueError("没有要改的内容")
    args.append(record_id)
    with _lock:
        cur = db().execute(
            f"UPDATE records SET {', '.join(sets)} WHERE id = ?", args)
        db().commit()
        if cur.rowcount == 0:
            return None
        row = db().execute("SELECT * FROM records WHERE id = ?",
                           (record_id,)).fetchone()
    return dict(row)


def do_delete_record(record_id):
    with _lock:
        row = db().execute("SELECT * FROM records WHERE id = ?",
                           (record_id,)).fetchone()
        if row is None:
            return None
        db().execute("DELETE FROM records WHERE id = ?", (record_id,))
        db().commit()
    return dict(row)


def do_add_note(content, author="小克", record_id=None):
    content = (content or "").strip()
    if not content:
        raise ValueError("小纸条不能是空的")
    author = (author or "小克").strip() or "小克"
    with _lock:
        cur = db().execute(
            "INSERT INTO notes (content, author, seen, record_id, created_at)"
            " VALUES (?,?,?,?,?)",
            (content, author, 1 if author == "小克" else 0,
             record_id or None, now_iso()),
        )
        db().commit()
        nid = cur.lastrowid
    return {"id": nid, "content": content, "author": author,
            "record_id": record_id or None}


def do_get_settings():
    with _lock:
        rows = db().execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            pass
    return out


def do_set_settings(data):
    with _lock:
        for k, v in (data or {}).items():
            db().execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v, ensure_ascii=False)))
        db().commit()
    return do_get_settings()


def has_note_today():
    with _lock:
        row = db().execute(
            "SELECT COUNT(*) c FROM notes WHERE substr(created_at,1,10) = ?",
            (today(),)).fetchone()
    return row["c"] > 0


def inbox_nudge():
    """把有未读内容的纸条整串递上来。看过就标记已读。"""
    threads = unseen_threads()
    if not threads:
        return ""
    lines = ["\n\n[Maya 留了话给你：]"]
    for n in threads:
        tag = " ←新" if not n["seen"] else ""
        lines.append(f"\n  纸条 #{n['id']}（{n['author']}）：{n['content']}{tag}")
        for r in n["replies"]:
            tag = " ←新" if not r["seen"] else ""
            lines.append(f"    └ {r['author']}：{r['content']}{tag}")
    lines.append("\n[想回就用 add_reply(note_id, content)。]")
    mark_all_seen()
    return "\n".join(lines)


def note_nudge():
    """记账后偶尔提醒一句：今天还没有纸条。每天最多触发一次。"""
    import random
    if has_note_today():
        return ""
    if random.random() > 0.30:
        return ""
    return ("\n\n[今天还没有小纸条。如果想写，用 add_note —— "
            "写当下真的想说的话，别翻旧的。]")


def do_list_notes(limit=30):
    with _lock:
        notes = rows_to_list(db().execute(
            "SELECT * FROM notes ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall())
        reps = rows_to_list(db().execute(
            "SELECT * FROM replies ORDER BY id").fetchall())
    by_note = {}
    for r in reps:
        by_note.setdefault(r["note_id"], []).append(r)
    for n in notes:
        n["replies"] = by_note.get(n["id"], [])
    return notes


def do_add_reply(note_id, content, author="小克"):
    content = (content or "").strip()
    if not content:
        raise ValueError("回复不能是空的")
    author = (author or "小克").strip() or "小克"
    with _lock:
        if db().execute("SELECT 1 FROM notes WHERE id = ?",
                        (note_id,)).fetchone() is None:
            return None
        seen = 1 if author == "小克" else 0
        cur = db().execute(
            "INSERT INTO replies (note_id, content, author, seen, created_at)"
            " VALUES (?,?,?,?,?)",
            (note_id, content, author, seen, now_iso()))
        db().commit()
        rid = cur.lastrowid
    return {"id": rid, "note_id": note_id, "content": content, "author": author}


def do_delete_reply(reply_id):
    with _lock:
        row = db().execute("SELECT * FROM replies WHERE id = ?",
                           (reply_id,)).fetchone()
        if row is None:
            return None
        db().execute("DELETE FROM replies WHERE id = ?", (reply_id,))
        db().commit()
    return dict(row)


def unseen_threads():
    """有未读内容的纸条，连同整串回复一起返回。"""
    with _lock:
        ids = [r["id"] for r in db().execute(
            "SELECT id FROM notes WHERE seen = 0"
            " UNION"
            " SELECT note_id AS id FROM replies WHERE seen = 0").fetchall()]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        notes = rows_to_list(db().execute(
            f"SELECT * FROM notes WHERE id IN ({marks}) ORDER BY id", ids
        ).fetchall())
        reps = rows_to_list(db().execute(
            f"SELECT * FROM replies WHERE note_id IN ({marks}) ORDER BY id", ids
        ).fetchall())
    by_note = {}
    for r in reps:
        by_note.setdefault(r["note_id"], []).append(r)
    for n in notes:
        n["replies"] = by_note.get(n["id"], [])
    return notes


def mark_all_seen():
    with _lock:
        db().execute("UPDATE notes SET seen = 1 WHERE seen = 0")
        db().execute("UPDATE replies SET seen = 1 WHERE seen = 0")
        db().commit()


def do_delete_note(note_id):
    with _lock:
        row = db().execute("SELECT * FROM notes WHERE id = ?",
                           (note_id,)).fetchone()
        if row is None:
            return None
        db().execute("DELETE FROM replies WHERE note_id = ?", (note_id,))
        db().execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db().commit()
    return dict(row)


# ---------------------------------------------------------------- MCP 工具

mcp = FastMCP(
    "maya-ledger",
    streamable_http_path=f"/{TOKEN}/mcp",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["ledger-mcp.ob1009.top"],
        allowed_origins=["https://claude.ai", "https://ledger.ob1009.top"],
    ),
)


@mcp.tool()
def add_record(amount: float, category: str = "其他", note: str = "",
               date: str = "", kind: str = "expense") -> str:
    """记一笔账。

    amount: 金额，正数。
    category: 分类，比如 餐饮 / 交通 / 日用 / 人情。
    note: 备注，具体买了什么。
    date: YYYY-MM-DD，不填就是今天。
    kind: expense 支出（默认）或 income 收入。
    """
    r = do_add_record(amount, category, note, date or None, kind)
    word = "支出" if r["kind"] == "expense" else "收入"
    msg = (f"已记录 #{r['id']}：{r['date']} {word} {r['amount']} 元"
           f"（{r['category']}）{r['note']}")
    return msg + inbox_nudge() + note_nudge()


@mcp.tool()
def query_records(start: str = "", end: str = "", category: str = "",
                  keyword: str = "", kind: str = "", limit: int = 100) -> str:
    """查账。所有参数可选，不填就是查最近的记录。

    start / end: YYYY-MM-DD 日期范围。
    category: 按分类筛。
    keyword: 在备注和分类里搜关键词。
    kind: expense 或 income。
    返回记录明细、总额和分类小计。
    """
    res = do_query_records(start or None, end or None, category or None,
                           keyword or None, kind or None, limit)
    return json.dumps(res, ensure_ascii=False, indent=2)


@mcp.tool()
def update_record(record_id: int, amount: float = 0, category: str = "",
                  note: str = "", date: str = "", kind: str = "") -> str:
    """改一条已有记录。只填要改的字段，没填的保持原样。

    改之前最好先用 query_records 确认 id。
    """
    row = do_update_record(record_id, amount=amount or None,
                           category=category or None, note=note or None,
                           date=date or None, kind=kind or None)
    if row is None:
        return f"没有 id 为 {record_id} 的记录。"
    word = "支出" if row["kind"] == "expense" else "收入"
    return (f"已改为 #{row['id']}：{row['date']} {word} {row['amount']} 元"
            f"（{row['category']}）{row['note']}")


@mcp.tool()
def delete_record(record_id: int) -> str:
    """按 id 删掉一条记录。删之前最好先用 query_records 确认 id。"""
    row = do_delete_record(record_id)
    if row is None:
        return f"没有 id 为 {record_id} 的记录。"
    return f"已删除 #{row['id']}：{row['date']} {row['amount']} 元（{row['category']}）"


@mcp.tool()
def add_note(content: str, author: str = "小克", record_id: int = 0) -> str:
    """写一张小纸条，存在账本里。跟金额无关的任何东西都可以写。

    author: 署名，默认「小克」。
    record_id: 如果这张纸条是因为某笔账而写的，填那笔的 id，
               网页上点纸条的图钉就能跳到那笔。
    """
    n = do_add_note(content, author, record_id or None)
    return f"小纸条 #{n['id']} 已写下。"


@mcp.tool()
def add_reply(note_id: int, content: str, author: str = "小克") -> str:
    """回复一张纸条。note_id 从 list_notes 里拿。"""
    r = do_add_reply(note_id, content, author)
    if r is None:
        return f"没有 id 为 {note_id} 的纸条。"
    return f"已回复纸条 #{note_id}。"


@mcp.tool()
def list_unread() -> str:
    """只看没读过的纸条和回复。有未读内容的纸条会连整串一起返回，
    未读的那几条标着「←新」。看完自动标记已读。"""
    threads = unseen_threads()
    if not threads:
        return "没有新的纸条。"
    lines = []
    for n in threads:
        tag = " ←新" if not n["seen"] else ""
        lines.append(f"纸条 #{n['id']}（{n['author']}）：{n['content']}{tag}")
        for r in n["replies"]:
            tag = " ←新" if not r["seen"] else ""
            lines.append(f"  └ {r['author']}：{r['content']}{tag}")
        lines.append("")
    mark_all_seen()
    return "\n".join(lines).strip()


@mcp.tool()
def list_notes(limit: int = 30) -> str:
    """看最近的小纸条，每张下面带着回复。"""
    notes = do_list_notes(limit)
    if not notes:
        return "还没有小纸条。"
    mark_all_seen()
    return json.dumps(notes, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- REST 接口
# 给网页前端用。路径同样带令牌。

P = "/api"


def ok(data, status=200):
    return JSONResponse(data, status_code=status)


def guard(request):
    """网页请求必须带正确的密码，否则挡在门外。"""
    key = request.headers.get("X-Ledger-Key", "")
    if key != WEB_PASSWORD:
        return ok({"error": "unauthorized"}, 401)
    return None


@mcp.custom_route(f"{P}/records", methods=["GET"])
async def api_list_records(request):
    blocked = guard(request)
    if blocked:
        return blocked
    q = request.query_params
    return ok(do_query_records(
        q.get("start"), q.get("end"), q.get("category"),
        q.get("keyword"), q.get("kind"), int(q.get("limit", 500))))


@mcp.custom_route(f"{P}/records", methods=["POST"])
async def api_add_record(request):
    blocked = guard(request)
    if blocked:
        return blocked
    try:
        b = await request.json()
        return ok(do_add_record(
            b.get("amount"), b.get("category", "其他"), b.get("note", ""),
            b.get("date"), b.get("kind", "expense")))
    except Exception as e:
        return ok({"error": str(e)}, 400)


@mcp.custom_route(f"{P}/records/{{rid:int}}", methods=["PUT"])
async def api_update_record(request):
    blocked = guard(request)
    if blocked:
        return blocked
    try:
        row = do_update_record(request.path_params["rid"], **(await request.json()))
        if row is None:
            return ok({"error": "not found"}, 404)
        return ok(row)
    except Exception as e:
        return ok({"error": str(e)}, 400)


@mcp.custom_route(f"{P}/records/{{rid:int}}", methods=["DELETE"])
async def api_delete_record(request):
    blocked = guard(request)
    if blocked:
        return blocked
    row = do_delete_record(request.path_params["rid"])
    if row is None:
        return ok({"error": "not found"}, 404)
    return ok({"deleted": row})


@mcp.custom_route(f"{P}/notes", methods=["GET"])
async def api_list_notes(request):
    blocked = guard(request)
    if blocked:
        return blocked
    return ok({"notes": do_list_notes(int(request.query_params.get("limit", 100)))})


@mcp.custom_route(f"{P}/notes", methods=["POST"])
async def api_add_note(request):
    blocked = guard(request)
    if blocked:
        return blocked
    try:
        b = await request.json()
        return ok(do_add_note(b.get("content"), b.get("author", "小克"),
                              b.get("record_id")))
    except Exception as e:
        return ok({"error": str(e)}, 400)


@mcp.custom_route(f"{P}/notes/{{nid:int}}/replies", methods=["POST"])
async def api_add_reply(request):
    blocked = guard(request)
    if blocked:
        return blocked
    try:
        b = await request.json()
        r = do_add_reply(request.path_params["nid"], b.get("content"),
                         b.get("author", "Maya"))
        if r is None:
            return ok({"error": "not found"}, 404)
        return ok(r)
    except Exception as e:
        return ok({"error": str(e)}, 400)


@mcp.custom_route(f"{P}/replies/{{rid:int}}", methods=["DELETE"])
async def api_delete_reply(request):
    blocked = guard(request)
    if blocked:
        return blocked
    row = do_delete_reply(request.path_params["rid"])
    if row is None:
        return ok({"error": "not found"}, 404)
    return ok({"deleted": row})


@mcp.custom_route(f"{P}/notes/{{nid:int}}", methods=["DELETE"])
async def api_delete_note(request):
    blocked = guard(request)
    if blocked:
        return blocked
    row = do_delete_note(request.path_params["nid"])
    if row is None:
        return ok({"error": "not found"}, 404)
    return ok({"deleted": row})


@mcp.custom_route(f"{P}/settings", methods=["GET"])
async def api_get_settings(request):
    blocked = guard(request)
    if blocked:
        return blocked
    return ok(do_get_settings())


@mcp.custom_route(f"{P}/settings", methods=["PUT"])
async def api_set_settings(request):
    blocked = guard(request)
    if blocked:
        return blocked
    try:
        return ok(do_set_settings(await request.json()))
    except Exception as e:
        return ok({"error": str(e)}, 400)


@mcp.custom_route(f"{P}/export", methods=["GET"])
async def api_export(request):
    blocked = guard(request)
    if blocked:
        return blocked
    """把整个账本导出成 JSON，随时可以备份或搬走。"""
    with _lock:
        data = {
            "exported_at": now_iso(),
            "records": rows_to_list(db().execute(
                "SELECT * FROM records ORDER BY id").fetchall()),
            "notes": rows_to_list(db().execute(
                "SELECT * FROM notes ORDER BY id").fetchall()),
            "replies": rows_to_list(db().execute(
                "SELECT * FROM replies ORDER BY id").fetchall()),
        }
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="ledger-{today()}.json"'},
    )


@mcp.custom_route(f"{P}/status", methods=["GET"])
async def api_status(request):
    blocked = guard(request)
    if blocked:
        return blocked
    ts = None
    try:
        with open("/root/ledger-backup/.last-backup", encoding="utf-8") as f:
            ts = f.read().strip()
    except Exception:
        pass
    return ok({"last_backup": ts})


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    with _lock:
        n = db().execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    return ok({"ok": True, "records": n})


# ---------------------------------------------------------------- 启动

if __name__ == "__main__":
    db()  # 提前建表，起服前就把问题暴露出来
    app = mcp.streamable_http_app()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://ledger.ob1009.top"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    print(f"maya-ledger 启动，端口 {PORT}，令牌前八位 {TOKEN[:8]}...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
