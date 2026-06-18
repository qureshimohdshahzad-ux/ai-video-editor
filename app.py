import os
import re
import json
import uuid
import time
import threading
import subprocess
import traceback
from pathlib import Path
 
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq
 
# ── Config ──────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp_processing") # <-- NEW ASSISTANCE FOLDER

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
TEMP_FOLDER.mkdir(exist_ok=True) # <-- CREATE IT
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}
 
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
 
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
 
groq_client = Groq(api_key=GROQ_API_KEY)
jobs = {}
 
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
 
def run_cmd(cmd, timeout=300):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stderr
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)
 
def analyze_with_groq(command_text, ref_url=""):
    system_prompt = """You are an expert video editor AI assistant.
You understand creators from India who speak Hindi and English (Hinglish).
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
    "quality_enhance": true,
    "denoise": true,
    "captions": false,
    "platform": "reels|shorts|youtube"
  },
  "edit_summary": "Short description in English",
  "edit_summary_hindi": "Hindi mein edit plan"
}"""
    user_msg = f"Creator command: {command_text}"
    if ref_url:
        user_msg += f"\nReference video URL: {ref_url}"
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
            "quality_enhance": True,
            "denoise": True,
            "captions": False,
            "platform": "reels"
        },
        "edit_summary": "Standard video enhancement with color grading.",
        "edit_summary_hindi": "रंग सुधार के साथ मानक वीडियो संपादन।"
    }
 
def build_ffmpeg_filters(plan):
    ep = plan.get("edit_plan", {})
    filters = []
    if ep.get("denoise"):
        filters.append("hqdn3d=1.5:1.5:6:6")
    if ep.get("quality_enhance") or ep.get("sharpen"):
        filters.append("unsharp=5:5:0.8:5:5:0.4")
    grade = ep.get("color_grade", "natural")
    b = ep.get("brightness", 0) / 100.0
    c = 1.0 + ep.get("contrast", 0) / 100.0
    sat = 1.0 + ep.get("saturation", 0) / 100.0
    color_presets = {
        "warm": f"colorbalance=rs=0.1:gs=-0.05:bs=-0.1,eq=brightness={b}:contrast={c}:saturation={sat}",
        "cool": f"colorbalance=rs=-0.08:gs=0:bs=0.1,eq=brightness={b}:contrast={c}:saturation={sat}",
        "vibrant": f"eq=brightness={b}:contrast={c}:saturation={sat+0.2}",
        "cinematic": f"colorbalance=rs=0.05:bs=0.08,eq=brightness={b}:contrast={c}:saturation={sat}",
        "natural": f"eq=brightness={b}:contrast={c}:saturation={sat}",
        "dark": f"eq=brightness={b-0.05}:contrast={c+0.1}:saturation={sat}",
    }
    filters.append(color_presets.get(grade, color_presets["natural"]))
    filters.append("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos")
    filters.append("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black")
    return ",".join(filters)
 
def apply_edits(input_path, output_path, plan, job_id):
    try:
        jobs[job_id]["progress"] = 10
        jobs[job_id]["status_text"] = "Analyzing video..."
        vf = build_ffmpeg_filters(plan)
        jobs[job_id]["progress"] = 30
        jobs[job_id]["status_text"] = "Applying color grade & quality boost..."
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path)
        ]
        jobs[job_id]["progress"] = 50
        jobs[job_id]["status_text"] = "Processing video..."
        ok, err = run_cmd(cmd, timeout=600)
        if not ok:
            cmd_simple = [
                "ffmpeg", "-y", "-i", str(input_path),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                str(output_path)
            ]
            ok, err2 = run_cmd(cmd_simple, timeout=300)
            if not ok:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"FFmpeg failed: {err}"
                return
        jobs[job_id]["progress"] = 100
        jobs[job_id]["status"] = "done"
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
    data = request.json or {}
    command = data.get("command", "").strip()
    ref_url = data.get("ref_url", "").strip()
    if not command:
        return jsonify({"error": "Please describe what kind of creator you are"}), 400
    plan = analyze_with_groq(command, ref_url)
    return jsonify(plan)
 
@app.route("/api/upload", methods=["POST"])
def upload():
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
    return send_file(str(out_path), as_attachment=True,
                     download_name="AI_Edited_Video.mp4", mimetype="video/mp4")
 
@app.route("/api/preview/<job_id>")
def preview(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 400
    return send_file(str(Path(job["output_file"])), mimetype="video/mp4")
 
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AI Video Editor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d0d0d;--bg2:#1a1a2e;--bg3:#242424;--border:#333;--text:#f0f0f0;--muted:#888;--purple:#a855f7;--purple-dark:#7c3aed;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--amber:#f59e0b}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.container{max-width:1100px;margin:0 auto;padding:0 1rem}
header{padding:1.25rem 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:1rem}
.logo{width:38px;height:38px;background:var(--purple-dark);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.3rem}
h1{font-size:1.5rem;font-weight:700}
.main{padding:2rem 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
@media(max-width:680px){.grid{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:14px;padding:1.25rem}
.card-hl{border-color:rgba(168,85,247,.4)}
.step{display:flex;align-items:center;gap:.6rem;margin-bottom:1rem}
.step-n{width:24px;height:24px;background:var(--purple-dark);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0}
h2{font-size:1rem;font-weight:600;color:#ccc;margin:0}
input,textarea{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:.65rem .85rem;color:var(--text);font-size:.95rem;font-family:inherit;outline:none}
input:focus,textarea:focus{border-color:var(--purple)}
textarea{resize:vertical;min-height:90px;line-height:1.5;margin-bottom:.5rem}
.btn{padding:.65rem 1.25rem;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;border:none;transition:all .15s;display:inline-flex;align-items:center;gap:.4rem;justify-content:center;width:100%}
.btn-p{background:var(--purple);color:#fff}
.btn-p:hover{background:var(--purple-dark)}
.btn-p:disabled{opacity:.4;cursor:not-allowed}
.btn-g{background:var(--green);color:#000}
.btn-o{background:transparent;color:var(--muted);border:1px solid var(--border);margin-top:.5rem}
.upload-zone{border:2px dashed var(--border);border-radius:12px;padding:2rem 1rem;text-align:center;cursor:pointer;transition:all .2s}
.upload-zone:hover{border-color:var(--purple);background:rgba(168,85,247,.05)}
.upload-zone .ico{font-size:2.5rem;margin-bottom:.5rem}
.hint{font-size:.78rem;color:var(--muted);margin-top:.3rem}
.progress-bar{width:100%;background:var(--bg3);border-radius:99px;height:6px;overflow:hidden;margin:.5rem 0}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--purple-dark),var(--purple));border-radius:99px;transition:width .4s}
.plan-grid{display:grid;grid-template-columns:1fr 1fr;gap:.4rem;margin-top:.75rem}
.plan-item{background:var(--bg3);border-radius:8px;padding:.45rem .7rem;font-size:.8rem}
.plan-label{color:var(--muted);font-size:.7rem;text-transform:uppercase;margin-bottom:2px}
.plan-value{font-weight:600}
.niche-chip{display:inline-flex;align-items:center;gap:.4rem;background:rgba(168,85,247,.15);border:1px solid rgba(168,85,247,.3);color:var(--purple);border-radius:99px;padding:.3rem .75rem;font-size:.85rem;font-weight:600;margin-bottom:.75rem}
.summary{background:var(--bg3);border-left:3px solid var(--purple);border-radius:0 8px 8px 0;padding:.7rem 1rem;margin:.5rem 0;font-size:.85rem;line-height:1.6;color:#ddd}
.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#fca5a5;border-radius:8px;padding:.75rem 1rem;font-size:.85rem;margin:.5rem 0;display:none}
video{width:100%;border-radius:10px;background:#000;margin-bottom:.75rem}
.voice-btn{padding:.5rem;border-radius:8px;background:var(--bg3);border:1px solid var(--border);cursor:pointer;font-size:1.1rem;flex-shrink:0}
.voice-btn.active{background:rgba(239,68,68,.15);border-color:var(--red);animation:pulse .8s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.row{display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem}
</style>
</head>
<body>
<header>
  <div class="container" style="display:flex;align-items:center;gap:1rem;width:100%">
    <div class="logo">🎬</div>
    <div>
      <h1>AI Video Editor</h1>
      <div style="font-size:.78rem;color:var(--muted)">Groq AI · Hindi &amp; English</div>
    </div>
  </div>
</header>
<div class="main"><div class="container"><div class="grid">
 
<!-- LEFT -->
<div style="display:flex;flex-direction:column;gap:1.25rem">
  <div class="card">
    <div class="step"><div class="step-n">1</div><h2>Upload your raw video</h2></div>
    <div class="upload-zone" id="zone" onclick="document.getElementById('fi').click()">
      <div class="ico">📹</div>
      <div>Click to upload or drag &amp; drop</div>
      <div class="hint">MP4, MOV, AVI, MKV · Max 500MB</div>
    </div>
    <input type="file" id="fi" accept="video/*" style="display:none" onchange="uploadFile(this)"/>
  </div>
 
  <div class="card">
    <div class="step"><div class="step-n">2</div><h2>Reference video (optional)</h2></div>
    <input type="url" id="ref" placeholder="Paste YouTube / Instagram / TikTok link..."/>
    <div class="hint" style="margin-top:.4rem">AI will copy the editing style</div>
  </div>
 
  <div class="card card-hl">
    <div class="step"><div class="step-n">3</div><h2>Tell AI what you want</h2></div>
    <textarea id="cmd" placeholder="Type in Hindi or English:&#10;&#10;Main fitness creator hoon, mera video trending style mein edit karo&#10;&#10;OR&#10;&#10;I'm a vlogger, make my video cinematic for YouTube..."></textarea>
    <div class="row">
      <button class="voice-btn" id="vbtn" onclick="toggleVoice()" title="Speak in Hindi or English">🎤</button>
      <span style="font-size:.8rem;color:var(--muted)" id="vstatus">Tap mic to speak</span>
    </div>
    <button class="btn btn-p" id="abtn" onclick="analyzeCommand()">🤖 Analyze with AI</button>
  </div>
</div>
 
<!-- RIGHT -->
<div style="display:flex;flex-direction:column;gap:1.25rem">
  <div class="card" id="plan-card" style="display:none">
    <h2 style="margin-bottom:.75rem">🧠 AI Edit Plan</h2>
    <div id="niche-disp"></div>
    <div id="summ-disp"></div>
    <div class="plan-grid" id="plan-grid"></div>
    <div style="margin-top:1rem">
      <button class="btn btn-p" id="ebtn" onclick="startEditing()" disabled>⚡ Start Editing</button>
      <div class="hint" style="text-align:center;margin-top:.4rem" id="ebtn-hint">Upload a video first</div>
    </div>
  </div>
 
  <div class="card" id="prog-card" style="display:none">
    <h2 style="margin-bottom:.75rem">⚙️ Editing in progress</h2>
    <div class="progress-bar"><div class="progress-fill" id="pfill" style="width:0%"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:.8rem;color:var(--muted)">
      <span id="ptxt">Starting...</span><span id="ppct">0%</span>
    </div>
    <div class="hint" style="margin-top:.5rem">May take 2–5 min for long videos</div>
  </div>
 
  <div class="card" id="result-card" style="display:none">
    <h2 style="margin-bottom:.75rem">✅ Your edited video is ready!</h2>
    <video id="pvid" controls playsinline></video>
    <a id="dlink" class="btn btn-g" style="text-decoration:none">⬇️ Download Edited Video</a>
    <button class="btn btn-o" onclick="resetAll()">🔄 Edit another video</button>
  </div>
 
  <div id="err-box" class="err"></div>
 
  <div class="card" id="placeholder" style="text-align:center;padding:3rem 1rem">
    <div style="font-size:3rem;opacity:.2;margin-bottom:.75rem">🎬</div>
    <div style="color:var(--muted);font-size:.9rem">Upload video · Add reference · Describe niche<br/>Let AI do the rest!</div>
    <div style="margin-top:1rem;font-size:.78rem;color:#444">Fitness · Vlog · Entertainment · Gaming<br/>Education · Fashion · Food · Travel</div>
  </div>
</div>
 
</div></div></div>
<script>
let fileId=null,plan=null,jobId=null,poll=null,rec=null,isRec=false;
 
const zone=document.getElementById('zone');
zone.addEventListener('dragover',e=>{e.preventDefault();zone.style.borderColor='var(--purple)'});
zone.addEventListener('dragleave',()=>zone.style.borderColor='');
zone.addEventListener('drop',e=>{e.preventDefault();zone.style.borderColor='';if(e.dataTransfer.files[0])uploadFile(null,e.dataTransfer.files[0])});
 
async function uploadFile(inp,file){
  file=file||inp.files[0];
  if(!file)return;
  zone.innerHTML='<div class="ico">⏳</div><div>Uploading '+file.name+'...</div>';
  const fd=new FormData();fd.append('video',file);
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    fileId=d.file_id;
    zone.innerHTML='<div class="ico">✅</div><div style="color:var(--green)">'+file.name+'</div><div class="hint">Uploaded!</div>';
    updateEditBtn();
  }catch(e){zone.innerHTML='<div class="ico">❌</div><div style="color:var(--red)">'+e.message+'</div>';}
}
 
function toggleVoice(){
  if(!('webkitSpeechRecognition'in window)&&!('SpeechRecognition'in window)){alert('Use Chrome for voice input!');return;}
  if(isRec){rec&&rec.stop();return;}
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  rec=new SR();rec.lang='hi-IN';rec.interimResults=true;
  rec.onstart=()=>{isRec=true;document.getElementById('vbtn').classList.add('active');document.getElementById('vstatus').textContent='🔴 Recording...';};
  rec.onresult=e=>{let t='';for(let i=e.resultIndex;i<e.results.length;i++)t+=e.results[i][0].transcript;document.getElementById('cmd').value=t;};
  rec.onend=rec.onerror=()=>{isRec=false;document.getElementById('vbtn').classList.remove('active');document.getElementById('vstatus').textContent='✅ Done!';setTimeout(()=>document.getElementById('vstatus').textContent='Tap mic to speak',2000);};
  rec.start();
}
 
async function analyzeCommand(){
  const cmd=document.getElementById('cmd').value.trim();
  if(!cmd){alert('Please describe your niche!');return;}
  const btn=document.getElementById('abtn');
  btn.disabled=true;btn.textContent='🤖 Analyzing...';
  document.getElementById('placeholder').style.display='none';
  document.getElementById('plan-card').style.display='none';
  try{
    const r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd,ref_url:document.getElementById('ref').value.trim()})});
    plan=await r.json();
    displayPlan(plan);
  }catch(e){showErr('AI analysis failed: '+e.message);}
  finally{btn.disabled=false;btn.textContent='🤖 Analyze with AI';}
}
 
function displayPlan(p){
  const ep=p.edit_plan||{};
  document.getElementById('niche-disp').innerHTML='<div class="niche-chip">🎯 '+p.niche+(p.niche_hindi?' · '+p.niche_hindi:'')+'</div>';
  const lang=p.detected_language||'english';
  const s=lang==='hindi'||lang==='hinglish'?(p.edit_summary_hindi||p.edit_summary):p.edit_summary;
  document.getElementById('summ-disp').innerHTML='<div class="summary">'+s+'</div>';
  const items=[['Color',ep.color_grade],['Platform',ep.platform],['Captions',ep.captions?'Yes':'No'],['Quality boost',ep.quality_enhance?'✅':'—'],['Denoise',ep.denoise?'✅':'—'],['Sharpen',ep.sharpen?'✅':'—']];
  document.getElementById('plan-grid').innerHTML=items.map(([k,v])=>'<div class="plan-item"><div class="plan-label">'+k+'</div><div class="plan-value">'+v+'</div></div>').join('');
  document.getElementById('plan-card').style.display='block';
  updateEditBtn();
}
 
function updateEditBtn(){
  const btn=document.getElementById('ebtn'),hint=document.getElementById('ebtn-hint');
  const ok=fileId&&plan;
  btn.disabled=!ok;
  hint.textContent=ok?'Ready to edit!':(fileId?'Analyze with AI first':'Upload a video first');
}
 
async function startEditing(){
  if(!fileId||!plan)return;
  document.getElementById('plan-card').style.display='none';
  document.getElementById('prog-card').style.display='block';
  document.getElementById('result-card').style.display='none';
  try{
    const r=await fetch('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fileId,plan})});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    jobId=d.job_id;pollProgress();
  }catch(e){showErr('Failed: '+e.message);document.getElementById('prog-card').style.display='none';}
}
 
function pollProgress(){
  if(poll)clearInterval(poll);
  poll=setInterval(async()=>{
    try{
      const r=await fetch('/api/status/'+jobId);
      const d=await r.json();
      document.getElementById('pfill').style.width=d.progress+'%';
      document.getElementById('ppct').textContent=d.progress+'%';
      document.getElementById('ptxt').textContent=d.status_text||'Processing...';
      if(d.status==='done'){clearInterval(poll);showResult();}
      else if(d.status==='error'){clearInterval(poll);showErr(d.error||'Unknown error');document.getElementById('prog-card').style.display='none';}
    }catch(e){}
  },1500);
}
 
function showResult(){
  document.getElementById('prog-card').style.display='none';
  document.getElementById('result-card').style.display='block';
  document.getElementById('pvid').src='/api/preview/'+jobId;
  document.getElementById('dlink').href='/api/download/'+jobId;
}
 
function showErr(msg){
  const b=document.getElementById('err-box');
  b.textContent='❌ '+msg;b.style.display='block';
  setTimeout(()=>b.style.display='none',8000);
}
 
function resetAll(){
  fileId=null;plan=null;jobId=null;
  if(poll)clearInterval(poll);
  document.getElementById('zone').innerHTML='<div class="ico">📹</div><div>Click to upload or drag & drop</div><div class="hint">MP4, MOV, AVI, MKV · Max 500MB</div>';
  document.getElementById('cmd').value='';document.getElementById('ref').value='';
  ['plan-card','prog-card','result-card'].forEach(id=>document.getElementById(id).style.display='none');
  document.getElementById('placeholder').style.display='block';
}
</script>
</body>
</html>"""
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
 
