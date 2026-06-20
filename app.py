import os, re, json, uuid, time, threading, subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp")
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)
TEMP_FOLDER.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
jobs = {}

# ============== HELPER FUNCTIONS ==============
def run_ffmpeg(cmd, timeout=300):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr
    except Exception as e:
        return False, str(e)

def analyze_with_groq(command_text):
    system_prompt = """You are a video editor. Reply ONLY with JSON: {\"color_grade\":\"cinematic_warm|vibrant|dark|natural\",\"caption_text\":\"short text\",\"music_mood\":\"energetic|chill|cinematic|upbeat\"}"""
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":command_text}],
            temperature=0.3, max_tokens=300
        )
        raw = re.sub(r"```json|```", "", resp.choices[0].message.content.strip())
        return json.loads(raw)
    except:
        return {"color_grade":"cinematic_warm","caption_text":"AWESOME","music_mood":"energetic"}

# ============== VIDEO PROCESSING ==============
def process_video(inp, out, plan, job_id):
    try:
        jobs[job_id].update({"progress":15, "status_text":"Applying effects..."})
        
        # Color grade
        grade = plan.get("color_grade", "cinematic_warm")
        if grade == "cinematic_warm":
            color_filter = "colorbalance=rs=0.08:gs=-0.02:bs=-0.05,eq=brightness=0.03:contrast=1.15:saturation=1.2"
        elif grade == "vibrant":
            color_filter = "eq=brightness=0.05:contrast=1.2:saturation=1.35"
        elif grade == "dark":
            color_filter = "eq=brightness=-0.05:contrast=1.25:saturation=0.9"
        else:
            color_filter = "eq=brightness=0.02:contrast=1.1:saturation=1.1"
        
        # Caption (BIG CENTER TEXT)
        caption = plan.get("caption_text", "AWESOME")
        caption = caption.replace("'", "").replace(":", "")[:30]
        
        # Find a font that exists on Render
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if not os.path.exists(font_path):
            font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
        if not os.path.exists(font_path):
            font_path = ""
        
        caption_filter = (
            f"drawtext=text='{caption}':"
            f"fontfile={font_path}:"
            f"fontsize=56:"
            f"fontcolor=white:"
            f"bordercolor=black:borderw=3:"
            f"x=(w-tw)/2:y=(h-text_h)/2"
        )
        
        # Full filter chain (NOT zoom - causing crashes)
        full_filter = f"{color_filter},{caption_filter}"
        
        # FFmpeg command
        cmd = [
            "ffmpeg", "-y",
            "-i", str(inp),
            "-vf", full_filter,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out)
        ]
        
        ok, err = run_ffmpeg(cmd, timeout=300)
        
        if ok:
            jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
        else:
            jobs[job_id].update({"status":"error","error":err[:200] if err else "Failed"})
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})

# ============== ROUTES ==============
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
    return jsonify(analyze_with_groq(data.get("command","")))

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
    jobs[jid] = {"status":"processing","progress":0,"status_text":"Starting...","output_file":None,"error":None}
    
    threading.Thread(target=process_video, args=(inp, out, plan, jid), daemon=True).start()
    return jsonify({"job_id": jid})

@app.route("/api/status/<jid>")
def status(jid):
    return jsonify(jobs.get(jid, {"status":"expired","progress":0,"status_text":"Job expired. Please try again."}))

@app.route("/api/download/<jid>")
def download(jid):
    j = jobs.get(jid)
    if not j or j.get("status") != "done":
        return jsonify({"error":"Not ready"}), 400
    return send_file(j["output_file"], as_attachment=True, download_name="edited.mp4")

# ============== HTML ==============
HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Video Editor</title>
<style>
body{background:#0d0d0d;color:#fff;font-family:Arial;padding:15px}
.wrap{max-width:900px;margin:auto}
.card{background:#1a1a2e;padding:18px;border-radius:12px;margin:12px 0}
.btn{background:#a855f7;color:white;padding:14px;border:none;border-radius:8px;width:100%;font-size:1.05rem;cursor:pointer;margin-top:8px}
.btn.green{background:#22c55e}
.btn.gray{background:#444}
#status,#result{display:none}
textarea,input{width:100%;background:#0d0d0d;color:#fff;border:1px solid #333;padding:10px;border-radius:6px;font-family:Arial;margin:6px 0}
.quick{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0}
.qbtn{background:#333;color:#fff;border:none;padding:10px;border-radius:6px;cursor:pointer}
.qbtn.sel{background:#a855f7}
</style></head><body>
<div class="wrap">
<h1>🎬 AI Video Editor Pro</h1>

<div class="card">
  <h3>1. Upload Video</h3>
  <input type="file" id="fi" accept="video/*" onchange="doUpload(this)">
</div>

<div class="card">
  <h3>2. Describe Style</h3>
  <textarea id="cmd" rows="3" placeholder="e.g. Make my fitness reel with bold caption ENERGY MODE, vibrant colors"></textarea>
</div>

<div class="card">
  <h3>3. Quick Color Preset</h3>
  <div class="quick">
    <button class="qbtn" onclick="setColor('cinematic_warm',this)">🎬 Cinematic</button>
    <button class="qbtn" onclick="setColor('vibrant',this)">🌈 Vibrant</button>
    <button class="qbtn" onclick="setColor('dark',this)">🌑 Dark</button>
    <button class="qbtn" onclick="setColor('natural',this)">☀️ Natural</button>
  </div>
</div>

<button class="btn" onclick="doAnalyze()">🤖 Generate Plan</button>

<div id="plan_area" class="card" style="display:none"></div>
<button id="edit_btn" class="btn green" style="display:none" onclick="startEdit()">⚡ Start Editing</button>

<div id="status" class="card">
  <h3 id="status_text">Processing...</h3>
  <div style="height:10px;background:#333;border-radius:5px"><div id="bar" style="width:0%;height:100%;background:#a855f7;border-radius:5px"></div></div>
  <p id="prog">0%</p>
</div>

<div id="result" class="card">
  <h3>✅ Done!</h3>
  <video id="preview" controls width="100%"></video><br><br>
  <a id="dl" class="btn" href="#">⬇️ Download</a>
  <button class="btn gray" onclick="location.reload()">🔄 New Edit</button>
</div>

</div>

<script>
let fid=null, plan=null, jid=null, colorChoice='cinematic_warm';

function setColor(c, btn){
  colorChoice = c;
  document.querySelectorAll('.qbtn').forEach(b => b.classList.remove('sel'));
  btn.classList.add('sel');
}

async function doUpload(e){
  const fd = new FormData();
  fd.append('video', e.files[0]);
  const r = await fetch('/api/upload',{method:'POST',body:fd});
  const d = await r.json();
  fid = d.file_id;
  alert('Uploaded!');
}

async function doAnalyze(){
  const text = document.getElementById('cmd').value.trim();
  if(!text) return alert('Describe what you want!');
  const r = await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:text})});
  plan = await r.json();
  plan.color_grade = colorChoice;
  document.getElementById('plan_area').innerHTML = '<h3>AI Plan</h3><pre>'+JSON.stringify(plan,null,2)+'</pre>';
  document.getElementById('plan_area').style.display = 'block';
  document.getElementById('edit_btn').style.display = 'block';
}

async function startEdit(){
  document.getElementById('status').style.display = 'block';
  const r = await fetch('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fid,plan:plan})});
  const d = await r.json();
  jid = d.job_id;
  poll();
}

function poll(){
  setInterval(async()=>{
    const r = await fetch('/api/status/'+jid);
    const d = await r.json();
    document.getElementById('bar').style.width = (d.progress||0)+'%';
    document.getElementById('prog').innerText = (d.progress||0)+'%';
    document.getElementById('status_text').innerText = d.status_text || 'Working...';
    if(d.status === 'done'){
      document.getElementById('status').style.display = 'none';
      document.getElementById('result').style.display = 'block';
      document.getElementById('preview').src = '/api/preview/'+jid;
      document.getElementById('dl').href = '/api/download/'+jid;
    }
  },2000);
}
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
