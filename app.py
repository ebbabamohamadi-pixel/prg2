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
from datetime import datetime, timedelta, timezone
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
UPLOAD_DIR = DATA_DIR / "uploads"
MAX_UPLOAD = 25 * 1024 * 1024
INDEX_FILE = Path(__file__).resolve().parent / "index.html"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_DOCS = {
    "pdf", "jpg", "jpeg", "png", "gif", "webp", "bmp",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv",
    "zip", "rar", "7z", "dwg", "dxf",
}

PROJECT_TYPES = ("jari", "omrani")
LETTER_TYPES = ("sadere", "varede")

# ===== کاربران (ثابت) =====
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
    # ✅ timezone-aware (جایگزین datetime.utcnow منقضی‌شده)
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def gen_id():
    return int(time.time() * 1000) + (uuid.uuid4().int % 10000)


def clean_str(v, max_len=300):
    if v is None:
        return ""
    return str(v).strip()[:max_len]


def norm_date(v):
    if not v:
        return ""
    s = str(v).strip()[:10]
    try:
        datetime.fromisoformat(s)
        return s
    except ValueError:
        return ""


def safe_filename(name):
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
    if d.get("project_type") and d["project_type"] not in PROJECT_TYPES:
        errs.append("نوع پروژه نامعتبر (فقط جاری یا عمرانی)")

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
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024


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
            project_type         TEXT    NOT NULL DEFAULT 'omrani',
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
            financial_progress REAL    NOT NULL DEFAULT 0,
            weight             REAL    NOT NULL DEFAULT 0,
            budget_amt         REAL    NOT NULL DEFAULT 0,
            spent_amt          REAL    NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS performance_records (
            id                 INTEGER PRIMARY KEY,
            project_id         INTEGER NOT NULL,
            from_date          TEXT    NOT NULL DEFAULT '',
            to_date            TEXT    NOT NULL DEFAULT '',
            subject            TEXT    NOT NULL DEFAULT '',
            physical_progress  REAL    NOT NULL DEFAULT 0,
            financial_progress REAL    NOT NULL DEFAULT 0,
            weight             REAL    NOT NULL DEFAULT 0,
            budget_amt         REAL    NOT NULL DEFAULT 0,
            spent_amt          REAL    NOT NULL DEFAULT 0,
            description        TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS project_letters (
            id          INTEGER PRIMARY KEY,
            project_id  INTEGER NOT NULL,
            letter_no   TEXT    NOT NULL DEFAULT '',
            letter_date TEXT    NOT NULL DEFAULT '',
            subject     TEXT    NOT NULL DEFAULT '',
            recipient   TEXT    NOT NULL DEFAULT '',
            letter_type TEXT    NOT NULL DEFAULT 'sadere'
                        CHECK (letter_type IN ('sadere','varede'))
        );
        CREATE TABLE IF NOT EXISTS project_notes (
            id          INTEGER PRIMARY KEY,
            project_id  INTEGER NOT NULL,
            date        TEXT    NOT NULL DEFAULT '',
            description TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS project_documents (
            id          INTEGER PRIMARY KEY,
            project_id  INTEGER NOT NULL,
            orig_name   TEXT    NOT NULL,
            stored_name TEXT    NOT NULL,
            file_size   INTEGER NOT NULL DEFAULT 0,
            mime_type   TEXT    NOT NULL DEFAULT '',
            uploaded_at TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS activity_progress (
            id                 INTEGER PRIMARY KEY,
            performance_id     INTEGER NOT NULL,
            project_id         INTEGER NOT NULL,
            year                INTEGER NOT NULL DEFAULT 0,
            month               INTEGER NOT NULL DEFAULT 0,
            physical_progress   REAL    NOT NULL DEFAULT 0,
            financial_progress  REAL    NOT NULL DEFAULT 0,
            description         TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS activity_progress_documents (
            id               INTEGER PRIMARY KEY,
            activity_progress_id INTEGER NOT NULL,
            project_id       INTEGER NOT NULL,
            orig_name        TEXT    NOT NULL,
            stored_name      TEXT    NOT NULL,
            file_size        INTEGER NOT NULL DEFAULT 0,
            mime_type        TEXT    NOT NULL DEFAULT '',
            uploaded_at      TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
        CREATE INDEX IF NOT EXISTS idx_progress_project ON progress_records(project_id);
        CREATE INDEX IF NOT EXISTS idx_perf_project ON performance_records(project_id);
        CREATE INDEX IF NOT EXISTS idx_letters_project ON project_letters(project_id);
        CREATE INDEX IF NOT EXISTS idx_notes_project ON project_notes(project_id);
        CREATE INDEX IF NOT EXISTS idx_docs_project ON project_documents(project_id);
        CREATE INDEX IF NOT EXISTS idx_actprog_perf ON activity_progress(performance_id);
        CREATE INDEX IF NOT EXISTS idx_actdoc_aprog ON activity_progress_documents(activity_progress_id);
    """)
    con.commit()

    # --- مهاجرت برای دیتابیس‌های قدیمی ---
    _add_column_if_missing(con, "projects", "project_code TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(con, "projects", "contract_number TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(con, "projects", "weight REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "projects", "contract_start_date TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(con, "projects", "contract_end_date TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(con, "projects", "project_type TEXT NOT NULL DEFAULT 'omrani'")
    _add_column_if_missing(con, "progress_records", "weight REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "progress_records", "budget_amt REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "progress_records", "spent_amt REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "performance_records", "weight REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "performance_records", "budget_amt REAL NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "performance_records", "spent_amt REAL NOT NULL DEFAULT 0")
    con.commit()

    count = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    if count == 0:
        _seed_db(con)
    con.close()


def _seed_db(con):
    projects_data = [
        (1001, "ساخت مجتمع تجاری صالحی", "واحد عمران", "active", "omrani",
         "2024-03-21", "2025-09-22", "PRJ-1402-101", "CT-1024", 100,
         "2024-03-15", "2025-09-01", 85000, 32000, 38,
         "2024-01-15T08:00:00Z", "2025-01-10T14:30:00Z"),
        (1002, "راه‌آهن سرخس-مشهد", "واحد ریلی", "active", "omrani",
         "2023-08-23", "2027-03-20", "PRJ-1402-014", "CT-0871", 250,
         "2023-08-10", "2027-02-20", 220000, 45000, 20,
         "2023-06-01T10:00:00Z", "2025-01-08T09:15:00Z"),
        (1003, "بهسازی شبکه آب شهری", "واحد زیربنایی", "active", "omrani",
         "2024-06-01", "2025-06-01", "PRJ-1403-030", "CT-1150", 80,
         "2024-05-25", "2025-05-20", 35000, 18000, 52,
         "2024-05-10T07:30:00Z", "2025-01-12T11:00:00Z"),
        (1004, "ساخت پارکینگ طبقاتی", "واحد عمران", "pending", "omrani",
         "2025-01-01", "2026-06-30", "PRJ-1403-044", "CT-1201", 60,
         "2024-12-20", "2026-06-15", 42000, 0, 0,
         "2024-11-20T16:00:00Z", "2024-11-20T16:00:00Z"),
        (1005, "احداث پل عابر پیاده ولیعصر", "واحد زیربنایی", "hold", "omrani",
         "2024-04-01", "2025-01-01", "PRJ-1403-009", "CT-0980", 40,
         "2024-03-25", "2024-12-25", 12000, 4000, 33,
         "2024-03-15T09:00:00Z", "2024-12-01T08:45:00Z"),
        (1006, "نصب چراغ‌های هوشمند شهری", "واحد برق", "active", "jari",
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
        (9001, 1001, "2024-03-21", "2024-09-22", "گودبرداری و فونداسیون", 25, 20, 40, 30000, 12000),
        (9002, 1001, "2024-09-23", "2025-01-10", "اسکلت‌سازی طبقات", 13, 12, 35, 35000, 15000),
        (9003, 1002, "2023-08-23", "2024-08-22", "مطالعات و خرید اراضی", 20, 18, 60, 60000, 25000),
        (9004, 1003, "2024-06-01", "2024-12-01", "کاشت لوله اصلی", 52, 51, 70, 28000, 14000),
    ]
    performance_data = [
        (9201, 1001, "2024-03-21", "2024-09-22", "فاز اول اجرا", 25, 20, 40, 30000, 11500, "مطابق برنامه زمان‌بندی پیش رفته است"),
        (9202, 1001, "2024-09-23", "2025-01-10", "فاز دوم اجرا", 13, 12, 35, 35000, 14800, "کمی تأخیر در تأمین مصالح"),
        (9203, 1002, "2023-08-23", "2024-08-22", "فاز مطالعات", 20, 18, 60, 60000, 24800, "طبق صورتجلسه کارفرما"),
    ]
    letters_data = [
        (9701, 1001, "1402-12-28-A-101", "2024-03-18", "ابلاغ شروع عملیات اجرایی", "شهرداری منطقه ۳", "sadere"),
        (9702, 1001, "1403-01-05-W-442", "2024-03-25", "درخواست هماهنگی تردد ماشین‌آلات", "اداره راه و شهرسازی", "varede"),
        (9703, 1002, "1402-05-20-A-088", "2023-08-11", "ارسال گزارش مطالعات مرحله اول", "شرکت راه‌آهن جمهوری", "sadere"),
    ]
    notes_data = [
        (9501, 1001, "2024-03-21", "شروع عملیات اجرایی پروژه"),
        (9502, 1001, "2024-12-15", "تأخیر در تأمین میلگرد به دلیل شرایط جوی"),
        (9503, 1002, "2024-02-20", "پایان فاز مطالعات توجیهی"),
        (9504, 1005, "2024-12-01", "توقف پروژه در انتظار تخصیص اعتبار"),
    ]
    con.executemany(
        "INSERT OR IGNORE INTO projects "
        "(id,name,manager,status,project_type,start_date,end_date,project_code,contract_number,weight,"
        "contract_start_date,contract_end_date,total_cost,spent_cost,progress_percent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        "(id,project_id,from_date,to_date,subject,physical_progress,financial_progress,weight,budget_amt,spent_amt) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        progress_data,
    )
    con.executemany(
        "INSERT OR IGNORE INTO performance_records "
        "(id,project_id,from_date,to_date,subject,physical_progress,financial_progress,weight,budget_amt,spent_amt,description) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        performance_data,
    )
    con.executemany(
        "INSERT OR IGNORE INTO project_letters "
        "(id,project_id,letter_no,letter_date,subject,recipient,letter_type) "
        "VALUES (?,?,?,?,?,?,?)",
        letters_data,
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
    k = row.keys()
    return {
        "id": row["id"], "from_date": row["from_date"], "to_date": row["to_date"],
        "subject": row["subject"],
        "physical_progress": row["physical_progress"],
        "financial_progress": row["financial_progress"],
        "weight": row["weight"] if "weight" in k else 0,
        "budget_amt": row["budget_amt"] if "budget_amt" in k else 0,
        "spent_amt": row["spent_amt"] if "spent_amt" in k else 0,
    }


def performance_to_dict(row):
    k = row.keys()
    return {
        "id": row["id"], "from_date": row["from_date"], "to_date": row["to_date"],
        "subject": row["subject"],
        "physical_progress": row["physical_progress"],
        "financial_progress": row["financial_progress"],
        "weight": row["weight"] if "weight" in k else 0,
        "budget_amt": row["budget_amt"] if "budget_amt" in k else 0,
        "spent_amt": row["spent_amt"] if "spent_amt" in k else 0,
        "description": row["description"],
        "progress": [],
    }


def letter_to_dict(row):
    return {
        "id": row["id"],
        "letter_no": row["letter_no"],
        "letter_date": row["letter_date"],
        "subject": row["subject"],
        "recipient": row["recipient"],
        "letter_type": row["letter_type"],
    }


def note_to_dict(row):
    return {"id": row["id"], "date": row["date"], "description": row["description"]}


def doc_to_dict(row):
    return {
        "id": row["id"], "orig_name": row["orig_name"],
        "file_size": row["file_size"], "mime_type": row["mime_type"],
        "uploaded_at": row["uploaded_at"],
    }


def project_to_dict(row, tasks=None, progress=None, perf=None, letters=None, notes=None, docs=None):
    return {
        "id": row["id"], "name": row["name"], "manager": row["manager"],
        "status": row["status"],
        "project_type": row["project_type"] if "project_type" in row.keys() else "omrani",
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
        "performance_records": perf if perf is not None else [],
        "letters": letters if letters is not None else [],
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


def _load_performance(db, pid):
    rows = db.execute(
        "SELECT * FROM performance_records WHERE project_id=? ORDER BY from_date, id", (pid,)
    ).fetchall()
    out = [performance_to_dict(r) for r in rows]
    # ✅ بارگذاری پیشرفت‌های ماهانه هر فعالیت به همراه مستندات
    for perf in out:
        perf["progress"] = _load_activity_progress(db, perf["id"])
    return out


def _act_progress_to_dict(row):
    return {
        "id": row["id"],
        "year": row["year"],
        "month": row["month"],
        "physical_progress": row["physical_progress"],
        "financial_progress": row["financial_progress"],
        "description": row["description"],
        "documents": [],
    }


def _load_activity_progress(db, perf_id):
    rows = db.execute(
        "SELECT * FROM activity_progress WHERE performance_id=? ORDER BY year, month, id", (perf_id,)
    ).fetchall()
    out = [dict(_act_progress_to_dict(r)) for r in rows]
    for ap in out:
        ap["documents"] = _load_activity_progress_docs(db, ap["id"])
    return out


def _load_activity_progress_docs(db, ap_id):
    rows = db.execute(
        "SELECT * FROM activity_progress_documents WHERE activity_progress_id=? "
        "ORDER BY uploaded_at DESC, id DESC", (ap_id,)
    ).fetchall()
    return [{
        "id": r["id"], "orig_name": r["orig_name"],
        "file_size": r["file_size"], "mime_type": r["mime_type"],
        "uploaded_at": r["uploaded_at"],
    } for r in rows]


def _load_letters(db, pid):
    rows = db.execute(
        "SELECT * FROM project_letters WHERE project_id=? ORDER BY letter_date, id", (pid,)
    ).fetchall()
    return [letter_to_dict(r) for r in rows]


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
        _load_performance(db, row["id"]),
        _load_letters(db, row["id"]),
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
            "(id,project_id,from_date,to_date,subject,physical_progress,financial_progress,weight,budget_amt,spent_amt) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (it.get("id") or gen_id(), pid, fd, td, subj,
             max(0, min(100, safe_num(it.get("physical_progress", 0)))),
             max(0, min(100, safe_num(it.get("financial_progress", 0)))),
             max(0, safe_num(it.get("weight", 0))),
             max(0, safe_num(it.get("budget_amt", 0))),
             max(0, safe_num(it.get("spent_amt", 0)))),
        )


def _insert_performance(db, pid, items):
    for it in items:
        if not isinstance(it, dict):
            continue
        fd, td = norm_date(it.get("from_date")), norm_date(it.get("to_date"))
        subj = clean_str(it.get("subject"), 300)
        if not fd or not td or not subj:
            continue
        perf_id = it.get("id") or gen_id()
        db.execute(
            "INSERT OR IGNORE INTO performance_records "
            "(id,project_id,from_date,to_date,subject,physical_progress,financial_progress,weight,budget_amt,spent_amt,description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (perf_id, pid, fd, td, subj,
             max(0, min(100, safe_num(it.get("physical_progress", 0)))),
             max(0, min(100, safe_num(it.get("financial_progress", 0)))),
             max(0, safe_num(it.get("weight", 0))),
             max(0, safe_num(it.get("budget_amt", 0))),
             max(0, safe_num(it.get("spent_amt", 0))),
             clean_str(it.get("description"), 1000)),
        )
        # ✅ درج پیشرفت‌های ماهانه‌ی هر فعالیت
        _insert_activity_progress(db, pid, perf_id, it.get("progress", []))


def _insert_activity_progress(db, pid, perf_id, items):
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            yr = int(it.get("year", 0) or 0)
            mo = int(it.get("month", 0) or 0)
        except (ValueError, TypeError):
            continue
        if yr < 1300 or mo < 1 or mo > 12:
            continue
        db.execute(
            "INSERT OR IGNORE INTO activity_progress "
            "(id,performance_id,project_id,year,month,physical_progress,financial_progress,description) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (it.get("id") or gen_id(), perf_id, pid, yr, mo,
             max(0, min(100, safe_num(it.get("physical_progress", 0)))),
             max(0, min(100, safe_num(it.get("financial_progress", 0)))),
             clean_str(it.get("description"), 1000)),
        )


def _insert_letters(db, pid, items):
    for it in items:
        if not isinstance(it, dict):
            continue
        lno = clean_str(it.get("letter_no"), 100)
        dt = norm_date(it.get("letter_date"))
        subj = clean_str(it.get("subject"), 300)
        if not lno or not dt or not subj:
            continue
        ltype = it.get("letter_type") if it.get("letter_type") in LETTER_TYPES else "sadere"
        db.execute(
            "INSERT OR IGNORE INTO project_letters "
            "(id,project_id,letter_no,letter_date,subject,recipient,letter_type) "
            "VALUES (?,?,?,?,?,?,?)",
            (it.get("id") or gen_id(), pid, lno, dt, subj,
             clean_str(it.get("recipient"), 200), ltype),
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
    db.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
    _insert_tasks(db, pid, d.get("tasks", []))
    db.execute("DELETE FROM progress_records WHERE project_id=?", (pid,))
    _insert_progress(db, pid, d.get("progress_records", []))

    # ✅ عملکرد و پیشرفت ماهانه:
    # - رکوردهای عملکردی که در payload نیستند → حذف کامل (همراه با پیشرفت ماهانه و مستنداتشان)
    # - رکوردهای موجود با شناسه‌ی ثابت → با INSERT OR IGNORE حفظ می‌شوند،
    #   و پیشرفت‌های ماهانه‌ی آن‌ها هم با INSERT OR IGNORE و شناسه‌ی ثابت نگه داشته می‌شوند،
    #   بنابراین مستندات فیزیکی متصل به آن شناسه‌ها از بین نمی‌روند.
    perf_items = d.get("performance_records", [])
    kept_perf_ids = set()
    for it in perf_items:
        if isinstance(it, dict) and it.get("id"):
            try:
                kept_perf_ids.add(int(it["id"]))
            except (ValueError, TypeError):
                pass
    existing_perf = db.execute(
        "SELECT id FROM performance_records WHERE project_id=?", (pid,)
    ).fetchall()
    for er in existing_perf:
        if er["id"] not in kept_perf_ids:
            _delete_activity_progress_by_perf(db, er["id"])
    db.execute("DELETE FROM performance_records WHERE project_id=?", (pid,))
    _insert_performance(db, pid, perf_items)

    db.execute("DELETE FROM project_letters WHERE project_id=?", (pid,))
    _insert_letters(db, pid, d.get("letters", []))
    db.execute("DELETE FROM project_notes WHERE project_id=?", (pid,))
    _insert_notes(db, pid, d.get("notes", []))


def _delete_activity_progress_by_perf(db, perf_id):
    """حذف همه‌ی پیشرفت‌های ماهانه‌ی یک فعالیت و مستنداتشان (فایل فیزیکی هم)."""
    ap_rows = db.execute(
        "SELECT id FROM activity_progress WHERE performance_id=?", (perf_id,)
    ).fetchall()
    for ap in ap_rows:
        _delete_activity_progress_docs(db, ap["id"])
    db.execute("DELETE FROM activity_progress WHERE performance_id=?", (perf_id,))


def _delete_activity_progress_docs(db, ap_id):
    rows = db.execute(
        "SELECT stored_name FROM activity_progress_documents WHERE activity_progress_id=?",
        (ap_id,)
    ).fetchall()
    for r in rows:
        p = UPLOAD_DIR / r["stored_name"]
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    db.execute("DELETE FROM activity_progress_documents WHERE activity_progress_id=?", (ap_id,))


def _delete_project_files(db, pid):
    rows = db.execute("SELECT stored_name FROM project_documents WHERE project_id=?", (pid,)).fetchall()
    for r in rows:
        p = UPLOAD_DIR / r["stored_name"]
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    db.execute("DELETE FROM project_documents WHERE project_id=?", (pid,))
    # ✅ حذف پیشرفت ماهانه‌ی فعالیت‌ها و مستنداتشان هنگام حذف پروژه
    ap_rows = db.execute(
        "SELECT apd.stored_name FROM activity_progress_documents apd "
        "JOIN activity_progress ap ON apd.activity_progress_id=ap.id "
        "WHERE ap.project_id=?", (pid,)
    ).fetchall()
    for r in ap_rows:
        p = UPLOAD_DIR / r["stored_name"]
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    db.execute("DELETE FROM activity_progress_documents WHERE activity_progress_id IN "
               "(SELECT id FROM activity_progress WHERE project_id=?)", (pid,))
    db.execute("DELETE FROM activity_progress WHERE project_id=?", (pid,))


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
    # ✅ timezone-aware (جایگزین utcnow منقضی‌شده)
    token = pyjwt.encode(
        {**payload, "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRES_H)},
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
    ptype = d.get("project_type") if d.get("project_type") in PROJECT_TYPES else "omrani"

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
                    "UPDATE projects SET name=?, manager=?, status=?, project_type=?, "
                    "start_date=?, end_date=?, "
                    "project_code=?, contract_number=?, weight=?, "
                    "contract_start_date=?, contract_end_date=?, "
                    "total_cost=?, spent_cost=?, progress_percent=?, updated_at=? WHERE id=?",
                    (clean_str(d["name"]), clean_str(d.get("manager")),
                     d.get("status", "active"), ptype,
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
                # ✅ id پروژه در پاسخ برگردانده می‌شود
                return jsonify({"id": pid, "projects": _all_projects(db)})

    # --- ایجاد جدید ---
    new_id = gen_id()
    db.execute(
        "INSERT INTO projects "
        "(id,name,manager,status,project_type,start_date,end_date,project_code,contract_number,weight,"
        "contract_start_date,contract_end_date,total_cost,spent_cost,progress_percent,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id, clean_str(d["name"]), clean_str(d.get("manager")),
         d.get("status", "active"), ptype,
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
    # ✅ id پروژه جدید در پاسخ برگردانده می‌شود
    return jsonify({"id": new_id, "projects": _all_projects(db)}), 201


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
    if "project_type" in d and d["project_type"] in PROJECT_TYPES:
        fields.append("project_type=?")
        values.append(d["project_type"])
    if "weight" in d:
        fields.append("weight=?"); values.append(max(0, safe_num(d["weight"])))
    if "total_cost" in d:
        fields.append("total_cost=?"); values.append(safe_num(d["total_cost"]))
    if "spent_cost" in d:
        fields.append("spent_cost=?"); values.append(safe_num(d["spent_cost"]))
    if "progress_percent" in d:
        fields.append("progress_percent=?")
        values.append(max(0, min(100, safe_num(d["progress_percent"]))))

    if ("tasks" in d or "progress_records" in d or "performance_records" in d
            or "letters" in d or "notes" in d):
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

    _delete_project_files(db, pid)
    db.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
    db.execute("DELETE FROM progress_records WHERE project_id=?", (pid,))
    db.execute("DELETE FROM performance_records WHERE project_id=?", (pid,))
    db.execute("DELETE FROM project_letters WHERE project_id=?", (pid,))
    db.execute("DELETE FROM project_notes WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    db.commit()
    return jsonify(_all_projects(db))


# ================================================================
#  مستندات — آپلود / دانلود / حذف
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
            (gen_id(), pid, orig, stored, size, f.mimetype or "", now_iso()),
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
#  پیشرفت ماهانه‌ی فعالیت‌های عملکرد — آپلود / دانلود / حذف مستندات
# ================================================================
@app.route("/api/activity-progress/<int:apid>/documents", methods=["POST"])
@token_required
def api_upload_ap_docs(apid):
    db = get_db()
    ap = db.execute(
        "SELECT id, performance_id, project_id FROM activity_progress WHERE id=?", (apid,)
    ).fetchone()
    if not ap:
        return jsonify({"error": "رکورد پیشرفت یافت نشد"}), 404

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
            "INSERT INTO activity_progress_documents "
            "(id,activity_progress_id,project_id,orig_name,stored_name,file_size,mime_type,uploaded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (gen_id(), apid, ap["project_id"], orig, stored, size, f.mimetype or "", now_iso()),
        )
        saved += 1

    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), ap["project_id"]))
    db.commit()

    ap_rows = db.execute(
        "SELECT * FROM activity_progress WHERE performance_id=?", (ap["performance_id"],)
    ).fetchall()
    out = [_act_progress_to_dict(r) for r in ap_rows]
    for o in out:
        o["documents"] = _load_activity_progress_docs(db, o["id"])
    resp = {"documents": out}
    if rejected:
        resp["warning"] = "فرمت فایل(های) زیر مجاز نیست: " + "، ".join(rejected)
    return jsonify(resp)


@app.route("/api/activity-progress/documents/<int:did>", methods=["GET"])
@token_required
def api_download_ap_doc(did):
    db = get_db()
    row = db.execute(
        "SELECT * FROM activity_progress_documents WHERE id=?", (did,)
    ).fetchone()
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


@app.route("/api/activity-progress/documents/<int:did>", methods=["DELETE"])
@token_required
def api_delete_ap_doc(did):
    db = get_db()
    row = db.execute(
        "SELECT * FROM activity_progress_documents WHERE id=?", (did,)
    ).fetchone()
    if not row:
        return jsonify({"error": "مستند یافت نشد"}), 404
    path = UPLOAD_DIR / row["stored_name"]
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    db.execute("DELETE FROM activity_progress_documents WHERE id=?", (did,))
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), row["project_id"]))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/activity-progress/<int:apid>", methods=["DELETE"])
@token_required
def api_delete_activity_progress(apid):
    """حذف یک رکورد پیشرفت ماهانه و تمام مستنداتش (فایل‌های فیزیکی هم)."""
    db = get_db()
    ap = db.execute(
        "SELECT id, performance_id, project_id FROM activity_progress WHERE id=?", (apid,)
    ).fetchone()
    if not ap:
        return jsonify({"error": "رکورد پیشرفت یافت نشد"}), 404
    _delete_activity_progress_docs(db, apid)
    db.execute("DELETE FROM activity_progress WHERE id=?", (apid,))
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), ap["project_id"]))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/performance-records/<int:perf_id>/activity-progress", methods=["POST"])
@token_required
def api_create_activity_progress(perf_id):
    """ایجاد مستقیم یک رکورد پیشرفت ماهانه‌ی جدید در دیتابیس."""
    db = get_db()
    perf = db.execute(
        "SELECT id, project_id FROM performance_records WHERE id=?", (perf_id,)
    ).fetchone()
    if not perf:
        return jsonify({"error": "رکورد عملکرد یافت نشد"}), 404
    d = request.get_json(force=True, silent=True) or {}
    try:
        yr = int(d.get("year", 0) or 0)
        mo = int(d.get("month", 0) or 0)
    except (ValueError, TypeError):
        return jsonify({"error": "سال و ماه باید عدد صحیح باشند"}), 400
    if yr < 1300 or mo < 1 or mo > 12:
        return jsonify({"error": "سال یا ماه نامعتبر است"}), 400
    new_id = gen_id()
    db.execute(
        "INSERT INTO activity_progress "
        "(id, performance_id, project_id, year, month, physical_progress, financial_progress, description) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (new_id, perf_id, perf["project_id"], yr, mo,
         max(0, min(100, safe_num(d.get("physical_progress", 0)))),
         max(0, min(100, safe_num(d.get("financial_progress", 0)))),
         clean_str(d.get("description"), 1000)),
    )
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), perf["project_id"]))
    db.commit()
    row = db.execute("SELECT * FROM activity_progress WHERE id=?", (new_id,)).fetchone()
    out = _act_progress_to_dict(row)
    out["documents"] = []
    return jsonify(out), 201


@app.route("/api/activity-progress/<int:apid>", methods=["PUT"])
@token_required
def api_update_activity_progress(apid):
    """به‌روزرسانی مستقیم یک رکورد پیشرفت ماهانه در دیتابیس."""
    db = get_db()
    ap = db.execute(
        "SELECT * FROM activity_progress WHERE id=?", (apid,)
    ).fetchone()
    if not ap:
        return jsonify({"error": "رکورد پیشرفت یافت نشد"}), 404
    d = request.get_json(force=True, silent=True) or {}
    fields, values = [], []
    if "year" in d:
        try:
            yr = int(d["year"] or 0)
        except (ValueError, TypeError):
            return jsonify({"error": "سال باید عدد صحیح باشد"}), 400
        if yr < 1300:
            return jsonify({"error": "سال نامعتبر است"}), 400
        fields.append("year=?"); values.append(yr)
    if "month" in d:
        try:
            mo = int(d["month"] or 0)
        except (ValueError, TypeError):
            return jsonify({"error": "ماه باید عدد صحیح باشد"}), 400
        if mo < 1 or mo > 12:
            return jsonify({"error": "ماه نامعتبر است"}), 400
        fields.append("month=?"); values.append(mo)
    if "physical_progress" in d:
        fields.append("physical_progress=?")
        values.append(max(0, min(100, safe_num(d["physical_progress"]))))
    if "financial_progress" in d:
        fields.append("financial_progress=?")
        values.append(max(0, min(100, safe_num(d["financial_progress"]))))
    if "description" in d:
        fields.append("description=?")
        values.append(clean_str(d["description"], 1000))
    if not fields:
        return jsonify({"error": "فیلدی برای به‌روزرسانی ارسال نشده است"}), 400
    values.append(apid)
    db.execute("UPDATE activity_progress SET " + ",".join(fields) + " WHERE id=?", values)
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), ap["project_id"]))
    db.commit()
    row = db.execute("SELECT * FROM activity_progress WHERE id=?", (apid,)).fetchone()
    out = _act_progress_to_dict(row)
    out["documents"] = _load_activity_progress_docs(db, apid)
    return jsonify(out)


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
@token_required  # ✅ محافظت‌شده با توکن
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
@token_required  # ✅ محافظت‌شده با توکن
def api_search():
    db = get_db()
    q = request.args.get("q", "").lower()
    st = request.args.get("status", "")
    pt = request.args.get("type", "")

    sql = "SELECT * FROM projects WHERE 1=1"
    params = []

    if q:
        sql += (" AND (LOWER(name) LIKE ? OR LOWER(manager) LIKE ? "
                "OR LOWER(project_code) LIKE ? OR LOWER(contract_number) LIKE ?)")
        params.extend([f"%{q}%"] * 4)
    if st:
        sql += " AND status=?"
        params.append(st)
    if pt in PROJECT_TYPES:
        sql += " AND project_type=?"
        params.append(pt)

    sql += " ORDER BY id"
    rows = db.execute(sql, params).fetchall()
    return jsonify([_project_full(db, r) for r in rows])


# ================================================================
#  خروجی CSV
# ================================================================
@app.route("/api/export/excel", methods=["GET"])
@token_required  # ✅ محافظت‌شده با توکن
def api_export_csv():
    db = get_db()
    rows = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
    sm = {"active": "فعال", "pending": "در انتظار", "hold": "متوقف"}
    tm = {"jari": "جاری", "omrani": "عمرانی"}

    lines = ["\uFEFFنام پروژه,نوع پروژه,کد پروژه,شماره قرارداد,وزن,واحد اجرایی,شروع,پایان,"
             "شروع قرارداد,پایان قرارداد,وضعیت,پیشرفت,بودجه,هزینه"]
    for r in rows:
        rtype = r["project_type"] if "project_type" in r.keys() else "omrani"
        lines.append(
            f'"{r["name"]}","{tm.get(rtype, rtype)}","{r["project_code"]}","{r["contract_number"]}",'
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
    # ✅ علت خطا در لاگ سرور ثبت می‌شود
    app.logger.exception("خطای داخلی سرور")
    return jsonify({"error": "خطای داخلی سرور"}), 500


# ================================================================
#  اجرا
# ================================================================
if __name__ == "__main__":
    # ✅ debug فقط با متغیر محیطی FLASK_DEBUG=1 فعال می‌شود
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║  بک‌اند مدیریت پروژه  (Flask + SQLite3)            ║")
    print("║  شرکت عمران و توسعه شرق رضوی                      ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  http://localhost:{PORT}{' ' * (28 - len(str(PORT)))}║")
    print(f"║  DB: {DB_FILE}{' ' * max(0, 34 - len(str(DB_FILE)))}║")
    print(f"║  Uploads: {UPLOAD_DIR}{' ' * max(0, 28 - len(str(UPLOAD_DIR)))}║")
    print(f"║  Debug: {'ON ' if DEBUG else 'OFF'}{' ' * 29}║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)