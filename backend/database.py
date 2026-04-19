import psycopg2
import psycopg2.extras
import os
from datetime import date

def get_connection():
    conn = psycopg2.connect(
        os.environ.get("DATABASE_URL"),
        cursor_factory=psycopg2.extras.RealDictCursor
    )
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT,
            password TEXT,
            max_tasks_per_day INTEGER,
            study_pattern TEXT,
            exam_style TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subject_profile (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            subject TEXT,
            study_speed INTEGER,
            data_count INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            subject TEXT,
            title TEXT,
            deadline TEXT,
            status TEXT DEFAULT 'ongoing'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_schedule (
            id SERIAL PRIMARY KEY,
            task_id INTEGER,
            date TEXT,
            task_name TEXT,
            completed INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id SERIAL PRIMARY KEY,
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
    conn = get_connection()
    cursor = conn.cursor()
    today = date.today().isoformat()

    cursor.execute("""
        SELECT * FROM tasks WHERE deadline < %s AND status = 'ongoing'
    """, (today,))
    expired = cursor.fetchall()

    for task in expired:
        cursor.execute("SELECT COUNT(*) as count FROM daily_schedule WHERE task_id = %s AND completed = 1", (task['id'],))
        completed = cursor.fetchone()['count']
        cursor.execute("SELECT COUNT(*) as count FROM daily_schedule WHERE task_id = %s", (task['id'],))
        planned = cursor.fetchone()['count']

        cursor.execute("""
            INSERT INTO task_history (user_id, subject, planned_tasks, completed_tasks, delay_days, difficulty, finished_at)
            VALUES (%s, %s, %s, %s, 0, NULL, %s)
        """, (task['user_id'], task['subject'], planned, completed, today))

        cursor.execute("DELETE FROM daily_schedule WHERE task_id = %s AND completed = 0", (task['id'],))
        cursor.execute("UPDATE tasks SET status = 'completed' WHERE id = %s", (task['id'],))

    conn.commit()
    conn.close()
    return [dict(task) for task in expired]


if __name__ == "__main__":
    init_db()
    print("DB 초기화 완료!")