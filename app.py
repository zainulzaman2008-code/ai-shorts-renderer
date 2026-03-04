from flask import Flask, request, jsonify, send_file
import os
import requests
import tempfile
import base64
import threading
import hashlib
import time
import subprocess
import json
import numpy as np
import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont

if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from moviepy.editor import (
    VideoFileClip, AudioFileClip, CompositeVideoClip,
    concatenate_videoclips, ImageClip
)
from moviepy.video.fx.all import crop

app = Flask(__name__)
jobs = {}

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
        job = jobs.get(job_id, {})
        if job.get('status') in ('done', 'error'):
            break
        try:
            requests.get(own_url, timeout=10)
        except:
            pass
        time.sleep(interval)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def download_file(url, path):
    r = requests.get(url, stream=True, timeout=60)
    with open(path, 'wb') as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)

def fetch_pexels_videos(topic, api_key, count=5):
    headers = {'Authorization': api_key}
    urls = []
    for query in [topic, 'technology future', 'space science']:
        if len(urls) >= count:
            break
        resp = requests.get(
            'https://api.pexels.com/videos/search',
            headers=headers,
            params={'query': query, 'per_page': 10, 'orientation': 'portrait'},
            timeout=30
        )
        for v in resp.json().get('videos', []):
            for vf in v['video_files']:
                if vf.get('width', 0) >= 720:
                    urls.append(vf['link'])
                    break
            if len(urls) >= count:
                break
    return urls[:count]

def fetch_pexels_thumbnail(topic, api_key):
    headers = {'Authorization': api_key}
    resp = requests.get(
        'https://api.pexels.com/v1/search',
        headers=headers,
        params={'query': topic, 'per_page': 1, 'orientation': 'portrait'},
        timeout=30
    )
    photos = resp.json().get('photos', [])
    if photos:
        return photos[0]['src']['large']
    return None

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

def build_caption_filter(script, audio_duration, W, H):
    """Build FFmpeg drawtext filter for captions - much faster than MoviePy clips."""
    words = script.split()
    if not words:
        return ""
    
    word_duration = audio_duration / len(words)
    line_size = 5
    groups = [words[i:i+line_size] for i in range(0, len(words), line_size)]
    
    filters = []
    t = 0
    
    # Title bar - always visible
    filters.append(
        f"drawbox=x=0:y=30:w={W}:h=90:color=black@0.6:t=fill"
    )
    filters.append(
        f"drawtext=text='TECH FACTS':fontsize=50:fontcolor=#FFD700:"
        f"x=(w-text_w)/2:y=55:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )
    
    # Caption bar - always visible
    filters.append(
        f"drawbox=x=0:y={H-230}:w={W}:h=150:color=black@0.55:t=fill"
    )
    
    for group in groups:
        group_duration = word_duration * len(group)
        group_start = t
        full_line = ' '.join(group)
        
        # White line always shown during group
        safe_line = full_line.replace("'", "\\'").replace(":", "\\:")
        filters.append(
            f"drawtext=text='{safe_line}':fontsize=65:fontcolor=white:"
            f"x=(w-text_w)/2:y={H-210}:"
            f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"enable='between(t,{group_start:.2f},{group_start+group_duration:.2f})'"
        )
        
        # Yellow highlight per word
        for wi, word in enumerate(group):
            word_start = group_start + wi * word_duration
            word_end = word_start + word_duration
            safe_word = word.replace("'", "\\'").replace(":", "\\:")
            # Calculate approximate x position
            chars_before = len(' '.join(group[:wi])) + (1 if wi > 0 else 0)
            total_chars = max(len(full_line), 1)
            x_pos = int((chars_before / total_chars) * W * 0.75) + int(W * 0.1)
            filters.append(
                f"drawtext=text='{safe_word}':fontsize=75:fontcolor=#FFD700:"
                f"x={x_pos}:y={H-218}:"
                f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                f"enable='between(t,{word_start:.2f},{word_end:.2f})'"
            )
        
        t += group_duration
    
    return ','.join(filters)

def build_video(job_id, topic, script, audio_base64):
    try:
        jobs[job_id] = {"status": "processing", "progress": "Starting..."}
        tmp = tempfile.mkdtemp()

        # 1. Save audio
        audio_path = os.path.join(tmp, 'voice.mp3')
        with open(audio_path, 'wb') as f:
            f.write(base64.b64decode(audio_base64))
        audio_clip = AudioFileClip(audio_path)
        total_duration = audio_clip.duration
        audio_clip.close()

        jobs[job_id]['progress'] = 'Fetching stock footage...'

        # 2. Fetch Pexels footage
        pexels_key = os.environ.get('PEXELS_API_KEY', '')
        video_urls = fetch_pexels_videos(topic, pexels_key, count=5)
        if not video_urls:
            raise Exception("No Pexels videos found for: " + topic)

        # 3. Download footage
        jobs[job_id]['progress'] = 'Downloading footage...'
        TARGET_W, TARGET_H = 1080, 1920
        segment_duration = 4.0
        needed_segments = int(total_duration / segment_duration) + 2

        segments = []
        for i, url in enumerate(video_urls):
            if len(segments) >= needed_segments:
                break
            vp = os.path.join(tmp, f'raw_{i}.mp4')
            try:
                download_file(url, vp)
                vc = VideoFileClip(vp)
                clip_ratio = vc.w / vc.h
                target_ratio = TARGET_W / TARGET_H
                if clip_ratio > target_ratio:
                    vc = vc.resize(height=TARGET_H)
                else:
                    vc = vc.resize(width=TARGET_W)
                x_c = vc.w / 2
                y_c = vc.h / 2
                vc = crop(vc, width=TARGET_W, height=TARGET_H, x_center=x_c, y_center=y_c)
                clip_seg_count = min(2, int(vc.duration / segment_duration))
                for s in range(clip_seg_count):
                    start = s * segment_duration
                    if start + segment_duration <= vc.duration:
                        seg = vc.subclip(start, start + segment_duration)
                        segments.append(seg)
                        if len(segments) >= needed_segments:
                            break
                vc.close()
            except Exception:
                continue

        if not segments:
            raise Exception("Could not process any video segments")

        # 4. Concatenate & export base video
        jobs[job_id]['progress'] = 'Compositing video...'
        full_bg = concatenate_videoclips(segments, method='compose')
        if full_bg.duration < total_duration:
            loops = int(total_duration / full_bg.duration) + 1
            full_bg = concatenate_videoclips([full_bg] * loops, method='compose')
        full_bg = full_bg.subclip(0, total_duration).set_fps(30)

        base_path = os.path.join(tmp, 'base.mp4')
        full_bg.write_videofile(
            base_path,
            fps=30,
            codec='libx264',
            audio=False,
            preset='ultrafast',
            threads=4,
            logger=None
        )
        full_bg.close()

        # 5. Add captions + audio using FFmpeg directly (FAST!)
        jobs[job_id]['progress'] = 'Adding captions and audio...'
        output_path = os.path.join(tmp, 'final_short.mp4')
        caption_filter = build_caption_filter(script, total_duration, TARGET_W, TARGET_H)

        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', base_path,
            '-i', audio_path,
            '-vf', caption_filter,
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-c:a', 'aac',
            '-shortest',
            '-threads', '4',
            output_path
        ]
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True)

        # 6. Upload video to Cloudinary
        jobs[job_id]['progress'] = 'Uploading to Cloudinary...'
        public_id = f"shorts/{job_id}"
        result = upload_to_cloudinary(output_path, public_id, 'video')
        video_url = result.get('secure_url')

        # 7. Upload thumbnail
        thumb_url = fetch_pexels_thumbnail(topic, pexels_key)
        cloudinary_thumb_url = None
        if thumb_url:
            thumb_path = os.path.join(tmp, 'thumbnail.jpg')
            download_file(thumb_url, thumb_path)
            thumb_result = upload_to_cloudinary(thumb_path, f"{public_id}_thumb", 'image')
            cloudinary_thumb_url = thumb_result.get('secure_url')

        jobs[job_id] = {
            "status": "done",
            "video_url": video_url,
            "thumbnail_url": cloudinary_thumb_url,
            "cloudinary_public_id": job_id
        }

    except Exception as e:
        jobs[job_id] = {"status": "error", "error": str(e)}


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def home():
    return jsonify({"status": "AI Shorts Renderer is running!"})

@app.route('/ping')
def ping():
    return jsonify({"status": "alive"})

@app.route('/render', methods=['POST'])
def render_video():
    data = request.json
    topic = data.get('topic', 'technology')
    script = data.get('script', '')
    audio_base64 = data.get('audio', '')
    if not audio_base64:
        return jsonify({"error": "No audio provided"}), 400
    job_id = os.urandom(8).hex()

    # Start render thread
    render_thread = threading.Thread(target=build_video, args=(job_id, topic, script, audio_base64))
    render_thread.daemon = True
    render_thread.start()

    # Start self-ping thread
    ping_thread = threading.Thread(target=self_ping, args=(job_id,))
    ping_thread.daemon = True
    ping_thread.start()

    return jsonify({"job_id": job_id})

@app.route('/status/<job_id>', methods=['GET'])
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)

@app.route('/wait/<job_id>', methods=['GET'])
def wait_for_job(job_id):
    timeout = 280
    interval = 10
    elapsed = 0
    while elapsed < timeout:
        job = jobs.get(job_id)
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
        return jsonify({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
