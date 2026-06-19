import os, re, json, uuid, time, threading, subprocess, traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp_processing")
for f in [UPLOAD_FOLDER, OUTPUT_FOLDER, TEMP_FOLDER]:
    f.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
jobs = {}

def allowed_file(f):
    return "." in f and f.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def run_cmd(cmd, timeout=300):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr
    except Exception as e:
        return False, str(e)

def clean_plan(raw):
    ep = raw.get('edit_plan') or raw
    return {
        'niche': raw.get('niche', 'general'),
        'niche_hindi': raw.get('niche_hindi', 'सामान्य'),
        'detected_language': raw.get('detected_language', 'english'),
        'edit_plan': {
            'color_grade': ep.get('color_grade', 'cinematic_warm'),
            'brightness': max(-10, min(15, ep.get('brightness', 3))),
            'contrast': max(-10, min(15, ep.get('contrast', 8))),
            'saturation': max(-10, min(20, ep.get('saturation', 12))),
            'sharpen': bool(ep.get('sharpen', True)),
            'denoise': bool(ep.get('denoise', True)),
            'platform': ep.get('platform', 'reels'),
            'caption_text': ep.get('caption_text') or 'YOUR MOMENT'
        },
        'edit_summary': raw.get('edit_summary', 'Cinematic grade with bold captions.'),
        'edit_summary_hindi': raw.get('edit_summary_hindi', 'सिनेमैटिक ग्रेड और कैप्शन।')
    }

def analyze_with_groq(command_text, ref_url=''):
    if not groq_client:
        return clean_plan({})
    system_prompt = 'You are an FFmpeg video filter generator. Respond ONLY with valid JSON. NO markdown. NO extra fields. EXACT FORMAT: {"niche":"gaming","niche_hindi":"गेमिंग","detected_language":"english","edit_plan":{"color_grade":"cinematic_warm","brightness":3,"contrast":8,"saturation":12,"sharpen":true,"denoise":true,"platform":"reels","caption_text":"BOLD TEXT"},"edit_summary":"Summary.","edit_summary_hindi":"सारांश।"} RULES: IGNORE speed_ramps, transitions, intros, audio. ONLY output keys above. caption_text must be short (<5 words).'
    msg = f'Creator Request: {command_text}'
    if ref_url:
        msg += f'\nReference URL: {ref_url}'
    try:
        resp = groq_client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role':'system','content':system_prompt},{'role':'user','content':msg}],
            temperature=0.1, max_tokens=300
        )
        raw = re.sub(r'```json|```','', resp.choices[0].message.content.strip()).strip()
        return clean_plan(json.loads(raw))
    except Exception as e:
        print(f'Groq error: {e}')
        return clean_plan({})

def build_filters(plan, w=854, h=480):
    ep = plan.get('edit_plan', {})
    f = []
    b = max(-0.06, min(0.06, ep.get('brightness', 3) / 100.0))
    c = max(0.9, min(1.08, 1.0 + ep.get('contrast', 8) / 100.0))
    s = max(0.9, min(1.15, 1.0 + ep.get('saturation', 12) / 100.0))
    f.append(f'eq=brightness={b}:contrast={c}:saturation={s}')
    if ep.get('sharpen'):
        f.append('unsharp=3:3:0.5')
    if ep.get('denoise'):
        f.append('hqdn3d=0.5:0.5:3:3')
    f.append(f'scale={w}:{h}:force_original_aspect_ratio=decrease')
    f.append(f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black')
    cap = ep.get('caption_text', 'YOUR MOMENT')
    if cap and len(cap) < 35:
        cap = cap.replace('"', '\\"').replace("'", "\\'")
        f.append(f'drawtext=text=\'{cap}\':fontsize=22:fontcolor=white:fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:x=(w-tw)/2:y=h-th-30:box=1:boxcolor=black@0.7:boxborderw=3')
    return ','.join(f)

def apply_edits(inp, out, plan, job_id):
    try:
        jobs[job_id].update({'progress':10,'status_text':'Optimizing for free tier...'})
        vf = build_filters(plan, 854, 480)
        cmd = ['ffmpeg','-y','-i',str(inp),'-vf',vf,'-c:a','copy','-c:v','libx264','-preset','ultrafast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(out)]
        jobs[job_id].update({'progress':40,'status_text':'Encoding 480p (safe mode)...'})
        ok, err = run_cmd(cmd, timeout=300)
        if not ok:
            jobs[job_id].update({'status':'error','error':'FFmpeg error: '+str(err)[-500:]})
            return
        jobs[job_id].update({'status':'done','progress':100,'status_text':'Ready!','output_file':str(out)})
    except Exception as e:
        jobs[job_id].update({'status':'error','error':'Thread crashed: '+str(e)[:250]})

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/health')
def health():
    return 'OK', 200

@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json or {}
    cmd = data.get('command','').strip()
    if not cmd:
        return jsonify({'error':'Please describe your niche'}), 400
    return jsonify(analyze_with_groq(cmd, data.get('ref_url','')))

@app.route('/api/upload', methods=['POST'])
def upload():
    if 'video' not in request.files:
        return jsonify({'error':'No video'}), 400
    f = request.files['video']
    if not f.filename or not allowed_file(f.filename):
        return jsonify({'error':'Invalid file type'}), 400
    fname = f'{uuid.uuid4().hex}_{secure_filename(f.filename)}'
    f.save(UPLOAD_FOLDER / fname)
    return jsonify({'file_id': fname})

@app.route('/api/edit', methods=['POST'])
def edit():
    data = request.json or {}
    fid, plan = data.get('file_id'), data.get('plan')
    if not fid or not plan:
        return jsonify({'error':'Missing data'}), 400
    inp = UPLOAD_FOLDER / fid
    if not inp.exists():
        return jsonify({'error':'File not found'}), 404
    size_mb = inp.stat().st_size / (1024*1024)
    if size_mb > 30:
        return jsonify({'error':f'Free tier limit: videos must be <30MB. Yours is {size_mb:.0f}MB.'}), 400
    jid = uuid.uuid4().hex
    out = OUTPUT_FOLDER / f'edited_{jid}.mp4'
    jobs[jid] = {'status':'processing','progress':0,'status_text':'Starting...','output_file':None,'error':None}
    def safe_worker():
        try:
            apply_edits(inp, out, plan, jid)
        except Exception as e:
            jobs[jid].update({'status':'error','error':str(e)[:200]})
    threading.Thread(target=safe_worker, daemon=True).start()
    return jsonify({'job_id': jid})

@app.route('/api/status/<jid>')
def status(jid):
    j = jobs.get(jid)
    if j:
        return jsonify(j)
    return jsonify({'status':'expired','progress':0,'status_text':'Job lost (server restart).','error':'Job not found - please refresh and try again.'}), 404

@app.route('/api/download/<jid>')
def download(jid):
    j = jobs.get(jid)
    if not j or j['status'] != 'done':
        return jsonify({'error':'Not ready'}), 400
    fp = Path(j['output_file'])
    if not fp.exists() or not fp.is_file():
        return jsonify({'error':'Output missing'}), 500
    return send_file(str(fp), mimetype='video/mp4', as_attachment=True, download_name='AI_Edited.mp4')

@app.route('/api/preview/<jid>')
def preview(jid):
    j = jobs.get(jid)
    if not j or j['status'] != 'done':
        return jsonify({'error':'Not ready'}), 400
    return send_file(j['output_file'], mimetype='video/mp4')

HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>AI Video Editor</title><style>*{box-sizing:border-box;margin:0;padding:0}:root{--bg:#0d0d0d;--bg2:#1a1a2e;--bg3:#242424;--border:#333;--text:#f0f0f0;--muted:#888;--purple:#a855f7;--pd:#7c3aed;--green:#22c55e;--red:#ef4444}body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}.wrap{max-width:1100px;margin:0 auto;padding:0 1rem}header{padding:1.2rem 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:1rem}.logo{width:36px;height:36px;background:var(--pd);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:1.2rem}h1{font-size:1.4rem;font-weight:700}.main{padding:1.5rem 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}@media(max-width:650px){.grid{grid-template-columns:1fr}}.card{background:var(--bg2);border:1px solid var(--border);border-radius:13px;padding:1.1rem}.hl{border-color:rgba(168,85,247,.4)}.sh{display:flex;align-items:center;gap:.55rem;margin-bottom:.9rem}.sn{width:22px;height:22px;background:var(--pd);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}h2{font-size:.95rem;font-weight:600;color:#ccc;margin:0}input,textarea{width:100%;background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:.6rem .8rem;color:var(--text);font-size:.9rem;font-family:inherit;outline:none}input:focus,textarea:focus{border-color:var(--purple)}textarea{resize:vertical;min-height:85px;line-height:1.5;margin-bottom:.45rem}.btn{padding:.6rem 1.1rem;border-radius:8px;font-size:.88rem;font-weight:600;cursor:pointer;border:none;display:flex;align-items:center;gap:.35rem;justify-content:center;width:100%;transition:all .15s}.bp{background:var(--purple);color:#fff}.bp:hover{background:var(--pd)}.bp:disabled{opacity:.4;cursor:not-allowed}.bg{background:var(--green);color:#000;text-decoration:none}.bo{background:transparent;color:var(--muted);border:1px solid var(--border);margin-top:.45rem}.uzone{border:2px dashed var(--border);border-radius:11px;padding:1.8rem 1rem;text-align:center;cursor:pointer;transition:all .2s}.uzone:hover{border-color:var(--purple);background:rgba(168,85,247,.05)}.ico{font-size:2.2rem;margin-bottom:.4rem}.hint{font-size:.75rem;color:var(--muted);margin-top:.25rem}.pbar{width:100%;background:var(--bg3);border-radius:99px;height:5px;overflow:hidden;margin:.45rem 0}.pfill{height:100%;background:linear-gradient(90deg,var(--pd),var(--purple));border-radius:99px;transition:width .4s}.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:.35rem;margin-top:.6rem}.pi{background:var(--bg3);border-radius:7px;padding:.4rem .65rem;font-size:.78rem}.pl{color:var(--muted);font-size:.68rem;text-transform:uppercase;margin-bottom:1px}.pv{font-weight:600}.chip{display:inline-flex;align-items:center;background:rgba(168,85,247,.15);border:1px solid rgba(168,85,247,.3);color:var(--purple);border-radius:99px;padding:.28rem .7rem;font-size:.82rem;font-weight:600;margin-bottom:.65rem}.summ{background:var(--bg3);border-left:3px solid var(--purple);border-radius:0 7px 7px 0;padding:.65rem .9rem;margin:.45rem 0;font-size:.82rem;line-height:1.55;color:#ddd}.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#fca5a5;border-radius:8px;padding:.65rem .9rem;font-size:.82rem;margin:.4rem 0;display:none}video{width:100%;border-radius:9px;background:#000;margin-bottom:.65rem}.vbtn{padding:.45rem;border-radius:7px;background:var(--bg3);border:1px solid var(--border);cursor:pointer;font-size:1rem;flex-shrink:0}.vbtn.on{background:rgba(239,68,68,.15);border-color:var(--red);animation:p .8s infinite}@keyframes p{0%,100%{opacity:1}50%{opacity:.5}}.row{display:flex;gap:.45rem;align-items:center;margin-bottom:.45rem}.ph{text-align:center;padding:2.5rem 1rem}</style></head><body><header><div class="wrap" style="display:flex;align-items:center;gap:1rem;width:100%"><div class="logo">🎬</div><div><h1>AI Video Editor</h1><div style="font-size:.75rem;color:var(--muted)">Groq AI - Hindi & English</div></div></div></header><div class="main"><div class="wrap"><div class="grid"><div style="display:flex;flex-direction:column;gap:1.1rem"><div class="card"><div class="sh"><div class="sn">1</div><h2>Upload your raw video</h2></div><div class="uzone" id="zone" onclick="document.getElementById('fi').click()"><div class="ico">📹</div><div>Click or drag & drop</div><div class="hint">MP4 MOV AVI MKV - Max 30MB, under 2 min</div></div><input type="file" id="fi" accept="video/*" style="display:none" onchange="doUpload(this)"/></div><div class="card"><div class="sh"><div class="sn">2</div><h2>Reference link (optional)</h2></div><input type="url" id="ref" placeholder="YouTube / Instagram / TikTok URL..."/><div class="hint" style="margin-top:.35rem">AI copies the editing style</div></div><div class="card hl"><div class="sh"><div class="sn">3</div><h2>Tell AI what you want</h2></div><textarea id="cmd" placeholder="Hindi ya English mein likho: Main fitness creator hoon, trending reels banao OR: I am a vlogger, make cinematic YouTube video"></textarea><div class="row"><button class="vbtn" id="vb" onclick="toggleVoice()">🎤</button><span style="font-size:.78rem;color:var(--muted)" id="vs">Tap mic to speak</span></div><button class="btn bp" id="ab" onclick="doAnalyze()">🤖 Analyze with AI</button></div></div><div style="display:flex;flex-direction:column;gap:1.1rem"><div class="card" id="pc" style="display:none"><h2 style="margin-bottom:.7rem">🧠 AI Edit Plan</h2><div id="nd"></div><div id="sd"></div><div class="pgrid" id="pg"></div><div style="margin-top:.9rem"><button class="btn bp" id="eb" onclick="doEdit()" disabled>⚡ Start Editing</button><div class="hint" style="text-align:center;margin-top:.35rem" id="eh">Upload a video first</div></div></div><div class="card" id="prg" style="display:none"><h2 style="margin-bottom:.7rem">⚙️ Editing...</h2><div class="pbar"><div class="pfill" id="pf" style="width:0%"></div></div><div style="display:flex;justify-content:space-between;font-size:.78rem;color:var(--muted)"><span id="pt">Starting...</span><span id="pp">0%</span></div><div class="hint" style="margin-top:.4rem">1-3 min on free tier</div></div><div class="card" id="rc" style="display:none"><h2 style="margin-bottom:.7rem">✅ Ready!</h2><video id="pv" controls playsinline></video><a id="dl" class="btn bg">⬇️ Download Edited Video</a><button class="btn bo" onclick="resetAll()">🔄 Edit another</button></div><div id="eb2" class="err"></div><div class="card ph" id="ph"><div style="font-size:2.8rem;opacity:.18;margin-bottom:.65rem">🎬</div><div style="color:var(--muted);font-size:.88rem">Upload - Reference - Describe - Edit!</div><div style="margin-top:.9rem;font-size:.76rem;color:#444">Fitness - Vlog - Entertainment - Gaming - Food - Travel</div></div></div></div></div></div><script>let fid=null,plan=null,jid=null,poll=null,rec=null,isRec=false;const zone=document.getElementById('zone');zone.addEventListener('dragover',e=>{e.preventDefault();zone.style.borderColor='var(--purple)'});zone.addEventListener('dragleave',()=>zone.style.borderColor='');zone.addEventListener('drop',e=>{e.preventDefault();zone.style.borderColor='';const f=e.dataTransfer.files[0];if(f)doUploadFile(f)});function doUpload(inp){if(inp.files[0])doUploadFile(inp.files[0]);}async function doUploadFile(file){if(file.size>30*1024*1024){zone.innerHTML='<div class="ico">❌</div><div style="color:var(--red)">File too large! Max 30MB for free tier.</div>';return;}zone.innerHTML='<div class="ico">⏳</div><div>Uploading '+file.name+'...</div>';const fd=new FormData();fd.append('video',file);try{const r=await fetch('/api/upload',{method:'POST',body:fd});const d=await r.json();if(d.error)throw new Error(d.error);fid=d.file_id;zone.innerHTML='<div class="ico">✅</div><div style="color:var(--green)">'+file.name+'</div><div class="hint">Uploaded!</div>';updBtn();}catch(e){zone.innerHTML='<div class="ico">❌</div><div style="color:var(--red)">'+e.message+'</div>';}}function toggleVoice(){if(!('webkitSpeechRecognition'in window)&&!('SpeechRecognition'in window)){alert('Use Chrome!');return;}if(isRec){rec&&rec.stop();return;}const SR=window.SpeechRecognition||window.webkitSpeechRecognition;rec=new SR();rec.lang='hi-IN';rec.interimResults=true;rec.onstart=()=>{isRec=true;document.getElementById('vb').classList.add('on');document.getElementById('vs').textContent='Recording...';};rec.onresult=e=>{let t='';for(let i=e.resultIndex;i<e.results.length;i++)t+=e.results[i][0].transcript;document.getElementById('cmd').value=t;};rec.onend=rec.onerror=()=>{isRec=false;document.getElementById('vb').classList.remove('on');document.getElementById('vs').textContent='Done!';setTimeout(()=>document.getElementById('vs').textContent='Tap mic to speak',2000);};rec.start();}async function doAnalyze(){const cmd=document.getElementById('cmd').value.trim();if(!cmd){alert('Please describe your niche!');return;}const btn=document.getElementById('ab');btn.disabled=true;btn.textContent='Analyzing...';document.getElementById('ph').style.display='none';document.getElementById('pc').style.display='none';try{const r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd,ref_url:document.getElementById('ref').value.trim()})});plan=await r.json();showPlan(plan);}catch(e){showErr('AI error: '+e.message);}finally{btn.disabled=false;btn.textContent='Analyze with AI';}}function showPlan(p){const ep=p.edit_plan||{};document.getElementById('nd').innerHTML='<div class="chip">🎯 '+p.niche+(p.niche_hindi?' · '+p.niche_hindi:'')+'</div>';const lang=p.detected_language||'english';const s=(lang==='hindi'||lang==='hinglish')?(p.edit_summary_hindi||p.edit_summary):p.edit_summary;document.getElementById('sd').innerHTML='<div class="summ">'+s+'</div>';const it=[['Color',ep.color_grade],['Platform',ep.platform],['Quality','480p Safe'],['Denoise',ep.denoise?'✅':'—']];document.getElementById('pg').innerHTML=it.map(([k,v])=>'<div class="pi"><div class="pl">'+k+'</div><div class="pv">'+v+'</div></div>').join('');document.getElementById('pc').style.display='block';updBtn();}function updBtn(){const ok=fid&&plan;document.getElementById('eb').disabled=!ok;document.getElementById('eh').textContent=ok?'Ready!':(fid?'Analyze first':'Upload video first');}async function doEdit(){if(!fid||!plan)return;document.getElementById('pc').style.display='none';document.getElementById('prg').style.display='block';document.getElementById('rc').style.display='none';try{const r=await fetch('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_id:fid,plan})});const d=await r.json();if(d.error)throw new Error(d.error);jid=d.job_id;doPoll();}catch(e){showErr(e.message);document.getElementById('prg').style.display='none';document.getElementById('pc').style.display='block';}}function doPoll(){if(poll)clearInterval(poll);poll=setInterval(async()=>{try{const r=await fetch('/api/status/'+jid);const d=await r.json();if(!d||d.error||d.status==='expired'){clearInterval(poll);showErr(d.error||'Processing failed. Server may have restarted. Refresh & try again.');document.getElementById('prg').style.display='none';return;}document.getElementById('pf').style.width=(d.progress||0)+'%';document.getElementById('pp').textContent=(d.progress||0)+'%';document.getElementById('pt').textContent=d.status_text||'Processing...';if(d.status==='done'){clearInterval(poll);showResult();}else if(d.status==='error'){clearInterval(poll);showErr(d.error||'Error');document.getElementById('prg').style.display='none';}}catch(e){}},1500);}function showResult(){document.getElementById('prg').style.display='none';document.getElementById('rc').style.display='block';document.getElementById('pv').src='/api/preview/'+jid;document.getElementById('dl').href='/api/download/'+jid;}function showErr(msg){const b=document.getElementById('eb2');b.textContent='Error: '+msg;b.style.display='block';setTimeout(()=>b.style.display='none',10000);}function resetAll(){fid=null;plan=null;jid=null;if(poll)clearInterval(poll);zone.innerHTML='<div class="ico">📹</div><div>Click or drag & drop</div><div class="hint">MP4 MOV AVI MKV - Max 30MB, under 2 min</div>';document.getElementById('cmd').value='';document.getElementById('ref').value='';['pc','prg','rc'].forEach(id=>document.getElementById(id).style.display='none');document.getElementById('ph').style.display='block';}</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
