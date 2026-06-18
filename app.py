import os, re, json, uuid, time, threading, subprocess, traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp_processing")

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
TEMP_FOLDER.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

groq_client = None
if GROQ_API_KEY:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        print(f"Groq init error: {e}")

jobs = {}

def allowed_file(f):
    return "." in f and f.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def run_cmd(cmd, timeout=600):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr
    except Exception as e:
        return False, str(e)

def analyze_with_groq(command_text, ref_url=""):
    if not groq_client:
        return {
            "niche":"general","niche_hindi":"सामान्य","detected_language":"english",
            "edit_plan":{"color_grade":"vibrant","brightness":5,"contrast":10,"saturation":15,
                         "sharpen":True,"quality_enhance":True,"denoise":True,"platform":"reels"},
            "edit_summary":"Standard enhancement applied.","edit_summary_hindi":"मानक सुधार लागू।"
        }

    system_prompt = """You are an expert video editor AI. Respond ONLY with valid JSON, no markdown.
Format:
{"niche":"fitness","niche_hindi":"फिटनेस","detected_language":"hindi","edit_plan":{"color_grade":"warm","brightness":5,"contrast":10,"saturation":15,"sharpen":true,"quality_enhance":true,"denoise":true,"platform":"reels"},"edit_summary":"English summary","edit_summary_hindi":"Hindi summary"}"""
    msg = f"Creator: {command_text}"
    if ref_url:
        msg += f"\nReference: {ref_url}"
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":msg}],
            temperature=0.3, max_tokens=600
        )
        raw = re.sub(r"```json|```","", resp.choices[0].message.content.strip()).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Groq error: {e}")
        return {
            "niche":"general","niche_hindi":"सामान्य","detected_language":"english",
            "edit_plan":{"color_grade":"vibrant","brightness":5,"contrast":10,"saturation":15,
                         "sharpen":True,"quality_enhance":True,"denoise":True,"platform":"reels"},
            "edit_summary":"Standard enhancement applied.","edit_summary_hindi":"मानक सुधार लागू।"
        }

def build_filters(plan):
    ep = plan.get("edit_plan", {})
    f = []
    if ep.get("denoise"): f.append("hqdn3d=1.5:1.5:6:6")
    if ep.get("sharpen"): f.append("unsharp=5:5:0.8:5:5:0.4")
    b = ep.get("brightness", 0) / 100.0
    c = 1.0 + ep.get("contrast", 0) / 100.0
    s = 1.0 + ep.get("saturation", 0) / 100.0
    g = ep.get("color_grade","natural")
    presets = {
        "warm": f"colorbalance=rs=0.1:gs=-0.05:bs=-0.1,eq=brightness={b}:contrast={c}:saturation={s}",
        "cool": f"colorbalance=rs=-0.08:bs=0.1,eq=brightness={b}:contrast={c}:saturation={s}",
        "vibrant": f"eq=brightness={b}:contrast={c}:saturation={s+0.2}",
        "cinematic": f"colorbalance=rs=0.05:bs=0.08,eq=brightness={b}:contrast={c}:saturation={s}",
        "dark": f"eq=brightness={b-0.05}:contrast={c+0.1}:saturation={s}",
        "natural": f"eq=brightness={b}:contrast={c}:saturation={s}",
    }
    f.append(presets.get(g, presets["natural"]))
    f.append("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos")
    f.append("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black")
    return ",".join(f)

def apply_edits(inp, out, plan, job_id):
    try:
        jobs[job_id].update({"progress":20,"status_text":"Applying AI edits..."})
        vf = build_filters(plan)
        cmd = ["ffmpeg","-y","-i",str(inp),"-vf",vf,
               "-c:v","libx264","-preset","fast","-crf","20",
               "-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)]
        jobs[job_id].update({"progress":50,"status_text":"Processing video..."})
        ok, err = run_cmd(cmd)
        if not ok:
            cmd2 = ["ffmpeg","-y","-i",str(inp),"-c:v","libx264","-preset","ultrafast",
                    "-crf","23","-c:a","aac",str(out)]
            ok, err2 = run_cmd(cmd2)
            if not ok:
                jobs[job_id].update({"status":"error","error":err}); return
        jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})

def apply_edits_two_step(inp, out, plan, job_id):
    try:
        # STEP 1: Pre-process to lightweight intermediate (saves memory!)
        jobs[job_id].update({"progress":10,"status_text":"Step 1/2: Optimizing video..."})
        temp_file = TEMP_FOLDER / f"temp_{job_id}.mp4"

        # First pass: Resize to 1080p max, reduce bitrate (memory-friendly)
        pre_cmd = [
            "ffmpeg", "-y", "-i", str(inp),
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            str(temp_file)
        ]

        ok, err = run_cmd(pre_cmd, timeout=300)
        if not ok:
            jobs[job_id].update({"status":"error","error":f"Step 1 failed: {err}"})
            return

        # STEP 2: Apply AI filters on smaller file
        jobs[job_id].update({"progress":50,"status_text":"Step 2/2: Applying AI effects..."})
        vf = build_filters(plan)

        final_cmd = [
            "ffmpeg", "-y", "-i", str(temp_file),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart", str(out)
        ]

        ok, err = run_cmd(final_cmd, timeout=300)

        # Cleanup temp file immediately to free space
        if temp_file.exists():
            temp_file.unlink()

        if not ok:
            jobs[job_id].update({"status":"error","error":f"Step 2 failed: {err}"})
            return

        jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})

    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    cmd = data.get("command","").strip()
    if not cmd: return jsonify({"error":"Please describe your niche"}), 400
    return jsonify(analyze_with_groq(cmd, data.get("ref_url","")))

@app.route("/api/upload", methods=["POST"])
def upload():
    if "video" not in request.files: return jsonify({"error":"No video"}), 400
    f = request.files["video"]
    if not f.filename or not allowed_file(f.filename):
        return jsonify({"error":"Invalid file type"}), 400
    fname = f"{uuid.uuid4().hex}_{secure_filename(f.filename)}"
    f.save(UPLOAD_FOLDER / fname)
    return jsonify({"file_id": fname})

@app.route("/api/edit", methods=["POST"])
def edit():
    data = request.json or {}
    fid, plan = data.get("file_id"), data.get("plan")
    if not fid or not plan: return jsonify({"error":"Missing data"}), 400
    inp = UPLOAD_FOLDER / fid
    if not inp.exists(): return jsonify({"error":"File not found"}), 404

    jid = uuid.uuid4().hex
    out = OUTPUT_FOLDER / f"edited_{jid}.mp4"
    jobs[jid] = {"status":"processing","progress":0,"status_text":"Starting...","output_file":None,"error":None}

    # Smart routing: Use 2-step for videos > 30MB (avoids memory crashes)
    file_size_mb = inp.stat().st_size / (1024 * 1024)
    if file_size_mb > 30:
        threading.Thread(target=apply_edits_two_step, args=(inp, out, plan, jid), daemon=True).start()
    else:
        threading.Thread(target=apply_edits, args=(inp, out, plan, jid), daemon=True).start()

    return jsonify({"job_id": jid})

@app.route("/api/status/<jid>")
def status(jid):
    j = jobs.get(jid)
    if j:
        return jsonify(j)
    else:
        return jsonify({"status":"expired","progress":0,"status_text":"Job expired. Please restart edit.","error":"Job ID not found (server may have restarted)"}), 404

@app.route("/api/download/<jid>")
def download(jid):
    j = jobs.get(jid)
    if not j or j["status"] != "done": return jsonify({"error":"Not ready"}), 400
    return send_file(j["output_file"], as_attachment=True, download_name="AI_Edited.mp4")

@app.route("/api/preview/<jid>")
def preview(jid):
    j = jobs.get(jid)
    if not j or j["status"] != "done": return jsonify({"error":"Not ready"}), 400
    return send_file(j["output_file"], mimetype="video/mp4")

HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>AI Video Editor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d0d0d;--bg2:#1a1a2e;--bg3:#242424;--border:#333;--text:#f0f0f0;--muted:#888;--purple:#a855f7;--pd:#7c3aed;--green:#22c55e;--red:#ef4444}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;padding:0 1rem}
header{padding:1.2rem 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:1rem}
.logo{width:36px;height:36px;background:var(--pd);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:1.2rem}
h1{font-size:1.4rem;font-weight:700}
.main{padding:1.5rem 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}
@media(max-width:650px){.grid{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:13px;padding:1.1rem}
.hl{border-color:rgba(168,85,247,.4)}
.sh{display:flex;align-items:center;gap:.55rem;margin-bottom:.9rem}
.sn{width:22px;height:22px;background:var(--pd);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}
h2{font-size:.95rem;font-weight:600;color:#ccc;margin:0}
input,textarea{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:.6rem .8rem;color:var(--text);font-size:.9rem;font-family:inherit;outline:none}
input:focus,textarea:focus{border-color:var(--purple)}
textarea{resize:vertical;min-height:85px;line-height:1.5;margin-bottom:.45rem}
.btn{padding:.6rem 1.1rem;border-radius:8px;font-size:.88rem;font-weight:600;cursor:pointer;border:none;display:flex;align-items:center;gap:.35rem;justify-content:center;width:100%;transition:all .15s}
.bp{background:var(--purple);color:#fff}.bp:hover{background:var(--pd)}.bp:disabled{opacity:.4;cursor:not-allowed}
.bg{background:var(--green);color:#000;text-decoration:none}
.bo{background:transparent;color:var(--muted);border:1px solid var(--border);margin-top:.45rem}
.uzone{border:2px dashed var(--border);border-radius:11px;padding:1.8rem 1rem;text-align:center;cursor:pointer;transition:all .2s}
.uzone:hover{border-color:var(--purple);background:rgba(168,85,247,.05)}
.ico{font-size:2.2rem;margin-bottom:.4rem}
.hint{font-size:.75rem;color:var(--muted);margin-top:.25rem}
.pbar{width:100%;background:var(--bg3);border-radius:99px;height:5px;overflow:hidden;margin:.45rem 0}
.pfill{height:100%;background:linear-gradient(90deg,var(--pd),var(--purple));border-radius:99px;transition:width .4s}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:.35rem;margin-top:.6rem}
.pi{background:var(--bg3);border-radius:7px;padding:.4rem .65rem;font-size:.78rem}
.pl{color:var(--muted);font-size:.68rem;text-transform:uppercase;margin-bottom:1px}
.pv{font-weight:600}
.chip{display:inline-flex;align-items:center;background:rgba(168,85,247,.15);border:1px solid rgba(168,85,247,.3);color:var(--purple);border-radius:99px;padding:.28rem .7rem;font-size:.82rem;font-weight:600;margin-bottom:.65rem}
.summ{background:var(--bg3);border-left:3px solid var(--purple);border-radius:0 7px 7px 0;padding:.65rem .9rem;margin:.45rem 0;font-size:.82rem;line-height:1.55;color:#ddd}
.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#fca5a5;border-radius:8px;padding:.65rem .9rem;font-size:.82rem;margin:.4rem 0;display:none}
video{width:100%;border-radius:9px;background:#000;margin-bottom:.65rem}
.vbtn{padding:.45rem;border-radius:7px;background:var(--bg3);border:1px solid var(--border);cursor:pointer;font-size:1rem;flex-shrink:0}
.vbtn.on{background:rgba(239,68,68,.15);border-color:var(--red);animation:p .8s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.5}}
.row{display:flex;gap:.45rem;align-items:center;margin-bottom:.45rem}
.ph{text-align:center;padding:2.5rem 1rem}
</style></head><body>
<header><div class="wrap" style="display:flex;align-items:center;gap:1rem;width:100%">
  <div class="logo">🎬</div>
  <div><h1>AI Video Editor</h1><div style="font-size:.75rem;color:var(--muted)">Groq AI · Hindi & English</div></div>
</div></header>
<div class="main"><div class="wrap"><div class="grid">
<div style="display:flex;flex-direction:column;gap:1.1rem">
  <div class="card">
    <div class="sh"><div class="sn">1</div><h2>Upload your raw video</h2></div>
    <div class="uzone" id="zone" onclick="document.getElementById('fi').click()">
      <div class="ico">📹</div><div>Click or drag & drop</div>
      <div class="hint">MP4 MOV AVI MKV · Max 500MB</div>
    </div>
    <input type="file" id="fi" accept="video/*" style="display:none" onchange="doUpload(this)"/>
  </div>
  <div class="card">
    <div class="sh"><div class="sn">2</div><h2>Reference link (optional)</h2></div>
    <input type="url" id="ref" placeholder="YouTube / Instagram / TikTok URL..."/>
    <div class="hint" style="margin-top:.35rem">AI copies the editing style</div>
  </div>
  <div class="card hl">
    <div class="sh"><div class="sn">3</div><h2>Tell AI what you want</h2></div>
    <textarea id="cmd" placeholder="Hindi ya 
