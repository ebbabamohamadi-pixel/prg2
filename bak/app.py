# ================================================================
#  داشبورد مدیریت پروژه — بک‌اند Flask + SQLite3
#  شرکت عمران و توسعه شرق رضوی
# ================================================================

import os
import re
import time
import uuid
import sqlite3
import secrets
import functools
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, request, jsonify, send_file, make_response, g
from flask_cors import CORS
import jwt as pyjwt


# ===== تنظیمات =====
PORT = int(os.getenv("PORT", 3000))
JWT_SECRET = os.getenv("JWT_SECRET", "SharghRezvi2024SecretKey")
JWT_EXPIRES_H = int(os.getenv("JWT_EXPIRES_H", 8))
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_FILE = DATA_DIR / "projects.db"
UPLOAD_DIR = DATA_DIR / "uploads"          # ✅ جدید: محل ذخیره مستندات
MAX_UPLOAD = 25 * 1024 * 1024              # حداکثر حجم هر فایل: ۲۵ مگابایت
INDEX_FILE = Path(__file__).resolve().parent / "index.html"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# فرمت‌های مجاز مستندات
ALLOWED_DOCS = {
    "pdf", "jpg", "jpeg", "png", "gif", "webp", "bmp",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv",
    "zip", "rar", "7z", "dwg", "dxf",
}

# ===== کاربران =====
USERS = [
    {"username": "admin", "password": "admin", "fullName": "ابراهیم بابامحمدی", "role": "admin"},
    {"username": "user",  "password": "user",  "fullName": "کاربر عادی",       "role": "user"},
]


# ================================================================
#  کمکی‌ها
# ================================================================
def safe_num(v, default=0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def now_iso():
    return datetime.now().isoformat(timespec="seconds") + "Z"


def gen_id():
    return int(time.time() * 1000) + (uuid.uuid4().int % 10000)


def clean_str(v, max_len=300):
    if v is None:
        return ""
    return str(v).strip()[:max_len]


def norm_date(v):
    """نرمال‌سازی تاریخ به YYYY-MM-DD — خروجی '' برای مقدار نامعتبر"""
    if not v:
        return ""
    s = str(v).strip()[:10]
    try:
        datetime.fromisoformat(s)
        return s
    except ValueError:
        return ""


def safe_filename(name):
    """پاک‌سازی نام فایل — حفظ حروف فارسی، حذف کاراکترهای خطرناک"""
    name = os.path.basename(name or "file")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    if len(name) > 180:
        root, ext = os.path.splitext(name)
        name = root[:170] + ext
    return name or "file"


def validate_project(d):
    errs = []
    if not isinstance(d, dict):
        return ["بدنه نامعتبر"]
    if len(clean_str(d.get("name"))) < 1:
        errs.append("نام پروژه الزامی است")
    if d.get("status") and d["status"] not in ("active", "pending", "hold"):
        errs.append("وضعیت نامعتبر")

    sd, ed = norm_date(d.get("start_date")), norm_date(d.get("end_date"))
    cs, ce = norm_date(d.get("contract_start_date")), norm_date(d.get("contract_end_date"))

    if sd and ed and sd > ed:
        errs.append("تاریخ شروع پروژه بعد از پایان آن است")
    if cs and ce and cs > ce:
        errs.append("تاریخ شروع قرارداد بعد از پایان قرارداد است")

    if safe_num(d.get("total_cost", 0)) < 0:
        errs.append("بودجه نمی‌تواند منفی باشد")
    if safe_num(d.get("spent_cost", 0)) < 0:
        errs.append("هزینه نمی‌تواند منفی باشد")
    pp = safe_num(d.get("progress_percent", 0))
    if pp < 0 or pp > 100:
        errs.append("پیشرفت باید بین ۰ تا ۱۰۰ باشد")
    if safe_num(d.get("weight", 0)) < 0:
        errs.append("وزن نمی‌تواند منفی باشد")
    return errs


def validate_task(d):
    errs = []
    if not isinstance(d, dict):
        return ["داده فعالیت نامعتبر"]
    if not clean_str(d.get("name")):
        errs.append("عنوان فعالیت الزامی است")
    return errs


# ================================================================
#  ساخت Flask app — قبل از هر چیز
# ================================================================
app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024   # کل درخواست حداکثر ۳۰ مگابایت


# ================================================================
#  دیتابیس SQLite3
# ================================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_FILE))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _add_column_if_missing(con, table, col_def):
    """مهاجرت: افزودن ستون به جدول موجود (بدون از دست رفتن داده‌ها)"""
    col_name = col_def.split()[0]
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if col_name not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        print(f"✔ ستون '{col_name}' به جدول '{table}' اضافه شد (مهاجرت)")


def init_db():
    con = sqlite3.connect(str(DB_FILE))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id                   INTEGER PRIMARY KEY,
            name                 TEXT    NOT NULL,
            manager              TEXT    NOT NULL DEFAULT '',
            status               TEXT    NOT NULL DEFAULT 'pending',
            start_date           TEXT    NOT NULL DEFAULT '',
            end_date             TEXT    NOT NULL DEFAULT '',
            project_code         TEXT    NOT NULL DEFAULT '',
            contract_number      TEXT    NOT NULL DEFAULT '',
            weight               REAL    NOT NULL DEFAULT 0,
            contract_start_date  TEXT    NOT NULL DEFAULT '',
            contract_end_date    TEXT    NOT NULL DEFAULT '',
            total_cost           REAL    NOT NULL DEFAULT 0,
            spent_cost           REAL    NOT NULL DEFAULT 0,
            progress_percent     REAL    NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL DEFAULT '',
            updated_at           TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id            INTEGER PRIMARY KEY,
            project_id    INTEGER NOT NULL,
            name          TEXT    NOT NULL,
            owner         TEXT    NOT NULL DEFAULT '',
            budget_needed REAL    NOT NULL DEFAULT 0,
            spent_cost    REAL    NOT NULL DEFAULT 0,
            start_date    TEXT    NOT NULL DEFAULT '',
            end_date      TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS progress_records (
            id                 INTEGER PRIMARY KEY,
            project_id         INTEGER NOT NULL,
            from_date          TEXT    NOT NULL DEFAULT '',
            to_date            TEXT    NOT NULL DEFAULT '',
            subject            TEXT    NOT NULL DEFAULT '',
            physical_progress  REAL    NOT NULL DEFAULT 0,
            financial_progress REAL    NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS project_notes (
            id          INTEGER PRIMARY KEY,
            project_id  INTEGER NOT NULL,
            date        TEXT    NOT NULL DEFAULT '',
            description TEXT    NOT NULL DEFAULT ''
        );
        /* ✅ جدید: جدول مستندات */
        CREATE TABLE IF NOT EXISTS project_documents (
            id          INTEGER PRIMARY KEY,
            project_id  INTEGER NOT NULL,
            orig_name   TEXT    NOT NULL,
            stored_name TEXT    NOT NULL,
            file_size   INTEGER NOT NULL DEFAULT 0,
            mime_type   TEXT    NOT NULL DEFAULT '',
            uploaded_at TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
        CREATE INDEX IF NOT EXISTS idx_progress_project ON progress_records(project_id);
        CREATE INDEX IF NOT EXISTS idx_notes_project ON project_notes(project_id);
        CREATE INDEX IF NOT EXISTS idx_docs_project ON project_documents(project_id);
    """)
    con.commit()

    # --- مهاجرت برای دیتابیس‌های قدیمی ---
    _add_column_if_missing(con, "projects", "project_code TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(con, "projects", "contract_number TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(con, "projects", "weight REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "projects", "contract_start_date TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(con, "projects", "contract_end_date TEXT NOT NULL DEFAULT ''")
    con.commit()

    count = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    if count == 0:
        _seed_db(con)
    con.close()


def _seed_db(con):
    projects_data = [
        (1001, "ساخت مجتمع تجاری صالحی", "واحد عمران", "active",
         "2024-03-21", "2025-09-22", "PRJ-1402-101", "CT-1024", 100,
         "2024-03-15", "2025-09-01", 85000, 32000, 38,
         "2024-01-15T08:00:00Z", "2025-01-10T14:30:00Z"),
        (1002, "راه‌آهن سرخس-مشهد", "واحد ریلی", "active",
         "2023-08-23", "2027-03-20", "PRJ-1402-014", "CT-0871", 250,
         "2023-08-10", "2027-02-20", 220000, 45000, 20,
         "2023-06-01T10:00:00Z", "2025-01-08T09:15:00Z"),
        (1003, "بهسازی شبکه آب شهری", "واحد زیربنایی", "active",
         "2024-06-01", "2025-06-01", "PRJ-1403-030", "CT-1150", 80,
         "2024-05-25", "2025-05-20", 35000, 18000, 52,
         "2024-05-10T07:30:00Z", "2025-01-12T11:00:00Z"),
        (1004, "ساخت پارکینگ طبقاتی", "واحد عمران", "pending",
         "2025-01-01", "2026-06-30", "PRJ-1403-044", "CT-1201", 60,
         "2024-12-20", "2026-06-15", 42000, 0, 0,
         "2024-11-20T16:00:00Z", "2024-11-20T16:00:00Z"),
        (1005, "احداث پل عابر پیاده ولیعصر", "واحد زیربنایی", "hold",
         "2024-04-01", "2025-01-01", "PRJ-1403-009", "CT-0980", 40,
         "2024-03-25", "2024-12-25", 12000, 4000, 33,
         "2024-03-15T09:00:00Z", "2024-12-01T08:45:00Z"),
        (1006, "نصب چراغ‌های هوشمند شهری", "واحد برق", "active",
         "2024-09-01", "2025-03-20", "PRJ-1403-052", "CT-1215", 25,
         "2024-08-20", "2025-03-10", 9500, 5200, 55,
         "2024-08-25T13:20:00Z", "2025-01-05T10:30:00Z"),
    ]
    tasks_data = [
        (101, 1001, "گودبرداری", "علی محمدی", 5000, 5000, "2024-03-21", "2024-05-21"),
        (102, 1001, "فونداسیون", "رضا احمدی", 15000, 12000, "2024-05-22", "2024-10-21"),
        (103, 1001, "ساخت اسکلت", "حسین نوری", 30000, 10000, "2024-10-22", "2025-06-21"),
        (201, 1002, "مطالعات توجیهی", "مهدی کاظمی", 8000, 8000, "2023-08-23", "2024-02-20"),
        (202, 1002, "خرید اراضی", "سعید رضوی", 40000, 25000, "2024-02-21", "2025-02-20"),
        (301, 1003, "کاشت لوله اصلی", "محمد حسینی", 15000, 12000, "2024-06-01", "2024-12-01"),
        (501, 1005, "طراحی", "زهرا میرزایی", 2000, 2000, "2024-04-01", "2024-06-01"),
    ]
    progress_data = [
        (9001, 1001, "2024-03-21", "2024-09-22", "گودبرداری و فونداسیون", 25, 20),
        (9002, 1001, "2024-09-23", "2025-01-10", "اسکلت‌سازی طبقات", 13, 12),
        (9003, 1002, "2023-08-23", "2024-08-22", "مطالعات و خرید اراضی", 20, 18),
        (9004, 1003, "2024-06-01", "2024-12-01", "کاشت لوله اصلی", 52, 51),
    ]
    notes_data = [
        (9501, 1001, "2024-03-21", "شروع عملیات اجرایی پروژه"),
        (9502, 1001, "2024-12-15", "تأخیر در تأمین میلگرد به دلیل شرایط جوی"),
        (9503, 1002, "2024-02-20", "پایان فاز مطالعات توجیهی"),
        (9504, 1005, "2024-12-01", "توقف پروژه در انتظار تخصیص اعتبار"),
    ]
    con.executemany(
        "INSERT OR IGNORE INTO projects "
        "(id,name,manager,status,start_date,end_date,project_code,contract_number,weight,"
        "contract_start_date,contract_end_date,total_cost,spent_cost,progress_percent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        projects_data,
    )
    con.executemany(
        "INSERT OR IGNORE INTO tasks "
        "(id,project_id,name,owner,budget_needed,spent_cost,start_date,end_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        tasks_data,
    )
    con.executemany(
        "INSERT OR IGNORE INTO progress_records "
        "(id,project_id,from_date,to_date,subject,physical_progress,financial_progress) "
        "VALUES (?,?,?,?,?,?,?)",
        progress_data,
    )
    con.executemany(
        "INSERT OR IGNORE INTO project_notes "
        "(id,project_id,date,description) VALUES (?,?,?,?)",
        notes_data,
    )
    con.commit()


# ================================================================
#  تبدیل Row → dict
# ================================================================
def task_to_dict(row):
    return {
        "id": row["id"], "name": row["name"], "owner": row["owner"],
        "budget_needed": row["budget_needed"], "spent_cost": row["spent_cost"],
        "start_date": row["start_date"], "end_date": row["end_date"],
    }


def progress_to_dict(row):
    return {
        "id": row["id"], "from_date": row["from_date"], "to_date": row["to_date"],
        "subject": row["subject"],
        "physical_progress": row["physical_progress"],
        "financial_progress": row["financial_progress"],
    }


def note_to_dict(row):
    return {"id": row["id"], "date": row["date"], "description": row["description"]}


def doc_to_dict(row):
    return {
        "id": row["id"],
        "orig_name": row["orig_name"],
        "file_size": row["file_size"],
        "mime_type": row["mime_type"],
        "uploaded_at": row["uploaded_at"],
    }


def project_to_dict(row, tasks=None, progress=None, notes=None, docs=None):
    return {
        "id": row["id"], "name": row["name"], "manager": row["manager"],
        "status": row["status"],
        "start_date": row["start_date"], "end_date": row["end_date"],
        "project_code": row["project_code"],
        "contract_number": row["contract_number"],
        "weight": row["weight"],
        "contract_start_date": row["contract_start_date"],
        "contract_end_date": row["contract_end_date"],
        "total_cost": row["total_cost"], "spent_cost": row["spent_cost"],
        "progress_percent": row["progress_percent"],
        "tasks": tasks if tasks is not None else [],
        "progress_records": progress if progress is not None else [],
        "notes": notes if notes is not None else [],
        "documents": docs if docs is not None else [],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def _load_tasks(db, pid):
    rows = db.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY id", (pid,)).fetchall()
    return [task_to_dict(r) for r in rows]


def _load_progress(db, pid):
    rows = db.execute(
        "SELECT * FROM progress_records WHERE project_id=? ORDER BY from_date, id", (pid,)
    ).fetchall()
    return [progress_to_dict(r) for r in rows]


def _load_notes(db, pid):
    rows = db.execute(
        "SELECT * FROM project_notes WHERE project_id=? ORDER BY date, id", (pid,)
    ).fetchall()
    return [note_to_dict(r) for r in rows]


def _load_docs(db, pid):
    rows = db.execute(
        "SELECT * FROM project_documents WHERE project_id=? ORDER BY uploaded_at DESC, id DESC", (pid,)
    ).fetchall()
    return [doc_to_dict(r) for r in rows]


def _project_full(db, row):
    return project_to_dict(
        row,
        _load_tasks(db, row["id"]),
        _load_progress(db, row["id"]),
        _load_notes(db, row["id"]),
        _load_docs(db, row["id"]),
    )


def _all_projects(db):
    rows = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
    return [_project_full(db, r) for r in rows]


def _insert_tasks(db, pid, tasks):
    for t in tasks:
        if not isinstance(t, dict):
            continue
        if not clean_str(t.get("name")):
            continue
        tid = t.get("id") or gen_id()
        db.execute(
            "INSERT OR IGNORE INTO tasks "
            "(id,project_id,name,owner,budget_needed,spent_cost,start_date,end_date) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, pid, clean_str(t.get("name")), clean_str(t.get("owner"), 200),
             safe_num(t.get("budget_needed", 0)), safe_num(t.get("spent_cost", 0)),
             norm_date(t.get("start_date")), norm_date(t.get("end_date"))),
        )


def _insert_progress(db, pid, items):
    for it in items:
        if not isinstance(it, dict):
            continue
        fd, td = norm_date(it.get("from_date")), norm_date(it.get("to_date"))
        subj = clean_str(it.get("subject"), 300)
        if not fd or not td or not subj:
            continue
        db.execute(
            "INSERT OR IGNORE INTO progress_records "
            "(id,project_id,from_date,to_date,subject,physical_progress,financial_progress) "
            "VALUES (?,?,?,?,?,?,?)",
            (it.get("id") or gen_id(), pid, fd, td, subj,
             max(0, min(100, safe_num(it.get("physical_progress", 0)))),
             max(0, min(100, safe_num(it.get("financial_progress", 0))))),
        )


def _insert_notes(db, pid, items):
    for it in items:
        if not isinstance(it, dict):
            continue
        dt = norm_date(it.get("date"))
        desc = clean_str(it.get("description"), 1000)
        if not dt or not desc:
            continue
        db.execute(
            "INSERT OR IGNORE INTO project_notes (id,project_id,date,description) VALUES (?,?,?,?)",
            (it.get("id") or gen_id(), pid, dt, desc),
        )


def _replace_children(db, pid, d):
    """جایگزینی کامل فعالیت‌ها، دوره‌های پیشرفت و توضیحات یک پروژه"""
    db.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
    _insert_tasks(db, pid, d.get("tasks", []))
    db.execute("DELETE FROM progress_records WHERE project_id=?", (pid,))
    _insert_progress(db, pid, d.get("progress_records", []))
    db.execute("DELETE FROM project_notes WHERE project_id=?", (pid,))
    _insert_notes(db, pid, d.get("notes", []))


def _delete_project_files(db, pid):
    """حذف فایل‌های مستندات یک پروژه از دیسک و رکوردهای آن"""
    rows = db.execute("SELECT stored_name FROM project_documents WHERE project_id=?", (pid,)).fetchall()
    for r in rows:
        p = UPLOAD_DIR / r["stored_name"]
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    db.execute("DELETE FROM project_documents WHERE project_id=?", (pid,))


# ساخت جداول هنگام وارد کردن ماژول
init_db()


# ================================================================
#  لاگ
# ================================================================
@app.before_request
def _log_request():
    print(f"[{datetime.now():%H:%M:%S}] {request.method} {request.path}")


# ================================================================
#  احراز هویت
# ================================================================
def token_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "توکن ارسال نشده است"}), 401
        token_str = auth_header[7:]
        try:
            decoded = pyjwt.decode(token_str, JWT_SECRET, algorithms=["HS256"])
        except pyjwt.ExpiredSignatureError:
            return jsonify({"error": "نشست منقضی شده است"}), 401
        except pyjwt.InvalidTokenError:
            return jsonify({"error": "توکن نامعتبر است"}), 401
        request.user = decoded
        return f(*args, **kwargs)
    return wrapper


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok", "time": now_iso()})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or {}
    username = clean_str(d.get("username"), 100)
    password = d.get("password", "")

    if not username or not password:
        return jsonify({"error": "نام کاربری و رمز عبور الزامی است"}), 400

    user = next((u for u in USERS if u["username"] == username and u["password"] == password), None)
    if not user:
        return jsonify({"error": "نام کاربری یا رمز عبور اشتباه است"}), 401

    payload = {"username": user["username"], "role": user["role"], "fullName": user["fullName"]}
    token = pyjwt.encode(
        {**payload, "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRES_H)},
        JWT_SECRET, algorithm="HS256",
    )
    return jsonify({"token": token, "user": payload})


@app.route("/api/auth/me", methods=["GET"])
@token_required
def api_me():
    return jsonify({"user": request.user})


# ================================================================
#  پروژه‌ها — CRUD
# ================================================================
@app.route("/api/projects", methods=["GET"])
@token_required
def api_get_projects():
    db = get_db()
    return jsonify(_all_projects(db))


@app.route("/api/projects/<int:pid>", methods=["GET"])
@token_required
def api_get_project(pid):
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "پروژه یافت نشد"}), 404
    return jsonify(_project_full(db, row))


@app.route("/api/projects", methods=["POST"])
@token_required
def api_save_project():
    d = request.get_json(silent=True) or {}
    errs = validate_project(d)
    if errs:
        return jsonify({"error": "؛ ".join(errs), "details": errs}), 400

    db = get_db()
    now = now_iso()
    tc = safe_num(d.get("total_cost", 0))
    sc = safe_num(d.get("spent_cost", 0))
    pp = max(0, min(100, safe_num(d.get("progress_percent", 0))))
    wt = max(0, safe_num(d.get("weight", 0)))

    # --- بروزرسانی ---
    if d.get("id"):
        try:
            pid = int(d["id"])
        except (ValueError, TypeError):
            pid = None
        if pid:
            existing = db.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
            if existing:
                db.execute(
                    "UPDATE projects SET name=?, manager=?, status=?, start_date=?, end_date=?, "
                    "project_code=?, contract_number=?, weight=?, "
                    "contract_start_date=?, contract_end_date=?, "
                    "total_cost=?, spent_cost=?, progress_percent=?, updated_at=? WHERE id=?",
                    (clean_str(d["name"]), clean_str(d.get("manager")),
                     d.get("status", "active"),
                     norm_date(d.get("start_date")), norm_date(d.get("end_date")),
                     clean_str(d.get("project_code"), 100),
                     clean_str(d.get("contract_number"), 100),
                     wt,
                     norm_date(d.get("contract_start_date")),
                     norm_date(d.get("contract_end_date")),
                     tc, sc, pp, now, pid),
                )
                _replace_children(db, pid, d)
                db.commit()
                return jsonify(_all_projects(db))

    # --- ایجاد جدید ---
    new_id = gen_id()
    db.execute(
        "INSERT INTO projects "
        "(id,name,manager,status,start_date,end_date,project_code,contract_number,weight,"
        "contract_start_date,contract_end_date,total_cost,spent_cost,progress_percent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id, clean_str(d["name"]), clean_str(d.get("manager")),
         d.get("status", "active"),
         norm_date(d.get("start_date")), norm_date(d.get("end_date")),
         clean_str(d.get("project_code"), 100),
         clean_str(d.get("contract_number"), 100),
         wt,
         norm_date(d.get("contract_start_date")),
         norm_date(d.get("contract_end_date")),
         tc, sc, pp, now, now),
    )
    _replace_children(db, new_id, d)
    db.commit()
    return jsonify(_all_projects(db)), 201


@app.route("/api/projects/<int:pid>", methods=["PATCH"])
@token_required
def api_patch_project(pid):
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "پروژه یافت نشد"}), 404

    d = request.get_json(silent=True) or {}
    fields, values = [], []

    text_fields = ["name", "manager", "status", "project_code", "contract_number"]
    date_fields = ["start_date", "end_date", "contract_start_date", "contract_end_date"]
    for key in text_fields:
        if key in d:
            fields.append(f"{key}=?")
            values.append(clean_str(d[key]))
    for key in date_fields:
        if key in d:
            fields.append(f"{key}=?")
            values.append(norm_date(d[key]))
    if "weight" in d:
        fields.append("weight=?"); values.append(max(0, safe_num(d["weight"])))
    if "total_cost" in d:
        fields.append("total_cost=?"); values.append(safe_num(d["total_cost"]))
    if "spent_cost" in d:
        fields.append("spent_cost=?"); values.append(safe_num(d["spent_cost"]))
    if "progress_percent" in d:
        fields.append("progress_percent=?")
        values.append(max(0, min(100, safe_num(d["progress_percent"]))))

    if "tasks" in d or "progress_records" in d or "notes" in d:
        _replace_children(db, pid, d)

    if fields:
        fields.append("updated_at=?")
        values.extend([now_iso(), pid])
        db.execute("UPDATE projects SET " + ",".join(fields) + " WHERE id=?", values)

    db.commit()
    row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return jsonify(_project_full(db, row))


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
@token_required
def api_delete_project(pid):
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        return jsonify({"error": "پروژه یافت نشد"}), 404

    _delete_project_files(db, pid)   # ✅ حذف فایل‌های مستندات از دیسک
    db.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
    db.execute("DELETE FROM progress_records WHERE project_id=?", (pid,))
    db.execute("DELETE FROM project_notes WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    db.commit()
    return jsonify(_all_projects(db))


# ================================================================
#  مستندات — آپلود / دانلود / حذف   ✅ جدید
# ================================================================
@app.route("/api/projects/<int:pid>/documents", methods=["POST"])
@token_required
def api_upload_docs(pid):
    db = get_db()
    proj = db.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "پروژه یافت نشد"}), 404

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "فایلی ارسال نشده است"}), 400

    saved, rejected = 0, []
    for f in files:
        orig = safe_filename(f.filename)
        ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else ""
        if ext not in ALLOWED_DOCS:
            rejected.append(orig)
            continue

        stored = f"{gen_id()}_{secrets.token_hex(4)}.{ext}"
        dest = UPLOAD_DIR / stored
        f.save(str(dest))
        size = dest.stat().st_size

        db.execute(
            "INSERT INTO project_documents "
            "(id,project_id,orig_name,stored_name,file_size,mime_type,uploaded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (gen_id(), pid, orig, stored, size,
             f.mimetype or "", now_iso()),
        )
        saved += 1

    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), pid))
    db.commit()

    row = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    out = _project_full(db, row)
    if rejected:
        out["warning"] = "فرمت فایل(های) زیر مجاز نیست: " + "، ".join(rejected)
    return jsonify(out)


@app.route("/api/documents/<int:did>", methods=["GET"])
@token_required
def api_download_doc(did):
    db = get_db()
    row = db.execute("SELECT * FROM project_documents WHERE id=?", (did,)).fetchone()
    if not row:
        return jsonify({"error": "مستند یافت نشد"}), 404

    path = UPLOAD_DIR / row["stored_name"]
    if not path.exists():
        return jsonify({"error": "فایل روی سرور یافت نشد"}), 404

    return send_file(
        str(path),
        download_name=row["orig_name"],
        as_attachment=True,
        mimetype=row["mime_type"] or "application/octet-stream",
    )


@app.route("/api/documents/<int:did>", methods=["DELETE"])
@token_required
def api_delete_doc(did):
    db = get_db()
    row = db.execute("SELECT * FROM project_documents WHERE id=?", (did,)).fetchone()
    if not row:
        return jsonify({"error": "مستند یافت نشد"}), 404

    path = UPLOAD_DIR / row["stored_name"]
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass

    db.execute("DELETE FROM project_documents WHERE id=?", (did,))
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), row["project_id"]))
    db.commit()
    return jsonify({"ok": True})


# ================================================================
#  فعالیت‌ها
# ================================================================
@app.route("/api/projects/<int:pid>/tasks", methods=["POST"])
@token_required
def api_add_task(pid):
    db = get_db()
    proj = db.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "پروژه یافت نشد"}), 404

    d = request.get_json(silent=True) or {}
    errs = validate_task(d)
    if errs:
        return jsonify({"error": "؛ ".join(errs), "details": errs}), 400

    tid = gen_id()
    db.execute(
        "INSERT INTO tasks "
        "(id,project_id,name,owner,budget_needed,spent_cost,start_date,end_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tid, pid, clean_str(d["name"]), clean_str(d.get("owner"), 200),
         safe_num(d.get("budget_needed", 0)), safe_num(d.get("spent_cost", 0)),
         norm_date(d.get("start_date")), norm_date(d.get("end_date"))),
    )
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), pid))
    db.commit()

    return jsonify({
        "id": tid, "name": clean_str(d["name"]),
        "owner": clean_str(d.get("owner"), 200),
        "budget_needed": safe_num(d.get("budget_needed", 0)),
        "spent_cost": safe_num(d.get("spent_cost", 0)),
        "start_date": norm_date(d.get("start_date")),
        "end_date": norm_date(d.get("end_date")),
    }), 201


@app.route("/api/projects/<int:pid>/tasks/<int:tid>", methods=["DELETE"])
@token_required
def api_delete_task(pid, tid):
    db = get_db()
    proj = db.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "پروژه یافت نشد"}), 404

    task_row = db.execute("SELECT id FROM tasks WHERE id=? AND project_id=?", (tid, pid)).fetchone()
    if not task_row:
        return jsonify({"error": "فعالیت یافت نشد"}), 404

    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), pid))
    db.commit()
    return jsonify({"message": "حذف شد", "tasks": _load_tasks(db, pid)})


# ================================================================
#  آمار
# ================================================================
@app.route("/api/stats", methods=["GET"])
def api_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM projects WHERE status='active'").fetchone()[0]
    pending = db.execute("SELECT COUNT(*) FROM projects WHERE status='pending'").fetchone()[0]
    hold = db.execute("SELECT COUNT(*) FROM projects WHERE status='hold'").fetchone()[0]
    tb = db.execute("SELECT COALESCE(SUM(total_cost),0) FROM projects").fetchone()[0]
    ts = db.execute("SELECT COALESCE(SUM(spent_cost),0) FROM projects").fetchone()[0]
    ap_val = db.execute("SELECT AVG(progress_percent) FROM projects").fetchone()[0]
    ap = round(ap_val) if ap_val else 0
    af_val = db.execute(
        "SELECT AVG(CASE WHEN total_cost>0 THEN spent_cost*100.0/total_cost ELSE 0 END) FROM projects"
    ).fetchone()[0]
    af = round(af_val) if af_val else 0
    dev = round((ts - tb) / tb * 100) if tb > 0 else 0

    return jsonify({
        "total": total, "active": active, "pending": pending, "hold": hold,
        "totalBudget": tb, "totalSpent": ts,
        "avgPhysical": ap, "avgFinancial": af, "totalDeviation": dev,
    })


# ================================================================
#  جستجو
# ================================================================
@app.route("/api/projects/search", methods=["GET"])
def api_search():
    db = get_db()
    q = request.args.get("q", "").lower()
    st = request.args.get("status", "")

    sql = "SELECT * FROM projects WHERE 1=1"
    params = []

    if q:
        sql += (" AND (LOWER(name) LIKE ? OR LOWER(manager) LIKE ? "
                "OR LOWER(project_code) LIKE ? OR LOWER(contract_number) LIKE ?)")
        params.extend([f"%{q}%"] * 4)
    if st:
        sql += " AND status=?"
        params.append(st)

    sql += " ORDER BY id"
    rows = db.execute(sql, params).fetchall()
    return jsonify([_project_full(db, r) for r in rows])


# ================================================================
#  خروجی CSV
# ================================================================
@app.route("/api/export/excel", methods=["GET"])
def api_export_csv():
    db = get_db()
    rows = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
    sm = {"active": "فعال", "pending": "در انتظار", "hold": "متوقف"}

    lines = ["\uFEFFنام پروژه,کد پروژه,شماره قرارداد,وزن,واحد اجرایی,شروع,پایان,"
             "شروع قرارداد,پایان قرارداد,وضعیت,پیشرفت,بودجه,هزینه"]
    for r in rows:
        lines.append(
            f'"{r["name"]}","{r["project_code"]}","{r["contract_number"]}",'
            f'{r["weight"]},"{r["manager"]}","{r["start_date"]}",'
            f'"{r["end_date"]}","{r["contract_start_date"]}","{r["contract_end_date"]}",'
            f'"{sm.get(r["status"], r["status"])}",'
            f'{r["progress_percent"]},{r["total_cost"]},{r["spent_cost"]}'
        )

    resp = make_response("\n".join(lines))
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename=projects_{datetime.now():%Y-%m-%d}.csv'
    )
    return resp


# ================================================================
#  سرو index.html
# ================================================================
@app.route("/")
def serve_index():
    if INDEX_FILE.exists():
        return send_file(str(INDEX_FILE))
    return jsonify({"message": "بک‌اند فعال — index.html را کنار app.py قرار دهید"})


# ================================================================
#  خطاها
# ================================================================
@app.errorhandler(413)
def _handle_too_large(err):
    return jsonify({"error": "حجم فایل بیش از حد مجاز است (۲۵ مگابایت)"}), 413


@app.errorhandler(404)
def _handle_404(err):
    return jsonify({"error": "یافت نشد", "path": request.path}), 404


@app.errorhandler(500)
def _handle_500(err):
    return jsonify({"error": "خطای داخلی سرور"}), 500


# ================================================================
#  اجرا
# ================================================================
if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  بک‌اند مدیریت پروژه  (Flask + SQLite3)            ║")
    print("║  شرکت عمران و توسعه شرق رضوی                      ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  http://localhost:{PORT}{' ' * (28 - len(str(PORT)))}║")
    print(f"║  DB: {DB_FILE}{' ' * max(0, 34 - len(str(DB_FILE)))}║")
    print(f"║  Uploads: {UPLOAD_DIR}{' ' * max(0, 28 - len(str(UPLOAD_DIR)))}║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    app.run(host="0.0.0.0", port=PORT, debug=True)