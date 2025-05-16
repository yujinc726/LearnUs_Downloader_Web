from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple
import re

from fastapi import FastAPI, HTTPException, Depends, Header, status, UploadFile, File, Query
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from learnus_client import LearnUsClient, LearnUsLoginError

app = FastAPI(title="LearnUs Calendar API")

# Session store now may contain either a LearnUsClient (for normal users) or None (for guest users).
_SESSIONS: Dict[str, Optional[LearnUsClient]] = {}

# Course cache {client_id: {course_id: (last_access_time, activities)}}
_COURSE_CACHE: Dict[int, Dict[int, Tuple[float, List]]] = {}


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str


# ------------------------------- Guest Auth ---------------------------------

class GuestLoginResponse(BaseModel):
    token: str


# Endpoint for anonymous users to obtain a short-lived session token that can be
# used for guest-only operations (such as HTML-based video download).  This
# mirrors the standard /login endpoint but skips credential verification and
# does NOT attach a LearnUsClient instance to the session store.

@app.post("/guest_login", response_model=GuestLoginResponse, summary="비회원 로그인")
def guest_login():
    token = uuid.uuid4().hex
    # Store a sentinel (None) so that token validation can still succeed while
    # allowing us to distinguish guest sessions from normal ones.
    _SESSIONS[token] = None
    return {"token": token}


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
import shutil, os

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

    video_page_url = f"{client.BASE_URL}/mod/vod/viewer.php?id={video_id}"
    try:
        title, m3u8_url = client.get_video_stream_info(video_page_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Probe duration (in seconds) using ffprobe, if available
    ffprobe_bin = os.getenv("FFPROBE_PATH") or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    stream_duration: Optional[float] = None
    stream_bitrate: Optional[int] = None  # bits per second
    if ffprobe_bin:
        try:
            probe_cmd = [
                ffprobe_bin,
                "-v", "error",
                "-show_entries", "format=duration,bit_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                m3u8_url,
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
                if lines:
                    try:
                        stream_duration = float(lines[0])
                    except ValueError:
                        pass
                    if len(lines) > 1:
                        try:
                            stream_bitrate = int(lines[1])  # bits/sec
                        except ValueError:
                            pass
        except Exception:
            # ignore probe errors
            stream_duration = None
            stream_bitrate = None

    # Prepare ffmpeg command
    ffmpeg_bin = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg_bin:
        raise HTTPException(status_code=500, detail="ffmpeg executable not found on server. Install ffmpeg and ensure it is in PATH.")

    if ext == "mp4":
        # For streaming to a non-seekable pipe, use fragmented MP4 flags instead of +faststart
        # faststart needs a seekable output to relocate the moov atom and fails, producing 0-byte files.
        codec_args = "-c copy -bsf:a aac_adtstoasc -movflags frag_keyframe+empty_moov -f mp4"
    else:  # mp3
        codec_args = "-vn -c:a libmp3lame -b:a 192k -f mp3"

    cmd = f"{shlex.quote(ffmpeg_bin)} -loglevel error -y -i {shlex.quote(m3u8_url)} {codec_args} pipe:1"

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
    if stream_duration:
        headers["X-Stream-Duration"] = str(stream_duration)
    if stream_bitrate:
        headers["X-Stream-Bitrate"] = str(stream_bitrate)
    media_type = "video/mp4" if ext == "mp4" else "audio/mpeg"
    return StreamingResponse(iterfile(), media_type=media_type, headers=headers)


# --------------------------- Guest download via HTML ---------------------------

@app.post("/guest/download")
async def guest_download(
    ext: str = Query(..., regex="^(mp4|mp3)$", description="Download type: mp4 or mp3"),
    file: UploadFile = File(..., description="HTML page containing .m3u8 URL"),
    x_auth_token: Optional[str] = Header(None),
):
    """Accept an HTML file uploaded by a guest user, extract the first m3u8 URL and
    convert/stream it as MP4 or MP3 to the client.

    The caller must include the token obtained from /guest_login in the
    X-Auth-Token header.  The session associated with that token must be a guest
    session (i.e. value is None).
    """

    # Basic token validation (guest only)
    if not x_auth_token or x_auth_token not in _SESSIONS:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    if _SESSIONS[x_auth_token] is not None:
        raise HTTPException(status_code=400, detail="Not a guest session")

    # Read uploaded HTML
    try:
        raw = await file.read()
        html_text = raw.decode("utf-8", errors="ignore")
    except Exception:
        raise HTTPException(status_code=400, detail="파일을 읽는 중 오류가 발생했습니다.")

    # ------------------------------------------------------------------
    # Parse HTML to obtain m3u8 URL & title (mirror get_video_stream_info)
    # ------------------------------------------------------------------
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")

    # Find m3u8 source tag
    source_tag = soup.find("source", {"type": "application/x-mpegURL"})
    if source_tag is None or not source_tag.get("src"):
        raise HTTPException(status_code=400, detail="HTML 내에서 m3u8 <source> 태그를 찾을 수 없습니다.")

    m3u8_url: str = source_tag["src"]

    # Extract and sanitise title
    title = file.filename.rsplit(".", 1)[0]
    header_div = soup.find("div", id="vod_header")
    if header_div is not None and header_div.find("h1") is not None:
        h1 = header_div.find("h1")
        for span in h1.find_all("span"):
            span.decompose()
        extracted = h1.get_text(strip=True)
        if extracted:
            invalid_chars = '\/:*?"<>|'
            title = extracted.translate(str.maketrans(invalid_chars, '＼／：＊？＂＜＞｜'))

    # Probe duration/bitrate using ffprobe if available (reuse logic above)
    ffprobe_bin = os.getenv("FFPROBE_PATH") or shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    stream_duration: Optional[float] = None
    stream_bitrate: Optional[int] = None
    if ffprobe_bin:
        try:
            probe_cmd = [
                ffprobe_bin,
                "-v", "error",
                "-show_entries", "format=duration,bit_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                m3u8_url,
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
                if lines:
                    try:
                        stream_duration = float(lines[0])
                    except ValueError:
                        pass
                    if len(lines) > 1:
                        try:
                            stream_bitrate = int(lines[1])
                        except ValueError:
                            pass
        except Exception:
            stream_duration = None
            stream_bitrate = None

    # ffmpeg command (same as /download)
    ffmpeg_bin = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg_bin:
        raise HTTPException(status_code=500, detail="ffmpeg executable not found on server. Install ffmpeg and ensure it is in PATH.")

    if ext == "mp4":
        codec_args = "-c copy -bsf:a aac_adtstoasc -movflags frag_keyframe+empty_moov -f mp4"
    else:
        codec_args = "-vn -c:a libmp3lame -b:a 192k -f mp3"

    cmd = f"{shlex.quote(ffmpeg_bin)} -loglevel error -y -i {shlex.quote(m3u8_url)} {codec_args} pipe:1"

    process = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None:
        raise HTTPException(status_code=500, detail="Failed to initiate ffmpeg stream")

    def iterfile():
        try:
            while True:
                chunk = process.stdout.read(1024 * 1024)
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
    if stream_duration:
        headers["X-Stream-Duration"] = str(stream_duration)
    if stream_bitrate:
        headers["X-Stream-Bitrate"] = str(stream_bitrate)

    media_type = "video/mp4" if ext == "mp4" else "audio/mpeg"
    return StreamingResponse(iterfile(), media_type=media_type, headers=headers)


# ----------------------------- Static mount (last) -----------------------------
_static_path = pathlib.Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_static_path, html=True), name="static") 