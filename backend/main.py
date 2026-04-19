from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from datetime import date, timedelta
from typing import Optional
import re
import os
import httpx
import json
from database import get_connection, init_db

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

# Gemini API 설정
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyD1-p3XMjvd_uGGUoZSKL1NKzj5dTWTRko")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


def generate_task_names(subject: str, chapters: int, exam_style: str) -> list[str]:
    """
    단원평가형: 1챕터 개념 → 1챕터 문제풀이 → 2챕터 개념 → ... → 기출문제 풀이
    모의고사형: 1챕터 개념 → 2챕터 개념 → ... → 1챕터 문제풀이 → ... → 기출문제 풀이
    과목 제한 없이 입력받은 subject 그대로 사용
    """
    task_names = []

    if exam_style == "단원평가형":
        for i in range(1, chapters + 1):
            task_names.append(f"{subject} {i}챕터 개념")
            task_names.append(f"{subject} {i}챕터 문제풀이")
    else:  # 모의고사형
        for i in range(1, chapters + 1):
            task_names.append(f"{subject} {i}챕터 개념")
        for i in range(1, chapters + 1):
            task_names.append(f"{subject} {i}챕터 문제풀이")

    task_names.append(f"{subject} 기출문제 풀이")
    return task_names


# ─────────────────────────────────────────
# 스케줄 분배 로직 (기존과 동일)
# ─────────────────────────────────────────

def calc_speed_from_history(subject: str, user_id: int, cursor) -> Optional[float]:
    records = cursor.execute("""
        SELECT * FROM task_history
        WHERE user_id = ? AND subject = ?
        ORDER BY finished_at ASC
    """, (user_id, subject)).fetchall()

    if not records:
        return None

    n = len(records)
    weights = list(range(1, n + 1))
    total_weight = sum(weights)

    avg_completion = sum(
        w * (r['completed_tasks'] / r['planned_tasks'])
        for w, r in zip(weights, records)
    ) / total_weight

    avg_delay = sum(r['delay_days'] for r in records) / n

    difficulty_score = {'어려웠어요': -1, '보통이요': 0, '쉬웠어요': 1}
    avg_difficulty = sum(
        difficulty_score.get(r['difficulty'], 0)
        for r in records
    ) / n

    speed = 2.5
    speed += avg_completion * 1.5
    speed -= avg_delay * 0.3
    speed += avg_difficulty * 0.5

    return max(0.0, min(5.0, speed))


def calc_daily_distribution(total_tasks: int, work_days: int, study_pattern: str,
                             force_min_one: bool = False) -> list[int]:
    if study_pattern == "분산형":
        base = total_tasks // work_days
        remainder = total_tasks % work_days
        distribution = [base] * work_days

    else:  # 집중형
        if force_min_one:
            distribution = [1] * work_days
            extra = total_tasks - work_days
            if extra > 0:
                weights = list(range(1, work_days + 1))
                total_weight = sum(weights)
                raw_extra = [extra * w / total_weight for w in weights]
                extra_dist = [int(r) for r in raw_extra]
                leftover = extra - sum(extra_dist)
                i = work_days - 1
                while leftover > 0 and i >= 0:
                    extra_dist[i] += 1
                    leftover -= 1
                    i -= 1
                distribution = [distribution[i] + extra_dist[i] for i in range(work_days)]
            remainder = total_tasks - sum(distribution)
        else:
            weights = list(range(1, work_days + 1))
            total_weight = sum(weights)
            raw = [total_tasks * w / total_weight for w in weights]
            distribution = [int(r) for r in raw]
            remainder = total_tasks - sum(distribution)

    i = work_days - 1
    while remainder > 0 and i >= 0:
        distribution[i] += 1
        remainder -= 1
        i -= 1

    return distribution


def get_buffer_days(total_days: int, speed: float) -> int:
    buffer_ratio = 0.05 + (speed / 5) * 0.25
    return max(1, round(total_days * buffer_ratio))


def assign_to_dates(task_names: list[str], start_date: date, end_date: date,
                    study_pattern: str, max_tasks_per_day: int,
                    user_id: int, cursor,
                    force_min_one: bool = False) -> dict:
    work_days = (end_date - start_date).days
    if work_days <= 0:
        work_days = 1

    total_tasks = len(task_names)
    distribution = calc_daily_distribution(total_tasks, work_days, study_pattern, force_min_one)

    result_dates = []
    overflow_dates = []
    task_index = 0
    current_date = start_date

    for day_idx in range(work_days):
        count = distribution[day_idx]

        if count == 0:
            current_date += timedelta(days=1)
            continue

        date_str = current_date.isoformat()

        existing_count = cursor.execute("""
            SELECT COUNT(*) FROM daily_schedule ds
            JOIN tasks t ON ds.task_id = t.id
            WHERE t.user_id = ? AND ds.date = ?
        """, (user_id, date_str)).fetchone()[0]

        for _ in range(count):
            if task_index >= total_tasks:
                break
            result_dates.append((date_str, task_names[task_index]))
            task_index += 1

        new_total = existing_count + count
        if new_total > max_tasks_per_day:
            overflow_dates.append(date_str)

        current_date += timedelta(days=1)

    while task_index < total_tasks:
        last_date = (end_date - timedelta(days=1)).isoformat()
        result_dates.append((last_date, task_names[task_index]))
        task_index += 1

    return {"dates": result_dates, "overflow_dates": overflow_dates}


def distribute_to_schedule(task_id: int, task_names: list[str], start_date: date,
                            deadline_date: date, speed: float, study_pattern: str,
                            max_tasks_per_day: int, user_id: int, cursor) -> dict:
    total_days = (deadline_date - start_date).days
    if total_days <= 0:
        raise HTTPException(status_code=400, detail="마감일이 오늘보다 이후여야 해요.")

    buffer_days = get_buffer_days(total_days, speed)
    work_end_date = deadline_date - timedelta(days=buffer_days)
    if work_end_date <= start_date:
        work_end_date = start_date + timedelta(days=1)

    assigned = assign_to_dates(
        task_names=task_names,
        start_date=start_date,
        end_date=work_end_date,
        study_pattern=study_pattern,
        max_tasks_per_day=max_tasks_per_day,
        user_id=user_id,
        cursor=cursor
    )

    for date_str, task_name in assigned["dates"]:
        cursor.execute("""
            INSERT INTO daily_schedule (task_id, date, task_name, completed)
            VALUES (?, ?, ?, 0)
        """, (task_id, date_str, task_name))

    return {"overflow_dates": assigned["overflow_dates"]}


# ─────────────────────────────────────────
# Pydantic 모델
# ─────────────────────────────────────────

class SignupData(BaseModel):
    name: str
    password: str
    max_tasks_per_day: int
    study_pattern: str
    exam_style: str

    @validator('name')
    def validate_name(cls, v):
        if not re.match(r'^[a-zA-Z가-힣]+$', v):
            raise ValueError('이름은 띄어쓰기 없이 한글 또는 영어만 가능해요.')
        return v

    @validator('password')
    def validate_password(cls, v):
        if not re.match(r'^\d{4}$', v):
            raise ValueError('비밀번호는 숫자 4자리로 설정해주세요.')
        return v

    @validator('max_tasks_per_day')
    def validate_max_tasks(cls, v):
        if v < 1 or v > 20:
            raise ValueError('하루 최대 task 수는 1~20개 사이로 입력해주세요.')
        return v

    @validator('study_pattern')
    def validate_study_pattern(cls, v):
        if v not in ['집중형', '분산형']:
            raise ValueError('공부 스타일은 집중형 또는 분산형만 가능해요.')
        return v

    @validator('exam_style')
    def validate_exam_style(cls, v):
        if v not in ['단원평가형', '모의고사형']:
            raise ValueError('시험 스타일은 단원평가형 또는 모의고사형만 가능해요.')
        return v


class LoginData(BaseModel):
    name: str
    password: str


class TaskCreateData(BaseModel):
    user_id: int
    subject: str          # 과목 제한 없음 — validator 제거
    chapters: int
    deadline: str

    @validator('chapters')
    def validate_chapters(cls, v):
        if v < 1 or v > 30:
            raise ValueError('챕터 수는 1~30 사이로 입력해주세요.')
        return v

    @validator('deadline')
    def validate_deadline(cls, v):
        try:
            d = date.fromisoformat(v)
        except ValueError:
            raise ValueError('날짜 형식은 YYYY-MM-DD여야 해요.')
        if d <= date.today():
            raise ValueError('마감일은 오늘 이후여야 해요.')
        return v


class SimpleTaskData(BaseModel):
    """일회성 Task — 특정 날짜에 딱 1개 등록"""
    user_id: int
    task_name: str
    date: str             # YYYY-MM-DD

    @validator('date')
    def validate_date(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError('날짜 형식은 YYYY-MM-DD여야 해요.')
        return v


class FeedbackData(BaseModel):
    difficulty: str

    @validator('difficulty')
    def validate_difficulty(cls, v):
        if v not in ['어려웠어요', '보통이요', '쉬웠어요']:
            raise ValueError('난이도는 어려웠어요 / 보통이요 / 쉬웠어요 중 하나여야 해요.')
        return v


class ChatData(BaseModel):
    user_id: int
    message: str


# ─────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────

# 회원가입
@app.post("/signup")
def signup(data: SignupData):
    conn = get_connection()
    cursor = conn.cursor()

    existing = cursor.execute(
        "SELECT id FROM users WHERE name = ?", (data.name,)
    ).fetchone()

    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="이미 사용 중인 이름이에요.")

    cursor.execute("""
        INSERT INTO users (name, password, max_tasks_per_day, study_pattern, exam_style)
        VALUES (?, ?, ?, ?, ?)
    """, (data.name, data.password, data.max_tasks_per_day, data.study_pattern, data.exam_style))

    conn.commit()
    user_id = cursor.lastrowid
    conn.close()

    return {"message": "회원가입 완료!", "user_id": user_id}


# 로그인
@app.post("/login")
def login(data: LoginData):
    conn = get_connection()
    cursor = conn.cursor()

    user = cursor.execute("""
        SELECT * FROM users WHERE name = ? AND password = ?
    """, (data.name, data.password)).fetchone()

    conn.close()

    if not user:
        raise HTTPException(status_code=401, detail="비밀번호가 맞지 않아요.")

    user = dict(user)

    return {
        "message": "로그인 성공!",
        "user_id": user["id"],
        "name": user["name"],
        "max_tasks_per_day": user["max_tasks_per_day"],
        "study_pattern": user["study_pattern"],
        "exam_style": user["exam_style"]
    }


# 장기 프로젝트 task 생성 + 자동 분배
@app.post("/tasks")
def create_task(data: TaskCreateData):
    conn = get_connection()
    cursor = conn.cursor()

    user = cursor.execute(
        "SELECT * FROM users WHERE id = ?", (data.user_id,)
    ).fetchone()

    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")

    speed = calc_speed_from_history(data.subject, data.user_id, cursor)

    if speed is None:
        profile = cursor.execute("""
            SELECT study_speed FROM subject_profile
            WHERE user_id = ? AND subject = ?
        """, (data.user_id, data.subject)).fetchone()
        if profile:
            speed = float(profile['study_speed'])

    if speed is None:
        speed = 2.5

    task_names = generate_task_names(data.subject, data.chapters, user['exam_style'])

    title = f"{data.subject} {data.chapters}챕터까지"
    cursor.execute("""
        INSERT INTO tasks (user_id, subject, title, deadline, status)
        VALUES (?, ?, ?, ?, 'ongoing')
    """, (data.user_id, data.subject, title, data.deadline))
    task_id = cursor.lastrowid

    result = distribute_to_schedule(
        task_id=task_id,
        task_names=task_names,
        start_date=date.today(),
        deadline_date=date.fromisoformat(data.deadline),
        speed=speed,
        study_pattern=user['study_pattern'],
        max_tasks_per_day=user['max_tasks_per_day'],
        user_id=data.user_id,
        cursor=cursor
    )

    schedule = cursor.execute("""
        SELECT date, task_name FROM daily_schedule
        WHERE task_id = ?
        ORDER BY date ASC, id ASC
    """, (task_id,)).fetchall()

    conn.commit()
    conn.close()

    return {
        "message": "task 생성 완료!",
        "task_id": task_id,
        "total_tasks": len(task_names),
        "speed_used": round(speed, 2),
        "schedule": [{"date": s["date"], "task_name": s["task_name"]} for s in schedule],
        "overflow_dates": result["overflow_dates"]
    }


# ★ 일회성 Task 생성 (특정 날짜에 1개만 등록)
@app.post("/tasks/simple")
def create_simple_task(data: SimpleTaskData):
    conn = get_connection()
    cursor = conn.cursor()

    user = cursor.execute(
        "SELECT * FROM users WHERE id = ?", (data.user_id,)
    ).fetchone()

    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")

    # tasks 테이블에 simple 타입으로 저장
    cursor.execute("""
        INSERT INTO tasks (user_id, subject, title, deadline, status)
        VALUES (?, '일회성', ?, ?, 'simple')
    """, (data.user_id, data.task_name, data.date))
    task_id = cursor.lastrowid

    # daily_schedule에 해당 날짜에 1개만 등록
    cursor.execute("""
        INSERT INTO daily_schedule (task_id, date, task_name, completed)
        VALUES (?, ?, ?, 0)
    """, (task_id, data.date, data.task_name))

    conn.commit()
    conn.close()

    return {
        "message": "일회성 task 등록 완료!",
        "task_id": task_id,
        "task_name": data.task_name,
        "date": data.date
    }


# task 목록 조회
@app.get("/tasks/{user_id}")
def get_tasks(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    tasks = cursor.execute("""
        SELECT * FROM tasks WHERE user_id = ? ORDER BY deadline ASC
    """, (user_id,)).fetchall()

    result = []
    for task in tasks:
        planned = cursor.execute("""
            SELECT COUNT(*) FROM daily_schedule WHERE task_id = ?
        """, (task['id'],)).fetchone()[0]

        completed = cursor.execute("""
            SELECT COUNT(*) FROM daily_schedule WHERE task_id = ? AND completed = 1
        """, (task['id'],)).fetchone()[0]

        completion_rate = round(completed / planned, 2) if planned > 0 else 0.0

        result.append({
            "task_id": task['id'],
            "subject": task['subject'],
            "title": task['title'],
            "deadline": task['deadline'],
            "status": task['status'],
            "planned_tasks": planned,
            "completed_tasks": completed,
            "completion_rate": completion_rate
        })

    conn.close()
    return result


# task 삭제
@app.delete("/tasks/{task_id}")
def delete_task(task_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    task = cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task를 찾을 수 없어요.")

    cursor.execute("DELETE FROM daily_schedule WHERE task_id = ?", (task_id,))
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    conn.commit()
    conn.close()
    return {"message": "task 삭제 완료!"}


# 날짜 범위 스케줄 조회
@app.get("/schedule/{user_id}/range")
def get_schedule_by_range(user_id: int, start: str, end: str):
    conn = get_connection()
    cursor = conn.cursor()

    schedules = cursor.execute("""
        SELECT ds.date,
               COUNT(*) as total,
               SUM(ds.completed) as completed
        FROM daily_schedule ds
        JOIN tasks t ON ds.task_id = t.id
        WHERE t.user_id = ? AND ds.date BETWEEN ? AND ?
        GROUP BY ds.date
        ORDER BY ds.date ASC
    """, (user_id, start, end)).fetchall()

    conn.close()
    return [dict(s) for s in schedules]


# 특정 날짜 스케줄 조회
@app.get("/schedule/{user_id}/{date_str}")
def get_schedule_by_date(user_id: int, date_str: str):
    conn = get_connection()
    cursor = conn.cursor()

    schedules = cursor.execute("""
        SELECT ds.id, ds.task_id, ds.date, ds.task_name, ds.completed,
               t.subject, t.deadline, t.status
        FROM daily_schedule ds
        JOIN tasks t ON ds.task_id = t.id
        WHERE t.user_id = ? AND ds.date = ?
        ORDER BY ds.id ASC
    """, (user_id, date_str)).fetchall()

    conn.close()
    return [dict(s) for s in schedules]


# 완료 체크 토글
@app.patch("/schedule/{schedule_id}/complete")
def complete_schedule(schedule_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    schedule = cursor.execute(
        "SELECT id, completed FROM daily_schedule WHERE id = ?", (schedule_id,)
    ).fetchone()

    if not schedule:
        conn.close()
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없어요.")

    new_status = 0 if schedule['completed'] == 1 else 1
    cursor.execute("""
        UPDATE daily_schedule SET completed = ? WHERE id = ?
    """, (new_status, schedule_id))

    conn.commit()
    conn.close()
    return {"message": "완료 상태 변경!", "completed": new_status}


# 미완료 task 재배분
@app.post("/redistribute/{user_id}")
def redistribute(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    user = cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")

    today = date.today()
    today_str = today.isoformat()

    # 일회성 task는 재배분 제외, ongoing 장기 프로젝트만 재배분
    ongoing_tasks = cursor.execute("""
        SELECT * FROM tasks
        WHERE user_id = ? AND status = 'ongoing'
        ORDER BY deadline ASC
    """, (user_id,)).fetchall()

    if not ongoing_tasks:
        conn.close()
        return {"message": "진행 중인 task가 없어요.", "redistributed": 0}

    total_redistributed = 0
    all_overflow_dates = []
    buffer_exhausted_tasks = []

    for task in ongoing_tasks:
        task_id = task['id']
        deadline_date = date.fromisoformat(task['deadline'])

        if deadline_date <= today:
            continue

        incomplete_names = [r['task_name'] for r in cursor.execute("""
            SELECT task_name FROM daily_schedule
            WHERE task_id = ? AND completed = 0 AND date < ?
            ORDER BY date ASC, id ASC
        """, (task_id, today_str)).fetchall()]

        if not incomplete_names:
            continue

        upcoming_names = [r['task_name'] for r in cursor.execute("""
            SELECT task_name FROM daily_schedule
            WHERE task_id = ? AND completed = 0 AND date >= ?
            ORDER BY date ASC, id ASC
        """, (task_id, today_str)).fetchall()]

        all_remaining = incomplete_names + upcoming_names

        cursor.execute("""
            DELETE FROM daily_schedule
            WHERE task_id = ? AND completed = 0
        """, (task_id,))

        profile = cursor.execute("""
            SELECT study_speed FROM subject_profile
            WHERE user_id = ? AND subject = ?
        """, (user_id, task['subject'])).fetchone()
        speed = float(profile['study_speed']) if profile else 2.5

        total_days = (deadline_date - today).days
        buffer_days = get_buffer_days(total_days, speed)
        work_end_date = deadline_date - timedelta(days=buffer_days)

        if work_end_date <= today:
            work_end_date = deadline_date
            force_min_one = True
            buffer_exhausted_tasks.append({
                "task_id": task_id,
                "subject": task['subject'],
                "title": task['title'],
                "deadline": task['deadline'],
                "remaining_tasks": len(all_remaining)
            })
        else:
            force_min_one = False

        assigned = assign_to_dates(
            task_names=all_remaining,
            start_date=today,
            end_date=work_end_date,
            study_pattern=user['study_pattern'],
            max_tasks_per_day=user['max_tasks_per_day'],
            user_id=user_id,
            cursor=cursor,
            force_min_one=force_min_one
        )

        for date_str, task_name in assigned["dates"]:
            cursor.execute("""
                INSERT INTO daily_schedule (task_id, date, task_name, completed)
                VALUES (?, ?, ?, 0)
            """, (task_id, date_str, task_name))

        for d in assigned["overflow_dates"]:
            if d not in all_overflow_dates:
                all_overflow_dates.append(d)

        total_redistributed += len(all_remaining)

    conn.commit()
    conn.close()

    response = {
        "message": f"{total_redistributed}개 task 재배분 완료!",
        "redistributed": total_redistributed,
        "overflow_dates": all_overflow_dates,
    }

    if buffer_exhausted_tasks:
        response["warning"] = "현재 공부 속도로는 일정을 완료하기 어려워요."
        response["buffer_exhausted_tasks"] = buffer_exhausted_tasks

    return response


# 마감일 지난 task 처리
@app.post("/check-expired/{user_id}")
def check_expired(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    today = date.today().isoformat()

    # ongoing 장기 프로젝트만 만료 체크 (일회성 task는 제외)
    expired = cursor.execute("""
        SELECT * FROM tasks
        WHERE user_id = ? AND deadline < ? AND status = 'ongoing'
    """, (user_id, today)).fetchall()

    expired_list = []

    for task in expired:
        completed = cursor.execute("""
            SELECT COUNT(*) FROM daily_schedule
            WHERE task_id = ? AND completed = 1
        """, (task['id'],)).fetchone()[0]

        planned = cursor.execute("""
            SELECT COUNT(*) FROM daily_schedule WHERE task_id = ?
        """, (task['id'],)).fetchone()[0]

        cursor.execute("""
            INSERT INTO task_history
            (user_id, subject, planned_tasks, completed_tasks, delay_days, difficulty, finished_at)
            VALUES (?, ?, ?, ?, 0, NULL, ?)
        """, (task['user_id'], task['subject'], planned, completed, today))

        cursor.execute("""
            DELETE FROM daily_schedule WHERE task_id = ? AND completed = 0
        """, (task['id'],))

        cursor.execute("""
            UPDATE tasks SET status = 'completed' WHERE id = ?
        """, (task['id'],))

        expired_list.append({
            "task_id": task['id'],
            "subject": task['subject'],
            "title": task['title'],
            "deadline": task['deadline'],
            "planned_tasks": planned,
            "completed_tasks": completed
        })

    conn.commit()
    conn.close()

    return {
        "message": f"{len(expired_list)}개 task가 마감 처리됐어요.",
        "expired_tasks": expired_list
    }


# 난이도 피드백 저장
@app.patch("/tasks/{task_id}/feedback")
def save_feedback(task_id: int, data: FeedbackData):
    conn = get_connection()
    cursor = conn.cursor()

    task = cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task를 찾을 수 없어요.")

    cursor.execute("""
        UPDATE task_history SET difficulty = ?
        WHERE user_id = ? AND subject = ? AND difficulty IS NULL
        ORDER BY finished_at DESC
        LIMIT 1
    """, (data.difficulty, task['user_id'], task['subject']))

    new_speed = calc_speed_from_history(task['subject'], task['user_id'], cursor)
    if new_speed is not None:
        cursor.execute("""
            UPDATE subject_profile SET study_speed = ?, data_count = data_count + 1
            WHERE user_id = ? AND subject = ?
        """, (round(new_speed), task['user_id'], task['subject']))

    conn.commit()
    conn.close()
    return {
        "message": "피드백 저장 완료!",
        "updated_speed": round(new_speed, 2) if new_speed else None
    }


# ★ 챗봇 엔드포인트 (Gemini 연동)
@app.post("/chat")
async def chat(data: ChatData):
    conn = get_connection()
    cursor = conn.cursor()

    user = cursor.execute(
        "SELECT * FROM users WHERE id = ?", (data.user_id,)
    ).fetchone()

    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")

    today_str = date.today().isoformat()

    # Gemini에게 보낼 프롬프트
    prompt = f"""
오늘 날짜는 {today_str}입니다.
사용자 메시지: "{data.message}"

위 메시지를 분석해서 아래 JSON 형식 중 하나로만 응답하세요. 다른 텍스트는 절대 포함하지 마세요.

장기 프로젝트 조건: 공부 과목 + 챕터/범위 + 시험/마감 언급이 있는 경우
일회성 Task 조건: 특정 날짜에 한 번만 할 일인 경우
일반 대화 조건: task 등록과 관련 없는 메시지

1. 장기 프로젝트인 경우:
{{"type": "project", "subject": "과목명", "chapters": 챕터수(숫자), "deadline": "YYYY-MM-DD", "reply": "챗봇 답변 메시지"}}

2. 일회성 Task인 경우:
{{"type": "simple", "task_name": "할일 내용", "date": "YYYY-MM-DD", "reply": "챗봇 답변 메시지"}}

3. 일반 대화인 경우:
{{"type": "chat", "reply": "챗봇 답변 메시지"}}

주의사항:
- 날짜는 반드시 YYYY-MM-DD 형식
- "다음주 월요일" 같은 상대적 날짜도 {today_str} 기준으로 계산해서 절대 날짜로 변환
- chapters는 반드시 숫자(정수)
- reply는 친근한 한국어로 작성
- JSON만 응답, 마크다운 코드블록 없이
"""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(
                f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1}
                }
            )
            res.raise_for_status()
            gemini_data = res.json()
            raw_text = gemini_data["candidates"][0]["content"]["parts"][0]["text"].strip()

            # JSON 파싱
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw_text)

    except Exception as e:
        conn.close()
        return {"reply": f"오류: {str(e)}", "type": "error"}

    task_result = None

    # 장기 프로젝트 자동 등록
    if parsed.get("type") == "project":
        try:
            deadline_date = date.fromisoformat(parsed["deadline"])
            if deadline_date <= date.today():
                conn.close()
                return {"reply": "마감일은 오늘 이후여야 해요!", "type": "error"}

            speed = calc_speed_from_history(parsed["subject"], data.user_id, cursor)
            if speed is None:
                speed = 2.5

            task_names = generate_task_names(
                parsed["subject"], int(parsed["chapters"]), user["exam_style"]
            )
            title = f"{parsed['subject']} {parsed['chapters']}챕터까지"

            cursor.execute("""
                INSERT INTO tasks (user_id, subject, title, deadline, status)
                VALUES (?, ?, ?, ?, 'ongoing')
            """, (data.user_id, parsed["subject"], title, parsed["deadline"]))
            task_id = cursor.lastrowid

            distribute_to_schedule(
                task_id=task_id,
                task_names=task_names,
                start_date=date.today(),
                deadline_date=deadline_date,
                speed=speed,
                study_pattern=user["study_pattern"],
                max_tasks_per_day=user["max_tasks_per_day"],
                user_id=data.user_id,
                cursor=cursor
            )
            conn.commit()
            task_result = {"type": "project", "task_id": task_id, "total_tasks": len(task_names)}

        except Exception as e:
            conn.close()
            return {"reply": f"등록 중 오류가 발생했어요: {str(e)}", "type": "error"}

    # 일회성 Task 자동 등록
    elif parsed.get("type") == "simple":
        try:
            cursor.execute("""
                INSERT INTO tasks (user_id, subject, title, deadline, status)
                VALUES (?, '일회성', ?, ?, 'simple')
            """, (data.user_id, parsed["task_name"], parsed["date"]))
            task_id = cursor.lastrowid

            cursor.execute("""
                INSERT INTO daily_schedule (task_id, date, task_name, completed)
                VALUES (?, ?, ?, 0)
            """, (task_id, parsed["date"], parsed["task_name"]))
            conn.commit()
            task_result = {"type": "simple", "task_id": task_id}

        except Exception as e:
            conn.close()
            return {"reply": f"등록 중 오류가 발생했어요: {str(e)}", "type": "error"}

    conn.close()
    return {
        "reply": parsed.get("reply", "처리됐어요!"),
        "type": parsed.get("type", "chat"),
        "task_result": task_result
    }