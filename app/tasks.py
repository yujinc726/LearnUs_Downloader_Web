from __future__ import annotations
import threading
import uuid
import os
from typing import Dict, List, Optional

from learnus_core import (
    extract_from_login,
    extract_from_html,
    download_mp4,
    download_mp3,
)


class DownloadTask:
    def __init__(self):
        self.id: str = str(uuid.uuid4())
        self.status: str = 'pending'  # pending, running, finished, failed
        self.progress: Dict[str, int] = {}
        self.files: List[str] = []
        self.error: Optional[str] = None

    def to_dict(self):
        return {
            'id': self.id,
            'status': self.status,
            'progress': self.progress,
            'files': [os.path.basename(f) for f in self.files],
            'error': self.error,
        }


tasks: Dict[str, DownloadTask] = {}
_tasks_lock = threading.Lock()


def _register_task(task: DownloadTask):
    with _tasks_lock:
        tasks[task.id] = task


def get_task(task_id: str) -> Optional[DownloadTask]:
    with _tasks_lock:
        return tasks.get(task_id)


# --------------------------
# Public API to start tasks
# --------------------------

def start_login_download(username: str, password: str, url: str, do_mp4: bool, do_mp3: bool, download_folder: str) -> str:
    task = DownloadTask()
    _register_task(task)

    thread = threading.Thread(
        target=_run_login_download,
        args=(task, username, password, url, do_mp4, do_mp3, download_folder),
        daemon=True,
    )
    thread.start()
    return task.id


def start_html_download(html_content: str, do_mp4: bool, do_mp3: bool, download_folder: str) -> str:
    task = DownloadTask()
    _register_task(task)

    thread = threading.Thread(
        target=_run_html_download,
        args=(task, html_content, do_mp4, do_mp3, download_folder),
        daemon=True,
    )
    thread.start()
    return task.id


# --------------------------
# Internal worker routines
# --------------------------

def _progress_callback(task: DownloadTask, file_name: str, progress: int):
    task.progress[file_name] = progress


def _run_login_download(task: DownloadTask, username: str, password: str, url: str, do_mp4: bool, do_mp3: bool, download_folder: str):
    try:
        task.status = 'running'
        video_title, m3u8_url = extract_from_login(username, password, url)
        if not video_title or not m3u8_url:
            task.status = 'failed'
            task.error = 'Failed to obtain video information. Check credentials or URL.'
            return

        _spawn_downloads(task, m3u8_url, video_title, do_mp4, do_mp3, download_folder)
        task.status = 'failed' if task.error else 'finished'
    except Exception as e:
        task.status = 'failed'
        task.error = str(e)


def _run_html_download(task: DownloadTask, html_content: str, do_mp4: bool, do_mp3: bool, download_folder: str):
    try:
        task.status = 'running'
        m3u8_url, video_title = extract_from_html(html_content)
        if not m3u8_url:
            task.status = 'failed'
            task.error = 'Unable to find m3u8 URL in HTML.'
            return
        _spawn_downloads(task, m3u8_url, video_title, do_mp4, do_mp3, download_folder)
        task.status = 'failed' if task.error else 'finished'
    except Exception as e:
        task.status = 'failed'
        task.error = str(e)


def _spawn_downloads(task: DownloadTask, m3u8_url: str, video_title: str, do_mp4: bool, do_mp3: bool, download_folder: str):
    def cb(file_name, prog):
        _progress_callback(task, file_name, prog)

    threads: list[threading.Thread] = []

    def _dl_wrapper(fn, *args):
        try:
            path = fn(*args, progress_cb=cb)
            task.files.append(path)
        except Exception as e:
            # if one download fails, mark task as failed; main thread will handle status
            task.error = str(e)

    if do_mp4:
        t = threading.Thread(
            target=_dl_wrapper,
            args=(download_mp4, m3u8_url, video_title, download_folder),
        )
        t.start(); threads.append(t)
    if do_mp3:
        t = threading.Thread(
            target=_dl_wrapper,
            args=(download_mp3, m3u8_url, video_title, download_folder),
        )
        t.start(); threads.append(t)

    for t in threads:
        t.join() 