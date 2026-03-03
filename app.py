from flask import Flask, request, jsonify, send_file
import os
import requests
import tempfile
import base64
import threading
import hashlib
import time
import PIL.Image

# Fix for newer Pillow versions
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from moviepy.editor import (
    VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip,
    concatenate_videoclips, ColorClip
)
from moviepy.video.fx.all import crop

app = Flask(__name__)
jobs = {}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def download_file(url, path):
    r = requests.get(url, stream=True, timeout=60)
    with open(path, 'wb') as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)


def fetch_pexels_videos(topic, api_key, count=6):
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


def build_caption_clips(script, audio_duration, video_size):
    W, H = video_size
    words = script.split()
    if not words:
        return []

    word_duration = audio_duration / len(words)
    clips = []
    line_size = 5
    groups = [words[i:i+line_size] for i in range(0, len(words), line_size)]

    t = 0
    for group in groups:
        group_duration = word_duration * len(group)
        group_start = t

        for wi, word in enumerate(group):
            word_start = group_start + wi * word_duration

            # Background bar
            bg = (ColorClip(size=(W, 160), color=(0, 0, 0))
                  .set_opacity(0.55)
                  .set_start(word_start)
                  .set_duration(word_duration)
                  .set_position(('center', H - 220)))

            # Full line in white
            full_line = ' '.join(group)
            txt_white = (TextClip(
                full_line,
                fontsize=72,
                color='white',
                font='DejaVu-Sans-Bold',
                stroke_color='black',
                stroke_width=3,
                method='label'
            )
            .set_start(word_start)
            .set_duration(word_duration)
            .set_position(('center', H - 210)))

            # Current word in yellow
            txt_yellow = (TextClip(
                word,
                fontsize=82,
                color='#FFD700',
                font='DejaVu-Sans-Bold',
                stroke_color='black',
                stroke_width=3,
                method='label'
            )
            .set_start(word_start)
            .set_duration(word_duration))

            chars_before = len(' '.join(group[:wi])) + (1 if wi > 0 else 0)
            total_chars = len(full_line)
            x_offset = int((chars_before / max(total_chars, 1)) * W * 0.8) + int(W * 0.1)
            txt_yellow = txt_yellow.set_position((x_offset, H - 218))

            clips.extend([bg, txt_white, txt_yellow])

        t += group_duration

    return clips


def build_video(job_id, topic, script, audio_base64):
    try:
        jobs[job_id] = {"status": "processing", "progress": "Starting..."}
        tmp = tempfile.mkdtemp()

        # ── 1. Save audio ──────────────────────────────
        audio_path = os.path.join(tmp, 'voice.mp3')
        with open(audio_path, 'wb') as f:
            f.write(base64.b64decode(audio_base64))
        audio_clip = AudioFileClip(audio_path)
        total_duration = audio_clip.duration

        jobs[job_id]['progress'] = 'Fetching stock footage...'

        # ── 2. Fetch Pexels footage ────────────────────
        pexels_key = os.environ.get('PEXELS_API_KEY', '')
        video_urls = fetch_pexels_videos(topic, pexels_key, count=8)
        if not video_urls:
            raise Exception("No Pexels videos found for: " + topic)

        # ── 3. Download & cut footage ──────────────────
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
            except Exception:
                continue

        if not segments:
            raise Exception("Could not process any video segments")

        # ── 4. Concatenate segments ────────────────────
        jobs[job_id]['progress'] = 'Compositing video...'
        full_bg = concatenate_videoclips(segments, method='compose')
        if full_bg.duration < total_duration:
            loops = int(total_duration / full_bg.duration) + 1
            full_bg = concatenate_videoclips([full_bg] * loops, method='compose')
        full_bg = full_bg.subclip(0, total_duration).set_fps(30)

        # ── 5. Captions ────────────────────────────────
        jobs[job_id]['progress'] = 'Adding captions...'
        caption_clips = build_caption_clips(script, total_duration, (TARGET_W, TARGET_H))

        # ── 6. Top title bar ───────────────────────────
        title_bar = (ColorClip(size=(TARGET_W, 100), color=(0, 0, 0))
                     .set_opacity(0.6)
                     .set_duration(total_duration)
                     .set_position(('center', 40)))
        title_text = (TextClip(
            'TECH FACTS',
            fontsize=52,
            color='#FFD700',
            font='DejaVu-Sans-Bold',
            method='label'
        ).set_duration(total_duration).set_position(('center', 48)))

        # ── 7. Compose ─────────────────────────────────
        all_clips = [full_bg, title_bar, title_text] + caption_clips
        final = CompositeVideoClip(all_clips, size=(TARGET_W, TARGET_H))
        final = final.set_audio(audio_clip).set_duration(total_duration)

        # ── 8. Export ──────────────────────────────────
        jobs[job_id]['progress'] = 'Rendering final video...'
        output_path = os.path.join(tmp, 'final_short.mp4')
        final.write_videofile(
            output_path,
            fps=30,
            codec='libx264',
            audio_codec='aac',
            preset='ultrafast',
            threads=2,
            logger=None
        )

        # ── 9. Upload video to Cloudinary ──────────────
        jobs[job_id]['progress'] = 'Uploading to Cloudinary...'
        public_id = f"shorts/{job_id}"
        result = upload_to_cloudinary(output_path, public_id, 'video')
        video_url = result.get('secure_url')

        # ── 10. Upload thumbnail to Cloudinary ─────────
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


@app.route('/render', methods=['POST'])
def render_video():
    data = request.json
    topic = data.get('topic', 'technology')
    script = data.get('script', '')
    audio_base64 = data.get('audio', '')

    if not audio_base64:
        return jsonify({"error": "No audio provided"}), 400

    job_id = os.urandom(8).hex()
    thread = threading.Thread(target=build_video, args=(job_id, topic, script, audio_base64))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route('/status/<job_id>', methods=['GET'])
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


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
        resp = requests.post(url, data={
            'public_id': public_id,
            'api_key': api_key,
            'timestamp': timestamp,
            'signature': signature
        })

        # Also delete thumbnail
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


@app.route('/wait/<job_id>', methods=['GET'])
def wait_for_job(job_id):
    timeout = 900  # 15 minutes max
    interval = 10  # check every 10 seconds
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
    return jsonify({"status": "timeout"}), 408


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
