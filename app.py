import os, re, json, uuid, time, threading, subprocess, traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

APP_VERSION = "pro-day2"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp_processing")
CAPTION_FOLDER = Path("/tmp/captions")

for f in [UPLOAD_FOLDER, OUTPUT_FOLDER, TEMP_FOLDER, CAPTION_FOLDER]:
    f.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
jobs = {}

def run_cmd(cmd, timeout=600):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr
    except Exception as e:
        return False, str(e)

def generate_captions(video_path, job_id):
    """Groq Whisper se word-by-word captions nikalta hai, SRT banata hai"""
    jobs[job_id].update({"progress":20, "status_text":"Generating auto captions (Hindi/English)..."})
    
    # Extract audio first (faster for Whisper)
    audio_path = TEMP_FOLDER / f"audio_{job_id}.wav"
    run_cmd(["ffmpeg","-y","-i",str(video_path),"-vn","-acodec","pcm_s16le","-ar","16000","-ac","1",str(audio_path)], timeout=120)
    
    # Call Whisper large-v3 for word timestamps
    with open(audio_path, "rb") as f:
        transcript = groq_client.audio.transcriptions.create(
            file=(str(audio_path), f.read()),
            model="whisper-large-v3",
            response_format="verbose_json",
            timestamp_granularities=["word"],
            language="hi"
        )
    
    # Build ASS subtitle file (for bold jumping captions, thicker outline)
    srt_path = CAPTION_FOLDER / f"captions_{job_id}.ass"
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans Bold,86,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,0,2,40,40,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [ass_header]
    
    for word in transcript.words:
        # Format timestamps for ASS (H:MM:SS.cc)
        start = word['start']
        end = word['end']
        w = word['word'].strip()
        if not w: continue
        
        # Tiny pop animation effect (scale word slightly when spoken)
        start_t = time.strftime('%H:%M:%S.', time.gmtime(start)) + f"{int((start%1)*100):02d}"
        end_t = time.strftime('%H:%M:%S.', time.gmtime(end)) + f"{int((end%1)*100):02d}"
        
        # Highlight trending/important words in yellow
        text = w
        if any(k in w.lower() for k in ["like","subscribe","share","fitness","paisa","trending","viral","gym","food","travel"]):
            text = f"{{\\c&H00FFFF&}}{w}{{\\c&HFFFFFF&}}"
        lines.append(f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{text}")
    
    with open(srt_path, "w") as f:
        f.write("\n".join(lines))
    
    audio_path.unlink(missing_ok=True)
    return srt_path

def process_video_pro(inp, out, plan, job_id):
    try:
        # Step 1: Voice Enhancement + Captions
        jobs[job_id].update({"progress":10, "status_text":"Enhancing studio voice..."})
        temp_voice = TEMP_FOLDER / f"voice_{job_id}.mp4"
        voice_filter = "highpass=f=80,lowpass=f=8000,acompressor=threshold=-18dB:ratio=6:attack=5:release=50,volume=1.5,afftdn=nf=-25,loudnorm=I=-14:TP=-1:LRA=11"
        run_cmd(["ffmpeg","-y","-i",str(inp),"-af",voice_filter,str(temp_voice)], timeout=300)
        
        # Step 2: Generate captions
        srt_path = generate_captions(temp_voice, job_id)
        
        # Step 3: Color grade + burn captions, 1080p
        jobs[job_id].update({"progress":60, "status_text":"Applying cinematic color + captions..."})
        vf = "colorbalance=rs=0.15:gs=-0.05:bs=-0.1,eq=brightness=0.04:contrast=1.15:saturation=1.25,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,subtitles="+str(srt_path)+":force_style='Fontname=DejaVu Sans Bold,Fontsize=86,PrimaryColour=&Hffffff,OutlineColour=&H000000,BorderStyle=1,Outline=7'"
        
        ok, err = run_cmd(["ffmpeg","-y","-i",str(temp_voice),"-vf",vf,"-c:v","libx264","-preset","fast","-crf","22","-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)], timeout=600)
        
        if ok:
            jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
        else:
            jobs[job_id].update({"status":"error","error":err})
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})
        traceback.print_exc()
    finally:
        for f in TEMP_FOLDER.glob(f"*{job_id}*"): f.unlink(missing_ok=True)
        for f in CAPTION_FOLDER.glob(f"*{job_id}*"): f.unlink(missing_ok=True)

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
    return jsonify({"color_grade":"cinematic_warm","captions":"kalkar_bold","edit_summary":"Trending cinematic reel with bold jumping captions"})

@app.route("/api/edit", methods=["POST"])
def edit():
    data = request.json or {}
    fid = data.get("file_id")
    inp = UPLOAD_FOLDER / fid
    jid = uuid.uuid4().hex
    out = OUTPUT_FOLDER / f"edited_{jid}.mp4"
    jobs[jid] = {"status":"processing","progress":0,"status_text":"Starting...","output_file":None,"error":None}
    threading.Thread(target=process_video_pro, args=(inp, out, {}, jid), daemon=True).start()
    return jsonify({"job_id": jid})

@app.route("/api/status/<jid>")
def status(jid):
    return jsonify(jobs.get(jid, {"status":"expired","progress":0,"status_text":"Job expired. Try again."}))

@app.route("/api/download/<jid>")
def download(jid):
    j = jobs.get(jid)
    if not j or j.get("status") != "done": return jsonify({"error":"Not ready"}), 400
    return send_file(j["output_file"], as_attachment=True, download_name="Trending_Reel.mp4")

HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>AI Reel Editor Pro</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:Arial,sans-serif}
body{background:#0d0d0d;color:#fff;padding:20px;max-width:800px;margin:auto}
.card{background:#1a1a2e;padding:25px;border-radius:12px;margin:15px 0}
.btn{background:#a855f7;color:white;border:none;padding:16px;border-radius:10px;font-size:1.2rem;width:100%;cursor:pointer;font-weight:bold}
.btn.green{background:#22c55e}
#status{display:none}
.bar{height:10px;background:#333;border-radius:5px;overflow:hidden}
.fill{height:100%;background:#a855f7;width:0%}
</style></head><body>
<h1 style="text-align:center;margin-bottom:20px">🎬 AI Trending Reel Editor</h1>
<div class="card">
<h2>1. Upload Raw Video (1080p, under 2 min)</h2>
<input type="file" id="fi" accept="video/*" onchange="doUpload(this)" style="width:100%;font-size:1.1rem">
</div>
<button class="btn" onclick="startEdit()" id="editbtn" disabled>⚡ Make Viral Reel (Bold Captions + Cinematic)</button>
<div id="status" class="card">
<h3 id="statustext">Processing...</h3>
<div class="bar"><div class="fill" id="bar"></div></div>
<p id="prog">0%</p>
</div>
<div id="result" class="card" style="display:none">
<h3>✅ Ready!</h3>
<video id="prev" controls width="100%"></video><br><br>
<a id="dl" class="btn green" href="#">⬇️ Download Reel</a>
</div>
<script>
let fid=null, jid=null;
async function doUpload(e){fid = (await(await fetch('/api/upload',{method:'POST',body:(()=>{const fd=new FormData();fd.append('video',e.files[0]);return fd})()})).json()).file_id;document.getElementById('editbtn').disabled=false}
async function startEdit(){
  document.getElementById('status').style.display='block';
  jid = (await(await fetch('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fid})})).json()).job_id;
  setInterval(async()=>{
    const d = await(await fetch('/api/status/'+jid)).json();
    document.getElementById('bar').style.width=(d.progress||0)+'%';
    document.getElementById('prog').textContent=(d.progress||0)+'% — '+d.status_text;
    if(d.status==='done'){document.getElementById('status').style.display='none';document.getElementById('result').style.display='block';document.getElementById('prev').src='/api/download/'+jid;document.getElementById('dl').href='/api/download/'+jid}
  },1500)
}
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
