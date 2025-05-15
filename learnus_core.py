import os
import sys
import requests
from bs4 import BeautifulSoup
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from ffmpeg_progress_yield import FfmpegProgress
from typing import Tuple, Callable, Optional

# ------------------------
# Core extraction helpers
# ------------------------

def extract_from_login(username: str, password: str, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Login to LearnUs and return (video_title, m3u8_url).
    Returns (None, None) if login or extraction fails."""
    session = requests.Session()
    base_headers = {
        'User-agent': 'Mozilla/5.0'
    }

    def post_request(u, headers, data):
        return session.post(u, headers=headers, data=data)

    def get_value_from_input(res_text, input_name):
        soup = BeautifulSoup(res_text, 'html.parser')
        input_tag = soup.find('input', {'name': input_name})
        return input_tag['value'] if input_tag else None

    def get_multiple_values(res_text, input_names):
        soup = BeautifulSoup(res_text, 'html.parser')
        values = {}
        for name in input_names:
            input_tag = soup.find('input', {'name': name})
            if not input_tag:
                return None
            values[name] = input_tag['value']
        return values

    headers = base_headers.copy()
    headers['Referer'] = 'https://ys.learnus.org/login/method/sso.php'
    data = {
        'ssoGubun': 'Login',
        'logintype': 'sso',
        'type': 'popup_login',
        'username': username,
        'password': password
    }
    res = post_request('https://ys.learnus.org/passni/sso/coursemosLogin.php', headers, data)
    s1 = get_value_from_input(res.text, 'S1')
    if not s1:
        return None, None  # 로그인 실패 시 None 반환

    headers['Referer'] = 'https://ys.learnus.org/'
    data.update({
        'app_id': 'ednetYonsei',
        'retUrl': 'https://ys.learnus.org',
        'failUrl': 'https://ys.learnus.org/login/index.php',
        'baseUrl': 'https://ys.learnus.org',
        'S1': s1,
        'loginUrl': 'https://ys.learnus.org/passni/sso/coursemosLogin.php',
        'ssoGubun': 'Login',
        'refererUrl': 'https://ys.learnus.org',
        'test': 'SSOAuthLogin',
        'loginType': 'invokeID',
        'E2': '',
    })
    res = post_request('https://infra.yonsei.ac.kr/sso/PmSSOService', headers, data)
    values = get_multiple_values(res.text, ['ssoChallenge', 'keyModulus'])
    if not values:
        return None, None
    sc, km = values['ssoChallenge'], values['keyModulus']

    keyPair = RSA.construct((int(km, 16), 0x10001))
    cipher = PKCS1_v1_5.new(keyPair)
    json_str = f'{{"userid":"{username}","userpw":"{password}","ssoChallenge":"{sc}"}}'
    e2 = cipher.encrypt(json_str.encode()).hex()

    headers['Referer'] = 'https://infra.yonsei.ac.kr/'
    data.update({
        'ssoChallenge': sc,
        'keyModulus': km,
        'keyExponent': '10001',
        'E2': e2,
    })
    res = post_request('https://ys.learnus.org/passni/sso/coursemosLogin.php', headers, data)
    s1 = get_value_from_input(res.text, 'S1')
    if not s1:
        return None, None

    headers['Referer'] = 'https://ys.learnus.org/'
    data.update({'S1': s1})
    res = post_request('https://infra.yonsei.ac.kr/sso/PmSSOAuthService', headers, data)
    values = get_multiple_values(res.text, ['E3', 'E4', 'S2', 'CLTID'])
    if not values:
        return None, None
    e3, e4, s2, cltid = values['E3'], values['E4'], values['S2'], values['CLTID']

    headers['Referer'] = 'https://infra.yonsei.ac.kr/'
    data = {
        'app_id': 'ednetYonsei',
        'retUrl': 'https://ys.learnus.org',
        'failUrl': 'https://ys.learnus.org/login/index.php',
        'baseUrl': 'https://ys.learnus.org',
        'loginUrl': 'https://ys.learnus.org/passni/sso/coursemosLogin.php',
        'E3': e3,
        'E4': e4,
        'S2': s2,
        'CLTID': cltid,
        'ssoGubun': 'Login',
        'refererUrl': 'https://ys.learnus.org',
        'test': 'SSOAuthLogin',
        'username': username,
        'password': password
    }
    post_request('https://ys.learnus.org/passni/sso/spLoginData.php', headers, data)
    session.get('https://ys.learnus.org/passni/spLoginProcess.php')

    res = session.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    m3u8_url = soup.find('source', {'type': 'application/x-mpegURL'})
    if not m3u8_url:
        return None, None
    m3u8_url = m3u8_url['src']

    video_title_tag = soup.find('div', id='vod_header').find('h1')
    for span in video_title_tag.find_all('span'):
        span.decompose()
    video_title = video_title_tag.get_text(strip=True)

    invalid_chars = '\\/:*?"<>|'
    translation_table = str.maketrans(invalid_chars, '＼／：＊？＂＜＞｜')
    video_title = video_title.translate(translation_table)

    return video_title, m3u8_url


def extract_from_html(html_content: str):
    soup = BeautifulSoup(html_content, 'html.parser')

    video_title_tag = soup.find('div', id='vod_header')
    if video_title_tag:
        title_node = video_title_tag.find('h1')
        if title_node:
            for span in title_node.find_all('span'):
                span.decompose()
            video_title = title_node.get_text(strip=True)
            fix_table = str.maketrans('\\/:*?"<>|', '＼／：＊？＂＜＞｜')
            video_title = video_title.translate(fix_table)
        else:
            video_title = 'LearnUs_Video'
    else:
        video_title = 'LearnUs_Video'

    m3u8_url = soup.find('source', {'type': 'application/x-mpegURL'})
    if not m3u8_url:
        return None, None
    m3u8_url = m3u8_url['src']

    return m3u8_url, video_title


# ------------------------
# Download utilities
# ------------------------

def _ffmpeg_default_flags():
    """Return platform-specific kwargs for subprocess (hide console on Windows)."""
    if sys.platform.startswith('win'):
        # CREATE_NO_WINDOW is only available on Windows platforms
        from subprocess import CREATE_NO_WINDOW  # type: ignore
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def download_mp4(m3u8_url: str, video_title: str, download_folder: str, ffmpeg_path: str = 'ffmpeg', progress_cb: Optional[Callable[[str, int], None]] = None) -> str:
    """Download video to MP4 and return the output file path."""
    mp4_file = f"{video_title}.mp4"
    mp4_path = os.path.join(download_folder, mp4_file)

    if not os.path.exists(download_folder):
        os.makedirs(download_folder)

    cmd = [ffmpeg_path, '-y', '-i', m3u8_url, '-bsf:a', 'aac_adtstoasc', '-c', 'copy', mp4_path]
    ff = FfmpegProgress(cmd)
    for progress in ff.run_command_with_progress(popen_kwargs=_ffmpeg_default_flags()):
        if progress_cb:
            progress_cb(mp4_file, progress)
    return mp4_path


def download_mp3(m3u8_url: str, video_title: str, download_folder: str, ffmpeg_path: str = 'ffmpeg', progress_cb: Optional[Callable[[str, int], None]] = None) -> str:
    """Download audio only to MP3 and return the output file path."""
    mp3_file = f"{video_title}.mp3"
    mp3_path = os.path.join(download_folder, mp3_file)

    if not os.path.exists(download_folder):
        os.makedirs(download_folder)

    cmd = [ffmpeg_path, '-y', '-i', m3u8_url, '-vn', '-acodec', 'libmp3lame', mp3_path]
    ff = FfmpegProgress(cmd)
    for progress in ff.run_command_with_progress(popen_kwargs=_ffmpeg_default_flags()):
        if progress_cb:
            progress_cb(mp3_file, progress)
    return mp3_path 