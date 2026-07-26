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

            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                content    TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            );
            """
        )
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


def do_delete_record(record_id):
    with _lock:
        row = db().execute("SELECT * FROM records WHERE id = ?",
                           (record_id,)).fetchone()
        if row is None:
            return None
        db().execute("DELETE FROM records WHERE id = ?", (record_id,))
        db().commit()
    return dict(row)


def do_add_note(content):
    content = (content or "").strip()
    if not content:
        raise ValueError("小纸条不能是空的")
    with _lock:
        cur = db().execute(
            "INSERT INTO notes (content, created_at) VALUES (?,?)",
            (content, now_iso()),
        )
        db().commit()
        nid = cur.lastrowid
    return {"id": nid, "content": content}


def do_list_notes(limit=30):
    with _lock:
        return rows_to_list(db().execute(
            "SELECT * FROM notes ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall())


def do_delete_note(note_id):
    with _lock:
        row = db().execute("SELECT * FROM notes WHERE id = ?",
                           (note_id,)).fetchone()
        if row is None:
            return None
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
    return (f"已记录 #{r['id']}：{r['date']} {word} {r['amount']} 元"
            f"（{r['category']}）{r['note']}")


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
def delete_record(record_id: int) -> str:
    """按 id 删掉一条记录。删之前最好先用 query_records 确认 id。"""
    row = do_delete_record(record_id)
    if row is None:
        return f"没有 id 为 {record_id} 的记录。"
    return f"已删除 #{row['id']}：{row['date']} {row['amount']} 元（{row['category']}）"


@mcp.tool()
def add_note(content: str) -> str:
    """写一张小纸条，存在账本里。跟金额无关的任何东西都可以写。"""
    n = do_add_note(content)
    return f"小纸条 #{n['id']} 已写下。"


@mcp.tool()
def list_notes(limit: int = 30) -> str:
    """看最近的小纸条。"""
    notes = do_list_notes(limit)
    if not notes:
        return "还没有小纸条。"
    return json.dumps(notes, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- REST 接口
# 给网页前端用。路径同样带令牌。

P = f"/{TOKEN}/api"


def ok(data, status=200):
    return JSONResponse(data, status_code=status)


@mcp.custom_route(f"{P}/records", methods=["GET"])
async def api_list_records(request):
    q = request.query_params
    return ok(do_query_records(
        q.get("start"), q.get("end"), q.get("category"),
        q.get("keyword"), q.get("kind"), int(q.get("limit", 500))))


@mcp.custom_route(f"{P}/records", methods=["POST"])
async def api_add_record(request):
    try:
        b = await request.json()
        return ok(do_add_record(
            b.get("amount"), b.get("category", "其他"), b.get("note", ""),
            b.get("date"), b.get("kind", "expense")))
    except Exception as e:
        return ok({"error": str(e)}, 400)


@mcp.custom_route(f"{P}/records/{{rid:int}}", methods=["DELETE"])
async def api_delete_record(request):
    row = do_delete_record(request.path_params["rid"])
    if row is None:
        return ok({"error": "not found"}, 404)
    return ok({"deleted": row})


@mcp.custom_route(f"{P}/notes", methods=["GET"])
async def api_list_notes(request):
    return ok({"notes": do_list_notes(int(request.query_params.get("limit", 100)))})


@mcp.custom_route(f"{P}/notes", methods=["POST"])
async def api_add_note(request):
    try:
        b = await request.json()
        return ok(do_add_note(b.get("content")))
    except Exception as e:
        return ok({"error": str(e)}, 400)


@mcp.custom_route(f"{P}/notes/{{nid:int}}", methods=["DELETE"])
async def api_delete_note(request):
    row = do_delete_note(request.path_params["nid"])
    if row is None:
        return ok({"error": "not found"}, 404)
    return ok({"deleted": row})


@mcp.custom_route(f"{P}/export", methods=["GET"])
async def api_export(request):
    """把整个账本导出成 JSON，随时可以备份或搬走。"""
    with _lock:
        data = {
            "exported_at": now_iso(),
            "records": rows_to_list(db().execute(
                "SELECT * FROM records ORDER BY id").fetchall()),
            "notes": rows_to_list(db().execute(
                "SELECT * FROM notes ORDER BY id").fetchall()),
        }
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="ledger-{today()}.json"'},
    )


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
