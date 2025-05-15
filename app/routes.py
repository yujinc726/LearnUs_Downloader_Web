from flask import Blueprint, request, jsonify, send_from_directory, current_app, abort, after_this_request
import os

from app.tasks import (
    start_login_download,
    start_html_download,
    get_task,
)

bp = Blueprint('api', __name__, url_prefix='/api')

DEFAULT_DOWNLOAD_FOLDER = os.getenv('LEARNUS_DL_DIR', os.path.join(os.getcwd(), 'downloads'))


@bp.route('/login-download', methods=['POST'])
def login_download():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    url = data.get('url')
    do_mp4 = bool(data.get('mp4'))
    do_mp3 = bool(data.get('mp3'))

    if not all([username, password, url]) or not (do_mp4 or do_mp3):
        return jsonify({'error': 'Missing parameters'}), 400

    task_id = start_login_download(username, password, url, do_mp4, do_mp3, DEFAULT_DOWNLOAD_FOLDER)
    return jsonify({'task_id': task_id})


@bp.route('/html-download', methods=['POST'])
def html_download():
    do_mp4 = request.form.get('mp4') == 'true'
    do_mp3 = request.form.get('mp3') == 'true'
    file = request.files.get('html')
    if not file or not (do_mp4 or do_mp3):
        return jsonify({'error': 'Missing parameters'}), 400
    html_content = file.read().decode('utf-8', errors='ignore')

    task_id = start_html_download(html_content, do_mp4, do_mp3, DEFAULT_DOWNLOAD_FOLDER)
    return jsonify({'task_id': task_id})


@bp.route('/progress/<task_id>')
def progress(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({'error': 'Invalid task id'}), 404
    return jsonify(task.to_dict())


@bp.route('/download/<task_id>/<filename>')
def download_file(task_id, filename):
    task = get_task(task_id)
    if not task or task.status != 'finished':
        return jsonify({'error': 'Task not finished'}), 404

    for path in task.files:
        if os.path.basename(path) == filename:

            @after_this_request
            def remove_file(response):
                """Delete the file (and its directory if empty) after it has been sent."""
                try:
                    os.remove(path)
                    dir_path = os.path.dirname(path)
                    # Remove directory if it became empty (ignores errors if not empty or fails)
                    if not os.listdir(dir_path):
                        os.rmdir(dir_path)
                except Exception:
                    # Best-effort cleanup; ignore any failure.
                    pass
                return response

            return send_from_directory(os.path.dirname(path), filename, as_attachment=True)

    abort(404) 