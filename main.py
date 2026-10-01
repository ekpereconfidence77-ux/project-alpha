import os
import uuid
import sqlite3
import base64
import io
import requests
from PIL import Image
from flask import Flask, request, jsonify, make_response, render_template_string

app = Flask(__name__)

DB_FILE = "alpha_memory.db"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

CHAT_MODEL = "openai/gpt-5.6-luna"


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
        """
        SELECT id, title, created_at
        FROM conversations
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,)
    ).fetchall()

    conn.close()
    return rows


def get_messages(conversation_id, user_id):
    conn = sqlite3.connect(DB_FILE)

    rows = conn.execute(
        """
        SELECT m.role, m.content, m.created_at
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        WHERE m.conversation_id = ? AND c.user_id = ?
        ORDER BY m.id ASC
        """,
        (conversation_id, user_id)
    ).fetchall()

    conn.close()
    return rows


def save_message(conversation_id, role, content):
    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        INSERT INTO messages (conversation_id, role, content)
        VALUES (?, ?, ?)
        """,
        (conversation_id, role, content)
    )

    conn.commit()
    conn.close()


def rename_conversation(conversation_id, user_id, title):
    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        UPDATE conversations
        SET title = ?
        WHERE id = ? AND user_id = ?
        """,
        (title[:80] or "New chat", conversation_id, user_id)
    )

    conn.commit()
    conn.close()


def conversation_belongs_to_user(conversation_id, user_id):
    conn = sqlite3.connect(DB_FILE)

    row = conn.execute(
        """
        SELECT id
        FROM conversations
        WHERE id = ? AND user_id = ?
        """,
        (conversation_id, user_id)
    ).fetchone()

    conn.close()
    return row is not None


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

body{
    margin:0;
    background:#11151b;
    color:#fff;
    font-family:Arial,sans-serif;
    height:100vh;
    display:flex
}

#sidebar{
    width:260px;
    background:#181d25;
    border-right:1px solid #2a303a;
    padding:12px;
    display:flex;
    flex-direction:column
}

#newChat{
    width:100%;
    padding:12px;
    border:0;
    border-radius:10px;
    background:#2b6cff;
    color:#fff;
    font-size:15px;
    margin-bottom:12px
}

#history{
    overflow-y:auto;
    flex:1
}

.history-item{
    padding:11px;
    border-radius:9px;
    margin-bottom:5px;
    color:#ddd;
    cursor:pointer;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis
}

.history-item:hover,
.history-item.active{
    background:#29303a
}

#main{
    flex:1;
    display:flex;
    flex-direction:column;
    min-width:0
}

header{
    padding:16px 18px;
    background:#181d25;
    border-bottom:1px solid #2a303a;
    font-size:20px;
    font-weight:700
}

#chat{
    flex:1;
    overflow-y:auto;
    padding:18px;
    display:flex;
    flex-direction:column;
    gap:12px
}

.message{
    max-width:85%;
    padding:12px 14px;
    border-radius:16px;
    line-height:1.45;
    white-space:pre-wrap;
    word-wrap:break-word
}

.user{
    align-self:flex-end;
    background:#2b6cff
}

.alpha{
    align-self:flex-start;
    background:#242a33
}

.status{
    text-align:center;
    color:#aab2bf;
    font-size:13px;
    min-height:18px;
    padding:0 12px 6px
}

.composer{
    display:flex;
    gap:8px;
    padding:10px;
    background:#181d25;
    border-top:1px solid #2a303a
}

input{
    flex:1;
    min-width:0;
    border:1px solid #343b46;
    background:#222832;
    color:#fff;
    border-radius:12px;
    padding:13px 14px;
    outline:none;
    font-size:16px
}

button{
    border:none;
    border-radius:12px;
    color:#fff;
    font-size:18px;
    min-width:48px;
    padding:0 14px;
    cursor:pointer
}

#micButton{
    background:#303641
}

#micButton.recording{
    background:#d22;
    animation:pulse 1s infinite
}

#sendButton{
    background:#2b6cff
}

button:disabled{
    opacity:.55;
    cursor:not-allowed
}

#imagePanel{
    display:none;
    padding:10px;
    background:#181d25;
    border-top:1px solid #2a303a
}

#imageFile{
    width:100%;
    margin-bottom:8px
}

#imagePreview{
    display:flex;
    gap:8px;
    overflow-x:auto;
    margin-bottom:8px
}

#imagePrompt{
    width:100%;
    margin-bottom:8px
}

#editButton{
    background:#7b3cff;
    width:100%;
    padding:12px
}

.image-result{
    max-width:100%;
    border-radius:12px;
    display:block
}

.download-image{
    display:inline-block;
    margin-top:8px;
    padding:9px 12px;
    border-radius:9px;
    background:#2b6cff;
    color:white;
    text-decoration:none
}

@keyframes pulse{
    0%{transform:scale(1)}
    50%{transform:scale(1.06)}
    100%{transform:scale(1)}
}

@media(max-width:700px){
    #sidebar{display:none}
    .message{max-width:92%}
}
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

<div id="imagePanel">

    <input
        id="imageFile"
        type="file"
        accept="image/*"
        multiple
    >

    <div id="imagePreview"></div>

    <div style="font-size:13px;opacity:.75;margin:6px 0">
        Select several photos to use together as references for ONE final image.
    </div>

    <div
        id="imageCount"
        style="font-size:13px;opacity:.8;margin-bottom:8px"
    >
        No photos selected
    </div>

    <input
        id="imagePrompt"
        placeholder="Tell Alpha how to edit the selected photos..."
    >

    <button
        id="editButton"
        onclick="editImage()"
    >
        🎨 Edit Selected Photos
    </button>

</div>

<div class="composer">

    <button
        id="imageButton"
        onclick="toggleImagePanel()"
    >
        🖼️
    </button>

    <button
        id="micButton"
        onclick="toggleRecording()"
    >
        🎤
    </button>

    <input
        id="message"
        placeholder="Talk to Alpha..."
        autocomplete="off"
    >

    <button
        id="sendButton"
        onclick="sendMessage()"
    >
        ➤
    </button>

</div>

</section>

<script>

let mediaRecorder=null;
let audioChunks=[];
let isRecording=false;
let recordingMimeType="";
let currentChatId=null;

const chat=document.getElementById("chat");
const historyBox=document.getElementById("history");
const messageInput=document.getElementById("message");
const micButton=document.getElementById("micButton");
const sendButton=document.getElementById("sendButton");
const statusBox=document.getElementById("status");

function setStatus(t){
    statusBox.textContent=t||"";
}

function addMessage(text,who){
    const div=document.createElement("div");
    div.className="message "+who;
    div.textContent=text;
    chat.appendChild(div);
    chat.scrollTop=chat.scrollHeight;
}

function speak(text){
    if(!("speechSynthesis" in window)) return;

    speechSynthesis.cancel();

    const u=new SpeechSynthesisUtterance(text);
    u.lang="en-US";
    u.rate=1;
    u.pitch=1;

    speechSynthesis.speak(u);
}

async function loadHistory(){
    try{
        const r=await fetch("/history");
        const d=await r.json();

        historyBox.innerHTML="";

        (d.conversations||[]).forEach(c=>{
            const div=document.createElement("div");

            div.className=
                "history-item"+
                (String(c.id)===String(currentChatId)?" active":"");

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

        if(!r.ok){
            throw new Error(d.error||"Could not load chat.");
        }

        (d.messages||[]).forEach(m=>{
            addMessage(
                m.content,
                m.role==="user"?"user":"alpha"
            );
        });

        setStatus("");

        await loadHistory();

    }catch(e){
        setStatus(e.message);
    }
}

async function newChat(){
    try{
        const r=await fetch("/new_chat",{
            method:"POST"
        });

        const d=await r.json();

        currentChatId=d.id;
        chat.innerHTML="";

        addMessage(
            "Hello 👋 I'm Alpha. Ask me anything, or tap 🎤 and talk to me.",
            "alpha"
        );

        await loadHistory();

    }catch(e){
        alert("Could not create a new chat: "+e.message);
    }
}

async function sendMessage(textFromVoice=null){

    const text=(
        textFromVoice!==null
        ?textFromVoice
        :messageInput.value
    ).trim();

    if(!text) return;

    addMessage(text,"user");

    messageInput.value="";

    setStatus("Alpha is thinking...");
    sendButton.disabled=true;

    try{

        const response=await fetch("/chat",{
            method:"POST",
            headers:{
                "Content-Type":"application/json"
            },
            body:JSON.stringify({
                message:text,
                conversation_id:currentChatId
            })
        });

        const raw=await response.text();

        let data;

        try{
            data=JSON.parse(raw);
        }catch(e){
            throw new Error(
                "Server returned a non-JSON error (HTTP "+
                response.status+")."
            );
        }

        if(!response.ok){
            throw new Error(
                data.error||"Chat request failed."
            );
        }

        currentChatId=data.conversation_id;

        addMessage(data.reply,"alpha");

        speak(data.reply);

        setStatus("");

        await loadHistory();

    }catch(e){

        addMessage(
            "Sorry, something went wrong: "+e.message,
            "alpha"
        );

        setStatus("");

    }finally{

        sendButton.disabled=false;
        messageInput.focus();
    }
}


function toggleImagePanel(){

    const panel=document.getElementById("imagePanel");

    panel.style.display=
        panel.style.display==="none"
        ?"block"
        :"none";
}


async function editImage(){

    const files=Array.from(
        document.getElementById("imageFile").files||[]
    );

    const prompt=document
        .getElementById("imagePrompt")
        .value
        .trim();

    if(!files.length){
        alert("Select one or more photos first.");
        return;
    }

    if(files.length>4){
        alert("Please select up to 4 photos at a time.");
        return;
    }

    if(!prompt){
        alert(
            "Tell Alpha what you want it to do with the selected photos."
        );
        return;
    }

    const totalBytes=files.reduce(
        (sum,f)=>sum+f.size,
        0
    );

    if(totalBytes>30*1024*1024){
        alert(
            "The selected photos are too large together. "+
            "Please keep the total under 30 MB."
        );
        return;
    }

    setStatus(
        `🎨 Combining ${files.length} reference photo`+
        `${files.length===1?"":"s"} into one image...`
    );

    const button=document.getElementById("editButton");
    button.disabled=true;

    try{

        const fd=new FormData();

        fd.append("prompt",prompt);

        files.forEach(file=>{
            fd.append("images",file);
        });

        const r=await fetch(
            "/image_edit",
            {
                method:"POST",
                body:fd
            }
        );

        const raw=await r.text();

        let d;

        try{
            d=JSON.parse(raw);
        }catch(e){
            throw new Error(
                "Alpha server returned HTML instead of JSON "+
                "(HTTP "+r.status+"). Refresh and try again."
            );
        }

        if(!r.ok){
            throw new Error(
                d.error||"Image operation failed."
            );
        }

        if(d.image_url){
            addImageMessage(
                d.image_url,
                "🎨 Final image"
            );
        }

        setStatus(
            "✅ One final image created from your references."
        );

    }catch(e){

        alert(
            "Image error: "+e.message
        );

        setStatus("");

    }finally{

        button.disabled=false;
    }
}


function updateImagePreview(){

    const files=Array.from(
        document.getElementById("imageFile").files||[]
    );

    const preview=document.getElementById("imagePreview");
    const count=document.getElementById("imageCount");

    preview.innerHTML="";

    count.textContent=
        files.length
        ?`${files.length} photo${files.length===1?"":"s"} selected`
        :"No photos selected";

    files.forEach(file=>{

        const wrap=document.createElement("div");

        wrap.style.minWidth="76px";

        const img=document.createElement("img");

        img.src=URL.createObjectURL(file);

        img.style.width="72px";
        img.style.height="72px";
        img.style.objectFit="cover";
        img.style.borderRadius="8px";

        wrap.appendChild(img);

        preview.appendChild(wrap);
    });
}


document
    .getElementById("imageFile")
    .addEventListener(
        "change",
        updateImagePreview
    );


function addImageMessage(
    url,
    label="🎨 Image"
){

    const div=document.createElement("div");

    div.className="message alpha";

    const title=document.createElement("div");

    title.textContent=label;
    title.style.marginBottom="6px";

    const img=document.createElement("img");

    img.src=url;
    img.className="image-result";

    const download=document.createElement("a");

    download.href=url;
    download.download="alpha-edited-image.png";
    download.textContent="⬇️ Save image";
    download.className="download-image";

    div.appendChild(title);
    div.appendChild(img);
    div.appendChild(download);

    chat.appendChild(div);

    chat.scrollTop=chat.scrollHeight;
}


async function toggleRecording(){

    if(isRecording){
        stopRecording();
    }else{
        await startRecording();
    }
}


async function startRecording(){

    if(!navigator.mediaDevices?.getUserMedia){

        alert(
            "Your browser does not support microphone recording."
        );

        return;
    }

    try{

        const stream=
            await navigator.mediaDevices.getUserMedia({
                audio:true
            });

        let options={};

        if(
            MediaRecorder.isTypeSupported(
                "audio/webm;codecs=opus"
            )
        ){
            options.mimeType=
                "audio/webm;codecs=opus";

        }else if(
            MediaRecorder.isTypeSupported(
                "audio/webm"
            )
        ){
            options.mimeType="audio/webm";

        }else if(
            MediaRecorder.isTypeSupported(
                "audio/mp4"
            )
        ){
            options.mimeType="audio/mp4";
        }

        mediaRecorder=
            new MediaRecorder(
                stream,
                options
            );

        recordingMimeType=
            mediaRecorder.mimeType||"audio/webm";

        audioChunks=[];

        mediaRecorder.ondataavailable=e=>{
            if(e.data?.size>0){
                audioChunks.push(e.data);
            }
        };

        mediaRecorder.onerror=()=>{
            setStatus(
                "Microphone recording failed."
            );
        };

        mediaRecorder.onstop=async()=>{

            stream
                .getTracks()
                .forEach(t=>t.stop());

            const blob=new Blob(
                audioChunks,
                {type:recordingMimeType}
            );

            audioChunks=[];

            if(!blob.size){

                setStatus("");

                alert(
                    "No audio was recorded."
                );

                return;
            }

            await transcribeAudio(blob);
        };

        mediaRecorder.start();

        isRecording=true;

        micButton.classList.add(
            "recording"
        );

        micButton.textContent="⏹️";

        setStatus(
            "🔴 Recording... tap again when you're done."
        );

    }catch(e){

        if(e.name==="NotAllowedError"){

            alert(
                "Microphone permission was denied. "+
                "Allow microphone access for Alpha in Chrome settings."
            );

        }else{

            alert(
                "Could not start microphone: "+
                e.message
            );
        }
    }
}


function stopRecording(){

    if(
        !mediaRecorder||
        mediaRecorder.state==="inactive"
    ){
        return;
    }

    isRecording=false;

    micButton.classList.remove(
        "recording"
    );

    micButton.textContent="🎤";

    setStatus(
        "⏳ Preparing voice message..."
    );

    mediaRecorder.stop();
}


async function transcribeAudio(blob){

    setStatus(
        "🧠 Converting your voice to text..."
    );

    micButton.disabled=true;

    try{

        const fd=new FormData();

        fd.append(
            "audio",
            blob,
            blob.type.includes("mp4")
            ?"voice.mp4"
            :"voice.webm"
        );

        const r=await fetch(
            "/transcribe",
            {
                method:"POST",
                body:fd
            }
        );

        const d=await r.json();

        if(!r.ok){
            throw new Error(
                d.error||"Transcription failed."
            );
        }

        const text=(d.text||"").trim();

        if(!text){
            throw new Error(
                "I could not detect any words."
            );
        }

        messageInput.value=text;

        setStatus(
            "✅ I heard: "+text
        );

        await sendMessage(text);

    }catch(e){

        setStatus("");

        alert(
            "Voice error: "+e.message
        );

    }finally{

        micButton.disabled=false;
    }
}


messageInput.addEventListener(
    "keydown",
    e=>{
        if(e.key==="Enter"){
            e.preventDefault();
            sendMessage();
        }
    }
);


(async()=>{

    await loadHistory();

    const r=await fetch(
        "/current_chat"
    );

    const d=await r.json();

    if(d.id){
        await openChat(d.id);
    }else{
        await newChat();
    }

})();
</script>

</body>
</html>
"""


@app.route("/")
def home():

    user_id = (
        request.cookies.get("alpha_user_id")
        or str(uuid.uuid4())
    )

    chat_id = request.cookies.get("alpha_chat_id")

    if not chat_id:
        chat_id = str(
            create_conversation(user_id)
        )

    response = make_response(
        render_template_string(HTML)
    )

    response.set_cookie(
        "alpha_user_id",
        user_id,
        max_age=60*60*24*365,
        httponly=True,
        samesite="Lax"
    )

    response.set_cookie(
        "alpha_chat_id",
        chat_id,
        max_age=60*60*24*365,
        httponly=True,
        samesite="Lax"
    )

    return response


@app.route("/current_chat")
def current_chat():

    user_id = (
        request.cookies.get("alpha_user_id")
        or str(uuid.uuid4())
    )

    chat_id = request.cookies.get(
        "alpha_chat_id"
    )

    if not chat_id:
        chat_id = str(
            create_conversation(user_id)
        )

    return jsonify({
        "id": int(chat_id)
    })


@app.route("/new_chat", methods=["POST"])
def new_chat():

    user_id = (
        request.cookies.get("alpha_user_id")
        or str(uuid.uuid4())
    )

    chat_id=create_conversation(user_id)

    response=jsonify({
        "id":chat_id
    })

    response.set_cookie(
        "alpha_chat_id",
        str(chat_id),
        max_age=60*60*24*365,
        httponly=True,
        samesite="Lax"
    )

    return response


@app.route("/history")
def history():

    user_id = (
        request.cookies.get("alpha_user_id")
        or str(uuid.uuid4())
    )

    rows=get_conversations(user_id)

    return jsonify({
        "conversations":[
            {
                "id":r[0],
                "title":r[1],
                "created_at":r[2]
            }
            for r in rows
        ]
    })


@app.route("/history/<int:conversation_id>")
def history_chat(conversation_id):

    user_id = (
        request.cookies.get("alpha_user_id")
        or str(uuid.uuid4())
    )

    rows=get_messages(
        conversation_id,
        user_id
    )

    return jsonify({
        "messages":[
            {
                "role":r[0],
                "content":r[1],
                "created_at":r[2]
            }
            for r in rows
        ]
    })


@app.route("/chat", methods=["POST"])
def chat():

    api_key=os.environ.get(
        "OPENROUTER_API_KEY"
    )

    if not api_key:
        return jsonify({
            "error":
            "OPENROUTER_API_KEY is not set in Render Environment Variables."
        }),500

    data=request.get_json(
        silent=True
    ) or {}

    user_message=str(
        data.get("message","")
    ).strip()

    if not user_message:
        return jsonify({
            "error":"Message cannot be empty."
        }),400

    user_id=(
        request.cookies.get("alpha_user_id")
        or str(uuid.uuid4())
    )

    conversation_id=data.get(
        "conversation_id"
    )

    try:
        conversation_id=(
            int(conversation_id)
            if conversation_id
            else None
        )
    except Exception:
        conversation_id=None

    if not conversation_id or not conversation_belongs_to_user(
        conversation_id,
        user_id
    ):
        conversation_id=create_conversation(
            user_id,
            user_message[:80]
        )

    # Save a memory when the user explicitly asks Alpha to remember something.
    lower=user_message.lower()

    memory_prefixes=[
        "remember that ",
        "remember this: ",
        "remember this ",
        "please remember that "
    ]

    for prefix in memory_prefixes:
        if lower.startswith(prefix):
            memory_text=user_message[len(prefix):].strip()

            if memory_text:
                save_memory(
                    user_id,
                    memory_text
                )

            break

    save_message(
        conversation_id,
        "user",
        user_message
    )

    rows=get_messages(
        conversation_id,
        user_id
    )

    memories=get_memories(
        user_id
    )

    system_prompt="""
You are Alpha, a helpful personal AI assistant.

Be friendly, clear, practical and honest.

Help the user with normal questions, coding, writing,
ideas, planning, learning and creative tasks.

Do not claim that you completed an action that you did not actually complete.

When the user asks for image editing, help them write precise
image-editing instructions, but the actual image generation
is handled by Alpha's image tool in the interface.

Keep answers reasonably concise unless the user asks for detail.
""".strip()

    if memories:

        memory_text="\n".join(
            "- "+m
            for m in memories[-30:]
        )

        system_prompt += (
            "\n\nUser memories saved by the user:\n"
            + memory_text
        )

    messages=[
        {
            "role":"system",
            "content":system_prompt
        }
    ]

    # Keep the conversation request reasonably sized.
    for role,content,_created_at in rows[-40:]:

        if role not in ("user","assistant"):
            continue

        messages.append({
            "role":role,
            "content":content
        })

    try:

        result=requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization":
                f"Bearer {api_key}",
                "Content-Type":
                "application/json",
                "HTTP-Referer":
                request.host_url.rstrip("/"),
                "X-Title":
                "Project Alpha"
            },
            json={
                "model":CHAT_MODEL,
                "messages":messages,
                "temperature":0.7
            },
            timeout=120
        )

        if not result.ok:

            try:
                error_data=result.json()

                error_message=(
                    error_data
                    .get("error",{})
                    .get(
                        "message",
                        result.text
                    )
                )

            except Exception:
                error_message=result.text

            return jsonify({
                "error":
                f"OpenRouter chat error ({result.status_code}): "
                f"{error_message}"
            }),502

        result_data=result.json()

        choices=result_data.get(
            "choices",
            []
        )

        if not choices:

            return jsonify({
                "error":
                "OpenRouter returned no chat response."
            }),502

        reply=(
            choices[0]
            .get("message",{})
            .get("content","")
        )

        if isinstance(reply,list):

            reply="".join(
                str(part.get("text",""))
                if isinstance(part,dict)
                else str(part)
                for part in reply
            )

        reply=str(reply).strip()

        if not reply:
            return jsonify({
                "error":
                "Alpha received an empty response."
            }),502

        save_message(
            conversation_id,
            "assistant",
            reply
        )

        # Give a new conversation a useful title.
        rows_after=get_messages(
            conversation_id,
            user_id
        )

        if len(rows_after)<=2:
            rename_conversation(
                conversation_id,
                user_id,
                user_message[:80]
            )

        response=jsonify({
            "reply":reply,
            "conversation_id":conversation_id
        })

        response.set_cookie(
            "alpha_chat_id",
            str(conversation_id),
            max_age=60*60*24*365,
            httponly=True,
            samesite="Lax"
        )

        return response

    except requests.Timeout:

        return jsonify({
            "error":
            "Alpha timed out while contacting the AI. Please try again."
        }),504

    except requests.RequestException as e:

        return jsonify({
            "error":
            f"Could not contact OpenRouter: {str(e)}"
        }),502

    except Exception as e:

        return jsonify({
            "error":
            f"Chat failed: {str(e)}"
        }),500


@app.route("/transcribe", methods=["POST"])
def transcribe():

    groq_key=os.environ.get(
        "GROQ_API_KEY"
    )

    if not groq_key:

        return jsonify({
            "error":
            "GROQ_API_KEY is not set in Render Environment Variables."
        }),500

    audio=request.files.get(
        "audio"
    )

    if not audio:

        return jsonify({
            "error":
            "No audio file was received."
        }),400

    try:

        audio_bytes=audio.read()

        if not audio_bytes:

            return jsonify({
                "error":
                "The recorded audio was empty."
            }),400

        filename=(
            audio.filename
            or "voice.webm"
        )

        content_type=(
            audio.mimetype
            or "audio/webm"
        )

        files={
            "file":(
                filename,
                audio_bytes,
                content_type
            )
        }

        data={
            "model":
            "whisper-large-v3-turbo",
            "response_format":
            "json"
        }

        result=requests.post(
            GROQ_TRANSCRIBE_URL,
            headers={
                "Authorization":
                f"Bearer {groq_key}"
            },
            files=files,
            data=data,
            timeout=60
        )

        if result.status_code!=200:

            try:

                error_data=result.json()

                error_message=(
                    error_data
                    .get("error",{})
                    .get(
                        "message",
                        result.text
                    )
                )

            except Exception:

                error_message=result.text

            return jsonify({
                "error":
                f"Groq transcription error: {error_message}"
            }),502

        result_data=result.json()

        text=result_data.get(
            "text",
            ""
        ).strip()

        return jsonify({
            "text":text
        })

    except requests.Timeout:

        return jsonify({
            "error":
            "Voice transcription timed out. Please try again."
        }),504

    except Exception as e:

        return jsonify({
            "error":
            f"Voice transcription failed: {str(e)}"
        }),500


@app.route("/image_edit", methods=["POST"])
def image_edit():
    """
    Alpha image editor using Cloudflare Workers AI FLUX.2 Klein 4B.

    Cloudflare supports up to 4 reference images for this model.
    Reference images are resized to fit Cloudflare's <512x512 input limit.
    """

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")

    if not account_id or not api_token:
        return jsonify({
            "error": (
                "Cloudflare is not configured. Add CLOUDFLARE_ACCOUNT_ID "
                "and CLOUDFLARE_API_TOKEN to Render Environment Variables."
            )
        }), 500

    prompt = str(request.form.get("prompt", "")).strip()
    images = [
        f for f in request.files.getlist("images")
        if f and f.filename
    ]

    if not prompt:
        return jsonify({
            "error": "Please describe the final image you want Alpha to create."
        }), 400

    # FLUX.2 Klein 4B supports a maximum of 4 reference images.
    if len(images) > 4:
        return jsonify({
            "error": "Cloudflare FLUX.2 supports up to 4 reference photos."
        }), 400

    if not images:
        return jsonify({
            "error": "Please select at least one reference photo."
        }), 400

    try:
        multipart_files = {}

        for index, image in enumerate(images):
            raw = image.read()
            if not raw:
                continue

            # Cloudflare requires each reference image to be smaller than
            # 512x512. Resize while preserving the original aspect ratio.
            try:
                source = Image.open(io.BytesIO(raw)).convert("RGB")
                source.thumbnail((511, 511), Image.Resampling.LANCZOS)

                output = io.BytesIO()
                source.save(output, format="JPEG", quality=92, optimize=True)
                image_bytes = output.getvalue()
            except Exception as exc:
                return jsonify({
                    "error": f"Could not process reference photo {index + 1}: {exc}"
                }), 400

            multipart_files[f"input_image_{index}"] = (
                f"reference_{index + 1}.jpg",
                image_bytes,
                "image/jpeg"
            )

        if not multipart_files:
            return jsonify({
                "error": "The uploaded images could not be read."
            }), 400

        final_prompt = f"""
Create ONE final photorealistic image using ALL supplied reference images.

USER INSTRUCTION:
{prompt}

IMPORTANT:
- Use all supplied reference images as visual references.
- Follow the user's requested composition exactly.
- If a reference provides a person's face, preserve that person's recognizable facial identity.
- Do not unnecessarily change facial structure, eyes, eyebrows, nose, lips, jawline, complexion, skin texture, or natural proportions.
- If a reference provides clothing, pose, background, lighting, camera angle, lens look, or photography style, use those elements when requested.
- Combine the useful elements into ONE coherent photograph.
- Keep the result photorealistic and professionally photographed.
- Preserve realistic skin texture, natural asymmetry, shadows, highlights, perspective, and proportions.
- Avoid plastic-looking skin, excessive beauty filters, distorted faces, extra fingers, malformed hands, duplicate features, warped objects, halos, seams, or obvious compositing artifacts.
- Do not create a collage.
- Do not place the reference images side by side.
- Do not output multiple images.
- Produce ONE finished final image.
""".strip()

        form_data = {
            "prompt": final_prompt,
            "width": "1024",
            "height": "1024"
        }

        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/ai/run/@cf/black-forest-labs/flux-2-klein-4b"
        )

        result = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_token}"
            },
            data=form_data,
            files=multipart_files,
            timeout=240
        )

        if not result.ok:
            try:
                error_data = result.json()
                errors = error_data.get("errors") or []
                messages = error_data.get("messages") or []
                detail = (
                    errors[0].get("message")
                    if errors and isinstance(errors[0], dict)
                    else None
                ) or (
                    messages[0].get("message")
                    if messages and isinstance(messages[0], dict)
                    else None
                ) or result.text
            except Exception:
                detail = result.text

            return jsonify({
                "error": f"Cloudflare image error ({result.status_code}): {detail}"
            }), 502

        # Workers AI returns JSON containing result.image as base64 for this model.
        content_type = result.headers.get("Content-Type", "")
        b64_image = None

        if "application/json" in content_type:
            result_data = result.json()
            model_result = result_data.get("result") or {}
            b64_image = model_result.get("image")

            # Be tolerant of a future response wrapper.
            if not b64_image:
                b64_image = result_data.get("image")

        if b64_image:
            if b64_image.startswith("data:image/"):
                image_url = b64_image
            else:
                image_url = f"data:image/jpeg;base64,{b64_image}"
        else:
            # Fallback in case the API returns the generated image as raw bytes.
            raw_output = result.content
            if not raw_output:
                return jsonify({
                    "error": "Cloudflare returned no generated image."
                }), 502

            encoded = base64.b64encode(raw_output).decode("utf-8")
            media_type = content_type.split(";")[0] or "image/jpeg"
            image_url = f"data:{media_type};base64,{encoded}"

        return jsonify({
            "success": True,
            "image_url": image_url,
            "model": "@cf/black-forest-labs/flux-2-klein-4b",
            "reference_count": len(multipart_files)
        })

    except requests.Timeout:
        return jsonify({
            "error": "Image generation timed out. Please try again."
        }), 504

    except requests.RequestException as exc:
        return jsonify({
            "error": f"Could not contact Cloudflare: {exc}"
        }), 502

    except Exception as exc:
        return jsonify({
            "error": f"Image generation failed: {exc}"
        }), 500


if __name__=="__main__":

    port=int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
