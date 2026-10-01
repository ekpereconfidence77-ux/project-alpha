import os
import uuid
import sqlite3
import base64
import io
import requests
from PIL import Image
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, request, jsonify, make_response, render_template_string, redirect, session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "dax-local-session-key-change-this-in-render"
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)

DB_FILE = "alpha_memory.db"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

CHAT_MODEL = "openrouter/free"


def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

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


def get_current_user_id():
    value = session.get("user_id")
    return str(value) if value is not None else None


def login_required_page(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not get_current_user_id():
            return redirect("/login")
        return fn(*args, **kwargs)
    return wrapper


def login_required_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user_id = get_current_user_id()
        if not user_id:
            return jsonify({"error": "Please log in to use Dax."}), 401
        return fn(*args, **kwargs)
    return wrapper


LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Dax — Sign in</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:#11151b;color:#fff;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;padding:20px}
.card{width:100%;max-width:420px;background:#181d25;border:1px solid #2a303a;border-radius:18px;padding:28px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.logo{font-size:28px;font-weight:700;text-align:center;margin-bottom:8px}
.sub{text-align:center;color:#aab2bf;margin-bottom:24px}
.tabs{display:flex;gap:6px;background:#11151b;border-radius:12px;padding:4px;margin-bottom:18px}
.tabs button{flex:1;border:0;border-radius:9px;padding:10px;background:transparent;color:#aab2bf;cursor:pointer;font-size:14px}
.tabs button.active{background:#29303a;color:#fff}
label{display:block;font-size:13px;color:#cbd1d9;margin:0 0 6px}
input{width:100%;padding:13px 14px;border:1px solid #343b46;border-radius:12px;background:#222832;color:#fff;outline:none;font-size:16px;margin-bottom:14px}
button.primary{width:100%;border:0;border-radius:12px;padding:13px;background:#fff;color:#11151b;font-weight:700;font-size:15px;cursor:pointer}
.error{min-height:20px;color:#ff8f8f;font-size:13px;margin:4px 0 12px;text-align:center}
.note{font-size:12px;color:#7f8997;text-align:center;margin-top:18px;line-height:1.5}
</style>
</head>
<body>
<div class="card">
<div class="logo">Dax</div>
<div class="sub">Your personal AI assistant</div>
<div class="tabs">
<button id="loginTab" class="active" onclick="showMode('login')">Log in</button>
<button id="registerTab" onclick="showMode('register')">Create account</button>
</div>
<form onsubmit="submitAuth(event)">
<label for="email">Email</label>
<input id="email" type="email" autocomplete="email" required placeholder="you@example.com">
<label for="password">Password</label>
<input id="password" type="password" autocomplete="current-password" required placeholder="At least 8 characters">
<div id="error" class="error"></div>
<button id="submit" class="primary" type="submit">Log in</button>
</form>
<div class="note">Use an email address and password to keep your Dax chats and memories connected to your account.</div>
</div>
<script>
let mode='login';
function showMode(next){
 mode=next;
 document.getElementById('loginTab').classList.toggle('active',mode==='login');
 document.getElementById('registerTab').classList.toggle('active',mode==='register');
 document.getElementById('submit').textContent=mode==='login'?'Log in':'Create account';
 document.getElementById('password').autocomplete=mode==='login'?'current-password':'new-password';
 document.getElementById('error').textContent='';
}
async function submitAuth(e){
 e.preventDefault();
 const button=document.getElementById('submit');
 const error=document.getElementById('error');
 error.textContent=''; button.disabled=true;
 try{
   const r=await fetch(mode==='login'?'/login':'/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value,password:document.getElementById('password').value})});
   const d=await r.json();
   if(!r.ok) throw new Error(d.error||'Authentication failed.');
   location.href='/';
 }catch(err){error.textContent=err.message;}finally{button.disabled=false;}
}
</script>
</body>
</html>
"""


HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dax</title>

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
    height:46px;
    padding:0 14px;
    border:1px solid #343b46;
    border-radius:12px;
    background:#222832;
    color:#fff;
    font-size:15px;
    margin-bottom:12px;
    flex:0 0 auto
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
    padding:10px 14px;
    background:#181d25;
    border-bottom:1px solid #2a303a;
    font-size:17px;
    font-weight:700;
    display:flex;
    align-items:center;
    min-height:58px
}

#chat{
    flex:1;
    overflow-y:auto;
    padding:24px 18px 150px;
    display:flex;
    flex-direction:column;
    gap:2px;
    scroll-behavior:smooth
}

.dax-welcome{
    width:100%;
    max-width:820px;
    margin:auto;
    padding:30px 8px 24px;
    text-align:center;
}

.dax-welcome h1{
    margin:0 0 8px;
    font-size:28px;
    font-weight:650;
    letter-spacing:-.4px;
}

.dax-welcome p{
    margin:0 0 22px;
    color:#9aa3af;
    font-size:15px;
}

.dax-suggestions{
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:10px;
    text-align:left;
}

.dax-suggestion{
    width:100%;
    height:auto;
    min-height:72px;
    border:1px solid #303743;
    border-radius:14px;
    background:#1b2028;
    color:#e8ebef;
    padding:13px 14px;
    cursor:pointer;
    font-size:14px;
    line-height:1.4;
    text-align:left;
    transition:background .15s,border-color .15s,transform .15s;
}

.dax-suggestion:hover{
    background:#232a34;
    border-color:#454e5d;
    transform:translateY(-1px);
}

.dax-suggestion strong{
    display:block;
    margin-bottom:4px;
    font-size:14px;
}

.dax-suggestion span{
    display:block;
    color:#9da6b3;
    font-size:12px;
}

@media(max-width:600px){
    .dax-welcome{
        padding:24px 4px 18px;
    }
    .dax-welcome h1{
        font-size:25px;
    }
    .dax-suggestions{
        grid-template-columns:1fr;
    }
}

.message{
    width:100%;
    max-width:820px;
    margin:0 auto;
    padding:13px 8px;
    line-height:1.55;
    white-space:pre-wrap;
    word-wrap:break-word;
    font-size:16px
}

.user{
    align-self:center;
    background:transparent;
    display:flex;
    justify-content:flex-end
}

.user::before{
    content:"You";
    display:none
}

.user{
    text-align:right
}

.user{
    color:#fff
}

.message-body{line-height:1.6;overflow-wrap:anywhere}
.message-body a{color:#7db7ff;text-decoration:underline}
.message-body code{background:#222832;border:1px solid #343b46;border-radius:5px;padding:2px 5px;font-family:monospace}

.alpha{
    align-self:center;
    background:transparent;
    color:#f2f4f7
}

.user, .alpha{
    border-radius:12px
}

.typing{
    display:flex;
    align-items:center;
    gap:5px;
    color:#9aa3af;
    padding:12px 8px
}

.typing span{
    width:7px;
    height:7px;
    border-radius:50%;
    background:#9aa3af;
    animation:typing 1.2s infinite ease-in-out
}

.typing span:nth-child(2){animation-delay:.15s}
.typing span:nth-child(3){animation-delay:.3s}

@keyframes typing{
    0%,60%,100%{transform:translateY(0);opacity:.35}
    30%{transform:translateY(-4px);opacity:1}
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
    padding:10px max(10px, calc((100vw - 860px)/2));
    background:#11151b;
    border-top:0;
    position:sticky;
    bottom:0;
    z-index:20
}

input, textarea{
    flex:1;
    min-width:0;
    border:1px solid #343b46;
    background:#222832;
    color:#fff;
    border-radius:22px;
    padding:12px 16px;
    outline:none;
    font-size:16px;
    font-family:inherit
}

#message{
    resize:none;
    min-height:46px;
    max-height:150px;
    line-height:1.4;
    overflow-y:auto
}

input:focus, textarea:focus{
    border-color:#596273;
    box-shadow:0 0 0 1px rgba(255,255,255,.04)
}

button{
    border:none;
    border-radius:50%;
    color:#fff;
    font-size:18px;
    width:46px;
    height:46px;
    padding:0;
    cursor:pointer;
    flex:0 0 46px
}

#micButton{
    background:#303641
}

#micButton.recording{
    background:#d22;
    animation:pulse 1s infinite
}

#sendButton{
    background:#fff;
    color:#11151b;
}

#imageButton, #micButton{
    background:#2b3039;
}

#imageButton, #micButton, #sendButton{
    align-self:flex-end
}

#sendButton.stop{
    background:#fff;
    color:#11151b;
}

button:disabled{
    opacity:.55;
    cursor:not-allowed
}

#imagePanel{
    display:none;
    padding:12px;
    background:#181d25;
    border-top:1px solid #2a303a;
    max-width:860px;
    width:100%;
    margin:0 auto
}

#imageFile{
    width:100%;
    margin-bottom:8px;
    color:#cdd3dc
}

#imagePreview{
    display:flex;
    gap:8px;
    overflow-x:auto;
    margin-bottom:8px
}

#imagePrompt{
    width:100%;
    margin-bottom:8px;
    border-radius:12px
}

#editButton{
    background:#7b3cff;
    width:100%;
    height:46px;
    border-radius:12px;
    padding:0 12px
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

#historyToggle{
    display:none;
    background:#303641;
    margin-right:8px;
    min-width:44px;
    padding:8px 12px;
}

#historyOverlay{
    display:none;
    position:fixed;
    inset:0;
    z-index:999;
    background:rgba(0,0,0,.45);
}

#closeHistory{
    display:none;
    width:100%;
    height:42px;
    border-radius:10px;
    flex:0 0 auto;
    margin-bottom:8px;
    background:#303641
}

@media(max-width:700px){
    #closeHistory{
        display:block;
        width:100%;
        margin-bottom:8px;
        background:#303641;
        padding:10px;
    }
}

@media(max-width:700px){
    #sidebar{
        display:block;
        position:fixed;
        left:0;
        top:0;
        bottom:0;
        width:280px;
        max-width:82vw;
        z-index:1000;
        transform:translateX(-105%);
        transition:transform .2s ease;
        box-shadow:8px 0 30px rgba(0,0,0,.35);
    }

    body.history-open #sidebar{
        transform:translateX(0);
    }

    body.history-open #historyOverlay{
        display:block;
    }

    #historyToggle{
        display:block;
    }

    .message{max-width:100%;padding-left:4px;padding-right:4px}
    #chat{padding:18px 10px 145px}
    .composer{padding:8px 8px 10px}
    #message{font-size:16px}
    #imageButton,#micButton,#sendButton{width:44px;height:44px;flex-basis:44px}
}
</style>
</head>

<body>

<aside id="sidebar">
    <button id="closeHistory" onclick="closeHistory()">✕ Close history</button>
    <button id="newChat" onclick="newChat()">＋ New chat</button>
    <div id="history"></div>
    <div style="border-top:1px solid #2a303a;padding-top:10px;margin-top:10px">
        <button id="logoutButton" onclick="logout()" style="width:100%;padding:10px;border:1px solid #343b46;border-radius:10px;background:#222832;color:#ddd;cursor:pointer">Log out</button>
    </div>
</aside>

<div id="historyOverlay" onclick="closeHistory()"></div>

<section id="main">

<header><button id="historyToggle" onclick="toggleHistory()">☰</button>Dax</header>

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
        Add photos only when you want Dax to use them for this image request. They are cleared automatically after a successful edit.
    </div>

    <div
        id="imageCount"
        style="font-size:13px;opacity:.8;margin-bottom:8px"
    >
        No photos selected
    </div>

    <input
        id="imagePrompt"
        placeholder="Tell Dax how to edit the selected photos..."
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

    <textarea
        id="message"
        rows="1"
        placeholder="Message Dax..."
        autocomplete="off"
        enterkeyhint="send"
    ></textarea>

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

function showWelcome(){
    chat.innerHTML="";

    const wrap=document.createElement("div");
    wrap.className="dax-welcome";

    const title=document.createElement("h1");
    title.textContent="How can I help you today?";
    wrap.appendChild(title);

    const sub=document.createElement("p");
    sub.textContent="Ask Dax anything, or start with a suggestion below.";
    wrap.appendChild(sub);

    const grid=document.createElement("div");
    grid.className="dax-suggestions";

    const suggestions=[
        ["✍️ Write something", "Draft a message, email, caption or story", "Write a professional message for me"],
        ["💡 Brainstorm ideas", "Get ideas for a project, business or content", "Give me 10 ideas for a new project"],
        ["📚 Learn something", "Explain a topic simply and step by step", "Explain this topic to me like a beginner"],
        ["🖼️ Edit a photo", "Upload photos and tell Dax what to change", "Help me edit a photo professionally"]
    ];

    suggestions.forEach(item=>{
        const button=document.createElement("button");
        button.type="button";
        button.className="dax-suggestion";
        button.innerHTML="<strong>"+item[0]+"</strong><span>"+item[1]+"</span>";
        button.onclick=()=>{
            if(item[2].startsWith("Help me edit")){
                toggleImagePanel();
                messageInput.focus();
                return;
            }
            messageInput.value=item[2];
            autoResize();
            messageInput.focus();
        };
        grid.appendChil
