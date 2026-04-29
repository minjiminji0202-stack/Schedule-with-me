from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from datetime import date, timedelta
from typing import Optional
import re
import os
import httpx
import json
from backend.database import get_connection, init_db

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "여기에_API_키_입력")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


def generate_task_names(subject: str, chapters: int, exam_style: str) -> list[str]:
    task_names = []
    if exam_style == "단원평가형":
        for i in range(1, chapters + 1):
            task_names.append(f"{subject} {i}챕터 개념")
            task_names.append(f"{subject} {i}챕터 문제풀이")
    else:
        for i in range(1, chapters + 1):
            task_names.append(f"{subject} {i}챕터 개념")
        for i in range(1, chapters + 1):
            task_names.append(f"{subject} {i}챕터 문제풀이")
    task_names.append(f"{subject} 기출문제 풀이")
    return task_names


def calc_speed_from_history(subject: str, user_id: int, cursor) -> Optional[float]:
    cursor.execute("""
        SELECT * FROM task_history WHERE user_id = %s AND subject = %s ORDER BY finished_at ASC
    """, (user_id, subject))
    records = cursor.fetchall()
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
    avg_difficulty = sum(difficulty_score.get(r['difficulty'], 0) for r in records) / n
    speed = 2.5
    speed += avg_completion * 1.5
    speed -= avg_delay * 0.3
    speed += avg_difficulty * 0.5
    return max(0.0, min(5.0, speed))


def calc_daily_distribution(total_tasks: int, work_days: int, study_pattern: str, force_min_one: bool = False) -> list[int]:
    if study_pattern == "분산형":
        base = total_tasks // work_days
        remainder = total_tasks % work_days
        distribution = [base] * work_days
    else:
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


def assign_to_dates(task_names, start_date, end_date, study_pattern, max_tasks_per_day, user_id, cursor, force_min_one=False):
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
        cursor.execute("""
            SELECT COUNT(*) as count FROM daily_schedule ds
            JOIN tasks t ON ds.task_id = t.id
            WHERE t.user_id = %s AND ds.date = %s
        """, (user_id, date_str))
        existing_count = cursor.fetchone()['count']
        for _ in range(count):
            if task_index >= total_tasks:
                break
            result_dates.append((date_str, task_names[task_index]))
            task_index += 1
        if existing_count + count > max_tasks_per_day:
            overflow_dates.append(date_str)
        current_date += timedelta(days=1)
    while task_index < total_tasks:
        last_date = (end_date - timedelta(days=1)).isoformat()
        result_dates.append((last_date, task_names[task_index]))
        task_index += 1
    return {"dates": result_dates, "overflow_dates": overflow_dates}


def distribute_to_schedule(task_id, task_names, start_date, deadline_date, speed, study_pattern, max_tasks_per_day, user_id, cursor):
    total_days = (deadline_date - start_date).days
    if total_days <= 0:
        raise HTTPException(status_code=400, detail="마감일이 오늘보다 이후여야 해요.")
    buffer_days = get_buffer_days(total_days, speed)
    work_end_date = deadline_date - timedelta(days=buffer_days)
    if work_end_date <= start_date:
        work_end_date = start_date + timedelta(days=1)
    assigned = assign_to_dates(task_names, start_date, work_end_date, study_pattern, max_tasks_per_day, user_id, cursor)
    for date_str, task_name in assigned["dates"]:
        cursor.execute("""
            INSERT INTO daily_schedule (task_id, date, task_name, completed) VALUES (%s, %s, %s, 0)
        """, (task_id, date_str, task_name))
    return {"overflow_dates": assigned["overflow_dates"]}


# ── Pydantic 모델 ──────────────────────────────────────

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
    subject: str
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
    user_id: int
    task_name: str
    date: str

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


# ── 엔드포인트 ──────────────────────────────────────────

@app.post("/signup")
def signup(data: SignupData):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE name = %s", (data.name,))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="이미 사용 중인 이름이에요.")
    cursor.execute("""
        INSERT INTO users (name, password, max_tasks_per_day, study_pattern, exam_style)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """, (data.name, data.password, data.max_tasks_per_day, data.study_pattern, data.exam_style))
    user_id = cursor.fetchone()['id']
    conn.commit()
    conn.close()
    return {"message": "회원가입 완료!", "user_id": user_id}


@app.post("/login")
def login(data: LoginData):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE name = %s AND password = %s", (data.name, data.password))
    user = cursor.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="이름 또는 비밀번호가 맞지 않아요.")
    return {
        "message": "로그인 성공!",
        "user_id": user["id"],
        "name": user["name"],
        "max_tasks_per_day": user["max_tasks_per_day"],
        "study_pattern": user["study_pattern"],
        "exam_style": user["exam_style"]
    }


@app.post("/tasks")
def create_task(data: TaskCreateData):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = %s", (data.user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")
    speed = calc_speed_from_history(data.subject, data.user_id, cursor)
    if speed is None:
        cursor.execute("SELECT study_speed FROM subject_profile WHERE user_id = %s AND subject = %s", (data.user_id, data.subject))
        profile = cursor.fetchone()
        if profile:
            speed = float(profile['study_speed'])
    if speed is None:
        speed = 2.5
    task_names = generate_task_names(data.subject, data.chapters, user['exam_style'])
    title = f"{data.subject} {data.chapters}챕터까지"
    cursor.execute("""
        INSERT INTO tasks (user_id, subject, title, deadline, status) VALUES (%s, %s, %s, %s, 'ongoing') RETURNING id
    """, (data.user_id, data.subject, title, data.deadline))
    task_id = cursor.fetchone()['id']
    result = distribute_to_schedule(task_id, task_names, date.today(), date.fromisoformat(data.deadline), speed, user['study_pattern'], user['max_tasks_per_day'], data.user_id, cursor)
    cursor.execute("SELECT date, task_name FROM daily_schedule WHERE task_id = %s ORDER BY date ASC, id ASC", (task_id,))
    schedule = cursor.fetchall()
    conn.commit()
    conn.close()
    return {
        "message": "task 생성 완료!", "task_id": task_id, "total_tasks": len(task_names),
        "speed_used": round(speed, 2),
        "schedule": [{"date": s["date"], "task_name": s["task_name"]} for s in schedule],
        "overflow_dates": result["overflow_dates"]
    }


@app.post("/tasks/simple")
def create_simple_task(data: SimpleTaskData):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = %s", (data.user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")
    cursor.execute("""
        INSERT INTO tasks (user_id, subject, title, deadline, status) VALUES (%s, '일회성', %s, %s, 'simple') RETURNING id
    """, (data.user_id, data.task_name, data.date))
    task_id = cursor.fetchone()['id']
    cursor.execute("INSERT INTO daily_schedule (task_id, date, task_name, completed) VALUES (%s, %s, %s, 0)", (task_id, data.date, data.task_name))
    conn.commit()
    conn.close()
    return {"message": "일회성 task 등록 완료!", "task_id": task_id, "task_name": data.task_name, "date": data.date}


@app.get("/tasks/{user_id}")
def get_tasks(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE user_id = %s ORDER BY deadline ASC", (user_id,))
    tasks = cursor.fetchall()
    result = []
    for task in tasks:
        cursor.execute("SELECT COUNT(*) as count FROM daily_schedule WHERE task_id = %s", (task['id'],))
        planned = cursor.fetchone()['count']
        cursor.execute("SELECT COUNT(*) as count FROM daily_schedule WHERE task_id = %s AND completed = 1", (task['id'],))
        completed = cursor.fetchone()['count']
        result.append({
            "task_id": task['id'], "subject": task['subject'], "title": task['title'],
            "deadline": task['deadline'], "status": task['status'],
            "planned_tasks": planned, "completed_tasks": completed,
            "completion_rate": round(completed / planned, 2) if planned > 0 else 0.0
        })
    conn.close()
    return result


@app.delete("/tasks/{task_id}")
def delete_task(task_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM tasks WHERE id = %s", (task_id,))
    task = cursor.fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task를 찾을 수 없어요.")
    cursor.execute("DELETE FROM daily_schedule WHERE task_id = %s", (task_id,))
    cursor.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    conn.commit()
    conn.close()
    return {"message": "task 삭제 완료!"}


@app.get("/schedule/{user_id}/range")
def get_schedule_by_range(user_id: int, start: str, end: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ds.date, COUNT(*) as total, SUM(ds.completed) as completed
        FROM daily_schedule ds JOIN tasks t ON ds.task_id = t.id
        WHERE t.user_id = %s AND ds.date BETWEEN %s AND %s
        GROUP BY ds.date ORDER BY ds.date ASC
    """, (user_id, start, end))
    schedules = cursor.fetchall()
    conn.close()
    return [dict(s) for s in schedules]


@app.get("/schedule/{user_id}/{date_str}")
def get_schedule_by_date(user_id: int, date_str: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ds.id, ds.task_id, ds.date, ds.task_name, ds.completed, t.subject, t.deadline, t.status
        FROM daily_schedule ds JOIN tasks t ON ds.task_id = t.id
        WHERE t.user_id = %s AND ds.date = %s ORDER BY ds.id ASC
    """, (user_id, date_str))
    schedules = cursor.fetchall()
    conn.close()
    return [dict(s) for s in schedules]


@app.patch("/schedule/{schedule_id}/complete")
def complete_schedule(schedule_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, completed FROM daily_schedule WHERE id = %s", (schedule_id,))
    schedule = cursor.fetchone()
    if not schedule:
        conn.close()
        raise HTTPException(status_code=404, detail="스케줄을 찾을 수 없어요.")
    new_status = 0 if schedule['completed'] == 1 else 1
    cursor.execute("UPDATE daily_schedule SET completed = %s WHERE id = %s", (new_status, schedule_id))
    conn.commit()
    conn.close()
    return {"message": "완료 상태 변경!", "completed": new_status}


@app.post("/redistribute/{user_id}")
def redistribute(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")
    today = date.today()
    today_str = today.isoformat()
    cursor.execute("SELECT * FROM tasks WHERE user_id = %s AND status = 'ongoing' ORDER BY deadline ASC", (user_id,))
    ongoing_tasks = cursor.fetchall()
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
        cursor.execute("SELECT task_name FROM daily_schedule WHERE task_id = %s AND completed = 0 AND date <= %s ORDER BY date ASC, id ASC", (task_id, today_str))
        incomplete_names = [r['task_name'] for r in cursor.fetchall()]
        if not incomplete_names:
            continue
        cursor.execute("SELECT task_name FROM daily_schedule WHERE task_id = %s AND completed = 0 AND date >= %s ORDER BY date ASC, id ASC", (task_id, today_str))
        upcoming_names = [r['task_name'] for r in cursor.fetchall()]
        all_remaining = incomplete_names + upcoming_names
        cursor.execute("DELETE FROM daily_schedule WHERE task_id = %s AND completed = 0", (task_id,))
        cursor.execute("SELECT study_speed FROM subject_profile WHERE user_id = %s AND subject = %s", (user_id, task['subject']))
        profile = cursor.fetchone()
        speed = float(profile['study_speed']) if profile else 2.5
        total_days = (deadline_date - today).days
        buffer_days = get_buffer_days(total_days, speed)
        work_end_date = deadline_date - timedelta(days=buffer_days)
        if work_end_date <= today:
            work_end_date = deadline_date
            force_min_one = True
            buffer_exhausted_tasks.append({"task_id": task_id, "subject": task['subject'], "title": task['title'], "deadline": task['deadline'], "remaining_tasks": len(all_remaining)})
        else:
            force_min_one = False
        assigned = assign_to_dates(all_remaining, today, work_end_date, user['study_pattern'], user['max_tasks_per_day'], user_id, cursor, force_min_one)
        for date_str, task_name in assigned["dates"]:
            cursor.execute("INSERT INTO daily_schedule (task_id, date, task_name, completed) VALUES (%s, %s, %s, 0)", (task_id, date_str, task_name))
        for d in assigned["overflow_dates"]:
            if d not in all_overflow_dates:
                all_overflow_dates.append(d)
        total_redistributed += len(all_remaining)
    conn.commit()
    conn.close()
    response = {"message": f"{total_redistributed}개 task 재배분 완료!", "redistributed": total_redistributed, "overflow_dates": all_overflow_dates}
    if buffer_exhausted_tasks:
        response["warning"] = "현재 공부 속도로는 일정을 완료하기 어려워요."
        response["buffer_exhausted_tasks"] = buffer_exhausted_tasks
    return response


@app.post("/check-expired/{user_id}")
def check_expired(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute("SELECT * FROM tasks WHERE user_id = %s AND deadline < %s AND status = 'ongoing'", (user_id, today))
    expired = cursor.fetchall()
    expired_list = []
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
        expired_list.append({"task_id": task['id'], "subject": task['subject'], "title": task['title'], "deadline": task['deadline'], "planned_tasks": planned, "completed_tasks": completed})
    conn.commit()
    conn.close()
    return {"message": f"{len(expired_list)}개 task가 마감 처리됐어요.", "expired_tasks": expired_list}


@app.patch("/tasks/{task_id}/feedback")
def save_feedback(task_id: int, data: FeedbackData):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
    task = cursor.fetchone()
    if not task:
        conn.close()
        raise HTTPException(status_code=404, detail="task를 찾을 수 없어요.")
    cursor.execute("""
        UPDATE task_history SET difficulty = %s
        WHERE user_id = %s AND subject = %s AND difficulty IS NULL
        ORDER BY finished_at DESC LIMIT 1
    """, (data.difficulty, task['user_id'], task['subject']))
    new_speed = calc_speed_from_history(task['subject'], task['user_id'], cursor)
    if new_speed is not None:
        cursor.execute("UPDATE subject_profile SET study_speed = %s, data_count = data_count + 1 WHERE user_id = %s AND subject = %s", (round(new_speed), task['user_id'], task['subject']))
    conn.commit()
    conn.close()
    return {"message": "피드백 저장 완료!", "updated_speed": round(new_speed, 2) if new_speed else None}


@app.post("/chat")
async def chat(data: ChatData):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = %s", (data.user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="유저를 찾을 수 없어요.")
    today_str = date.today().isoformat()
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
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
            )
            res.raise_for_status()
            gemini_data = res.json()
            raw_text = gemini_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw_text)
    except Exception as e:
        conn.close()
        return {"reply": f"오류: {str(e)}", "type": "error"}

    task_result = None
    if parsed.get("type") == "project":
        try:
            deadline_date = date.fromisoformat(parsed["deadline"])
            if deadline_date <= date.today():
                conn.close()
                return {"reply": "마감일은 오늘 이후여야 해요!", "type": "error"}
            speed = calc_speed_from_history(parsed["subject"], data.user_id, cursor)
            if speed is None:
                speed = 2.5
            task_names = generate_task_names(parsed["subject"], int(parsed["chapters"]), user["exam_style"])
            title = f"{parsed['subject']} {parsed['chapters']}챕터까지"
            cursor.execute("INSERT INTO tasks (user_id, subject, title, deadline, status) VALUES (%s, %s, %s, %s, 'ongoing') RETURNING id", (data.user_id, parsed["subject"], title, parsed["deadline"]))
            task_id = cursor.fetchone()['id']
            distribute_to_schedule(task_id, task_names, date.today(), deadline_date, speed, user["study_pattern"], user["max_tasks_per_day"], data.user_id, cursor)
            conn.commit()
            task_result = {"type": "project", "task_id": task_id, "total_tasks": len(task_names)}
        except Exception as e:
            conn.close()
            return {"reply": f"등록 중 오류가 발생했어요: {str(e)}", "type": "error"}
    elif parsed.get("type") == "simple":
        try:
            cursor.execute("INSERT INTO tasks (user_id, subject, title, deadline, status) VALUES (%s, '일회성', %s, %s, 'simple') RETURNING id", (data.user_id, parsed["task_name"], parsed["date"]))
            task_id = cursor.fetchone()['id']
            cursor.execute("INSERT INTO daily_schedule (task_id, date, task_name, completed) VALUES (%s, %s, %s, 0)", (task_id, parsed["date"], parsed["task_name"]))
            conn.commit()
            task_result = {"type": "simple", "task_id": task_id}
        except Exception as e:
            conn.close()
            return {"reply": f"등록 중 오류가 발생했어요: {str(e)}", "type": "error"}

    conn.close()
    return {"reply": parsed.get("reply", "처리됐어요!"), "type": parsed.get("type", "chat"), "task_result": task_result}