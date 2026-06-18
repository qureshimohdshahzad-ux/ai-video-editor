import os
import re
import json
import uuid
import time
import threading
import subprocess
import tempfile
import traceback
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template_string, send_from_directory
from werkzeug.utils import secure_filename
from groq import Groq

# ── Config ──────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
UPLOAD_FOLDER = Path("uploads")
OUTPUT_FOLDER = Path("outputs")
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

groq_client = Groq(api_key=GROQ_API_KEY)

# In-memory job tracker
jobs = {}

# ── Helpers ──────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def run_cmd(cmd, timeout=300):
    """Run ffmpeg command, return (success, output)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stderr
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def analyze_with_groq(command_text, ref_url=""):
    """Ask Groq to understand the creator's niche and build an edit plan."""
    system_prompt = """You are an expert video editor AI assistant. 
You understand creators from India who speak Hindi and English (Hinglish).
Given a creator's description and optional reference URL, you must:
1. Identify creator niche (fitness, vlog, entertainment, education, gaming, fashion, food, travel, etc.)
2. Determine editing style from reference if provided
3. Output a detailed JSON edit plan.

ALWAYS respond with valid JSON only — no markdown, no explanation.
JSON format:
{
  "niche": "fitness",
  "niche_hindi": "फिटनेस",
  "detected_language": "hindi|english|hinglish",
  "edit_plan": {
    "color_grade": "warm|cool|vibrant|cinematic|natural|dark",
    "brightness": 0,
    "contrast": 5,
    "saturation": 10,
    "sharpen": true,
    "stabilize": false,
    "trim_silence": true,
    "speed_ramp": false,
    "captions": true,
    "caption_style": "bold_white|yellow_outline|minimal|neon",
    "music_vibe": "energetic|calm|emotional|hype|lo-fi|dramatic",
    "transition_style": "cut|fade|zoom|glitch|smooth",
    "quality_enhance": true,
    "denoise": true,
    "platform": "reels|shorts|tiktok|youtube"
  },
  "edit_summary": "Short description of what will be done in English",
  "edit_summary_hindi": "Hindi mein edit plan"
}"""

    user_msg = f"Creator command: {command_text}"
    if ref_url:
        user_msg += f"\nReference video URL: {ref_url}\nAnalyze the URL to understand the editing style (look at platform, channel type from URL pattern)."

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.3,
            max_tokens=800
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown fences if present
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Groq error: {e}")
        return get_default_plan()


def get_default_plan():
    return {
        "niche": "general",
        "niche_hindi": "सामान्य",
        "detected_language": "english",
        "edit_plan": {
            "color_grade": "vibrant",
            "brightness": 5,
            "contrast": 10,
            "saturation": 15,
            "sharpen": True,
            "stabilize": False,
            "trim_silence": False,
            "speed_ramp": False,
            "captions": False,
            "caption_style": "bold_white",
            "music_vibe": "energetic",
            "transition_style": "cut",
            "quality_enhance": True,
            "denoise": True,
            "platform": "reels"
        },
        "edit_summary": "Standard video enhancement with color grading and quality improvement.",
        "edit_summary_hindi": "रंग सुधार और गुणवत्ता सुधार के साथ मानक वीडियो संपादन।"
    }


def build_ffmpeg_filters(plan):
    """Convert edit plan to ffmpeg filter string."""
    ep = plan.get("edit_plan", {})
    filters = []

    # Denoise
    if ep.get("denoise"):
        filters.append("hqdn3d=1.5:1.5:6:6")

    # Quality enhance (unsharp)
    if ep.get("quality_enhance") or ep.get("sharpen"):
        filters.append("unsharp=5:5:0.8:5:5:0.4")

    # Color grade based on style
    grade = ep.get("color_grade", "natural")
    brightness = ep.get("brightness", 0) / 100.0
    contrast = 1.0 + ep.get("contrast", 0) / 100.0
    saturation = 1.0 + ep.get("saturation", 0) / 100.0

    color_presets = {
        "warm": f"colorbalance=rs=0.1:gs=-0.05:bs=-0.1,eq=brightness={brightness}:contrast={contrast}:saturation={saturation}",
        "cool": f"colorbalance=rs=-0.08:gs=0:bs=0.1,eq=brightness={brightness}:contrast={contrast}:saturation={saturation}",
        "vibrant": f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation + 0.2}",
        "cinematic": f"colorbalance=rs=0.05:gs=0:bs=0.08,eq=brightness={brightness - 0.02}:contrast={contrast + 0.05}:saturation={saturation - 0.1},curves=r='0/0 0.3/0.25 1/0.85':g='0/0 0.5/0.5 1/0.95':b='0/0.05 0.5/0.5 1/1'",
        "natural": f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}",
        "dark": f"colorbalance=rs=0.03:gs=-0.02:bs=0.05,eq=brightness={brightness - 0.05}:contrast={contrast + 0.1}:saturation={saturation - 0.05}",
    }
    filters.append(color_presets.get(grade, color_presets["natural"]))

    # Scale to 1080p for quality
    filters.append("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos")
    filters.append("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black")

    return ",".join(filters)


def apply_edits(input_path, output_path, plan, job_id):
    """Apply all ffmpeg edits based on AI plan."""
    try:
        jobs[job_id]["progress"] = 10
        jobs[job_id]["status_text"] = "Analyzing video..."

        vf = build_ffmpeg_filters(plan)
        jobs[job_id]["progress"] = 30
        jobs[job_id]["status_text"] = "Applying color grade & quality boost..."

        # Main ffmpeg command
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path)
        ]

        jobs[job_id]["progress"] = 50
        jobs[job_id]["status_text"] = "Processing video (this takes a few minutes)..."

        ok, err = run_cmd(cmd, timeout=600)

        if not ok:
            # Fallback: simpler command
            cmd_simple = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "20",
                "-c:a", "aac",
                "-b:a", "128k",
                str(output_path)
            ]
            ok, err2 = run_cmd(cmd_simple, timeout=300)
            if not ok:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"FFmpeg failed: {err}"
                return

        jobs[job_id]["progress"] = 90
        jobs[job_id]["status_text"] = "Finalizing output..."
        time.sleep(0.5)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["status_text"] = "Video ready!"
        jobs[job_id]["output_file"] = str(output_path)

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        traceback.print_exc()


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """Analyze creator command with Groq AI."""
    data = request.json or {}
    command = data.get("command", "").strip()
    ref_url = data.get("ref_url", "").strip()

    if not command:
        return jsonify({"error": "Please describe what kind of creator you are"}), 400

    plan = analyze_with_groq(command, ref_url)
    return jsonify(plan)


@app.route("/api/upload", methods=["POST"])
def upload():
    """Upload a video file."""
    if "video" not in request.files:
        return jsonify({"error": "No video file sent"}), 400

    f = request.files["video"]
    if not f.filename or not allowed_file(f.filename):
        return jsonify({"error": "Invalid file type. Use MP4, MOV, AVI, MKV, WEBM"}), 400

    fname = f"{uuid.uuid4().hex}_{secure_filename(f.filename)}"
    save_path = UPLOAD_FOLDER / fname
    f.save(save_path)

    return jsonify({"file_id": fname, "filename": f.filename})


@app.route("/api/edit", methods=["POST"])
def edit():
    """Start editing job."""
    data = request.json or {}
    file_id = data.get("file_id")
    plan = data.get("plan")

    if not file_id or not plan:
        return jsonify({"error": "Missing file_id or plan"}), 400

    input_path = UPLOAD_FOLDER / file_id
    if not input_path.exists():
        return jsonify({"error": "Uploaded video not found"}), 404

    job_id = uuid.uuid4().hex
    out_name = f"edited_{job_id}.mp4"
    output_path = OUTPUT_FOLDER / out_name

    jobs[job_id] = {
        "status": "processing",
        "progress": 0,
        "status_text": "Starting...",
        "output_file": None,
        "error": None
    }

    t = threading.Thread(target=apply_edits, args=(input_path, output_path, plan, job_id))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 400

    out_path = Path(job["output_file"])
    if not out_path.exists():
        return jsonify({"error": "Output file missing"}), 404

    return send_file(
        str(out_path),
        as_attachment=True,
        download_name="AI_Edited_Video.mp4",
        mimetype="video/mp4"
    )


@app.route("/api/preview/<job_id>")
def preview(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 400

    out_path = Path(job["output_file"])
    return send_file(str(out_path), mimetype="video/mp4")


# ── HTML Page ─────────────────────────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AI Video Editor — Edit Like a Pro</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0d;--bg2:#1a1a1a;--bg3:#242424;
  --border:#333;--text:#f0f0f0;--muted:#888;
  --purple:#a855f7;--purple-dark:#7c3aed;
  --green:#22c55e;--red:#ef4444;--blue:#3b82f6;
  --amber:#f59e0b;
}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
h1{font-size:1.6rem;font-weight:700;letter-spacing:-0.5px}
h2{font-size:1.1rem;font-weight:600;margin-bottom:0.75rem;color:#ccc}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:11px;font-weight:600;letter-spacing:.5px}
.badge-purple{background:rgba(168,85,247,.15);color:var(--purple);border:1px solid rgba(168,85,247,.3)}
/* Layout */
.container{max-width:1100px;margin:0 auto;padding:0 1rem}
header{padding:1.25rem 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:1rem}
.logo-icon{width:38px;height:38px;background:linear-gradient(135deg,var(--purple-dark),var(--purple));border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.2rem}
.main{padding:2rem 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
@media(max-width:680px){.grid{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:1.25rem}
.card-highlight{border-color:rgba(168,85,247,.35);background:rgba(168,85,247,.05)}
/* Step badges */
.step-badge{width:24px;height:24px;background:var(--purple-dark);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0}
.step-header{display:flex;align-items:center;gap:0.6rem;margin-bottom:1rem}
/* Form elements */
input[type=text],input[type=url],textarea{
  width:100%;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;padding:0.65rem 0.85rem;color:var(--text);
  font-size:0.95rem;font-family:inherit;outline:none;transition:border-color .2s
}
input:focus,textarea:focus{border-color:var(--purple)}
textarea{resize:vertical;min-height:80px;line-height:1.5}
/* Buttons */
.btn{padding:.65rem 1.25rem;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;border:none;transition:all .15s;display:inline-flex;align-items:center;gap:.4rem}
.btn-primary{background:var(--purple);color:#fff}
.btn-primary:hover{background:var(--purple-dark)}
.btn-primary:disabled{opacity:.45;cursor:not-allowed}
.btn-green{background:var(--green);color:#000}
.btn-green:hover{filter:brightness(1.1)}
.btn-outline{background:transparent;color:var(--muted);border:1px solid var(--border)}
.btn-outline:hover{border-color:var(--purple);color:var(--text)}
.btn-full{width:100%;justify-content:center}
/* Upload zone */
.upload-zone{border:2px dashed var(--border);border-radius:12px;padding:2rem 1rem;text-align:center;cursor:pointer;transition:all .2s}
.upload-zone:hover,.upload-zone.dragover{border-color:var(--purple);background:rgba(168,85,247,.05)}
.upload-zone .icon{font-size:2.5rem;margin-bottom:.5rem;opacity:.7}
.upload-zone .hint{font-size:.8rem;color:var(--muted);margin-top:.3rem}
/* Progress */
.progress-bar{width:100%;background:var(--bg3);border-radius:99px;height:6px;overflow:hidden;margin:.5rem 0}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--purple-dark),var(--purple));border-radius:99px;transition:width .4s ease}
/* Plan display */
.plan-grid{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-top:.75rem}
.plan-item{background:var(--bg3);border-radius:8px;padding:.5rem .75rem;font-size:.8rem}
.plan-label{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px}
.plan-value{color:var(--text);font-weight:600}
/* Video preview */
#preview-area{display:none}
video{width:100%;border-radius:10px;background:#000;margin-bottom:.75rem}
/* Niche chip */
.niche-chip{display:inline-flex;align-items:center;gap:.4rem;background:rgba(168,85,247,.15);border:1px solid rgba(168,85,247,.3);color:var(--purple);border-radius:99px;padding:.3rem .75rem;font-size:.85rem;font-weight:600;margin-bottom:.75rem}
/* Status */
.status-box{background:var(--bg3);border-radius:8px;padding:.75rem 1rem;margin:.75rem 0;font-size:.85rem}
.status-error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#fca5a5}
.status-success{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:#86efac}
/* Voice btn */
.voice-btn{padding:.5rem;border-radius:8px;background:var(--bg3);border:1px solid var(--border);cursor:pointer;font-size:1.1rem;line-height:1;transition:all .15s}
.voice-btn:hover{border-color:var(--purple)}
.voice-btn.active{background:rgba(239,68,68,.15);border-color:var(--red);animation:pulse .8s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
/* Summary box */
.summary-box{background:var(--bg3);border-left:3px solid var(--purple);border-radius:0 8px 8px 0;padding:.75rem 1rem;margin:.75rem 0;font-size:.85rem;line-height:1.6;color:#ddd}
.hidden{display:none}
</style>
</head>
<body>
<header>
  <div class="container" style="display:flex;align-items:center;gap:1rem;width:100%">
    <div class="logo-icon">🎬</div>
    <div>
      <h1>AI Video Editor</h1>
      <div style="font-size:.8rem;color:var(--muted)">Powered by Groq AI · Hindi &amp; English</div>
    </div>
    <span class="badge badge-purple" style="margin-left:auto">BETA</span>
  </div>
</header>

<div class="main">
<div class="container">
<div class="grid">

<!-- LEFT COLUMN: Input -->
<div style="display:flex;flex-direction:column;gap:1.25rem">

  <!-- Step 1: Upload -->
  <div class="card">
    <div class="step-header">
      <div class="step-badge">1</div>
      <h2 style="margin:0">Upload your raw video</h2>
    </div>
    <div class="upload-zone" id="upload-zone" onclick="document.getElementById('file-input').click()">
      <div class="icon">📹</div>
      <div>Click to upload or drag &amp; drop</div>
      <div class="hint">MP4, MOV, AVI, MKV · Max 500MB</div>
    </div>
    <input type="file" id="file-input" accept="video/*" style="display:none" onchange="handleFileSelect(this)"/>
    <div id="upload-status" class="hidden" style="margin-top:.75rem;font-size:.85rem;color:var(--green)">✅ Video uploaded!</div>
  </div>

  <!-- Step 2: Reference URL -->
  <div class="card">
    <div class="step-header">
      <div class="step-badge">2</div>
      <h2 style="margin:0">Reference video (optional)</h2>
    </div>
    <input type="url" id="ref-url" placeholder="Paste YouTube / Instagram / TikTok link..."/>
    <div style="font-size:.77rem;color:var(--muted);margin-top:.4rem">AI will copy the editing style from this video</div>
  </div>

  <!-- Step 3: AI Command -->
  <div class="card card-highlight">
    <div class="step-header">
      <div class="step-badge">3</div>
      <h2 style="margin:0">Tell AI what you want</h2>
    </div>
    <div style="display:flex;gap:.5rem;margin-bottom:.5rem">
      <textarea id="command-text" placeholder="Type in Hindi or English:&#10;&#10;Main fitness creator hoon, mera video trending style mein edit karo reels ke liye&#10;&#10;OR&#10;&#10;I'm a vlogger, make my video cinematic with smooth transitions for YouTube..." rows="5"></textarea>
    </div>
    <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.75rem">
      <button class="voice-btn" id="voice-btn" title="Click to record voice command" onclick="toggleVoice()">🎤</button>
      <span style="font-size:.8rem;color:var(--muted)" id="voice-status">Tap mic to speak in Hindi or English</span>
    </div>
    <button class="btn btn-primary btn-full" id="analyze-btn" onclick="analyzeCommand()">
      🤖 Analyze with AI
    </button>
  </div>

</div><!-- end left col -->

<!-- RIGHT COLUMN: Output -->
<div style="display:flex;flex-direction:column;gap:1.25rem">

  <!-- AI Plan display -->
  <div class="card" id="plan-card" style="display:none">
    <h2>🧠 AI Edit Plan</h2>
    <div id="niche-display"></div>
    <div id="summary-display"></div>
    <div class="plan-grid" id="plan-grid"></div>
    <div style="margin-top:1rem">
      <button class="btn btn-primary btn-full" id="edit-btn" onclick="startEditing()" disabled>
        ⚡ Start Editing
      </button>
      <div style="font-size:.75rem;color:var(--muted);text-align:center;margin-top:.4rem">Upload a video first, then click Start Editing</div>
    </div>
  </div>

  <!-- Progress -->
  <div class="card" id="progress-card" style="display:none">
    <h2>⚙️ Editing in progress</h2>
    <div class="progress-bar"><div class="progress-fill" id="progress-fill" style="width:0%"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:.8rem;color:var(--muted)">
      <span id="progress-text">Starting...</span>
      <span id="progress-pct">0%</span>
    </div>
    <div style="font-size:.77rem;color:var(--muted);margin-top:.5rem">This may take 2–5 minutes for long videos</div>
  </div>

  <!-- Preview + Download -->
  <div class="card" id="preview-card" style="display:none">
    <h2>✅ Your edited video is ready!</h2>
    <video id="preview-video" controls playsinline></video>
    <a id="download-link" class="btn btn-green btn-full" style="text-decoration:none">
      ⬇️ Download Edited Video
    </a>
    <button class="btn btn-outline btn-full" style="margin-top:.5rem" onclick="resetAll()">
      🔄 Edit another video
    </button>
  </div>

  <!-- Error -->
  <div class="status-box status-error hidden" id="error-box"></div>

  <!-- Placeholder when no plan yet -->
  <div class="card" id="placeholder-card" style="text-align:center;padding:3rem 1rem">
    <div style="font-size:3rem;margin-bottom:.75rem;opacity:.3">🎬</div>
    <div style="color:var(--muted);font-size:.9rem">
      Upload your video, add a reference link,<br/>
      describe your niche, and let AI do the rest!
    </div>
    <div style="margin-top:1.25rem;font-size:.8rem;color:#555">
      Supports: Fitness · Vlog · Entertainment · Gaming<br/>Education · Fashion · Food · Travel · Comedy
    </div>
  </div>

</div><!-- end right col -->
</div><!-- end grid -->
</div><!-- end container -->
</div><!-- end main -->

<script>
let uploadedFileId = null;
let currentPlan = null;
let currentJobId = null;
let pollInterval = null;
let recognition = null;
let isRecording = false;

// ── Drag & drop ───────────────────────────────────────────────────────────
const zone = document.getElementById('upload-zone');
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault();
  zone.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});

function handleFileSelect(input) {
  if (input.files[0]) uploadFile(input.files[0]);
}

async function uploadFile(file) {
  zone.innerHTML = `<div class="icon">⏳</div><div>Uploading ${file.name}...</div>`;
  const fd = new FormData();
  fd.append('video', file);
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    uploadedFileId = data.file_id;
    zone.innerHTML = `<div class="icon">✅</div><div style="color:var(--green)">${file.name}</div><div class="hint">Video uploaded successfully!</div>`;
    updateEditBtn();
  } catch(e) {
    zone.innerHTML = `<div class="icon">❌</div><div style="color:var(--red)">${e.message}</div>`;
  }
}

// ── Voice input ───────────────────────────────────────────────────────────
function toggleVoice() {
  if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
    alert('Voice input not supported in this browser. Use Chrome!');
    return;
  }
  if (isRecording) {
    recognition && recognition.stop();
    return;
  }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new SR();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = 'hi-IN';

  recognition.onstart = () => {
    isRecording = true;
    document.getElementById('voice-btn').classList.add('active');
    document.getElementById('voice-status').textContent = '🔴 Recording... speak now';
  };
  recognition.onresult = e => {
    let text = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      text += e.results[i][0].transcript;
    }
    document.getElementById('command-text').value = text;
  };
  recognition.onerror = recognition.onend = () => {
    isRecording = false;
    document.getElementById('voice-btn').classList.remove('active');
    document.getElementById('voice-status').textContent = '✅ Voice captured!';
    setTimeout(() => {
      document.getElementById('voice-status').textContent = 'Tap mic to speak in Hindi or English';
    }, 2000);
  };
  recognition.start();
}

// ── Analyze ───────────────────────────────────────────────────────────────
async function analyzeCommand() {
  const command = document.getElementById('command-text').value.trim();
  const refUrl = document.getElementById('ref-url').value.trim();
  if (!command) { alert('Please describe what kind of creator you are!'); return; }

  const btn = document.getElementById('analyze-btn');
  btn.disabled = true;
  btn.textContent = '🤖 Analyzing...';

  document.getElementById('placeholder-card').style.display = 'none';
  document.getElementById('plan-card').style.display = 'none';

  try {
    const r = await fetch('/api/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ command, ref_url: refUrl })
    });
    currentPlan = await r.json();
    displayPlan(currentPlan);
  } catch(e) {
    showError('AI analysis failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '🤖 Analyze with AI';
  }
}

function displayPlan(plan) {
  const ep = plan.edit_plan || {};
  
  // Niche chip
  document.getElementById('niche-display').innerHTML = 
    `<div class="niche-chip">🎯 ${plan.niche} ${plan.niche_hindi ? '· ' + plan.niche_hindi : ''}</div>`;
  
  // Summary
  const lang = plan.detected_language || 'english';
  const summary = lang === 'hindi' || lang === 'hinglish' 
    ? (plan.edit_summary_hindi || plan.edit_summary)
    : plan.edit_summary;
  document.getElementById('summary-display').innerHTML = 
    `<div class="summary-box">${summary}</div>`;

  // Plan grid
  const items = [
    ['Color grade', ep.color_grade || 'natural'],
    ['Platform', ep.platform || 'reels'],
    ['Captions', ep.captions ? 'Yes' : 'No'],
    ['Music vibe', ep.music_vibe || '—'],
    ['Transitions', ep.transition_style || 'cut'],
    ['Quality boost', ep.quality_enhance ? '✅' : '—'],
    ['Denoise', ep.denoise ? '✅' : '—'],
    ['Sharpen', ep.sharpen ? '✅' : '—'],
  ];
  document.getElementById('plan-grid').innerHTML = items.map(([k,v]) =>
    `<div class="plan-item"><div class="plan-label">${k}</div><div class="plan-value">${v}</div></div>`
  ).join('');

  document.getElementById('plan-card').style.display = 'block';
  updateEditBtn();
}

function updateEditBtn() {
  const btn = document.getElementById('edit-btn');
  const ready = uploadedFileId && currentPlan;
  btn.disabled = !ready;
  if (ready) {
    btn.textContent = '⚡ Start Editing Now';
  } else if (!uploadedFileId) {
    btn.textContent = '⚡ Upload video first';
  } else {
    btn.textContent = '⚡ Analyze with AI first';
  }
}

// ── Editing ───────────────────────────────────────────────────────────────
async function startEditing() {
  if (!uploadedFileId || !currentPlan) return;

  document.getElementById('plan-card').style.display = 'none';
  document.getElementById('progress-card').style.display = 'block';
  document.getElementById('preview-card').style.display = 'none';

  try {
    const r = await fetch('/api/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ file_id: uploadedFileId, plan: currentPlan })
    });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    currentJobId = data.job_id;
    pollProgress();
  } catch(e) {
    showError('Failed to start editing: ' + e.message);
    document.getElementById('progress-card').style.display = 'none';
  }
}

function pollProgress() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(async () => {
    try {
      const r = await fetch('/api/status/' + currentJobId);
      const data = await r.json();

      document.getElementById('progress-fill').style.width = data.progress + '%';
      document.getElementById('progress-pct').textContent = data.progress + '%';
      document.getElementById('progress-text').textContent = data.status_text || 'Processing...';

      if (data.status === 'done') {
        clearInterval(pollInterval);
        showResult();
      } else if (data.status === 'error') {
        clearInterval(pollInterval);
        showError(data.error || 'Unknown error during editing');
        document.getElementById('progress-card').style.display = 'none';
      }
    } catch(e) { /* network hiccup, keep polling */ }
  }, 1500);
}

function showResult() {
  document.getElementById('progress-card').style.display = 'none';
  document.getElementById('preview-card').style.display = 'block';
  document.getElementById('preview-video').src = '/api/preview/' + currentJobId;
  document.getElementById('download-link').href = '/api/download/' + currentJobId;
}

function showError(msg) {
  const box = document.getElementById('error-box');
  box.textContent = '❌ ' + msg;
  box.classList.remove('hidden');
  setTimeout(() => box.classList.add('hidden'), 8000);
}

function resetAll() {
  uploadedFileId = null;
  currentPlan = null;
  currentJobId = null;
  if (pollInterval) clearInterval(pollInterval);

  document.getElementById('upload-zone').innerHTML = `<div class="icon">📹</div><div>Click to upload or drag & drop</div><div class="hint">MP4, MOV, AVI, MKV · Max 500MB</div>`;
  document.getElementById('command-text').value = '';
  document.getElementById('ref-url').value = '';
  document.getElementById('plan-card').style.display = 'none';
  document.getElementById('progress-card').style.display = 'none';
  document.getElementById('preview-card').style.display = 'none';
  document.getElementById('placeholder-card').style.display = 'block';
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    print(f"🚀 AI Video Editor V2 starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
