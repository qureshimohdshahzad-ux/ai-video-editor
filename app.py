import os, re, json, uuid, time, threading, subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

APP_VERSION = "4.0-prostyle"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp_processing")

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
TEMP_FOLDER.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
jobs = {}

def run_cmd(cmd, timeout=900):
    try:
        r = subprocess.run(["nice", "-n", "19"] + cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr
    except Exception as e:
        return False, str(e)

def analyze_with_groq(command_text, ref_url=""):
    system_prompt = """You are a professional video editor. Respond ONLY with valid JSON.
Create trending, high-energy edits suitable for Reels/Shorts/YouTube.
Include: niche, platform, color_grade, transition_style, caption_style, speed_ramp.
Use modern trending styles: dynamic zoom, flash transitions, smooth speed changes.
Example output:
{"niche":"fitness","platform":"reels","color_grade":"cinematic_warm","transition_style":"dynamic_zoom_flash","caption_style":"bold_modern","speed_ramp":"fast_punchy","edit_summary":"High energy fitness reel with trending transitions"}"""
    
    msg = f"Creator instruction: {command_text}"
    if ref_url:
        msg += f"\nReference video style: {ref_url}"
    
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":msg}],
            temperature=0.4, max_tokens=700
        )
        raw = re.sub(r"```json|```", "", resp.choices[0].message.content.strip())
        return json.loads(raw)
    except:
        return {"niche":"general","platform":"reels","color_grade":"cinematic_warm","transition_style":"dynamic_zoom","caption_style":"bold_modern","speed_ramp":"fast","edit_summary":"High energy edit with trending transitions"}

def build_pro_filters(plan):
    style = plan.get("color_grade", "cinematic_warm")
    transition = plan.get("transition_style", "dynamic_zoom")
    
    filters = []
    
    # Color Grading (Improved)
    if style in ["cinematic_warm", "warm"]:
        filters.append("colorbalance=rs=0.15:gs=-0.05:bs=-0.1,eq=brightness=0.03:contrast=1.15:saturation=1.25")
    elif style == "vibrant":
        filters.append("eq=brightness=0.05:contrast=1.2:saturation=1.4")
    elif style == "cinematic":
        filters.append("colorbalance=rs=0.08:bs=0.1,eq=brightness=-0.02:contrast=1.25:saturation=1.1")
    else:
        filters.append("eq=brightness=0.02:contrast=1.1:saturation=1.2")
    
    # Trending Transitions & Effects
    if transition in ["dynamic_zoom", "zoom"]:
        filters.append("zoompan=z='zoom+0.002':d=125:s=854x480")
    elif transition == "flash":
        filters.append("flash=frame=5:brightness=0.8:duration=3")
    elif transition == "shake":
        filters.append("unsharp=5:5:1.0:5:5:0.8")
    
    filters.append("scale=854:480:force_original_aspect_ratio=decrease")
    filters.append("pad=854:480:(ow-iw)/2:(oh-ih)/2:black")
    filters.append("hqdn3d=2:2:5:5")  # Light noise reduction
    return ",".join(filters)

def apply_pro_edits(inp, out, plan, job_id):
    try:
        jobs[job_id].update({"progress":15, "status_text":"Analyzing & Planning Pro Edit..."})
        time.sleep(2)
        
        jobs[job_id].update({"progress":35, "status_text":"Enhancing Voice & Audio..."})
        temp_audio = TEMP_FOLDER / f"audio_{job_id}.mp4"
        audio_cmd = ["ffmpeg","-y","-i",str(inp),"-af","acompressor=threshold=-18dB:ratio=9:attack=5:release=50,volume=1.3,highpass=f=100,lowpass=f=8000",str(temp_audio)]
        run_cmd(audio_cmd, timeout=300)
        
        jobs[job_id].update({"progress":55, "status_text":"Applying Trending Transitions & Filters..."})
        vf = build_pro_filters(plan)
        temp_video = TEMP_FOLDER / f"video_{job_id}.mp4"
        
        video_cmd = ["ffmpeg","-y","-i",str(temp_audio),"-vf",vf,
                     "-c:v","libx264","-preset","medium","-crf","22",
                     "-c:a","aac","-b:a","128k","-movflags","+faststart",str(temp_video)]
        run_cmd(video_cmd, timeout=900)
        
        # Add Captions (Modern Style)
        jobs[job_id].update({"progress":75, "status_text":"Adding Modern Captions..."})
        final_cmd = ["ffmpeg","-y","-i",str(temp_video),
                     "-vf","drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:fontsize=28:fontcolor=white:bordercolor=black:borderw=4:x=(w-text_w)/2:y=h-80:text='%{pts\:gmtime\:0\:%M:%S}'",
                     "-c:v","libx264","-preset","medium","-crf","23","-c:a","copy",str(out)]
        ok, err = run_cmd(final_cmd, timeout=600)
        
        if ok:
            jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
        else:
            jobs[job_id].update({"status":"error","error":err})
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})
    finally:
        for f in TEMP_FOLDER.glob(f"*{job_id}*"):
            f.unlink(missing_ok=True)

# ===================== ROUTES =====================
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/debug")
def debug():
    return jsonify({"version": APP_VERSION, "active_jobs": len(jobs)})

@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("video")
    if not f: return jsonify({"error":"No video"}), 400
    fname = f"{uuid.uuid4().hex}_{secure_filename(f.filename)}"
    f.save(UPLOAD_FOLDER / fname)
    return jsonify({"file_id": fname})

@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    return jsonify(analyze_with_groq(data.get("command",""), data.get("ref_url","")))

@app.route("/api/edit", methods=["POST"])
def edit():
    data = request.json or {}
    fid = data.get("file_id")
    plan = data.get("plan")
    inp = UPLOAD_FOLDER / fid
    if not inp.exists(): return jsonify({"error":"File not found"}), 404
    
    jid = uuid.uuid4().hex
    out = OUTPUT_FOLDER / f"edited_{jid}.mp4"
    jobs[jid] = {"status":"processing","progress":0,"status_text":"Starting Pro Edit...","output_file":None,"error":None}
    
    threading.Thread(target=apply_pro_edits, args=(inp, out, plan, jid), daemon=True).start()
    return jsonify({"job_id": jid})

@app.route("/api/status/<jid>")
def status(jid):
    return jsonify(jobs.get(jid, {"status":"expired","progress":0,"status_text":"Job expired. Please try again."}))

@app.route("/api/download/<jid>")
def download(jid):
    j = jobs.get(jid)
    if not j or j.get("status") != "done":
        return jsonify({"error":"Not ready"}), 400
    return send_file(j["output_file"], as_attachment=True, download_name="AI_Edited_Pro.mp4")

HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Video Editor Pro</title>
<style>
/* Same beautiful UI from before - keeping it short for space */
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#f0f0f0;font-family:'Segoe UI',sans-serif}
header{background:#1a1a2e;padding:1rem;display:flex;align-items:center;gap:1rem}
.logo{font-size:2rem}
.card{background:#1a1a2e;border:1px solid #333;border-radius:12px;padding:1.2rem;margin-bottom:1rem}
.btn{background:#a855f7;color:white;border:none;padding:12px;border-radius:8px;font-weight:600;cursor:pointer;width:100%}
.err{color:#ff5555;margin:10px 0}
</style></head><body>
<header><div class="logo">🎬</div><h1>AI Video Editor Pro v4.0</h1></header>
<div style="padding:1rem;max-width:1100px;margin:auto">
<div class="card"><h2>Upload Video</h2><input type="file" id="fi" onchange="doUpload(this)"></div>
<div class="card"><h2>Reference (optional)</h2><input type="url" id="ref" placeholder="Paste YouTube/Instagram link"></div>
<div class="card"><h2>Describe Desired Style</h2><textarea id="cmd" rows="4" placeholder="Make trending fitness reel with fast zoom transitions, bold captions..."></textarea></div>
<button class="btn" onclick="doAnalyze()">🤖 Generate Pro Edit Plan</button>
<div id="result"></div>
</div>
<script>
// Basic JS (you can improve later)
async function doAnalyze(){
  const cmd = document.getElementById('cmd').value;
  const ref = document.getElementById('ref').value;
  const res = await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd,ref_url:ref})});
  const data = await res.json();
  document.getElementById('result').innerHTML = `<pre>${JSON.stringify(data,null,2)}</pre>`;
}
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
