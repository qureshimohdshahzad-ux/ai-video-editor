import os, re, json, uuid, time, threading, subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename

try:
    from groq import Groq
except ImportError:
    Groq = None

APP_VERSION = "v7-safe"

UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp_processing")
for f in [UPLOAD_FOLDER, OUTPUT_FOLDER, TEMP_FOLDER]:
    try:
        f.mkdir(exist_ok=True, parents=True)
    except:
        pass

ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
groq_client = None
if GROQ_API_KEY and Groq:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
        print("Groq initialized")
    except Exception as e:
        print(f"Groq init failed: {e}")
        groq_client = None

jobs = {}

def allowed_file(f):
    return "." in f and f.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def run_cmd(cmd, timeout=400):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr or ""
    except Exception as e:
        return False, str(e)

def get_duration(path):
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout)
        return float(d.get("format", {}).get("duration", 10.0))
    except:
        return 10.0

def extract_audio(video_path, audio_path):
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "mp3", "-ar", "16000", "-ac", "1",
        "-b:a", "64k", str(audio_path)
    ]
    return run_cmd(cmd, timeout=120)

def transcribe_with_whisper(audio_path, language="hi"):
    if not groq_client:
        return None
    try:
        with open(str(audio_path), "rb") as af:
            audio_data = af.read()
        result = groq_client.audio.transcriptions.create(
            file=(audio_path.name, audio_data),
            model="whisper-large-v3-turbo",
            response_format="verbose_json",
            language=language,
            temperature=0.0
        )
        return result
    except Exception as e:
        print(f"Whisper error (hi): {e}")
        try:
            with open(str(audio_path), "rb") as af:
                audio_data = af.read()
            result = groq_client.audio.transcriptions.create(
                file=(audio_path.name, audio_data),
                model="whisper-large-v3-turbo",
                response_format="verbose_json",
                temperature=0.0
            )
            return result
        except Exception as e2:
            print(f"Whisper error (auto): {e2}")
            return None

def create_srt_from_whisper(whisper_result, srt_path):
    try:
        segments = whisper_result.segments if hasattr(whisper_result, 'segments') else whisper_result.get('segments', [])
        with open(srt_path, 'w', encoding='utf-8') as f:
            for i, seg in enumerate(segments, 1):
                start = seg['start'] if isinstance(seg, dict) else seg.start
                end = seg['end'] if isinstance(seg, dict) else seg.end
                text = seg['text'] if isinstance(seg, dict) else seg.text
                def fmt(t):
                    h = int(t // 3600)
                    m = int((t % 3600) // 60)
                    s = int(t % 60)
                    ms = int((t - int(t)) * 1000)
                    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
                words = text.strip().split()
                if len(words) > 4:
                    text = " ".join(words[:4])
                text = text.upper().strip()
                f.write(f"{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n")
        return True
    except Exception as e:
        print(f"SRT error: {e}")
        return False

def clean_plan(raw):
    if not isinstance(raw, dict):
        raw = {}
    ep = raw.get('edit_plan') or {}
    if not isinstance(ep, dict):
        ep = {}
    return {
        'niche': str(raw.get('niche', 'general')),
        'detected_language': str(raw.get('detected_language', 'english')),
        'edit_plan': {
            'color_grade': str(ep.get('color_grade', 'vibrant')),
            'brightness': max(-15, min(20, int(ep.get('brightness', 5) or 5))),
            'contrast': max(-15, min(25, int(ep.get('contrast', 15) or 15))),
            'saturation': max(-15, min(30, int(ep.get('saturation', 20) or 20))),
            'sharpen': bool(ep.get('sharpen', True)),
            'auto_captions': bool(ep.get('auto_captions', True)),
            'add_fade': bool(ep.get('add_fade', True))
        },
        'edit_summary': str(raw.get('edit_summary', 'Pro edit applied.'))
    }

def analyze_with_groq(command_text, ref_url=''):
    if not groq_client:
        return clean_plan({})
    system_prompt = 'You are FFmpeg video editor AI. Respond ONLY with valid JSON: {"niche":"fitness","detected_language":"english","edit_plan":{"color_grade":"vibrant","brightness":5,"contrast":15,"saturation":20,"sharpen":true,"auto_captions":true,"add_fade":true},"edit_summary":"Summary"}. color_grade options: vibrant, cinematic_warm, cool, dark, natural.'
    msg = f'Creator request: {command_text}'
    if ref_url:
        msg += f' Reference: {ref_url}'
    try:
        resp = groq_client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': msg}
            ],
            temperature=0.2,
            max_tokens=300
        )
        raw = re.sub(r'```json|```', '', resp.choices[0].message.content.strip()).strip()
        return clean_plan(json.loads(raw))
    except Exception as e:
        print(f'Groq error: {e}')
        return clean_plan({})

def apply_pro_edits(inp, out, plan, job_id):
    
