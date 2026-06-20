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
    temp_audio = TEMP_FOLDER / f"audio_{job_id}.mp3"
    srt_file = TEMP_FOLDER / f"subs_{job_id}.srt"
    try:
        ep = plan.get('edit_plan', {})
        lang = 'hi' if plan.get('detected_language') in ['hindi', 'hinglish'] else 'en'
        duration = get_duration(inp)
        
        has_subtitles = False
        if ep.get('auto_captions', True) and groq_client:
            jobs[job_id].update({'progress': 10, 'status_text': 'Extracting audio...'})
            ok, _ = extract_audio(inp, temp_audio)
            if ok and temp_audio.exists():
                jobs[job_id].update({'progress': 25, 'status_text': 'AI listening...'})
                whisper_result = transcribe_with_whisper(temp_audio, lang)
                if whisper_result:
                    jobs[job_id].update({'progress': 40, 'status_text': 'Creating captions...'})
                    if create_srt_from_whisper(whisper_result, srt_file):
                        has_subtitles = True
                        print("Captions created")
        
        jobs[job_id].update({'progress': 50, 'status_text': 'Applying dramatic edits...'})
        grade = ep.get('color_grade', 'vibrant')
        
        if grade == 'cinematic_warm':
            color = "colorbalance=rs=0.3:gs=0:bs=-0.2,eq=brightness=0.05:contrast=1.4:saturation=1.5:gamma=0.95"
        elif grade == 'cool':
            color = "colorbalance=rs=-0.2:gs=0:bs=0.3,eq=brightness=0.05:contrast=1.35:saturation=1.3"
        elif grade == 'dark':
            color = "eq=brightness=-0.05:contrast=1.6:saturation=1.3:gamma=0.85"
        elif grade == 'vibrant':
            color = "eq=brightness=0.1:contrast=1.5:saturation=1.8:gamma=0.95"
        else:
            color = "eq=brightness=0.05:contrast=1.3:saturation=1.4"
        
        filters = [color]
        if ep.get('sharpen', True):
            filters.append('unsharp=7:7:1.5')
        filters.append('vignette=PI/4')
        filters.append('scale=1080:1920:force_original_aspect_ratio=decrease')
        filters.append('pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black')
        
        if has_subtitles and srt_file.exists():
            srt_str = str(srt_file).replace('\\', '/').replace(':', '\\:')
            sub_style = "FontName=DejaVu Sans,FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&HCC000000,BorderStyle=4,Outline=3,Shadow=2,Bold=1,Alignment=2,MarginV=150"
            filters.append(f"subtitles='{srt_str}':force_style='{sub_style}'")
        
        if ep.get('add_fade', True) and duration > 1.5:
            fade_out_start = max(0.5, duration - 0.5)
            filters.append(f"fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out_start}:d=0.4")
        
        vf = ",".join(filters)
        af = "highpass=f=80,acompressor=threshold=-18dB:ratio=5:attack=150:release=800,equalizer=f=3000:width_type=o:width=2:g=4,volume=1.5"
        
        jobs[job_id].update({'progress': 70, 'status_text': 'Encoding HD...'})
        cmd = [
            'ffmpeg', '-y', '-i', str(inp),
            '-vf', vf, '-af', af,
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k',
            '-movflags', '+faststart', str(out)
        ]
        ok, err = run_cmd(cmd, timeout=500)
        
        if not ok:
            print(f"MAIN FAILED: {str(err)[-500:]}")
            jobs[job_id].update({'progress': 80, 'status_text': 'Safe mode...'})
            simple_vf = f"{color},unsharp=5:5:1.2,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
            cmd2 = [
                'ffmpeg', '-y', '-i', str(inp),
                '-vf', simple_vf,
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '22',
                '-c:a', 'aac', '-b:a', '128k', str(out)
            ]
            ok, err = run_cmd(cmd2, timeout=300)
            if not ok:
                print(f"FALLBACK FAILED: {str(err)[-500:]}")
                jobs[job_id].update({'status': 'error', 'error': 'FFmpeg failed: ' + str(err)[-200:]})
                return
            print("FALLBACK OK")
        else:
            print("MAIN SUCCESS")
        
        jobs[job_id].update({
            'status': 'done', 'progress': 100,
            'status_text': 'Ready!', 'output_file': str(out)
        })
    except Exception as e:
        print(f"EXCEPTION: {e}")
        jobs[job_id].update({'status': 'error', 'error': 'Error: ' + str(e)[:200]})
    finally:
        for f in [temp_audio, srt_file]:
            try:
                if f.exists():
                    f.unlink()
            except:
                pass

@app.route('/')
def index():
    try:
        return render_template_string(HTML)
    except Exception as e:
        return f"<h1>Error</h1><p>{e}</p>", 500

@app.route('/health')
def health():
    return 'OK', 200

@app.route('/api/debug')
def debug():
    try:
        return jsonify({
            'version': APP_VERSION,
            'groq_ready': groq_client is not None,
            'groq_key_set': bool(GROQ_API_KEY),
            'active_jobs': len(jobs)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/analyze', methods=['POST'])
def analyze():
    try:
        data = request.get_json(silent=True) or {}
        cmd = data.get('command', '').strip()
        if not cmd:
            return jsonify({'error': 'Please describe your video'}), 400
        return jsonify(analyze_with_groq(cmd, data.get('ref_url', '')))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload', methods=['POST'])
def upload():
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'No video'}), 400
        f = request.files['video']
        if not f.filename or not allowed_file(f.filename):
            return jsonify({'error': 'Invalid file'}), 400
        fname = f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'
        f.save(UPLOAD_FOLDER / fname)
        return jsonify({'file_id': fname})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/edit', methods=['POST'])
def edit():
    try:
        data = request.get_json(silent=True) or {}
        fid = data.get('file_id')
        plan = data.get('plan')
        if not fid or not plan:
            return jsonify({'error': 'Missing data'}), 400
        inp = UPLOAD_FOLDER / fid
        if not inp.exists():
            return jsonify({'error': 'File not found'}), 404
        size_mb = inp.stat().st_size / (1024 * 1024)
        if size_mb > 80:
            return jsonify({'error': f'File too large: {size_mb:.0f}MB'}), 400
        jid = uuid.uuid4().hex
        out = OUTPUT_FOLDER / f'edited_{jid}.mp4'
        jobs[jid] = {'status': 'processing', 'progress': 0, 'status_text': 'Starting...', 'output_file': None, 'error': None}
        def worker():
            try:
                apply_pro_edits(inp, out, plan, jid)
            except Exception as e:
                jobs[jid].update({'status': 'error', 'error': str(e)[:200]})
        threading.Thread(target=worker, daemon=True).start()
        return jsonify({'job_id': jid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/status/<jid>')
def status(jid):
    try:
        j = jobs.get(jid)
        if j:
            return jsonify(j)
        return jsonify({'status': 'expired', 'progress': 0, 'error': 'Job expired'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/<jid>')
def download(jid):
    try:
        j = jobs.get(jid)
        if not j or j['status'] != 'done':
            return jsonify({'error': 'Not ready'}), 400
        fp = Path(j['output_file'])
        if not fp.exists():
            return jsonify({'error': 'File missing'}), 500
        return send_file(str(fp), mimetype='video/mp4', as_attachment=True, download_name='AI_Pro_Reel.mp4')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/preview/<jid>')
def preview(jid):
    try:
        j = jobs.get(jid)
        if not j or j['status'] != 'done':
            return jsonify({'error': 'Not ready'}), 400
        response = send_file(j['output_file'], mimetype='video/mp4')
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500

HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>AI Pro Video Editor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,sans-serif}
body{background:#0a0a0a;color:#fff;min-height:100vh;padding:20px}
.wrap{max-width:1200px;margin:0 auto}
h1{font-size:1.8rem;margin-bottom:5px;color:#a855f7}
.sub{color:#888;font-size:.9rem;margin-bottom:25px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
.card{background:#1a1a2e;border:1px solid #333;border-radius:14px;padding:18px;margin-bottom:15px}
.card.hl{border-color:#a855f7}
h2{font-size:1rem;margin-bottom:12px;color:#ddd}
.step{display:inline-block;background:#a855f7;color:#fff;width:24px;height:24px;border-radius:50%;text-align:center;line-height:24px;font-size:12px;font-weight:700;margin-right:8px}
.uzone{border:2px dashed #444;border-radius:12px;padding:30px 15px;text-align:center;cursor:pointer}
.uzone:hover{border-color:#a855f7}
.ico{font-size:2.5rem;margin-bottom:8px}
input,textarea{width:100%;background:#242424;border:1px solid #444;border-radius:8px;padding:10px 12px;color:#fff;font-size:.9rem;outline:none;font-family:inherit}
input:focus,textarea:focus{border-color:#a855f7}
textarea{min-height:80px;resize:vertical;margin-bottom:10px}
.btn{padding:12px 18px;border-radius:8px;font-size:.95rem;font-weight:700;cursor:pointer;border:none;width:100%;margin-top:8px}
.btn-p{background:#a855f7;color:#fff}
.btn-p:disabled{opacity:.4;cursor:not-allowed}
.btn-g{background:#22c55e;color:#000;text-decoration:none;display:block;text-align:center}
.btn-o{background:transparent;border:1px solid #444;color:#888}
.row{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.mic{padding:10px 14px;border-radius:8px;background:#242424;border:1px solid #444;cursor:pointer;font-size:1.1rem}
.mic.on{background:rgba(239,68,68,0.2);border-color:#ef4444}
.effects{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.fxbtn{background:#242424;border:1px solid #444;border-radius:8px;padding:8px 10px;cursor:pointer;font-size:.78rem;text-align:center;color:#ccc}
.fxbtn:hover{border-color:#a855f7;color:#fff}
.fxbtn.sel{background:#a855f7;border-color:#a855f7;color:#fff}
.toggle{display:flex;align-items:center;gap:10px;padding:8px;background:#242424;border-radius:8px;margin-bottom:6px}
.toggle input{width:auto;margin:0}
.toggle label{flex:1;font-size:.85rem}
.pbar{width:100%;background:#242424;border-radius:99px;height:6px;overflow:hidden;margin:8px 0}
.pfill{height:100%;background:#a855f7;transition:.4s}
.chip{display:inline-block;background:rgba(168,85,247,0.15);border:1px solid rgba(168,85,247,0.4);color:#c4b5fd;border-radius:99px;padding:4px 12px;font-size:.8rem;margin-bottom:8px}
.summ{background:#242424;border-left:3px solid #a855f7;padding:10px 14px;margin:8px 0;font-size:.85rem}
.err{background:rgba(239,68,68,0.1);border:1px solid #ef4444;color:#fca5a5;padding:10px;border-radius:8px;margin:10px 0;display:none;font-size:.85rem}
video{width:100%;border-radius:10px;background:#000;margin-bottom:10px;max-height:600px}
.hint{font-size:.72rem;color:#666;margin-top:5px;text-align:center}
.feat{display:flex;justify-content:space-between;font-size:.78rem;color:#aaa;padding:4px 0}
.feat span:last-child{color:#22c55e;font-weight:700}
</style></head><body>
<div class="wrap">
<h1>🎬 AI Pro Video Editor</h1>
<div class="sub">Auto-Captions · Cinematic Color · 1080p HD</div>
<div class="grid">
<div>
<div class="card">
<h2><span class="step">1</span>Upload Video</h2>
<div class="uzone" id="zone" onclick="document.getElementById('fi').click()">
<div class="ico">📹</div>
<div>Click or drag video</div>
<div class="hint">MP4/MOV · Max 80MB</div>
</div>
<input type="file" id="fi" accept="video/*" style="display:none" onchange="up(this)">
</div>
<div class="card">
<h2><span class="step">2</span>Reference (optional)</h2>
<input type="url" id="ref" placeholder="YouTube link...">
</div>
<div class="card hl">
<h2><span class="step">3</span>Describe Your Vibe</h2>
<textarea id="cmd" placeholder="Hindi/English mein likho..."></textarea>
<div class="row">
<button class="mic" id="vb" onclick="voice()">🎤</button>
<span style="font-size:.8rem;color:#888;flex:1" id="vs">Tap mic to speak</span>
</div>
<button class="btn btn-p" id="ab" onclick="ai()">🤖 Generate Plan</button>
</div>
<div class="card">
<h2>⚡ Quick Effects</h2>
<div class="effects">
<div class="fxbtn" onclick="fx('vibrant',this)">🌈 Vibrant</div>
<div class="fxbtn" onclick="fx('cinematic_warm',this)">🎬 Cinematic</div>
<div class="fxbtn" onclick="fx('cool',this)">❄️ Cool</div>
<div class="fxbtn" onclick="fx('dark',this)">🌑 Dark</div>
<div class="fxbtn" onclick="fx('natural',this)">🌿 Natural</div>
</div>
</div>
<div class="card">
<h2>✨ Features</h2>
<div class="toggle">
<input type="checkbox" id="acap" checked>
<label for="acap">📝 Auto-Captions</label>
</div>
<div class="toggle">
<input type="checkbox" id="afade" checked>
<label for="afade">🌅 Fade In/Out</label>
</div>
</div>
</div>
<div>
<div class="card" id="pc" style="display:none">
<h2>🧠 Edit Plan</h2>
<div id="info"></div>
<button class="btn btn-p" id="eb" onclick="edit()" disabled>⚡ Start Editing</button>
<div class="hint" id="eh">Upload first</div>
</div>
<div class="card" id="prg" style="display:none">
<h2>⚙️ Editing...</h2>
<div class="pbar"><div class="pfill" id="pf" style="width:0%"></div></div>
<div style="display:flex;justify-content:space-between;font-size:.8rem;color:#888">
<span id="pt">Starting...</span><span id="pp">0%</span>
</div>
</div>
<div class="card" id="rc" style="display:none">
<h2>✅ Ready!</h2>
<video id="pv" controls playsinline preload="metadata"></video>
<a id="dl" class="btn btn-g">⬇️ Download</a>
<button class="btn btn-o" onclick="reset()">🔄 New</button>
</div>
<div id="eb2" class="err"></div>
<div class="card" id="ph" style="text-align:center;padding:40px 20px">
<div style="font-size:3rem;opacity:0.2">🎬</div>
<div style="color:#666;margin-top:10px">Upload → Describe → Edit!</div>
</div>
</div>
</div>
</div>
<script>
let fid=null,plan=null,jid=null,poll=null,rec=null,isRec=false,fxOverride=null;
const zone=document.getElementById('zone');
zone.ondragover=e=>{e.preventDefault();zone.style.borderColor='#a855f7'};
zone.ondragleave=()=>zone.style.borderColor='';
zone.ondrop=e=>{e.preventDefault();zone.style.borderColor='';if(e.dataTransfer.files[0])upf(e.dataTransfer.files[0])};
function up(i){if(i.files[0])upf(i.files[0])}
async function upf(f){
  if(f.size>80*1024*1024){zone.innerHTML='<div class="ico">❌</div><div style="color:#ef4444">Too large!</div>';return}
  zone.innerHTML='<div class="ico">⏳</div><div>Uploading...</div>';
  const fd=new FormData();fd.append('video',f);
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    fid=d.file_id;
    zone.innerHTML='<div class="ico">✅</div><div style="color:#22c55e">'+f.name+'</div>';
    upd();
  }catch(e){zone.innerHTML='<div class="ico">❌</div><div style="color:#ef4444">'+e.message+'</div>'}
}
function voice(){
  if(!('webkitSpeechRecognition'in window)){alert('Use Chrome!');return}
  if(isRec){rec&&rec.stop();return}
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  rec=new SR();rec.lang='hi-IN';rec.interimResults=true;
  rec.onstart=()=>{isRec=true;document.getElementById('vb').classList.add('on');document.getElementById('vs').textContent='🔴 Recording...'}
  rec.onresult=e=>{let t='';for(let i=e.resultIndex;i<e.results.length;i++)t+=e.results[i][0].transcript;document.getElementById('cmd').value=t}
  rec.onend=()=>{isRec=false;document.getElementById('vb').classList.remove('on');document.getElementById('vs').textContent='Tap mic'}
  rec.start();
}
function fx(style,btn){
  document.querySelectorAll('.fxbtn').forEach(b=>b.classList.remove('sel'));
  btn.classList.add('sel');
  fxOverride=style;
  if(plan){plan.edit_plan.color_grade=style;show(plan)}
}
async function ai(){
  const c=document.getElementById('cmd').value.trim();
  if(!c){alert('Describe your video!');return}
  const b=document.getElementById('ab');
  b.disabled=true;b.textContent='Generating...';
  document.getElementById('ph').style.display='none';
  try{
    const r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:c,ref_url:document.getElementById('ref').value.trim()})});
    plan=await r.json();
    if(plan.error)throw new Error(plan.error);
    if(fxOverride)plan.edit_plan.color_grade=fxOverride;
    plan.edit_plan.auto_captions=document.getElementById('acap').checked;
    plan.edit_plan.add_fade=document.getElementById('afade').checked;
    show(plan);
  }catch(e){err('AI error: '+e.message)}
  finally{b.disabled=false;b.textContent='🤖 Generate Plan'}
}
function show(p){
  const ep=p.edit_plan||{};
  document.getElementById('info').innerHTML=
    '<div class="chip">🎯 '+(p.niche||'general')+'</div>'+
    '<div class="summ">'+(p.edit_summary||'Pro edit')+'</div>'+
    '<div class="feat"><span>🎨 Color</span><span>'+(ep.color_grade||'vibrant')+'</span></div>'+
    '<div class="feat"><span>📝 Captions</span><span>'+(ep.auto_captions?'ON':'OFF')+'</span></div>';
  document.getElementById('pc').style.display='block';
  upd();
}
function upd(){
  const ok=fid&&plan;
  document.getElementById('eb').disabled=!ok;
  document.getElementById('eh').textContent=ok?'✅ Ready!':(fid?'Generate plan':'Upload video');
}
async function edit(){
  if(!fid||!plan)return;
  document.getElementById('pc').style.display='none';
  document.getElementById('prg').style.display='block';
  try{
    const r=await fetch('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fid,plan:plan})});
    const d=await r.json();
    if(d.error)throw new Error(d.error);
    jid=d.job_id;poll_status();
  }catch(e){err(e.message);document.getElementById('prg').style.display='none'}
}
function poll_status(){
  if(poll)clearInterval(poll);
  poll=setInterval(async()=>{
    try{
      const r=await fetch('/api/status/'+jid);
      const d=await r.json();
      if(!d||d.error||d.status==='expired'){clearInterval(poll);err(d.error||'Failed');document.getElementById('prg').style.display='none';return}
      document.getElementById('pf').style.width=(d.progress||0)+'%';
      document.getElementById('pp').textContent=(d.progress||0)+'%';
      document.getElementById('pt').textContent=d.status_text||'Processing...';
      if(d.status==='done'){clearInterval(poll);poll=null;done()}
      else if(d.status==='error'){clearInterval(poll);poll=null;err(d.error);document.getElementById('prg').style.display='none'}
    }catch(e){}
  },2000);
}
function done(){
  if(poll){clearInterval(poll);poll=null}
  document.getElementById('prg').style.display='none';
  document.getElementById('rc').style.display='block';
  const v=document.getElementById('pv');
  v.src='/api/preview/'+jid+'?t='+Date.now();v.load();
  document.getElementById('dl').href='/api/download/'+jid;
}
function err(m){
  const b=document.getElementById('eb2');
  b.textContent='⚠️ '+m;b.style.display='block';
  setTimeout(()=>b.style.display='none',10000);
}
function reset(){
  if(poll){clearInterval(poll);poll=null}
  fid=null;plan=null;jid=null;fxOverride=null;
  const v=document.getElementById('pv');v.pause();v.src='';v.load();
  zone.innerHTML='<div class="ico">📹</div><div>Click or drag video</div>';
  document.getElementById('cmd').value='';document.getElementById('ref').value='';
  document.querySelectorAll('.fxbtn').forEach(b=>b.classList.remove('sel'));
  ['pc','prg','rc'].forEach(id=>document.getElementById(id).style.display='none');
  document.getElementById('ph').style.display='block';
}
</script></body></html>"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
