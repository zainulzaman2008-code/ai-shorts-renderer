from flask import Flask, request, jsonify, send_file
import os
import requests
import tempfile
import base64
import threading
import time
import textwrap
import PIL.Image

# Fix for newer Pillow versions
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

from moviepy.editor import (
    VideoFileClip, AudioFileClip, TextClip, CompositeVideoClip,
    concatenate_videoclips, ColorClip
)
from moviepy.video.fx.all import resize, crop

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
    """Return up to `count` portrait video URLs from Pexels."""
    headers = {'Authorization': api_key}
    urls = []
    # Try specific topic first, then fallback to generic tech
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


def build_caption_clips(script, audio_duration, video_size):
    """Create word-by-word caption clips with yellow highlight on current word."""
    W, H = video_size
    words = script.split()
    if not words:
        return []

    word_duration = audio_duration / len(words)
    clips = []

    # Group words into lines of ~5 words
    line_size = 5
    groups = [words[i:i+line_size] for i in range(0, len(words), line_size)]

    t = 0
    for group in groups:
        group_duration = word_duration * len(group)
        group_start = t

        for wi, word in enumerate(group):
            word_start = group_start + wi * word_duration

            # Build line with current word highlighted
            line_parts = []
            for j, w in enumerate(group):
                line_parts.append(('yellow' if j == wi else 'white', w))

            # Background bar
            bg = ColorClip(size=(W, 160), color=(0, 0, 0))
            bg = bg.set_opacity(0.55).set_start(word_start).set_duration(word_duration).set_position(('center', H - 220))

            # Full line text (white)
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

            # Highlighted word (yellow, bold, slightly larger)
            txt_yellow = (TextClip(
                word,
                fontsize=80,
                color='#FFD700',
                font='DejaVu-Sans-Bold',
                stroke_color='black',
                stroke_width=3,
                method='label'
            )
            .set_start(word_start)
            .set_duration(word_duration))

            # Position yellow word roughly where it appears in the line
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
            raise Exception("No Pexels videos found for topic: " + topic)

        # ── 3. Download & cut footage into ~4s clips ───
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

                # Resize to fill 9:16
                clip_ratio = vc.w / vc.h
                target_ratio = TARGET_W / TARGET_H
                if clip_ratio > target_ratio:
                    vc = vc.resize(height=TARGET_H)
                else:
                    vc = vc.resize(width=TARGET_W)

                # Centre-crop
                x_c = vc.w / 2
                y_c = vc.h / 2
                vc = crop(vc, width=TARGET_W, height=TARGET_H, x_center=x_c, y_center=y_c)

                # Take up to 2 segments from this clip
                clip_seg_count = min(2, int(vc.duration / segment_duration))
                for s in range(clip_seg_count):
                    start = s * segment_duration
                    if start + segment_duration <= vc.duration:
                        seg = vc.subclip(start, start + segment_duration)
                        # Subtle zoom-in effect
                        seg = seg.fl_image(lambda f: PIL.Image.fromarray(f)
                                           .resize((TARGET_W, TARGET_H), PIL.Image.LANCZOS)
                                           .__array__() if False else f)
                        segments.append(seg)
                        if len(segments) >= needed_segments:
                            break
            except Exception:
                continue

        if not segments:
            raise Exception("Could not process any video segments")

        # ── 4. Concatenate segments to match audio length ──
        jobs[job_id]['progress'] = 'Compositing video...'
        full_bg = concatenate_videoclips(segments, method='compose')
        if full_bg.duration < total_duration:
            # Loop if needed
            loops = int(total_duration / full_bg.duration) + 1
            full_bg = concatenate_videoclips([full_bg] * loops, method='compose')
        full_bg = full_bg.subclip(0, total_duration)
        full_bg = full_bg.set_fps(30)

        # ── 5. Captions ────────────────────────────────
        jobs[job_id]['progress'] = 'Adding captions...'
        caption_clips = build_caption_clips(script, total_duration, (TARGET_W, TARGET_H))

        # ── 6. Top title bar ───────────────────────────
        title_bar = ColorClip(size=(TARGET_W, 100), color=(0, 0, 0))
        title_bar = title_bar.set_opacity(0.6).set_duration(total_duration).set_position(('center', 40))
        title_text = (TextClip(
            '⚡ TECH FACTS',
            fontsize=52,
            color='#FFD700',
            font='DejaVu-Sans-Bold',
            method='label'
        ).set_duration(total_duration).set_position(('center', 48)))

        # ── 7. Compose final ───────────────────────────
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

        jobs[job_id] = {"status": "done", "path": output_path}

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


@app.route('/download/<job_id>', methods=['GET'])
def download_video(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job['status'] != 'done':
        return jsonify({"error": "Not ready", "status": job['status']}), 404
    return send_file(job['path'], mimetype='video/mp4', as_attachment=True, download_name='short.mp4')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
