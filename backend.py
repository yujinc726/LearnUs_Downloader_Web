from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple
import re

from fastapi import FastAPI, HTTPException, Depends, Header, status
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from learnus_client import LearnUsClient, LearnUsLoginError

app = FastAPI(title="LearnUs Calendar API")

# In-memory session store {token: LearnUsClient}
_SESSIONS: Dict[str, LearnUsClient] = {}

# Course cache {client_id: {course_id: (last_access_time, activities)}}
_COURSE_CACHE: Dict[int, Dict[int, Tuple[float, List]]] = {}


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str


# -------------------------------- Utils ---------------------------------

def get_client(x_auth_token: Optional[str] = Header(None)) -> LearnUsClient:
    if not x_auth_token or x_auth_token not in _SESSIONS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token")
    return _SESSIONS[x_auth_token]


def _get_course_activities_cached(client: LearnUsClient, course_id: int, ttl: int = 900):
    """Return activities from cache if still fresh; otherwise fetch and update cache."""
    import time

    cache = _COURSE_CACHE.setdefault(id(client), {})
    if course_id in cache and time.time() - cache[course_id][0] < ttl:
        return cache[course_id][1]

    activities = client.get_course_activities(course_id)
    cache[course_id] = (time.time(), activities)
    return activities


# -------------------------------- Routes --------------------------------

@app.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    client = LearnUsClient()
    try:
        client.login(payload.username, payload.password)
    except LearnUsLoginError:
        raise HTTPException(status_code=400, detail="로그인에 실패했습니다. 학번/비밀번호를 확인해주세요.")
    except Exception:
        raise HTTPException(status_code=400, detail="로그인 중 알 수 없는 오류가 발생했습니다.")
    token = uuid.uuid4().hex
    _SESSIONS[token] = client
    return {"token": token}


@app.get("/courses")
def get_courses(client: LearnUsClient = Depends(get_client)):
    return client.get_courses()


@app.get("/events")
def get_events(course_id: Optional[int] = None, client: LearnUsClient = Depends(get_client)):
    """Return events aggregated across all courses unless `course_id` is provided."""

    # Determine course set
    if course_id is None:
        course_ids = [c["id"] for c in client.get_courses()]
    else:
        course_ids = [course_id]

    calendar_events: List[dict] = []
    todo_videos: List[dict] = []
    todo_assigns: List[dict] = []

    # Map course id to name for prefixing titles
    course_name_map = {
        c["id"]: re.sub(r"\s*\([^)]*\)$", "", c["name"])
        for c in client.get_courses()
    }

    # Parallel fetch of course activities with simple ThreadPoolExecutor
    def fetch(cid):
        return cid, _get_course_activities_cached(client, cid)

    activities_by_course: Dict[int, List] = {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(16, len(course_ids))) as pool:
        for cid, acts in pool.map(fetch, course_ids):
            activities_by_course[cid] = acts

    # Collect assignments that require detail fetch
    assign_need_detail: List[Tuple[int, int, object]] = []  # (course_id, module_id, activity_ref)

    for cid in course_ids:
        activities = activities_by_course[cid]

        for a in activities:
            if a.type == "assign":
                # Skip if already completed
                if a.completed:
                    continue
                # If due_time already known, we may not need detail fetch
                if a.due_time is None:
                    assign_need_detail.append((cid, a.id, a))
            # nothing else yet

    # Fetch assignment details in parallel
    def fetch_assign(module_id):
        return module_id, client.get_assignment_detail(module_id)

    if assign_need_detail:
        with ThreadPoolExecutor(max_workers=min(16, len(assign_need_detail))) as pool:
            for module_id, detail in pool.map(lambda t: fetch_assign(t[1]), assign_need_detail):
                # find corresponding activity object
                for cid, mid, act in assign_need_detail:
                    if mid == module_id:
                        act.extra.update(detail)
                        if detail.get("due_time") and act.due_time is None:
                            act.due_time = detail["due_time"]
                        break

    # Now build lists
    for cid in course_ids:
        activities = activities_by_course[cid]

        for a in activities:
            full_title = f"[{course_name_map.get(cid, '')}] {a.title}"

            if a.type == "assign":
                # Re-evaluate after details
                if a.completed or a.extra.get("submitted"):
                    continue
                if not a.due_time:
                    continue
                # Skip past deadline
                from datetime import datetime
                now = datetime.now()
                if a.due_time < now:
                    continue
                todo_assigns.append({"id": a.id, "title": full_title, "due": a.due_time.isoformat()})
            elif a.type == "vod":
                if a.completed or not a.due_time:
                    continue
                from datetime import datetime
                now = datetime.now()
                if a.due_time < now:
                    continue
                todo_videos.append({"id": a.id, "title": full_title, "due": a.due_time.isoformat()})

            if a.due_time:
                calendar_events.append({
                    "id": a.id,
                    "title": full_title,
                    "type": a.type,
                    "completed": a.completed,
                    "start": a.due_time.isoformat(),
                    "allDay": True,
                })

    # Sort
    calendar_events.sort(key=lambda x: x["start"])
    todo_videos.sort(key=lambda x: x["due"])
    todo_assigns.sort(key=lambda x: x["due"])

    return {"calendar": calendar_events, "videos": todo_videos, "assignments": todo_assigns}


# Simple health/token validation endpoint
@app.get("/ping")
def ping(client: LearnUsClient = Depends(get_client)):
    return {"ok": True}


# Logout: remove session & cache
@app.post("/logout")
def logout(x_auth_token: Optional[str] = Header(None)):
    if not x_auth_token or x_auth_token not in _SESSIONS:
        raise HTTPException(status_code=401, detail="Invalid token")
    client = _SESSIONS.pop(x_auth_token)
    _COURSE_CACHE.pop(id(client), None)
    return {"ok": True}


# ----------------------------- Static files -----------------------------
# Serve simple frontend (static/index.html etc.) under /
import pathlib


# (mount is added *after* all API routes to ensure they take lower precedence)

# -------------------------------- New Video Endpoints --------------------------------

import subprocess
import shlex
from urllib.parse import quote

@app.get("/videos")
def list_videos(course_id: int, client: LearnUsClient = Depends(get_client)):
    """Return list of VOD (video) activities for the given course."""
    activities = _get_course_activities_cached(client, course_id)
    videos = [
        {
            "id": a.id,
            "title": a.title,
            "completed": a.completed,
            "open": a.open_time.isoformat() if a.open_time else None,
            "due": a.due_time.isoformat() if a.due_time else None,
        }
        for a in activities
        if a.type == "vod"
    ]
    return {"videos": videos}


@app.get("/download/{video_id}.{ext}")
def download_video(video_id: int, ext: str, client: LearnUsClient = Depends(get_client)):
    """Stream MP4/MP3 conversion of the given video module to the user.

    ext must be "mp4" or "mp3".
    """
    if ext not in {"mp4", "mp3"}:
        raise HTTPException(status_code=400, detail="Unsupported extension. Use mp4 or mp3.")

    video_page_url = f"{client.BASE_URL}/mod/vod/view.php?id={video_id}"
    try:
        title, m3u8_url = client.get_video_stream_info(video_page_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Prepare ffmpeg command
    if ext == "mp4":
        codec_args = "-c copy -bsf:a aac_adtstoasc -movflags +faststart -f mp4"
    else:  # mp3
        codec_args = "-vn -c:a libmp3lame -b:a 192k -f mp3"

    cmd = f"ffmpeg -loglevel error -y -i {shlex.quote(m3u8_url)} {codec_args} pipe:1"

    # Spawn subprocess
    process = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if process.stdout is None:
        raise HTTPException(status_code=500, detail="Failed to initiate ffmpeg stream")

    # Streaming generator
    def iterfile():
        try:
            while True:
                chunk = process.stdout.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                yield chunk
        finally:
            process.stdout.close()
            process.kill()

    filename = f"{title}.{ext}"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
    }
    media_type = "video/mp4" if ext == "mp4" else "audio/mpeg"
    return StreamingResponse(iterfile(), media_type=media_type, headers=headers)


# ----------------------------- Static mount (last) -----------------------------
_static_path = pathlib.Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_static_path, html=True), name="static") 