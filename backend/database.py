import sqlite3
from datetime import date
 
def get_connection():
    conn = sqlite3.connect("scheduler.db")
    conn.row_factory = sqlite3.Row
    return conn
 
def init_db():
    conn = get_connection()
    cursor = conn.cursor()
 
    # 1. 사용자 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            password TEXT,
            max_tasks_per_day INTEGER,
            study_pattern TEXT,
            exam_style TEXT
        )
    """)
 
    # 2. 과목별 공부 속도 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subject_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT,
            study_speed INTEGER,
            data_count INTEGER DEFAULT 0
        )
    """)
 
    # 3. 과제/프로젝트 테이블
    # status: 'ongoing' = 장기 프로젝트 진행 중
    #         'completed' = 장기 프로젝트 완료/만료
    #         'simple' = 일회성 task
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT,
            title TEXT,
            deadline TEXT,
            status TEXT DEFAULT 'ongoing'
        )
    """)
 
    # 4. 일별 task 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            date TEXT,
            task_name TEXT,
            completed INTEGER DEFAULT 0
        )
    """)
 
    # 5. 과거 기록 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            subject TEXT,
            planned_tasks INTEGER,
            completed_tasks INTEGER,
            delay_days INTEGER DEFAULT 0,
            difficulty TEXT,
            finished_at TEXT
        )
    """)
 
    conn.commit()
    conn.close()
 
 
def check_expired_tasks():
    """매일 자정 실행 — 마감일 지난 프로젝트 처리"""
    conn = get_connection()
    cursor = conn.cursor()
    today = date.today().isoformat()
 
    # ongoing 장기 프로젝트만 만료 체크 (simple 제외)
    expired = cursor.execute("""
        SELECT * FROM tasks
        WHERE deadline < ? AND status = 'ongoing'
    """, (today,)).fetchall()
 
    for task in expired:
        completed = cursor.execute("""
            SELECT COUNT(*) FROM daily_schedule
            WHERE task_id = ? AND completed = 1
        """, (task['id'],)).fetchone()[0]
 
        planned = cursor.execute("""
            SELECT COUNT(*) FROM daily_schedule
            WHERE task_id = ?
        """, (task['id'],)).fetchone()[0]
 
        cursor.execute("""
            INSERT INTO task_history
            (user_id, subject, planned_tasks, completed_tasks,
             delay_days, difficulty, finished_at)
            VALUES (?, ?, ?, ?, 0, NULL, ?)
        """, (task['user_id'], task['subject'], planned, completed, today))
 
        cursor.execute("""
            DELETE FROM daily_schedule
            WHERE task_id = ? AND completed = 0
        """, (task['id'],))
 
        cursor.execute("""
            UPDATE tasks SET status = 'completed'
            WHERE id = ?
        """, (task['id'],))
 
    conn.commit()
    conn.close()
 
    return [dict(task) for task in expired]
 
 
if __name__ == "__main__":
    init_db()
    print("DB 초기화 완료!")
 