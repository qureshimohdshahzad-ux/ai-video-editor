import os, re, json, uuid, time, threading, subprocess, traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

APP_VERSION = "4.5-stable"

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

def run_cmd(cmd, timeout=600):
    try:
        r = subprocess.run(["nice", "-n", "19"] + cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)

def analyze_with_groq(command_text, ref_url=""):
    system_prompt = """You are a professional trending reel editor. Respond ONLY with this exact JSON format:
{
  "color_grade": "cinematic_warm",
  "transition_style": "fast_zoom",
  "caption_style": "bold_white_black_outline",
  "speed_ramp": "smooth_fast",
  "edit_summary": "Short description"
}"""
    msg = f"User request: {command_text}"
    if ref_url: msg += f"\nReference: {ref_url}"
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":msg}],
            temperature=0.3, max_tokens=400
        )
        raw = re.sub(r"```json|```", "", resp.choices[0].message.content.strip())
        return json.loads(raw)
    except:
        return {"color_grade":"cinematic_warm","transition_style":"fast_zoom","caption_style":"bold_white_black_outline","speed_ramp":"smooth_fast","edit_summary":"Trending reel"}

def build_filters():
    return "colorbalance=rs=0.15:gs=-0.05:bs=-0.12,eq=brightness=0.05:contrast=1.18:saturation=1.25,zoompan=z='if(lte(zoom,1.0),1.25,zoom-0.004)':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':d=80:s=854x480,hqdn3d=1:1:4:4,scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2:black"

def apply_pro_edits(inp, out, plan, job_id):
    try:
        jobs[job_id].update({"progress":20, "status_text":"Applying cinematic warm grade + zoom..."})
        
        vf = build_filters()
        cmd = [
            "ffmpeg", "-y", "-i", str(inp),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)
        ]
        
        ok, err = run_cmd(cmd, timeout=480)
        
        if ok:
            jobs[job_id].update({"progress":70, "status_text":"Adding bold captions..."})
            caption_cmd = [
                "ffmpeg", "-y", "-i", str(out),
                "-vf", r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:fontsize=32:fontcolor=white:bordercolor=black:borderw=6:x=(w-text_w)/2:y=h-90:text='%{pts\:gmtime\:0\:%M:%S}'",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-c:a", "copy", str(out)
            ]
            run_cmd(caption_cmd, timeout=300)
            jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
        else:
            jobs[job_id].update({"status":"error","error":err or "FFmpeg failed"})
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})
        print("Error in apply_pro_edits:", traceback.format_exc())

# ====================== ROUTES ======================
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files["video"]
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
    if not inp.exists():
        return jsonify({"error":"File not found"}), 404
    
    jid = uuid.uuid4().hex
    out = OUTPUT_FOLDER / f"edited_{jid}.mp4"
    jobs[jid] = {"status":"processing","progress":0,"status_text":"Starting Edit...","output_file":None,"error":None}
    
    threading.Thread(target=apply_pro_edits, args=(inp, out, plan, jid), daemon=True).start()
    return jsonify({"job_id": jid})

@app.route("/api/status/<jid>")
def status(jid):
    job = jobs.get(jid)
    if job:
        return jsonify(job)
    return jsonify({"status":"expired","progress":0,"status_text":"Job expired. Please try again."})

@app.route("/api/download/<jid>")
def download(jid):
    j = jobs.get(jid)
    if not j or j.get("status") != "done":
        return jsonify({"error":"Not ready"}), 400
    return send_file(j["output_file"], as_attachment=True, download_name="Trending_Reel.mp4")

HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Video Editor Pro</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#f0f0f0;font-family:'Segoe UI',sans-serif}
.wrap{max-width:1100px;margin:auto;padding:15px}
header{background:#1a1a2e;padding:15px;border-radius:12px;display:flex;align-items:center;gap:15px;margin-bottom:20px}
.card{background:#1a1a2e;border:1px solid #333;border-radius:12px;padding:20px;margin-bottom:15px}
.btn{background:#a855f7;color:white;border:none;padding:14px;border-radius:10px;font-size:1.1rem;cursor:pointer;width:100%;margin-top:10px}
.btn.green{background:#22c55e}
#status{display:none}
</style></head><body>
<div class="wrap">
<header><div style="font-size:2.5rem">🎬</div><h1>AI Video Editor Pro v4.5</h1></header>

<div class="card">
  <h2>1. Upload Video</h2>
  <input type="file" id="fi" accept="video/*" onchange="doUpload(this)">
</div>

<div class="card">
  <h2>2. Reference (optional)</h2>
  <input type="url" id="ref" placeholder="YouTube or Instagram link">
</div>

<div class="card">
  <h2>3. Describe Style</h2>
  <textarea id="cmd" rows="4" placeholder="Make a trending intro reel with fast zoom transitions, energy, bold white captions with black outline..."></textarea>
</div>

<button class="btn" onclick="doAnalyze()">🤖 Generate Pro Edit Plan</button>

<div id="plan_area" class="card" style="display:none"></div>
<button id="edit_btn" class="btn green" style="display:none" onclick="startEdit()">⚡ Start Pro Editing</button>

<div id="status" class="card">
  <h3 id="status_text">Processing...</h3>
  <div style="height:8px;background:#333;border-radius:4px;overflow:hidden"><div id="progress_bar" style="width:0%;height:100%;background:#a855f7"></div></div>
  <p id="progress_text">0%</p>
</div>

<div id="result" class="card" style="display:none">
  <h3>✅ Video Ready!</h3>
  <video id="preview" controls width="100%"></video><br><br>
  <a id="download_link" class="btn" href="#" download>⬇️ Download Video</a>
</div>
</div>

<script>
let fid = null, plan = null, jid = null;

async function doUpload(inp){
  const file = inp.files[0];
  const fd = new FormData(); fd.append('video', file);
  const r = await fetch('/api/upload',{method:'POST',body:fd});
  const d = await r.json();
  fid = d.file_id;
  alert('Uploaded successfully!');
}

async function doAnalyze(){
  const cmd = document.getElementById('cmd').value.trim();
  if(!cmd) return alert("Please describe the style!");
  const r = await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({command:cmd, ref_url:document.getElementById('ref').value})});
  plan = await r.json();
  document.getElementById('plan_area').innerHTML = `<h3>AI Edit Plan</h3><pre>${JSON.stringify(plan,null,2)}</pre>`;
  document.getElementById('plan_area').style.display = 'block';
  document.getElementById('edit_btn').style.display = 'block';
}

async function startEdit(){
  if(!fid || !plan) return alert("Please upload video and generate plan first");
  document.getElementById('status').style.display = 'block';
  const r = await fetch('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({file_id:fid, plan:plan})});
  const d = await r.json();
  jid = d.job_id;
  pollStatus();
}

function pollStatus(){
  const int = setInterval(async()=>{
    const r = await fetch('/api/status/'+jid);
    const d = await r.json();
    document.getElementById('progress_bar').style.width = (d.progress||0) + '%';
    document.getElementById('progress_text').innerText = (d.progress||0) + '%';
    document.getElementById('status_text').innerText = d.status_text || 'Processing...';
    if(d.status === "done"){
      clearInterval(int);
      document.getElementById('status').style.display = 'none';
      document.getElementById('result').style.display = 'block';
      document.getElementById('preview').src = '/api/preview/'+jid;
      document.getElementById('download_link').href = '/api/download/'+jid;
    } else if(d.status === "error"){
      clearInterval(int);
      alert("Error: " + (d.error || "Processing failed"));
    }
  },1500);
}
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
