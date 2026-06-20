import os, re, json, uuid, time, threading, subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from werkzeug.utils import secure_filename
from groq import Groq

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

UPLOAD_FOLDER = Path("/tmp/uploads")
OUTPUT_FOLDER = Path("/tmp/outputs")
TEMP_FOLDER = Path("/tmp/temp")
for d in [UPLOAD_FOLDER, OUTPUT_FOLDER, TEMP_FOLDER]:
    d.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
jobs = {}

def run_ffmpeg(cmd, timeout=300):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stderr[:500] if r.stderr else ""
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)

def find_font():
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return ""

def analyze_with_groq(cmd):
    prompt = """You are a video editor. Reply ONLY with JSON: {\"caption_text\":\"2-4 words\", \"color_grade\":\"vibrant|cinematic_warm|dark|natural\", \"transition\":\"zoom|fade|flash|none\"}"""
    try:
        r = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":prompt},{"role":"user","content":cmd}],
            temperature=0.3, max_tokens=200
        )
        return json.loads(re.sub(r"```json|```", "", r.choices[0].message.content.strip()))
    except:
        return {"caption_text":"AWESOME", "color_grade":"vibrant", "transition":"zoom"}

def process_video(inp, out, plan, job_id):
    try:
        jobs[job_id].update({"progress":10, "status_text":"Starting..."})
        time.sleep(0.5)
        
        jobs[job_id].update({"progress":25, "status_text":"Applying color grade..."})
        grade = plan.get("color_grade", "vibrant")
        
        if grade == "vibrant":
            color_filter = "eq=brightness=0.05:contrast=1.25:saturation=1.4"
        elif grade == "cinematic_warm":
            color_filter = "colorbalance=rs=0.15:gs=-0.03:bs=-0.1,eq=brightness=0.02:contrast=1.2:saturation=1.2"
        elif grade == "dark":
            color_filter = "eq=brightness=-0.08:contrast=1.3:saturation=0.85"
        else:
            color_filter = "eq=brightness=0.03:contrast=1.15:saturation=1.15"
        
        transition = plan.get("transition", "zoom")
        if transition == "zoom":
            jobs[job_id].update({"progress":40, "status_text":"Adding zoom transition..."})
            transition_filter = "zoompan=z='min(zoom+0.0012,1.15)':d=1:s=1920:1080:fps=30"
        elif transition == "fade":
            transition_filter = "fade=t=in:st=0:d=0.5,fade=t=out:st=END-0.5:d=0.5"
        elif transition == "flash":
            transition_filter = "geq=lum='if(lt(X,W/2-100),255,if(lt(X,W/2+100),255,128))':cb=128:cr=128,boxblur=10:1"
        else:
            transition_filter = "null"
        
        jobs[job_id].update({"progress":60, "status_text":"Adding caption..."})
        caption = plan.get("caption_text", "AWESOME").upper()
        caption = re.sub(r"[^\w\s]", "", caption)[:30]
        
        font = find_font()
        if font:
            words = caption.split()
            if len(words) > 2:
                mid = len(words) // 2
                line1 = " ".join(words[:mid])
                line2 = " ".join(words[mid:])
                caption_filter = (
                    f"drawtext=text='{line1}':fontfile={font}:fontsize=80:fontcolor=white:"
                    f"bordercolor=black:borderw=6:shadowcolor=black@0.8:shadowx=4:shadowy=4:"
                    f"x=(w-tw)/2:y=(h/2)-100,"
                    f"drawtext=text='{line2}':fontfile={font}:fontsize=80:fontcolor=white:"
                    f"bordercolor=black:borderw=6:shadowcolor=black@0.8:shadowx=4:shadowy=4:"
                    f"x=(w-tw)/2:y=(h/2)+10"
                )
            else:
                caption_filter = (
                    f"drawtext=text='{caption}':fontfile={font}:fontsize=96:fontcolor=white:"
                    f"bordercolor=black:borderw=8:shadowcolor=black@0.8:shadowx=5:shadowy=5:"
                    f"x=(w-tw)/2:y=(h-text_h)/2"
                )
        else:
            caption_filter = ""
        
        filters = [color_filter, transition_filter]
        if caption_filter:
            filters.append(caption_filter)
        full_filter = ",".join(filters)
        
        jobs[job_id].update({"progress":80, "status_text":"Encoding HD video..."})
        cmd = [
            "ffmpeg", "-y", "-i", str(inp),
            "-vf", full_filter,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out)
        ]
        
        ok, err = run_ffmpeg(cmd, timeout=300)
        
        if ok:
            jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
        else:
            jobs[job_id].update({"progress":90, "status_text":"Retrying without text..."})
            cmd2 = [
                "ffmpeg", "-y", "-i", str(inp),
                "-vf", color_filter,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out)
            ]
            ok2, _ = run_ffmpeg(cmd2, timeout=300)
            if ok2:
                jobs[job_id].update({"status":"done","progress":100,"status_text":"Ready!","output_file":str(out)})
            else:
                jobs[job_id].update({"status":"error","error":err or "FFmpeg failed"})
                
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})

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
    return jsonify(analyze_with_groq(data.get("command","make a cool video")))

@app.route("/api/edit", methods=["POST"])
def edit():
    data = request.json or {}
    fid = data.get("file_id")
    plan = data.get("plan")
    if not fid or not plan:
        return jsonify({"error":"Missing data"}), 400
    inp = UPLOAD_FOLDER / fid
    
