from flask import Flask, request, jsonify
import os
import requests
import tempfile
import base64
import threading
import hashlib
import time
import json
import subprocess
import random

app = Flask(__name__)

# ─────────────────────────────────────────────
# FILE-BASED JOB STORE
# ─────────────────────────────────────────────

JOBS_DIR = '/tmp/jobs'
os.makedirs(JOBS_DIR, exist_ok=True)

def set_job(job_id, data):
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    with open(path, 'w') as f:
        json.dump(data, f)

def get_job(job_id):
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, 'r') as f:
        return json.load(f)

# ─────────────────────────────────────────────
# SELF-PING SYSTEM
# ─────────────────────────────────────────────

def self_ping(job_id, interval=25):
    own_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')
    if own_url:
        own_url = f"https://{own_url}/ping"
    else:
        own_url = "http://localhost:8080/ping"
    while True:
        job = get_job(job_id)
        if job and job.get('status') in ('done', 'error'):
            break
        try:
            requests.get(own_url, timeout=10)
        except:
            pass
        time.sleep(interval)

# ─────────────────────────────────────────────
# KEYWORD EXTRACTOR
# ─────────────────────────────────────────────

def extract_keywords(topic, script):
    stop_words = {'the','a','an','is','are','was','were','in','on','at','to','for',
                  'of','and','or','but','with','this','that','it','be','have','has',
                  'will','can','do','did','not','from','by','as','we','our','your'}
    words = (topic + ' ' + script).lower().split()
    keywords = []
    for word in words:
        word = word.strip('.,!?')
        if len(word) > 4 and word not in stop_words and word not in keywords:
            keywords.append(word)
        if len(keywords) >= 10:
            break
    topic_clean = topic.strip().split()[:3]
    final_keywords = topic_clean + keywords[:7]
    return final_keywords

# ─────────────────────────────────────────────
# MULTI-SOURCE VIDEO FETCHER
# ─────────────────────────────────────────────

def fetch_pexels_videos(keywords, api_key, count=6):
    headers = {'Authorization': api_key}
    urls = []
    for keyword in keywords:
        if len(urls) >= count:
            break
        try:
            resp = requests.get(
                'https://api.pexels.com/videos/search',
                headers=headers,
                params={'query': keyword, 'per_page': 5, 'orientation': 'portrait'},
                timeout=15
            )
            for v in resp.json().get('videos', []):
                for vf in v['video_files']:
                    if vf.get('width', 0) >= 720:
                        urls.append(vf['link'])
                        break
                if len(urls) >= count:
                    break
        except:
            continue
    return urls[:count]

def fetch_pixabay_videos(keywords, api_key, count=6):
    urls = []
    for keyword in keywords:
        if len(urls) >= count:
            break
        try:
            resp = requests.get(
                'https://pixabay.com/api/videos/',
                params={
                    'key': api_key,
                    'q': keyword,
                    'video_type': 'film',
                    'per_page': 5,
                    'orientation': 'vertical'
                },
                timeout=15
            )
            for v in resp.json().get('hits', []):
                video_url = v.get('videos', {}).get('medium', {}).get('url')
                if video_url:
                    urls.append(video_url)
                if len(urls) >= count:
                    break
        except:
            continue
    return urls[:count]

def fetch_nasa_videos(keywords, count=2):
    urls = []
    for keyword in keywords[:3]:
        if len(urls) >= count:
            break
        try:
            resp = requests.get(
                'https://images-api.nasa.gov/search',
                params={'q': keyword, 'media_type': 'video'},
                timeout=15
            )
            items = resp.json().get('collection', {}).get('items', [])
            for item in items[:3]:
                try:
                    nasa_id = item['data'][0]['nasa_id']
                    asset_resp = requests.get(
                        f'https://images-api.nasa.gov/asset/{nasa_id}',
                        timeout=10
                    )
                    for asset in asset_resp.json().get('collection', {}).get('items', []):
                        href = asset.get('href', '')
                        if href.endswith('.mp4') and '~mobile' not in href:
                            urls.append(href)
                            break
                    if len(urls) >= count:
                        break
                except:
                    continue
        except:
            continue
    return urls[:count]

def fetch_pexels_thumbnail(keyword, api_key):
    headers = {'Authorization': api_key}
    try:
        resp = requests.get(
            'https://api.pexels.com/v1/search',
            headers=headers,
            params={'query': keyword, 'per_page': 1, 'orientation': 'portrait'},
            timeout=15
        )
        photos = resp.json().get('photos', [])
        if photos:
            return photos[0]['src']['large']
    except:
        pass
    return None

# ─────────────────────────────────────────────
# CLOUDINARY
# ─────────────────────────────────────────────

def upload_to_cloudinary(file_path, public_id, resource_type='video'):
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME')
    api_key = os.environ.get('CLOUDINARY_API_KEY')
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')
    timestamp = str(int(time.time()))
    params = f"public_id={public_id}&timestamp={timestamp}"
    signature = hashlib.sha1((params + api_secret).encode()).hexdigest()
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/upload"
    with open(file_path, 'rb') as f:
        resp = requests.post(url, data={
            'api_key': api_key,
            'timestamp': timestamp,
            'public_id': public_id,
            'signature': signature
        }, files={'file': f}, timeout=120)
    return resp.json()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def find_binary(name):
    try:
        return subprocess.check_output(['which', name]).decode().strip()
    except:
        for path in [f'/usr/bin/{name}', f'/usr/local/bin/{name}']:
            if os.path.exists(path):
                return path
    return name

def download_file(url, path):
    r = requests.get(url, stream=True, timeout=60)
    with open(path, 'wb') as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)

def build_title_filter(W, H):
    return (
        f"drawbox=x=0:y=30:w={W}:h=90:color=black@0.6:t=fill,"
        f"drawtext=text='TECH FACTS':fontsize=50:fontcolor=#FFD700:"
        f"x=(w-text_w)/2:y=55:"
        f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )

# ─────────────────────────────────────────────
# MAIN VIDEO BUILD
# ─────────────────────────────────────────────

def build_video(job_id, topic, script, audio_base64):
    try:
        set_job(job_id, {"status": "processing", "progress": "Starting..."})
        tmp = tempfile.mkdtemp()

        FFMPEG = find_binary('ffmpeg')
        FFPROBE = find_binary('ffprobe')

        # 1. Save audio
        audio_path = os.path.join(tmp, 'voice.mp3')
        with open(audio_path, 'wb') as f:
            f.write(base64.b64decode(audio_base64))

        result = subprocess.run([
            FFPROBE, '-v', 'quiet', '-print_format', 'json',
            '-show_format', audio_path
        ], capture_output=True, text=True)
        audio_info = json.loads(result.stdout)
        total_duration = float(audio_info['format']['duration'])

        # Ensure video is at least 50 seconds
        total_duration = max(total_duration, 50.0)

        set_job(job_id, {"status": "processing", "progress": "Extracting keywords..."})

        # 2. Extract keywords
        keywords = extract_keywords(topic, script)

        set_job(job_id, {"status": "processing", "progress": "Fetching footage from multiple sources..."})

        # 3. Fetch from multiple sources
        pexels_key = os.environ.get('PEXELS_API_KEY', '')
        pixabay_key = os.environ.get('PIXABAY_API_KEY', '')

        video_urls = []

        pexels_urls = fetch_pexels_videos(keywords, pexels_key, count=6)
        video_urls.extend(pexels_urls)

        if pixabay_key:
            pixabay_urls = fetch_pixabay_videos(keywords, pixabay_key, count=6)
            video_urls.extend(pixabay_urls)

        space_keywords = ['space', 'nasa', 'galaxy', 'planet', 'star', 'universe', 'rocket', 'asteroid']
        if any(k.lower() in space_keywords for k in keywords):
            nasa_urls = fetch_nasa_videos(keywords, count=2)
            video_urls.extend(nasa_urls)

        random.shuffle(video_urls)

        if not video_urls:
            raise Exception("No videos found from any source")

        # 4. Download & process footage
        set_job(job_id, {"status": "processing", "progress": "Downloading footage..."})
        TARGET_W, TARGET_H = 720, 1280
        segment_duration = 4.0
        needed_segments = int(total_duration / segment_duration) + 3

        segment_paths = []
        for i, url in enumerate(video_urls):
            if len(segment_paths) >= needed_segments:
                break
            vp = os.path.join(tmp, f'raw_{i}.mp4')
            seg_path = os.path.join(tmp, f'seg_{i}.mp4')
            try:
                download_file(url, vp)
                subprocess.run([
                    FFMPEG, '-y', '-i', vp,
                    '-vf', f'scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,'
                           f'crop={TARGET_W}:{TARGET_H}',
                    '-t', str(segment_duration),
                    '-an', '-preset', 'ultrafast',
                    '-threads', '1',
                    '-c:v', 'libx264', seg_path
                ], capture_output=True, check=True, timeout=60)
                segment_paths.append(seg_path)
            except Exception:
                continue

        if not segment_paths:
            raise Exception("Could not process any video segments")

        # 5. Concatenate segments
        set_job(job_id, {"status": "processing", "progress": "Compositing video..."})

        total_seg_duration = len(segment_paths) * segment_duration
        loops = int(total_duration / total_seg_duration) + 2
        concat_list = []
        for _ in range(loops):
            concat_list.extend(segment_paths)
        concat_list = concat_list[:needed_segments + 3]

        list_file = os.path.join(tmp, 'list.txt')
        with open(list_file, 'w') as f:
            for sp in concat_list:
                f.write(f"file '{sp}'\n")

        base_path = os.path.join(tmp, 'base.mp4')
        subprocess.run([
            FFMPEG, '-y', '-f', 'concat', '-safe', '0',
            '-i', list_file,
            '-t', str(total_duration),
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-threads', '1',
            '-bufsize', '512k',
            '-maxrate', '1M',
            '-an', base_path
        ], capture_output=True, check=True)

        # 6. Add title bar + audio
        set_job(job_id, {"status": "processing", "progress": "Adding title and audio..."})
        output_path = os.path.join(tmp, 'final_short.mp4')
        title_filter = build_title_filter(TARGET_W, TARGET_H)

        subprocess.run([
            FFMPEG, '-y',
            '-i', base_path,
            '-i', audio_path,
            '-vf', title_filter,
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-threads', '1',
            '-bufsize', '512k',
            '-maxrate', '1M',
            '-c:a', 'aac', '-shortest',
            output_path
        ], capture_output=True, check=True)

        # 7. Upload to Cloudinary
        set_job(job_id, {"status": "processing", "progress": "Uploading to Cloudinary..."})
        public_id = f"shorts/{job_id}"
        result = upload_to_cloudinary(output_path, public_id, 'video')
        video_url = result.get('secure_url')

        # 8. Thumbnail
        thumb_url = fetch_pexels_thumbnail(keywords[0] if keywords else topic, pexels_key)
        cloudinary_thumb_url = None
        if thumb_url:
            thumb_path = os.path.join(tmp, 'thumbnail.jpg')
            download_file(thumb_url, thumb_path)
            thumb_result = upload_to_cloudinary(thumb_path, f"{public_id}_thumb", 'image')
            cloudinary_thumb_url = thumb_result.get('secure_url')

        set_job(job_id, {
            "status": "done",
            "video_url": video_url,
            "thumbnail_url": cloudinary_thumb_url,
            "cloudinary_public_id": job_id
        })

    except Exception as e:
        set_job(job_id, {"status": "error", "error": str(e)})


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def home():
    return jsonify({"status": "AI Shorts Renderer is running!"})

@app.route('/ping')
def ping():
    try:
        ffprobe_path = subprocess.check_output(['which', 'ffprobe']).decode().strip()
        ffmpeg_path = subprocess.check_output(['which', 'ffmpeg']).decode().strip()
    except:
        ffprobe_path = 'not found'
        ffmpeg_path = 'not found'
    return jsonify({"status": "alive", "ffprobe": ffprobe_path, "ffmpeg": ffmpeg_path})

@app.route('/render', methods=['POST'])
def render_video():
    data = request.json
    topic = data.get('topic', 'technology')
    script = data.get('script', '')
    audio_base64 = data.get('audio', '')
    if not audio_base64:
        return jsonify({"error": "No audio provided"}), 400
    job_id = os.urandom(8).hex()
    set_job(job_id, {"status": "processing", "progress": "Queued..."})

    render_thread = threading.Thread(target=build_video, args=(job_id, topic, script, audio_base64))
    render_thread.daemon = True
    render_thread.start()

    ping_thread = threading.Thread(target=self_ping, args=(job_id,))
    ping_thread.daemon = True
    ping_thread.start()

    return jsonify({"job_id": job_id})

@app.route('/status/<job_id>', methods=['GET'])
def check_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)

@app.route('/wait/<job_id>', methods=['GET'])
def wait_for_job(job_id):
    timeout = 280
    interval = 10
    elapsed = 0
    while elapsed < timeout:
        job = get_job(job_id)
        if not job:
            return jsonify({"status": "not_found"}), 404
        if job['status'] == 'done':
            return jsonify(job)
        if job['status'] == 'error':
            return jsonify(job), 500
        time.sleep(interval)
        elapsed += interval
    return jsonify({"status": "timeout"})

@app.route('/delete/<job_id>', methods=['DELETE'])
def delete_from_cloudinary(job_id):
    try:
        cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME')
        api_key = os.environ.get('CLOUDINARY_API_KEY')
        api_secret = os.environ.get('CLOUDINARY_API_SECRET')
        timestamp = str(int(time.time()))
        public_id = f"shorts/{job_id}"
        params = f"public_id={public_id}&timestamp={timestamp}"
        signature = hashlib.sha1((params + api_secret).encode()).hexdigest()
        url = f"https://api.cloudinary.com/v1_1/{cloud_name}/video/destroy"
        requests.post(url, data={
            'public_id': public_id,
            'api_key': api_key,
            'timestamp': timestamp,
            'signature': signature
        })
        thumb_id = f"shorts/{job_id}_thumb"
        params2 = f"public_id={thumb_id}&timestamp={timestamp}"
        sig2 = hashlib.sha1((params2 + api_secret).encode()).hexdigest()
        url2 = f"https://api.cloudinary.com/v1_1/{cloud_name}/image/destroy"
        requests.post(url2, data={
            'public_id': thumb_id,
            'api_key': api_key,
            'timestamp': timestamp,
            'signature': sig2
        })
        job_path = os.path.join(JOBS_DIR, f"{job_id}.json")
        if os.path.exists(job_path):
            os.remove(job_path)
        return jsonify({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
