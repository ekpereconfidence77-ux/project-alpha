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


def db_connect():
    """Open a SQLite connection configured for Render/concurrent requests."""
    conn = sqlite3.connect(DB_FILE, timeout=60, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

CHAT_MODEL = "openrouter/free"
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL") or "openai/gpt-oss-120b"


def init_db():
    conn = db_connect()
    # WAL allows reads while another request is writing.  Set it once at
    # startup; subsequent connections inherit the database journal mode.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

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
    conn = db_connect()

    rows = conn.execute(
        "SELECT memory FROM memories WHERE user_id = ? ORDER BY id ASC",
        (user_id,)
    ).fetchall()

    conn.close()
    return [row[0] for row in rows]


def save_memory(user_id, memory):
    conn = db_connect()

    conn.execute(
        "INSERT INTO memories (user_id, memory) VALUES (?, ?)",
        (user_id, memory)
    )

    conn.commit()
    conn.close()


def create_conversation(user_id, title="New chat"):
    conn = db_connect()

    cur = conn.execute(
        "INSERT INTO conversations (user_id, title) VALUES (?, ?)",
        (user_id, title[:80] or "New chat")
    )

    conversation_id = cur.lastrowid

    conn.commit()
    conn.close()

    return conversation_id


def get_conversations(user_id):
    conn = db_connect()

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
    conn = db_connect()

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
    conn = db_connect()

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
    conn = db_connect()

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
    conn = db_connect()

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
    width:280px;
    background:#181d25;
    border-right:1px solid #2a303a;
    padding:12px;
    display:flex;
    flex-direction:column
}

#sidebarBrand{
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding:8px 8px 16px;
    font-size:22px;
    font-weight:700;
}

#sidebarBrand small{
    font-size:13px;
    color:#8f98a6;
    font-weight:500;
}

#sidebarTools{
    display:grid;
    gap:4px;
    margin-bottom:12px;
}

.sidebar-tool{
    width:100%;
    height:42px;
    border-radius:10px;
    background:transparent;
    color:#e8ebef;
    text-align:left;
    padding:0 12px;
    font-size:15px;
    border:0;
    cursor:pointer;
}

.sidebar-tool:hover{background:#232831}

#recentLabel{
    color:#8f98a6;
    font-size:12px;
    font-weight:700;
    padding:10px 10px 7px;
    text-transform:uppercase;
    letter-spacing:.4px;
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

.sources-box{
    margin-top:12px;
    padding:10px 12px;
    border:1px solid #2f3641;
    border-radius:12px;
    background:#181d25;
}

.sources-title{
    font-size:13px;
    font-weight:700;
    color:#dfe4eb;
    margin-bottom:7px;
}

.source-item{
    display:flex;
    align-items:center;
    gap:8px;
    padding:7px 0;
    border-top:1px solid #292f38;
}

.source-item:first-of-type{border-top:0}

.source-item a{
    color:#9fc7ff;
    text-decoration:none;
    font-size:13px;
    line-height:1.35;
    overflow-wrap:anywhere;
}

.source-item a:hover{text-decoration:underline}

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
        display:flex;
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
    <div id="sidebarBrand"><span>Dax</span><small>AI assistant</small></div>
    <button id="closeHistory" onclick="closeHistory()">✕ Close</button>
    <div id="sidebarTools">
        <button class="sidebar-tool" onclick="focusSearch()">🔎 &nbsp;Search</button>
        <button class="sidebar-tool" onclick="toggleImagePanel();closeHistory()">🖼️ &nbsp;Images</button>
        <button class="sidebar-tool" onclick="showLibraryNotice()">📚 &nbsp;Library</button>
        <button class="sidebar-tool" onclick="showProjectNotice()">📁 &nbsp;Projects</button>
        <button class="sidebar-tool" onclick="showPluginNotice()">◉ &nbsp;Plugins</button>
    </div>
    <div id="recentLabel">Recents</div>
    <button id="newChat" onclick="newChat();closeHistory()">＋ New chat</button>
    <div id="history"></div>
    <div style="border-top:1px solid #2a303a;padding-top:10px;margin-top:10px">
        <button id="logoutButton" onclick="logout()" style="width:100%;padding:10px;border:1px solid #343b46;border-radius:10px;background:#222832;color:#ddd;cursor:pointer">Log out</button>
    </div>
</aside>

<div id="historyOverlay" onclick="closeHistory()"></div>

<section id="main">

<header><button id="historyToggle" onclick="toggleHistory()">☰</button><span>Dax</span></header>

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
        grid.appendChild(button);
    });

    wrap.appendChild(grid);
    chat.appendChild(wrap);
}

function escapeHtml(value){
    return String(value)
        .replace(/&/g,"&amp;")
        .replace(/</g,"&lt;")
        .replace(/>/g,"&gt;")
        .replace(/\"/g,"&quot;")
        .replace(/'/g,"&#039;");
}

function renderDaxMarkdown(text){
    let safe=escapeHtml(text);

    // Markdown links are generated by Dax for verified web references.
    safe=safe.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );

    safe=safe.replace(/`([^`]+)`/g,"<code>$1</code>");
    safe=safe.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
    safe=safe.replace(/(^|\n)#{1,3}\s+(.+)/g,"$1<strong>$2</strong>");
    safe=safe.replace(/\n/g,"<br>");

    return safe;
}

function addMessage(text,who){
    const div=document.createElement("div");
    div.className="message "+who;

    if(who==="user"){
        const bubble=document.createElement("div");
        bubble.textContent=text;
        bubble.style.display="inline-block";
        bubble.style.maxWidth="85%";
        bubble.style.background="#2f6fed";
        bubble.style.padding="10px 14px";
        bubble.style.borderRadius="18px 18px 5px 18px";
        bubble.style.textAlign="left";
        div.appendChild(bubble);
    }else{
        const parsed=splitSources(text);
        const body=document.createElement("div");
        body.className="message-body";
        body.innerHTML=renderDaxMarkdown(parsed.body);
        div.appendChild(body);
        const sourcesEl=renderSources(parsed.sources);
        if(sourcesEl) div.appendChild(sourcesEl);

        const actions=document.createElement("div");
        actions.style.marginTop="7px";
        actions.style.display="flex";
        actions.style.gap="6px";

        const readButton=document.createElement("button");
        readButton.type="button";
        readButton.textContent="🔊 Read aloud";
        readButton.title="Read this message aloud";
        readButton.style.border="0";
        readButton.style.background="transparent";
        readButton.style.cursor="pointer";
        readButton.style.padding="4px 0";
        readButton.style.fontSize="12px";
        readButton.style.opacity=".7";
        readButton.onclick=()=>speak(text);
        actions.appendChild(readButton);

        div.appendChild(actions);
    }

    chat.appendChild(div);
    chat.scrollTop=chat.scrollHeight;
}

function showTyping(){
    removeTyping();
    const div=document.createElement("div");
    div.id="typingIndicator";
    div.className="message alpha typing";
    div.innerHTML="<span></span><span></span><span></span>";
    chat.appendChild(div);
    chat.scrollTop=chat.scrollHeight;
}

function removeTyping(){
    document.getElementById("typingIndicator")?.remove();
}

function autoResize(){
    messageInput.style.height="auto";
    messageInput.style.height=Math.min(messageInput.scrollHeight,150)+"px";
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

function focusSearch(){
    const q=prompt("Search your recent Dax chats:");
    if(!q) return;
    const items=[...document.querySelectorAll(".history-item")];
    let found=0;
    items.forEach(item=>{
        const match=item.textContent.toLowerCase().includes(q.toLowerCase());
        item.style.display=match?"block":"none";
        if(match) found++;
    });
    if(!found) alert("No matching recent chat found.");
}

function showLibraryNotice(){
    alert("Library is coming next. Your conversations remain available under Recents.");
}

function showProjectNotice(){
    alert("Projects are coming next. Dax is keeping your existing chats safe.");
}

function showPluginNotice(){
    alert("Plugins are coming next. Dax currently uses its built-in tools and web search.");
}

function renderSources(sources){
    if(!Array.isArray(sources)||!sources.length) return null;
    const box=document.createElement("div");
    box.className="sources-box";
    const title=document.createElement("div");
    title.className="sources-title";
    title.textContent="Sources";
    box.appendChild(title);
    sources.slice(0,8).forEach(source=>{
        if(!source || !source.url) return;
        const row=document.createElement("div");
        row.className="source-item";
        const a=document.createElement("a");
        a.href=source.url;
        a.target="_blank";
        a.rel="noopener noreferrer";
        a.textContent=source.title||source.url;
        row.appendChild(a);
        box.appendChild(row);
    });
    return box;
}

function splitSources(text){
    const marker="\n\n**Sources**\n";
    const idx=text.indexOf(marker);
    if(idx<0) return {body:text,sources:[]};
    const body=text.slice(0,idx);
    const tail=text.slice(idx+marker.length);
    const sources=[];
    const re=/- \[(.*?)\]\((https?:\/\/[^\s)]+)\)/g;
    let m;
    while((m=re.exec(tail))!==null) sources.push({title:m[1],url:m[2]});
    return {body,sources};
}

function toggleHistory(){
    document.body.classList.toggle("history-open");
}

function closeHistory(){
    document.body.classList.remove("history-open");
}

async function logout(){
    if(!confirm("Log out of Dax?")) return;
    try{ await fetch("/logout",{method:"POST"}); }finally{ location.href="/login"; }
}

async function loadHistory(){
    try{
        const r=await fetch("/history");
        if(r.status===401){ location.href="/login"; return; }
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
    closeHistory();
    clearImageComposer();
    chat.innerHTML="";

    setStatus("Loading chat...");

    try{
        const r=await fetch("/history/"+id);
        const d=await r.json();

        if(!r.ok){
            throw new Error(d.error||"Could not load chat.");
        }

        const savedMessages=d.messages||[];

        savedMessages.forEach(m=>{
            addMessage(
                m.content,
                m.role==="user"?"user":"alpha"
            );
        });

        if(savedMessages.length===0){
            showWelcome();
        }

        setStatus("");

        await loadHistory();

    }catch(e){
        setStatus(e.message);
    }
}

async function newChat(){
    closeHistory();
    clearImageComposer();
    try{
        const r=await fetch("/new_chat",{
            method:"POST"
        });

        const d=await r.json();

        currentChatId=d.id;
        chat.innerHTML="";

        showWelcome();

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

    setStatus("");
    sendButton.disabled=true;
    messageInput.disabled=true;
    showTyping();

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

        removeTyping();
        addMessage(data.reply,"alpha");

        // Dax does NOT read replies automatically.
        // Use the "🔊 Read aloud" button on a reply when requested.
        setStatus("");

        await loadHistory();

    }catch(e){

        removeTyping();
        addMessage(
            "Sorry, something went wrong: "+e.message,
            "alpha"
        );

        setStatus("");

    }finally{

        sendButton.disabled=false;
        messageInput.disabled=false;
        autoResize();
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
            "Tell Dax what you want it to do with the selected photos."
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
        `🎨 Dax is editing ${files.length} photo`+
        `${files.length===1?"":"s"} with FLUX.2 Klein 9B...`
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
                "Dax server returned HTML instead of JSON "+
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

            // Behave like a normal chat attachment composer: once the
            // image has been successfully created, the source photos are
            // no longer kept selected in the composer. The final result
            // remains in the conversation view.
            clearImageComposer();
        }

        setStatus("");

    }catch(e){

        alert(
            "Image error: "+e.message
        );

        setStatus("");

    }finally{

        button.disabled=false;
    }
}


function clearImageComposer(){

    const fileInput=document.getElementById("imageFile");
    const preview=document.getElementById("imagePreview");
    const count=document.getElementById("imageCount");
    const prompt=document.getElementById("imagePrompt");
    const panel=document.getElementById("imagePanel");

    // Clear the browser file selection so the source photos are not
    // carried into the next image request.
    if(fileInput){
        fileInput.value="";
    }

    if(preview){
        preview.innerHTML="";
    }

    if(count){
        count.textContent="No photos selected";
    }

    if(prompt){
        prompt.value="";
    }

    // Close the image composer after a successful edit, like a normal
    // chat attachment workflow.
    if(panel){
        panel.style.display="none";
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
                "Allow microphone access for Dax in Chrome settings."
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
        if(e.key==="Enter" && !e.shiftKey){
            e.preventDefault();
            if(!sendButton.disabled){
                sendMessage();
            }
        }
    }
);

messageInput.addEventListener("input",autoResize);

messageInput.addEventListener("paste",()=>{
    setTimeout(autoResize,0);
});


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



@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if request.path.startswith(("/chat", "/image_edit", "/transcribe", "/history", "/current_chat", "/new_chat")):
        app.logger.exception("Unhandled Dax API error")
        return jsonify({
            "error": f"Dax server error: {str(exc)}"
        }), 500
    raise exc

@app.route("/")
@login_required_page
def home():
    user_id = get_current_user_id()
    chat_id = request.cookies.get("alpha_chat_id")

    if not chat_id or not conversation_belongs_to_user(chat_id, user_id):
        chat_id = str(create_conversation(user_id))

    response = make_response(render_template_string(HTML))
    response.set_cookie("alpha_chat_id", chat_id, max_age=60*60*24*365, httponly=True, samesite="Lax")
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if get_current_user_id():
            return redirect("/")
        return render_template_string(LOGIN_HTML)

    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400
    if not password:
        return jsonify({"error": "Enter your password."}), 400

    conn = db_connect()
    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if not row or not check_password_hash(row[1], password):
        return jsonify({"error": "Incorrect email or password."}), 401

    session.clear()
    session["user_id"] = row[0]
    return jsonify({"success": True})


@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    try:
        conn = db_connect()
        cur = conn.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, generate_password_hash(password)))
        user_id = cur.lastrowid
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account with that email already exists. Log in instead."}), 409

    session.clear()
    session["user_id"] = user_id
    return jsonify({"success": True})


@app.route("/me")
@login_required_api
def me():
    conn = db_connect()
    row = conn.execute("SELECT email FROM users WHERE id = ?", (get_current_user_id(),)).fetchone()
    conn.close()
    return jsonify({"email": row[0] if row else ""})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    response = jsonify({"success": True})
    response.delete_cookie("alpha_chat_id")
    return response


@app.route("/current_chat")
@login_required_api
def current_chat():

    user_id = get_current_user_id()

    chat_id = request.cookies.get(
        "alpha_chat_id"
    )

    if not chat_id:
        chat_id = str(
            create_conversation(user_id)
        )

    response = jsonify({
        "id": int(chat_id)
    })
    response.set_cookie(
        "alpha_chat_id",
        str(chat_id),
        max_age=60*60*24*365,
        httponly=True,
        samesite="Lax"
    )
    return response


@app.route("/new_chat", methods=["POST"])
@login_required_api
def new_chat():

    user_id = get_current_user_id()

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
@login_required_api
def history():

    user_id = get_current_user_id()

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
@login_required_api
def history_chat(conversation_id):

    user_id = get_current_user_id()

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
@login_required_api
def chat():
    """
    Dax chat provider strategy:
      1) Groq is the primary chat provider.
      2) Groq GPT-OSS can use browser_search when current sources are needed.
      3) OpenRouter is a secondary fallback.
    This avoids making an exhausted OpenRouter free-model quota a single
    point of failure for normal chat.
    """

    data = request.get_json(silent=True) or {}
    user_message = str(data.get("message", "")).strip()

    if not user_message:
        return jsonify({"error": "Message cannot be empty."}), 400

    user_id = get_current_user_id()
    conversation_id = data.get("conversation_id")

    try:
        conversation_id = int(conversation_id) if conversation_id else None
    except Exception:
        conversation_id = None

    if not conversation_id or not conversation_belongs_to_user(
        conversation_id, user_id
    ):
        conversation_id = create_conversation(user_id, user_message[:80])

    # Explicit user memory only.
    lower = user_message.lower()
    memory_prefixes = [
        "remember that ",
        "remember this: ",
        "remember this ",
        "please remember that "
    ]

    for prefix in memory_prefixes:
        if lower.startswith(prefix):
            memory_text = user_message[len(prefix):].strip()
            if memory_text:
                save_memory(user_id, memory_text)
            break

    save_message(conversation_id, "user", user_message)

    rows = get_messages(conversation_id, user_id)
    memories = get_memories(user_id)

    system_prompt = """
You are Dax, a helpful personal AI assistant.

Be friendly, clear, practical and honest.
Help the user with normal questions, coding, writing, ideas, planning,
learning and creative tasks.

Do not claim that you completed an action that you did not actually complete.
Keep answers reasonably concise unless the user asks for detail.

When browser search is available and used, use the information it returns
for current or recently changing questions. Include useful source references
when the provider supplies them. Never invent sources or URLs.

If the user asks you to remember something, the application may save it as
a user memory. Do not claim to remember something unless it is present in
the supplied conversation or memory context.
""".strip()

    if memories:
        memory_text = "\n".join("- " + m for m in memories[-30:])
        system_prompt += (
            "\n\nUser memories saved by the user:\n" + memory_text
        )

    messages = [{"role": "system", "content": system_prompt}]

    for role, content, _created_at in rows[-40:]:
        if role in ("user", "assistant"):
            messages.append({
                "role": role,
                "content": content
            })

    web_keywords = (
        "search the web", "search online", "look this up", "look it up",
        "find online", "latest", "current", "today", "news", "recent",
        "source", "sources", "reference", "references", "according to",
        "what happened", "right now", "this week"
    )
    use_web = any(k in lower for k in web_keywords)

    groq_key = os.environ.get("GROQ_API_KEY")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")

    result = None
    result_provider = None
    provider_errors = []

    # ------------------------------------------------------------
    # PRIMARY: GROQ
    # ------------------------------------------------------------
    if groq_key:
        groq_payload = {
            "model": GROQ_CHAT_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_completion_tokens": 4096
        }

        if use_web:
            # GPT-OSS supports Groq's server-side browser_search.
            groq_payload["tools"] = [{"type": "browser_search"}]
            groq_payload["tool_choice"] = "required"

        try:
            groq_result = requests.post(
                GROQ_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type": "application/json"
                },
                json=groq_payload,
                timeout=120
            )

            if groq_result.ok:
                result = groq_result
                result_provider = "groq"
            else:
                try:
                    error_data = groq_result.json()
                    groq_error = (
                        error_data.get("error", {}).get(
                            "message", groq_result.text
                        )
                    )
                except Exception:
                    groq_error = groq_result.text

                provider_errors.append(
                    f"Groq ({groq_result.status_code}): {groq_error}"
                )

                # If browser search itself fails, retry Groq as ordinary chat
                # before moving to another provider.
                if use_web:
                    retry_payload = {
                        "model": GROQ_CHAT_MODEL,
                        "messages": messages,
                        "temperature": 0.7,
                        "max_completion_tokens": 4096
                    }

                    try:
                        retry = requests.post(
                            GROQ_CHAT_URL,
                            headers={
                                "Authorization": f"Bearer {groq_key}",
                                "Content-Type": "application/json"
                            },
                            json=retry_payload,
                            timeout=120
                        )
                        if retry.ok:
                            result = retry
                            result_provider = "groq"
                            use_web = False
                        else:
                            try:
                                retry_error = retry.json().get(
                                    "error", {}
                                ).get("message", retry.text)
                            except Exception:
                                retry_error = retry.text
                            provider_errors.append(
                                f"Groq ordinary chat retry ({retry.status_code}): "
                                f"{retry_error}"
                            )
                    except requests.RequestException as exc:
                        provider_errors.append(
                            f"Groq ordinary chat retry: {exc}"
                        )

        except requests.RequestException as exc:
            provider_errors.append(f"Groq request: {exc}")

    # ------------------------------------------------------------
    # SECONDARY: OPENROUTER
    # ------------------------------------------------------------
    if result is None and openrouter_key:
        or_headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": request.host_url.rstrip("/"),
            "X-Title": "Dax"
        }

        # OpenRouter free-model web search is deliberately NOT attached here.
        # If OpenRouter's free quota is exhausted, ordinary chat should still
        # fail over cleanly without another tool-related failure.
        or_payload = {
            "model": CHAT_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 4096
        }

        try:
            or_result = requests.post(
                OPENROUTER_URL,
                headers=or_headers,
                json=or_payload,
                timeout=120
            )

            if or_result.ok:
                result = or_result
                result_provider = "openrouter"
            else:
                try:
                    error_data = or_result.json()
                    or_error = (
                        error_data.get("error", {}).get(
                            "message", or_result.text
                        )
                    )
                except Exception:
                    or_error = or_result.text

                provider_errors.append(
                    f"OpenRouter ({or_result.status_code}): {or_error}"
                )

        except requests.RequestException as exc:
            provider_errors.append(f"OpenRouter request: {exc}")

    if result is None:
        detail = " | ".join(provider_errors) if provider_errors else (
            "Neither GROQ_API_KEY nor OPENROUTER_API_KEY is configured."
        )
        return jsonify({
            "error": (
                "Dax could not get a response from its AI providers. "
                + detail
            )
        }), 502

    try:
        result_data = result.json()
    except Exception:
        return jsonify({
            "error": "Dax received an invalid response from its AI provider."
        }), 502

    choices = result_data.get("choices", [])
    if not choices:
        return jsonify({
            "error": "Dax received no chat response from its AI provider."
        }), 502

    message_obj = choices[0].get("message", {})
    reply = message_obj.get("content", "")

    if isinstance(reply, list):
        reply = "".join(
            str(part.get("text", ""))
            if isinstance(part, dict)
            else str(part)
            for part in reply
        )

    reply = str(reply).strip()

    if not reply:
        return jsonify({
            "error": "Dax received an empty response."
        }), 502

    # Groq browser_search can expose the search results used by the model.
    # Keep the sources in the chat so Dax can display clickable references.
    if use_web and result_provider == "groq":
        executed_tools = message_obj.get("executed_tools") or []
        sources = []

        def collect_search_items(value):
            if isinstance(value, dict):
                for key in ("search_results", "results"):
                    nested = value.get(key)
                    if isinstance(nested, (list, dict)):
                        yield from collect_search_items(nested)
                url = value.get("url") or value.get("link")
                title = value.get("title") or value.get("name") or value.get("source") or url
                if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                    yield (str(title or url), url)
                for key, nested in value.items():
                    if key not in {"search_results", "results", "url", "link", "title", "name", "source"}:
                        if isinstance(nested, (dict, list)):
                            yield from collect_search_items(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from collect_search_items(nested)

        for tool in executed_tools:
            for title, url in collect_search_items(tool):
                pair = (title, url)
                if pair not in sources:
                    sources.append(pair)

        if sources:
            reply += "\n\n**Sources**\n" + "\n".join(
                f"- [{title}]({url})"
                for title, url in sources[:5]
            )

    save_message(
        conversation_id,
        "assistant",
        reply
    )

    rows_after = get_messages(
        conversation_id,
        user_id
    )

    if len(rows_after) <= 2:
        rename_conversation(
            conversation_id,
            user_id,
            user_message[:80]
        )

    response = jsonify({
        "reply": reply,
        "conversation_id": conversation_id
    })

    response.set_cookie(
        "alpha_chat_id",
        str(conversation_id),
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax"
    )

    return response


@app.route("/transcribe", methods=["POST"])
@login_required_api
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
@login_required_api
def image_edit():
    """
    Dax image editor using Cloudflare Workers AI FLUX.2 Klein 9B.

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
            "error": "Please describe the final image you want Dax to create."
        }), 400

    # FLUX.2 Klein 9B supports a maximum of 4 reference images.
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
            # 512x512. Keep the full source as reference 1. For a single
            # uploaded photo, also create a dedicated high-detail identity
            # crop so the model gets more facial information than it can
            # retain from the whole photo alone.
            try:
                source = Image.open(io.BytesIO(raw)).convert("RGB")
                original_w, original_h = source.size
                source.thumbnail((511, 511), Image.Resampling.LANCZOS)

                output = io.BytesIO()
                source.save(output, format="JPEG", quality=95, optimize=True)
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

            # Single-photo identity aid: a square crop from the upper/central
            # area where a portrait face is normally located. It is deliberately
            # generated from the same uploaded image, so it cannot introduce a
            # second person's identity.
            if len(images) == 1:
                try:
                    identity_source = Image.open(io.BytesIO(raw)).convert("RGB")
                    w, h = identity_source.size
                    crop_size = max(1, min(w, h, int(min(w, h) * 0.78)))
                    center_x = w / 2.0
                    center_y = h * 0.42
                    left = int(max(0, min(w - crop_size, center_x - crop_size / 2)))
                    top = int(max(0, min(h - crop_size, center_y - crop_size / 2)))
                    crop = identity_source.crop((left, top, left + crop_size, top + crop_size))
                    crop.thumbnail((511, 511), Image.Resampling.LANCZOS)

                    crop_output = io.BytesIO()
                    crop.save(crop_output, format="JPEG", quality=97, optimize=True)
                    identity_bytes = crop_output.getvalue()

                    multipart_files["input_image_1"] = (
                        "identity_reference.jpg",
                        identity_bytes,
                        "image/jpeg"
                    )
                except Exception:
                    # The full reference remains usable even if the optional
                    # identity crop cannot be produced.
                    pass

        if not multipart_files:
            return jsonify({
                "error": "The uploaded images could not be read."
            }), 400

        if len(multipart_files) == 1:
            final_prompt = f"""
Edit the single supplied reference photo into ONE finished photorealistic image.

USER INSTRUCTION:
{prompt}

SINGLE-PHOTO EDITING RULES:
- The first supplied image is the FULL ORIGINAL PHOTO and is the primary composition/source reference.
- If a second supplied image is present, it is an IDENTITY-ONLY FACE REFERENCE cropped from the same original photo. It is NOT a second person and must never be pasted in as a separate face or duplicate subject.
- Use the identity reference to preserve the exact person's facial structure and recognizable identity: face shape, forehead, eyes, eyebrows, nose, nostrils, lips, mouth shape, cheeks, jawline, chin, ears, hairline, skin tone, natural asymmetry, facial hair and skin texture.
- Preserve the same person and recognizable identity as strongly as possible. Do not redesign, beautify, age, or replace the face unless the user explicitly asks for a face change.
- Preserve the original composition, pose, proportions, hairstyle, clothing, accessories, and background unless the user explicitly asks to change them.
- Make ONLY the changes requested by the user; do not invent extra changes.
- If the user asks for a face swap, use the supplied face/reference as the identity source and blend it naturally into the target image.
- If the user asks to change clothing, pose, lighting, background, or photography style, change those requested elements while keeping everything else consistent.
- Keep realistic skin texture, natural asymmetry, facial proportions, shadows, highlights, perspective, lens characteristics, and photographic detail.
- Do not duplicate the subject, split the image, create a collage, or place the source beside the result.
- Do not output multiple images.
- Produce ONE coherent final photograph.
""".strip()
        else:
            final_prompt = f"""
Create ONE final photorealistic image using the supplied reference images.

USER INSTRUCTION:
{prompt}

MULTI-PHOTO EDITING RULES:
- There are multiple reference images, so combine only the visual elements that are relevant to the user's instruction.
- Do NOT automatically blend every person, object, face, background, or feature from every reference.
- Decide which reference supplies the subject, identity, clothing, pose, background, lighting, or style based on the user's instruction.
- Preserve recognizable identity when a face reference is supplied.
- Follow the user's requested composition exactly.
- Keep the result as ONE coherent photorealistic photograph.
- Do not create a collage or place reference images side by side.
- Do not output multiple images.
- Avoid duplicate people, duplicate faces, extra fingers, malformed hands, warped objects, halos, seams, or obvious compositing artifacts.
""".strip()

        form_data = {
            "prompt": final_prompt,
            "width": "1024",
            "height": "1024"
        }

        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/ai/run/@cf/black-forest-labs/flux-2-klein-9b"
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
            "model": "@cf/black-forest-labs/flux-2-klein-9b",
            "reference_count": len(multipart_files),
            "identity_reference_added": len(images) == 1 and "input_image_1" in multipart_files
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
