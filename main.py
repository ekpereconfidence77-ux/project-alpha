import os
import uuid
import sqlite3
import requests
from flask import Flask, request, jsonify, make_response, render_template_string

app = Flask(__name__)

DB_FILE = "alpha_memory.db"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            memory TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_memories(user_id):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT memory FROM memories WHERE user_id = ? ORDER BY id ASC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def save_memory(user_id, memory):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT INTO memories (user_id, memory) VALUES (?, ?)",
        (user_id, memory)
    )
    conn.commit()
    conn.close()


def create_conversation(user_id, title="New chat"):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute(
        "INSERT INTO conversations (user_id, title) VALUES (?, ?)",
        (user_id, title[:80] or "New chat")
    )
    conversation_id = cur.lastrowid
    conn.commit()
    conn.close()
    return conversation_id


def get_conversations(user_id):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        """SELECT id, title, created_at
           FROM conversations
           WHERE user_id = ?
           ORDER BY id DESC""",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_messages(conversation_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        """SELECT m.role, m.content, m.created_at
           FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           WHERE m.conversation_id = ? AND c.user_id = ?
           ORDER BY m.id ASC""",
        (conversation_id, user_id)
    ).fetchall()
    conn.close()
    return rows


def save_message(conversation_id, role, content):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
        (conversation_id, role, content)
    )
    conn.commit()
    conn.close()


def rename_conversation(conversation_id, user_id, title):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """UPDATE conversations SET title = ?
           WHERE id = ? AND user_id = ?""",
        (title[:80] or "New chat", conversation_id, user_id)
    )
    conn.commit()
    conn.close()


init_db()

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Project Alpha</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#11151b;color:#fff;font-family:Arial,sans-serif;height:100vh;display:flex}
#sidebar{width:260px;background:#181d25;border-right:1px solid #2a303a;padding:12px;display:flex;flex-direction:column}
#newChat{width:100%;padding:12px;border:0;border-radius:10px;background:#2b6cff;color:#fff;font-size:15px;margin-bottom:12px}
#history{overflow-y:auto;flex:1}
.history-item{padding:11px;border-radius:9px;margin-bottom:5px;color:#ddd;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.history-item:hover,.history-item.active{background:#29303a}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
header{padding:16px 18px;background:#181d25;border-bottom:1px solid #2a303a;font-size:20px;font-weight:700}
#chat{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
.message{max-width:85%;padding:12px 14px;border-radius:16px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}
.user{align-self:flex-end;background:#2b6cff}.alpha{align-self:flex-start;background:#242a33}
.status{text-align:center;color:#aab2bf;font-size:13px;min-height:18px;padding:0 12px 6px}
.composer{display:flex;gap:8px;padding:10px;background:#181d25;border-top:1px solid #2a303a}
input{flex:1;min-width:0;border:1px solid #343b46;background:#222832;color:#fff;border-radius:12px;padding:13px 14px;outline:none;font-size:16px}
button{border:none;border-radius:12px;color:#fff;font-size:18px;min-width:48px;padding:0 14px;cursor:pointer}
#micButton{background:#303641}#micButton.recording{background:#d22;animation:pulse 1s infinite}
#sendButton{background:#2b6cff}
button:disabled{opacity:.55;cursor:not-allowed}
@keyframes pulse{0%{transform:scale(1)}50%{transform:scale(1.06)}100%{transform:scale(1)}}
@media(max-width:700px){ #sidebar{display:none} }
</style>
</head>
<body>
<aside id="sidebar">
<button id="newChat" onclick="newChat()">＋ New chat</button>
<div id="history"></div>
</aside>

<section id="main">
<header>🤖 Project Alpha</header>
<div id="chat"></div>
<div id="status" class="status"></div>

<div id="imagePanel" style="display:none;padding:10px;background:#181d25;border-top:1px solid #2a303a">
  <input id="imageFile" type="file" accept="image/*" style="width:100%;margin-bottom:8px">
  <input id="imagePrompt" placeholder="Tell Alpha how to edit this picture..." style="width:100%;margin-bottom:8px">
  <button id="editButton" onclick="editImage()" style="background:#7b3cff;width:100%;padding:12px">🎨 Edit / Generate Image</button>
</div>

<div class="composer">
<button id="imageButton" onclick="toggleImagePanel()">🖼️</button>
<button id="micButton" onclick="toggleRecording()">🎤</button>
<input id="message" placeholder="Talk to Alpha..." autocomplete="off">
<button id="sendButton" onclick="sendMessage()">➤</button>
</div>
</section>

<script>
let mediaRecorder=null,audioChunks=[],isRecording=false,recordingMimeType="";
let currentChatId=null;

const chat=document.getElementById("chat");
const historyBox=document.getElementById("history");
const messageInput=document.getElementById("message");
const micButton=document.getElementById("micButton");
const sendButton=document.getElementById("sendButton");
const statusBox=document.getElementById("status");

function setStatus(t){statusBox.textContent=t||""}

function addMessage(text,who){
 const div=document.createElement("div");
 div.className="message "+who;
 div.textContent=text;
 chat.appendChild(div);
 chat.scrollTop=chat.scrollHeight;
}

function speak(text){
 if(!("speechSynthesis" in window))return;
 speechSynthesis.cancel();
 const u=new SpeechSynthesisUtterance(text);
 u.lang="en-US";u.rate=1;u.pitch=1;speechSynthesis.speak(u);
}

async function loadHistory(){
 try{
  const r=await fetch("/history");
  const d=await r.json();
  historyBox.innerHTML="";
  (d.conversations||[]).forEach(c=>{
   const div=document.createElement("div");
   div.className="history-item"+(String(c.id)===String(currentChatId)?" active":"");
   div.textContent=c.title;
   div.onclick=()=>openChat(c.id);
   historyBox.appendChild(div);
  });
 }catch(e){}
}

async function openChat(id){
 currentChatId=id;
 chat.innerHTML="";
 setStatus("Loading chat...");
 try{
  const r=await fetch("/history/"+id);
  const d=await r.json();
  if(!r.ok)throw new Error(d.error||"Could not load chat.");
  (d.messages||[]).forEach(m=>addMessage(m.content,m.role==="user"?"user":"alpha"));
  setStatus("");
  await loadHistory();
 }catch(e){setStatus(e.message)}
}

async function newChat(){
 try{
  const r=await fetch("/new_chat",{method:"POST"});
  const d=await r.json();
  currentChatId=d.id;
  chat.innerHTML="";
  addMessage("Hello 👋 I'm Alpha. Ask me anything, or tap 🎤 and talk to me.","alpha");
  await loadHistory();
 }catch(e){alert("Could not create a new chat: "+e.message)}
}

async function sendMessage(textFromVoice=null){
 const text=(textFromVoice!==null?textFromVoice:messageInput.value).trim();
 if(!text)return;
 addMessage(text,"user");messageInput.value="";
 setStatus("Alpha is thinking...");sendButton.disabled=true;
 try{
  const response=await fetch("/chat",{
   method:"POST",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({message:text,conversation_id:currentChatId})
  });
  const raw=await response.text();let data;
  try{data=JSON.parse(raw)}catch(e){throw new Error("Server returned a non-JSON error (HTTP "+response.status+").")}
  if(!response.ok)throw new Error(data.error||"Chat request failed.");
  currentChatId=data.conversation_id;
  addMessage(data.reply,"alpha");speak(data.reply);
  setStatus("");await loadHistory();
 }catch(e){addMessage("Sorry, something went wrong: "+e.message,"alpha");setStatus("")}
 finally{sendButton.disabled=false;messageInput.focus()}
}


function toggleImagePanel(){
 const panel=document.getElementById("imagePanel");
 panel.style.display=(panel.style.display==="none"?"block":"none");
}

async function editImage(){
 const file=document.getElementById("imageFile").files[0];
 const prompt=document.getElementById("imagePrompt").value.trim();

 if(!prompt){
   alert("Tell Alpha what you want it to do with the image.");
   return;
 }

 setStatus(file ? "🎨 Editing your image..." : "🎨 Generating image...");
 const button=document.getElementById("editButton");
 button.disabled=true;

 try{
   const fd=new FormData();
   fd.append("prompt",prompt);
   if(file) fd.append("image",file);

   const r=await fetch("/image_edit",{method:"POST",body:fd});
   const d=await r.json();
   if(!r.ok) throw new Error(d.error||"Image operation failed.");

   addImageMessage(d.image_url);
   setStatus("✅ Image ready.");
   document.getElementById("imagePanel").style.display="none";
 }catch(e){
   alert("Image error: "+e.message);
   setStatus("");
 }finally{
   button.disabled=false;
 }
}

function addImageMessage(url){
 const div=document.createElement("div");
 div.className="message alpha";
 const img=document.createElement("img");
 img.src=url;
 img.style.maxWidth="100%";
 img.style.borderRadius="12px";
 img.style.display="block";
 div.appendChild(img);
 chat.appendChild(div);
 chat.scrollTop=chat.scrollHeight;
}

async function toggleRecording(){if(isRecording)stopRecording();else await startRecording()}

async function startRecording(){
 if(!navigator.mediaDevices?.getUserMedia){alert("Your browser does not support microphone recording.");return}
 try{
  const stream=await navigator.mediaDevices.getUserMedia({audio:true});
  let options={};
  if(MediaRecorder.isTypeSupported("audio/webm;codecs=opus"))options.mimeType="audio/webm;codecs=opus";
  else if(MediaRecorder.isTypeSupported("audio/webm"))options.mimeType="audio/webm";
  else if(MediaRecorder.isTypeSupported("audio/mp4"))options.mimeType="audio/mp4";
  mediaRecorder=new MediaRecorder(stream,options);
  recordingMimeType=mediaRecorder.mimeType||"audio/webm";audioChunks=[];
  mediaRecorder.ondataavailable=e=>{if(e.data?.size>0)audioChunks.push(e.data)};
  mediaRecorder.onerror=()=>setStatus("Microphone recording failed.");
  mediaRecorder.onstop=async()=>{
   stream.getTracks().forEach(t=>t.stop());
   const blob=new Blob(audioChunks,{type:recordingMimeType});audioChunks=[];
   if(!blob.size){setStatus("");alert("No audio was recorded.");return}
   await transcribeAudio(blob);
  };
  mediaRecorder.start();isRecording=true;
  micButton.classList.add("recording");micButton.textContent="⏹️";
  setStatus("🔴 Recording... tap again when you're done.");
 }catch(e){
  if(e.name==="NotAllowedError")alert("Microphone permission was denied. Allow microphone access for Alpha in Chrome settings.");
  else alert("Could not start microphone: "+e.message);
 }
}

function stopRecording(){
 if(!mediaRecorder||mediaRecorder.state==="inactive")return;
 isRecording=false;micButton.classList.remove("recording");micButton.textContent="🎤";
 setStatus("⏳ Preparing voice message...");mediaRecorder.stop();
}

async function transcribeAudio(blob){
 setStatus("🧠 Converting your voice to text...");micButton.disabled=true;
 try{
  const fd=new FormData();fd.append("audio",blob,blob.type.includes("mp4")?"voice.mp4":"voice.webm");
  const r=await fetch("/transcribe",{method:"POST",body:fd});
  const d=await r.json();if(!r.ok)throw new Error(d.error||"Transcription failed.");
  const text=(d.text||"").trim();if(!text)throw new Error("I could not detect any words.");
  messageInput.value=text;setStatus("✅ I heard: "+text);await sendMessage(text);
 }catch(e){setStatus("");alert("Voice error: "+e.message)}
 finally{micButton.disabled=false}
}

messageInput.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();sendMessage()}});

(async()=>{
 await loadHistory();
 const r=await fetch("/current_chat");
 const d=await r.json();
 if(d.id)await openChat(d.id);else await newChat();
})();
</script>
</body>
</html>
"""



@app.route("/")
def home():
    user_id = request.cookies.get("alpha_user_id") or str(uuid.uuid4())
    chat_id = request.cookies.get("alpha_chat_id")

    response = make_response(render_template_string(HTML))
    response.set_cookie("alpha_user_id", user_id, max_age=60*60*24*365, httponly=True, samesite="Lax")

    if not chat_id:
        chat_id = str(create_conversation(user_id))
    response.set_cookie("alpha_chat_id", chat_id, max_age=60*60*24*365, httponly=True, samesite="Lax")
    return response


@app.route("/current_chat")
def current_chat():
    user_id = request.cookies.get("alpha_user_id") or str(uuid.uuid4())
    chat_id = request.cookies.get("alpha_chat_id")

    if not chat_id:
        chat_id = str(create_conversation(user_id))

    return jsonify({"id": int(chat_id)})


@app.route("/new_chat", methods=["POST"])
def new_chat():
    user_id = request.cookies.get("alpha_user_id") or str(uuid.uuid4())
    chat_id = create_conversation(user_id)
    response = jsonify({"id": chat_id})
    response.set_cookie("alpha_chat_id", str(chat_id), max_age=60*60*24*365, httponly=True, samesite="Lax")
    return response


@app.route("/history")
def history():
    user_id = request.cookies.get("alpha_user_id") or str(uuid.uuid4())
    rows = get_conversations(user_id)
    return jsonify({
        "conversations": [
            {"id": r[0], "title": r[1], "created_at": r[2]} for r in rows
        ]
    })


@app.route("/history/<int:conversation_id>")
def history_chat(conversation_id):
    user_id = request.cookies.get("alpha_user_id") or str(uuid.uuid4())
    rows = get_messages(conversation_id, user_id)
    return jsonify({
        "messages": [
            {"role": r[0], "content": r[1], "created_at": r[2]} for r in rows
        ]
    })


@app.route("/transcribe", methods=["POST"])
def transcribe():
    groq_key = os.environ.get("GROQ_API_KEY")

    if not groq_key:
        return jsonify({
            "error": "GROQ_API_KEY is not set in Render Environment Variables."
        }), 500

    audio = request.files.get("audio")

    if not audio:
        return jsonify({"error": "No audio file was received."}), 400

    try:
        audio_bytes = audio.read()

        if not audio_bytes:
            return jsonify({"error": "The recorded audio was empty."}), 400

        filename = audio.filename or "voice.webm"
        content_type = audio.mimetype or "audio/webm"

        files = {
            "file": (filename, audio_bytes, content_type)
        }

        data = {
            "model": "whisper-large-v3-turbo",
            "response_format": "json"
        }

        result = requests.post(
            GROQ_TRANSCRIBE_URL,
            headers={
                "Authorization": f"Bearer {groq_key}"
            },
            files=files,
            data=data,
            timeout=60
        )

        if result.status_code != 200:
            try:
                error_data = result.json()
                error_message = error_data.get("error", {}).get("message", result.text)
            except Exception:
                error_message = result.text

            return jsonify({
                "error": f"Groq transcription error: {error_message}"
            }), 502

        result_data = result.json()
        text = result_data.get("text", "").strip()

        return jsonify({"text": text})

    except requests.Timeout:
        return jsonify({
            "error": "Voice transcription timed out. Please try again."
        }), 504

    except Exception as e:
        return jsonify({
            "error": f"Voice transcription failed: {str(e)}"
        }), 500



@app.route("/image_edit", methods=["POST"])
def image_edit():
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not openrouter_key:
        return jsonify({"error": "OPENROUTER_API_KEY is not set."}), 500

    prompt = str(request.form.get("prompt", "")).strip()
    image = request.files.get("image")

    if not prompt:
        return jsonify({"error": "Please describe what you want the image to do."}), 400

    try:
        body = {
            "model": "google/gemini-3.1-flash-image",
            "prompt": prompt
        }

        if image:
            raw = image.read()
            if not raw:
                return jsonify({"error": "The image file was empty."}), 400

            if len(raw) > 8 * 1024 * 1024:
                return jsonify({"error": "Please use an image smaller than 8 MB."}), 400

            import base64
            content_type = image.mimetype or "image/jpeg"
            encoded = base64.b64encode(raw).decode("utf-8")

            body["input_references"] = [{
                "type": "image_url",
                "image_url": {
                    "url": f"data:{content_type};base64,{encoded}"
                }
            }]

        result = requests.post(
            "https://openrouter.ai/api/v1/images",
            headers={
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json"
            },
            json=body,
            timeout=180
        )

        if not result.ok:
            try:
                err = result.json()
                detail = err.get("error", {}).get("message", result.text)
            except Exception:
                detail = result.text[:1000]

            return jsonify({
                "error": f"Image API error (HTTP {result.status_code}): {detail}"
            }), 502

        result_data = result.json()
        images = result_data.get("data") or []

        if not images or not images[0].get("b64_json"):
            return jsonify({"error": "The image service returned no image."}), 502

        media_type = images[0].get("media_type") or "image/png"
        image_url = f"data:{media_type};base64,{images[0]['b64_json']}"

        return jsonify({
            "image_url": image_url,
            "cost": (result_data.get("usage") or {}).get("cost")
        })

    except requests.Timeout:
        return jsonify({"error": "Image generation timed out. Please try again."}), 504
    except Exception as e:
        return jsonify({"error": f"Image operation failed: {str(e)}"}), 500

@app.route("/chat", methods=["POST"])
def chat():
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not openrouter_key:
        return jsonify({"error": "OPENROUTER_API_KEY is not set."}), 500

    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    if not message:
        return jsonify({"error": "Message is empty."}), 400

    user_id = request.cookies.get("alpha_user_id") or str(uuid.uuid4())
    conversation_id = data.get("conversation_id") or request.cookies.get("alpha_chat_id")

    try:
        conversation_id = int(conversation_id) if conversation_id else None
    except Exception:
        conversation_id = None

    if not conversation_id:
        conversation_id = create_conversation(user_id, message[:80])
    else:
        existing = get_messages(conversation_id, user_id)
        if not existing:
            conn = sqlite3.connect(DB_FILE)
            row = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id)
            ).fetchone()
            conn.close()
            if not row:
                conversation_id = create_conversation(user_id, message[:80])

    lower_message = message.lower()

    if lower_message.startswith("remember that "):
        memory = message[len("remember that "):].strip()
        if memory:
            save_memory(user_id, memory)
        save_message(conversation_id, "user", message)
        reply = "Got it. I'll remember that for this browser."
        save_message(conversation_id, "assistant", reply)
        response = jsonify({"reply": reply, "conversation_id": conversation_id})
        response.set_cookie("alpha_chat_id", str(conversation_id), max_age=60*60*24*365, httponly=True, samesite="Lax")
        return response

    memories = get_memories(user_id)
    system_prompt = """You are Alpha, a friendly personal AI assistant.
You are used by multiple people.
Each person has separate private memories.
Never reveal one person's memories to another person.
Be helpful, natural, and concise.
When current information is needed, use the web search tool and clearly ground factual claims in the sources you find.
"""

    if memories:
        system_prompt += "\nPrivate memories for this user:\n" + "".join(f"- {m}\n" for m in memories)

    previous = get_messages(conversation_id, user_id)
    messages = [{"role": "system", "content": system_prompt}]
    for role, content, _ in previous[-20:]:
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    payload = {
        "model": "openrouter/free:online",
        "messages": messages
    }

    try:
        result = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=90
        )

        if result.status_code != 200:
            try:
                upstream = result.json()
                detail = upstream.get("error", {}).get("message", result.text)
            except Exception:
                detail = result.text[:1000]
            return jsonify({"error": f"OpenRouter error (HTTP {result.status_code}): {detail}"}), 502

        result_data = result.json()
        reply = result_data["choices"][0]["message"]["content"]

        save_message(conversation_id, "user", message)
        save_message(conversation_id, "assistant", reply)

        # Give the conversation a useful title from the first user message.
        if len(previous) == 0:
            rename_conversation(conversation_id, user_id, message[:80])

        response = jsonify({
            "reply": reply,
            "conversation_id": conversation_id
        })
        response.set_cookie("alpha_chat_id", str(conversation_id), max_age=60*60*24*365, httponly=True, samesite="Lax")
        return response

    except requests.Timeout:
        return jsonify({"error": "The AI request timed out. Please try again."}), 504
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach OpenRouter: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"AI request failed: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
