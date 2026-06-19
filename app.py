import os, re, json, uuid, time, threading, subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

APP_VERSION = "4.2-final"

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
    system_prompt = """You are a professional trending reel editor. Respect user's request exactly.
Respond ONLY with valid JSON."""
    msg = f"User request: {command_text}"
    if ref_url: msg += f"\nReference: {ref_url}"
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":msg}],
            temperature=0.3, max_tokens=600
        )
        raw = re.sub(r"```json|```", "", resp.choices[0].message.content.strip())
        return json.loads(raw)
    except:
        return {"color_grade":"cinematic_warm","transition_style":"fast_zoom","caption_style":"bold_white_outline","edit_summary":"Trending reel with zoom transitions"}

def build_filters():
    return "colorbalance=rs=0.18:gs=-0.05:bs=-0.12,eq=brightness=0.05:contrast=1.2:saturation=1.3,zoompan=z='if(lte(zoom,1.0),1.25,zoom-0.003)':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':d=80:s=854x480,hqdn3d=1:1:4:4,scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2:black"

def apply_pro_edits(inp, out, plan, job_id):
    try:
        jobs[job_id].update({"progress":15, "status_text":"Enhancing Audio..."})
        temp1 = TEMP_FOLDER / f"t1_{job_id}.mp4"
        run_cmd(["ffmpeg","-y","-i",str(inp),"-af","volume=1.3,acompressor=threshold=-18dB:ratio=6:attack=5:release=60",str(temp1)], 300)

        jobs[job_id].update({"progress":50, "status_text":"Applying Cinematic Warm Grade + Fast Zoom Transitions..."})
        vf = build_filters()
        temp2 = TEMP_FOLDER / f"t2_{job_id}.mp4"
        run_cmd(["ffmpeg","-y","-i",str(temp1),"-vf",vf,"-c:v","libx264","-preset","medium","-crf","21","-c:a","copy",str(temp2)], 800)

        jobs[job_id].update({"progress":80, "status_text":"Adding Bold Captions..."})
        final_cmd = [
            "ffmpeg","-y","-i",str(temp2),
            "-vf", r"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:fontsize=34:fontcolor=white:bordercolor=black:borderw=6:x=(w-text_w)/2:y=h-85:text='%{pts\:gmtime\:0\:%M:%S}'",
            "-c:v","libx264","-preset","medium","-crf","22","-c:a","copy","-movflags","+faststart",str(out)
        ]
        ok, _ = run_cmd(final_cmd, 600)
        
        if ok:
            jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
        else:
            jobs[job_id].update({"status":"error","error":"Final render failed"})
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})
    finally:
        for f in list(TEMP_FOLDER.glob(f"*{job_id}*")):
            f.unlink(missing_ok=True)

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
    return jsonify(jobs.get(jid, {"status":"expired","progress":0,"status_text":"Job expired. Please refresh and try again."}))

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
body{background:#0d0d0d;color:#eee;font-family:'Segoe UI',sans-serif}
.wrap{max-width:1100px;margin:auto;padding:15px}
header{background:#1a1a2e;padding:15px;border-radius:12px;display:flex;align-items:center;gap:15px;margin-bottom:20px}
.card{background:#1a1a2e;border:1px solid #333;border-radius:12px;padding:20px;margin-bottom:15px}
.btn{background:#a855f7;color:white;border:none;padding:14px;border-radius:10px;font-size:1.1rem;cursor:pointer;width:100%;margin-top:10px}
.btn.green{background:#22c55e}
.hint{color:#aaa;font-size:0.9rem}
#status{display:none}
</style></head><body>
<div class="wrap">
<header><div style="font-size:2.5rem">🎬</div><h1>AI Video Editor Pro</h1></header>

<div class="card">
  <h2>1. Upload Video</h2>
  <input type="file" id="fi" accept="video/*" onchange="doUpload(this)">
</div>

<div class="card">
  <h2>2. Reference Link (optional)</h2>
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
  <div class="pbar"><div id="progress_bar" style="width:0%;height:8px;background:#a855f7;border-radius:4px;"></div></div>
  <p id="progress_text">0%</p>
</div>

<div id="result" class="card" style="display:none">
  <h3>✅ Video Ready!</h3>
  <video id="preview" controls width="100%"></video>
  <a id="download_link" class="btn" href="#" download>⬇️ Download Video</a>
</div>
</div>

<script>
let current_fid = null;
let current_plan = null;
let current_jid = null;

async function doUpload(inp){
  const file = inp.files[0];
  const fd = new FormData();
  fd.append('video', file);
  const res = await fetch('/api/upload', {method:'POST', body:fd});
  const data = await res.json();
  current_fid = data.file_id;
  alert("Video uploaded successfully!");
}

async function doAnalyze(){
  const text = document.getElementById('cmd').value.trim();
  if(!text) return alert("Please write your style description!");
  
  const res = await fetch('/api/analyze',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({command:text, ref_url:document.getElementById('ref').value})
  });
  current_plan = await res.json();
  
  document.getElementById('plan_area').innerHTML = `<h3>AI Edit Plan</h3><pre>${JSON.stringify(current_plan, null, 2)}</pre>`;
  document.getElementById('plan_area').style.display = 'block';
  document.getElementById('edit_btn').style.display = 'block';
}

async function startEdit(){
  if(!current_fid || !current_plan) return;
  
  document.getElementById('status').style.display = 'block';
  document.getElementById('result').style.display = 'none';
  
  const res = await fetch('/api/edit',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({file_id:current_fid, plan:current_plan})
  });
  const data = await res.json();
  current_jid = data.job_id;
  pollProgress();
}

function pollProgress(){
  const interval = setInterval(async () => {
    const res = await fetch('/api/status/' + current_jid);
    const data = await res.json();
    
    document.getElementById('progress_bar').style.width = data.progress + '%';
    document.getElementById('progress_text').innerText = data.progress + '%';
    document.getElementById('status_text').innerText = data.status_text || 'Processing...';
    
    if(data.status === "done"){
      clearInterval(interval);
      document.getElementById('status').style.display = 'none';
      document.getElementById('result').style.display = 'block';
      document.getElementById('preview').src = '/api/preview/' + current_jid;
      document.getElementById('download_link').href = '/api/download/' + current_jid;
    }
  }, 1800);
}
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
