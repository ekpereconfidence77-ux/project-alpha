import os
import uuid
import sqlite3
import base64
import io
import requests
from PIL import Image
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, request, jsonify, make_response, render_template_string, redirect, session, Response, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# Render terminates TLS at the proxy. Trust the forwarded host/protocol so
# OAuth redirect URLs are generated as https://... instead of http://....
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Production must use an environment-provided secret. Keep the local fallback
# only for development so a missing Render secret fails loudly and clearly.
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip()
if os.environ.get("RENDER") and not SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY must be set in Render Environment Variables.")
app.secret_key = SECRET_KEY or "dax-local-development-secret-change-me"
_secure_cookie = os.environ.get("COOKIE_SECURE")
if _secure_cookie is None:
    _secure_cookie = "1" if os.environ.get("RENDER") else "0"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_secure_cookie.lower() in ("1", "true", "yes", "on"),
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)

DB_FILE = os.environ.get("DB_FILE", "alpha_memory.db")


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
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL") or "qwen/qwen3.8-27b"
# Image editing uses Pollinations only. No paid or Qwen fallback is required.
POLLINATIONS_API_KEY = os.environ.get("POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_EDIT_MODELS = [
    m.strip() for m in os.environ.get("POLLINATIONS_EDIT_MODELS", "kontext,p-image-edit").split(",") if m.strip()
]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _google_redirect_uri():
    """Return the OAuth callback URI, preferring the explicit Render setting."""
    configured = GOOGLE_REDIRECT_URI.rstrip("/")
    if configured:
        return configured
    return request.url_root.rstrip("/") + "/auth/google/callback"


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
            google_sub TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Backward-compatible migration for databases created before Google sign-in.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
    except sqlite3.OperationalError:
        pass

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dax_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            conversation_id INTEGER,
            image_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'Dax image',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dax_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '📁',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dax_project_chats (
            project_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL UNIQUE,
            PRIMARY KEY(project_id, conversation_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dax_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            conversation_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            data_url TEXT,
            text_content TEXT,
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

    cur = conn.execute(
        """
        INSERT INTO messages (conversation_id, role, content)
        VALUES (?, ?, ?)
        """,
        (conversation_id, role, content)
    )
    message_id = cur.lastrowid

    conn.commit()
    conn.close()
    return message_id


def save_attachment(user_id, conversation_id, message_id, kind, filename, mime_type, data_url=None, text_content=None):
    conn = db_connect()
    conn.execute(
        """INSERT INTO dax_attachments
           (user_id, conversation_id, message_id, kind, filename, mime_type, data_url, text_content)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(user_id), conversation_id, message_id, kind, filename[:200], mime_type[:120], data_url, text_content)
    )
    conn.commit(); conn.close()


def get_message_attachments(conversation_id, user_id):
    conn = db_connect()
    rows = conn.execute(
        """SELECT message_id, kind, filename, mime_type, data_url, text_content
           FROM dax_attachments
           WHERE conversation_id=? AND user_id=? ORDER BY id ASC""",
        (conversation_id, str(user_id))
    ).fetchall()
    conn.close()
    return rows


def extract_document_text(file_obj):
    raw = file_obj.read()
    if len(raw) > 2_000_000:
        raise ValueError("Each document must be 2 MB or smaller.")
    name = (file_obj.filename or "document").lower()
    mime = file_obj.mimetype or "application/octet-stream"
    if name.endswith((".txt", ".md", ".csv", ".json", ".log")) or mime.startswith("text/") or mime == "application/json":
        return raw.decode("utf-8", errors="replace")[:60000]
    if name.endswith(".pdf") or mime == "application/pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)[:60000]
        except ImportError:
            raise ValueError("PDF reading needs the pypdf package. Add pypdf to requirements.txt and redeploy.")
        except Exception as exc:
            raise ValueError(f"Could not read PDF: {exc}")
    if name.endswith(".docx") or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        try:
            from docx import Document
            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs)[:60000]
        except ImportError:
            raise ValueError("DOCX reading needs python-docx. Add python-docx to requirements.txt and redeploy.")
        except Exception as exc:
            raise ValueError(f"Could not read DOCX: {exc}")
    raise ValueError("Supported documents: TXT, MD, CSV, JSON, PDF, and DOCX.")


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


def save_dax_image(user_id, conversation_id, image_url, title="Dax image"):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO dax_images (user_id, conversation_id, image_url, title) VALUES (?, ?, ?, ?)",
        (str(user_id), conversation_id, image_url, title[:120] or "Dax image")
    )
    image_id = cur.lastrowid
    conn.commit()
    conn.close()
    return image_id


def get_dax_images(user_id, limit=60):
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, conversation_id, image_url, title, created_at FROM dax_images WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (str(user_id), int(limit))
    ).fetchall()
    conn.close()
    return rows


def get_projects(user_id):
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, name, icon, created_at FROM dax_projects WHERE user_id = ? ORDER BY id DESC",
        (str(user_id),)
    ).fetchall()
    conn.close()
    return rows


def create_project(user_id, name, icon="📁"):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO dax_projects (user_id, name, icon) VALUES (?, ?, ?)",
        (str(user_id), name[:80] or "New project", icon)
    )
    project_id = cur.lastrowid
    conn.commit()
    conn.close()
    return project_id


def search_user_content(user_id, q):
    conn = db_connect()
    like = "%" + q + "%"
    chats = conn.execute(
        "SELECT id, title, created_at FROM conversations WHERE user_id = ? AND (title LIKE ? OR id IN (SELECT conversation_id FROM messages WHERE content LIKE ?)) ORDER BY id DESC LIMIT 40",
        (str(user_id), like, like)
    ).fetchall()
    images = conn.execute(
        "SELECT id, title, created_at FROM dax_images WHERE user_id = ? AND title LIKE ? ORDER BY id DESC LIMIT 20",
        (str(user_id), like)
    ).fetchall()
    conn.close()
    return chats, images


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
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Daxx — Log in</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}html,body{margin:0;min-height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#0b0d0f;color:#f7f7f8}body{min-height:100dvh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 50% -10%,#20242b 0,#0b0d0f 48%)}.login-shell{width:min(420px,100%)}.brand{text-align:center;margin-bottom:28px}.brand-mark{width:48px;height:48px;border-radius:15px;margin:0 auto 14px;display:grid;place-items:center;background:#fff;color:#111;font-size:23px;font-weight:800}.brand h1{font-size:30px;line-height:1.15;margin:0 0 7px;letter-spacing:-.6px}.brand p{margin:0;color:#9b9fa7;font-size:14px}.card{background:#15171b;border:1px solid #2d3036;border-radius:18px;padding:24px;box-shadow:0 18px 60px rgba(0,0,0,.42)}.tabs{display:grid;grid-template-columns:1fr 1fr;background:#0f1114;border:1px solid #2b2e34;padding:3px;border-radius:11px;margin-bottom:22px}.tabs button{height:38px;border:0;border-radius:8px;background:transparent;color:#969ba4;font-weight:600;font-size:13px;cursor:pointer}.tabs button.active{background:#2a2d32;color:#fff}.field{margin-bottom:15px}label{display:block;margin:0 0 7px;font-size:13px;font-weight:600;color:#dfe1e5}input{width:100%;height:48px;border:1px solid #373a41;border-radius:11px;background:#0f1114;color:#fff;padding:0 13px;outline:none;font-size:15px}input:focus{border-color:#777d88;box-shadow:0 0 0 3px rgba(255,255,255,.05)}.primary{width:100%;height:48px;border:0;border-radius:11px;background:#fff;color:#111;font-weight:700;cursor:pointer;margin-top:2px}.primary:disabled{opacity:.55}.or{display:flex;align-items:center;gap:10px;color:#777c85;font-size:12px;margin:19px 0}.or:before,.or:after{content:"";height:1px;background:#2d3036;flex:1}.google-btn{height:48px;width:100%;border:1px solid #3a3d43;border-radius:11px;background:#fff;color:#1f1f1f;text-decoration:none;display:flex;align-items:center;justify-content:center;gap:10px;font-weight:650;font-size:14px}.google-icon{width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font-weight:800;color:#4285f4;font-size:17px}.error{min-height:19px;color:#ff8e8e;font-size:13px;margin:4px 0 8px;line-height:1.4}.note{text-align:center;color:#777c85;font-size:11px;line-height:1.55;margin-top:17px}.security{text-align:center;color:#60656d;font-size:11px;margin-top:15px}@media(max-width:460px){body{padding:16px}.card{padding:20px;border-radius:16px}.brand{margin-bottom:22px}.brand h1{font-size:27px}}
</style>\n<style id="daxx-chatgpt-style-fix">\n/* DAXX CHATGPT-STYLE MESSAGE CONTROLS */\n.message-actions{\n    display:flex!important;\n    align-items:center!important;\n    justify-content:flex-start!important;\n    flex-wrap:nowrap!important;\n    gap:4px!important;\n    width:100%!important;\n    margin-top:4px!important;\n    padding:0 2px!important;\n    min-height:28px!important;\n}\n.message-action{\n    appearance:none!important;\n    -webkit-appearance:none!important;\n    display:inline-flex!important;\n    align-items:center!important;\n    justify-content:center!important;\n    width:auto!important;\n    min-width:0!important;\n    height:28px!important;\n    min-height:28px!important;\n    padding:0 8px!important;\n    margin:0!important;\n    border:0!important;\n    border-radius:8px!important;\n    background:transparent!important;\n    color:#9da3ad!important;\n    font:500 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif!important;\n    white-space:nowrap!important;\n    box-shadow:none!important;\n    cursor:pointer!important;\n}\n.message-action:hover{background:#1b1e23!important;color:#e8eaee!important}\n.message-action:active{transform:none!important}\n.alpha .message-actions{padding-left:0!important}\n.alpha .message-body{width:100%!important}\n.user .message-actions{justify-content:flex-end!important}\n.user .message-action{color:#8f96a0!important}\n\n/* Clean ChatGPT-like bubbles on phones. */\n@media(max-width:600px){\n    #chat{padding:10px 14px 150px!important}\n    .message{padding:8px 0!important;margin:0!important;font-size:16px!important;line-height:1.58!important}\n    .user .message-body{max-width:86%!important;padding:10px 14px!important;background:#2f3033!important;border-radius:18px 18px 5px 18px!important}\n    .alpha .message-body{max-width:100%!important;padding:3px 0!important;background:transparent!important}\n    .message-actions{gap:2px!important;margin-top:3px!important}\n    .message-action{height:27px!important;min-height:27px!important;padding:0 7px!important;font-size:11px!important;border-radius:7px!important}\n}\n</style>\n</head>
<body><main class="login-shell"><div class="brand"><div class="brand-mark">D</div><h1>Welcome to Daxx</h1><p>Your personal AI assistant</p></div><section class="card"><div class="tabs"><button id="loginTab" class="active" type="button" onclick="showMode('login')">Log in</button><button id="registerTab" type="button" onclick="showMode('register')">Create account</button></div><form onsubmit="submitAuth(event)"><div class="field"><label for="email">Email</label><input id="email" type="email" autocomplete="email" placeholder="you@example.com" required></div><div class="field"><label for="password">Password</label><input id="password" type="password" autocomplete="current-password" placeholder="Your password" required></div><div id="error" class="error"></div><button id="submit" class="primary" type="submit">Log in</button></form><div class="or"><span>OR</span></div><a class="google-btn" href="/auth/google"><span class="google-icon">G</span><span>Continue with Google</span></a><div class="note">Your chats, files and memories stay connected to your Daxx account.</div></section><div class="security">Secure sign-in • Daxx</div></main><script>
let mode='login';
function showMode(next){mode=next;document.getElementById('loginTab').classList.toggle('active',mode==='login');document.getElementById('registerTab').classList.toggle('active',mode==='register');document.getElementById('submit').textContent=mode==='login'?'Log in':'Create account';document.getElementById('password').autocomplete=mode==='login'?'current-password':'new-password';document.getElementById('password').placeholder=mode==='login'?'Your password':'At least 8 characters';document.getElementById('error').textContent='';}
const params=new URLSearchParams(location.search);if(params.get('google_error'))document.getElementById('error').textContent=params.get('google_error').replace(/\+/g,' ');
async function submitAuth(e){e.preventDefault();const b=document.getElementById('submit'),err=document.getElementById('error');b.disabled=true;err.textContent='';try{const r=await fetch(mode==='login'?'/login':'/register',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({email:document.getElementById('email').value.trim(),password:document.getElementById('password').value})});const d=await r.json();if(!r.ok)throw new Error(d.error||'Authentication failed.');location.replace('/');}catch(x){err.textContent=x.message||'Authentication failed.'}finally{b.disabled=false}}
</script></body></html>
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
html,body{width:100%;height:100%;margin:0}
body{
    background:#0f1115;
    color:#f4f4f5;
    font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
    height:100dvh;
    min-height:100vh;
    display:flex;
    overflow:hidden;
    -webkit-font-smoothing:antialiased;
    text-rendering:optimizeLegibility
}
button,input,textarea{font:inherit}
button{touch-action:manipulation}
::selection{background:#315da8;color:#fff}

#sidebar{
    width:272px;
    flex:0 0 272px;
    background:#17191e;
    border-right:1px solid #292c33;
    padding:12px;
    display:flex;
    flex-direction:column
}

.side-tool{width:100%;height:46px;border-radius:12px;background:transparent;color:#d8dde5;text-align:left;padding:0 12px;margin-bottom:3px;font-size:15px}
.side-tool:hover{background:#222832}
.section-label{font-size:11px;font-weight:700;color:#7f8997;padding:16px 10px 8px}
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
    flex:1 1 auto;
    min-width:0;
    min-height:0;
    display:flex;
    flex-direction:column;
    background:#0f1115
}

header{
    padding:10px 18px;
    background:rgba(15,17,21,.92);
    border-bottom:1px solid #292c33;
    backdrop-filter:blur(14px);
    -webkit-backdrop-filter:blur(14px);
    font-size:17px;
    font-weight:700;
    display:flex;
    align-items:center;
    min-height:58px
}

#topbar-title{display:flex;align-items:center;gap:8px;min-width:0}
#topbar-actions{margin-left:auto;display:flex;align-items:center;gap:5px}
.topbar-btn{width:40px;height:40px;border:0;border-radius:10px;background:transparent;color:#dce2ea;display:flex;align-items:center;justify-content:center;cursor:pointer}
.topbar-btn:hover{background:#252b35}
.topbar-btn.active{background:#2b3442;color:#fff}
.topbar-btn svg{width:21px;height:21px;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
#chatMoreMenu{position:absolute;right:10px;top:58px;z-index:50;display:none;width:210px;background:#20252d;border:1px solid #343b46;border-radius:13px;box-shadow:0 12px 35px rgba(0,0,0,.35);padding:6px}
#chatMoreMenu button{width:100%;text-align:left;border:0;background:transparent;color:#e6e9ee;padding:11px 12px;border-radius:9px;cursor:pointer;font-size:14px}
#chatMoreMenu button:hover{background:#2a303a}
#webModeNote{font-size:11px;color:#8ab4ff;display:none;margin-left:4px}

#chat{
    flex:1;
    min-height:0;
    overflow-y:auto;
    padding:28px 20px 180px;
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
    max-width:860px;
    margin:0 auto;
    padding:14px 10px;
    line-height:1.65;
    white-space:pre-wrap;
    overflow-wrap:anywhere;
    font-size:16px;
}
.user{align-self:center;display:flex;justify-content:flex-end;color:#fff}
.user .message-body{background:#2a2f38;border-radius:18px 18px 5px 18px;padding:11px 14px;max-width:min(78%,620px);text-align:left}
.alpha{align-self:center;background:transparent;color:#f2f4f7}
.user,.alpha{border-radius:14px}
.message-body{line-height:1.65;overflow-wrap:anywhere}
.message-body a{color:#8ab4ff;text-decoration:underline}
.message-body code{background:#20242b;border:1px solid #333943;border-radius:6px;padding:2px 5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.message-body pre{background:#171a20;border:1px solid #2b3038;border-radius:12px;padding:12px;overflow:auto}
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

#attachmentMenu{display:none;position:absolute;left:10px;bottom:64px;background:#20252d;border:1px solid #343b46;border-radius:16px;padding:8px;box-shadow:0 12px 35px rgba(0,0,0,.4);z-index:40;min-width:190px}
.attachment-option{display:flex;align-items:center;gap:10px;width:100%;height:44px;border-radius:11px;background:transparent;text-align:left;padding:0 12px;font-size:14px}
.attachment-option:hover{background:#2b3039}
.composer-wrap{position:sticky;bottom:0;z-index:20;background:#11151b}
.icon-button{background:#2b3039;display:flex;align-items:center;justify-content:center}
.icon-button svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#attachmentPreview{display:none;max-width:860px;width:100%;margin:0 auto;padding:6px 12px 0;font-size:12px;color:#cdd3dc}
#attachmentPreview img{width:54px;height:54px;object-fit:cover;border-radius:10px;margin-right:6px}
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

#popoutPanel{display:none;position:fixed;inset:0;z-index:1200;background:#11151b;overflow:auto}
#popoutHeader{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:12px;padding:16px 18px;background:#181d25;border-bottom:1px solid #2a303a}
#popoutTitle{font-size:19px;font-weight:700;flex:1}
#popoutClose{width:42px;height:42px;background:#303641;border-radius:50%}
.popout-content{max-width:900px;margin:0 auto;padding:20px}
.popout-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px}
.popout-card{background:#1b2028;border:1px solid #303743;border-radius:14px;padding:14px;color:#fff;text-align:left}
.popout-card button{width:100%;height:auto;min-height:44px;border-radius:10px;background:#2a3039;text-align:left;padding:10px 12px;font-size:14px}
.library-img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;display:block;margin-bottom:8px}
.search-box{width:100%;border-radius:12px;margin-bottom:14px}
@media(max-width:600px){.popout-content{padding:14px}.popout-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}

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
    #chat{padding:18px 12px 170px}
   .composer{padding:8px 8px 10px}
    #message{font-size:16px}
    #imageButton,#micButton,#sendButton{width:44px;height:44px;flex-basis:44px}
}

/* Final responsive polish */
.side-tool{height:44px;color:#cfd3da}
.side-tool:active,.topbar-btn:active,.message-action:active{transform:scale(.98)}
.section-label{letter-spacing:.02em}
.history-item{font-size:14px;color:#c8ccd3}
#history{scrollbar-width:thin}
.composer{width:100%;max-width:920px;margin:0 auto;padding:10px 14px 14px;gap:8px;background:#0f1115}
.composer textarea{box-shadow:0 2px 12px rgba(0,0,0,.18)}
.icon-button{flex:0 0 46px}
#sendButton{box-shadow:0 2px 10px rgba(255,255,255,.08)}
#status{max-width:920px;margin:0 auto;width:100%}
.install-banner{backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
@media(min-width:701px){
  #historyToggle{display:none!important}
}
@media(max-width:700px){
  body{height:100dvh}
  header{padding:8px 10px;min-height:54px}
  #topbar-title{font-size:16px}
  #topbar-actions{gap:1px}
  .topbar-btn{width:38px;height:38px}
  #chat{padding:14px 8px 180px}
  .message{font-size:15.5px;padding:11px 4px}
  .user .message-body{max-width:88%;border-radius:17px 17px 5px 17px}
  .dax-welcome{padding:30px 4px 12px}
  .dax-welcome h1{font-size:26px}
  .composer-wrap{padding-top:8px}
  .composer{padding:7px 8px max(9px,env(safe-area-inset-bottom));gap:6px}
  #message{min-height:44px;max-height:128px;padding:11px 13px;border-radius:20px}
  #imageButton,#micButton,#sendButton{width:44px;height:44px;flex-basis:44px}
  #attachmentMenu{left:8px;bottom:61px}
}
@media(max-width:380px){
  #topbar-actions .topbar-btn:nth-child(n+3){display:none}
  #message{font-size:15px}
  #imageButton,#micButton,#sendButton{width:42px;height:42px;flex-basis:42px}
}

/* DAXX ANDROID / CHATGPT-STYLE FINAL UI */
html,body{background:#0b0d10!important}
body{display:flex!important;align-items:stretch!important;justify-content:flex-start!important;padding:0!important;width:100%;height:100dvh;min-height:100dvh;overflow:hidden!important}
#main{width:100%;min-width:0;height:100dvh;background:#0b0d10!important}
#sidebar{position:fixed;left:0;top:0;bottom:0;width:300px;max-width:86vw;z-index:1000;transform:translateX(-105%);transition:transform .22s ease;box-shadow:12px 0 40px rgba(0,0,0,.5);display:flex}
body.history-open #sidebar{transform:translateX(0)}
#historyOverlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:999;display:none;backdrop-filter:blur(2px)}
body.history-open #historyOverlay{display:block}
#closeHistory{display:block!important}
header{position:sticky!important;top:0;z-index:60;height:56px;min-height:56px;padding:6px 8px!important;background:rgba(11,13,16,.96)!important;border-bottom:1px solid #24272d!important}
#topbar-title{gap:7px;font-size:17px!important;font-weight:650;flex:1}
#historyToggle{display:flex!important;width:40px;height:40px;border-radius:10px;background:transparent;color:#e8eaed;align-items:center;justify-content:center;font-size:22px;border:0}
#topbar-actions{gap:0!important}
.topbar-btn{width:40px!important;height:40px!important;border-radius:10px!important}
#chat{padding:8px 10px 148px!important;gap:0!important;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
.dax-welcome{max-width:520px!important;margin:auto!important;padding:34px 10px 18px!important}
.dax-welcome h1{font-size:25px!important;font-weight:650!important}
.dax-welcome p{font-size:14px!important;margin-bottom:18px!important}
.dax-suggestions{grid-template-columns:1fr 1fr!important;gap:8px!important}
.dax-suggestion{min-height:68px!important;border-radius:13px!important;padding:11px!important;background:#15181d!important;border-color:#292d34!important}
.message{max-width:100%!important;width:100%!important;padding:10px 3px!important;font-size:15.5px!important}
.user .message-body{max-width:88%!important;background:#2a2f35!important;border-radius:18px 18px 5px 18px!important;padding:10px 13px!important}
.alpha .message-body{max-width:100%!important;padding:4px 2px!important}
.message-actions{padding-left:2px}
.composer-wrap{position:fixed!important;left:0;right:0;bottom:0;z-index:80;background:linear-gradient(to top,#0b0d10 78%,rgba(11,13,16,0))!important;padding:7px 8px max(8px,env(safe-area-inset-bottom))!important}
.composer{display:flex!important;width:100%!important;max-width:100%!important;height:auto!important;min-height:54px!important;margin:0!important;padding:6px!important;gap:6px!important;background:#1a1d22!important;border:1px solid #30343b!important;border-radius:27px!important;box-shadow:0 4px 22px rgba(0,0,0,.32)!important}
#message{min-height:42px!important;max-height:120px!important;padding:10px 11px!important;border:0!important;background:transparent!important;box-shadow:none!important;border-radius:20px!important;font-size:16px!important}
#message:focus{border:0!important;box-shadow:none!important}
#imageButton,#micButton,#sendButton{width:42px!important;height:42px!important;flex:0 0 42px!important}
#imageButton,#micButton{background:#292d34!important}
#sendButton{background:#f1f3f5!important;color:#11151b!important}
#attachmentMenu{left:8px!important;bottom:62px!important;border-radius:16px!important}
#attachmentPreview{max-width:none!important;padding:4px 4px 6px!important}
#imagePanel{max-width:none!important;margin:0!important;border-radius:14px 14px 0 0}
#status{font-size:12px!important;padding-bottom:4px!important}
.install-banner{bottom:82px!important;width:calc(100vw - 20px)!important}
@media(min-width:701px){
  body{padding:0!important}
  #sidebar{position:fixed;display:flex;transform:translateX(-105%)}
  body.history-open #sidebar{transform:translateX(0)}
  #historyToggle{display:flex!important}
  #chat{padding-left:max(12px,calc((100vw - 760px)/2))!important;padding-right:max(12px,calc((100vw - 760px)/2))!important}
  .composer-wrap{padding-left:max(8px,calc((100vw - 760px)/2))!important;padding-right:max(8px,calc((100vw - 760px)/2))!important}
}
@media(max-width:430px){
  .dax-suggestions{grid-template-columns:1fr!important}
  .dax-suggestion{min-height:62px!important}
  #topbar-actions .topbar-btn:nth-child(n+3){display:none!important}
  .message{font-size:15.2px!important}
}
@media(max-width:360px){
  #topbar-actions .topbar-btn:nth-child(n+2){display:none!important}
  .dax-welcome h1{font-size:23px!important}
}


/* ===== DAXX MOBILE-FIRST CHAT UI: FINAL OVERRIDE ===== */
html,body{width:100%;height:100%;margin:0!important;padding:0!important;background:#0b0d0f!important;overflow:hidden!important}
body{display:block!important;color:#f7f7f8!important;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif!important}
#sidebar{position:fixed!important;left:0!important;top:0!important;bottom:0!important;width:292px!important;max-width:86vw!important;height:100dvh!important;z-index:200!important;display:flex!important;flex-direction:column!important;transform:translateX(-105%)!important;background:#17181c!important;border:0!important;border-right:1px solid #2b2d31!important;padding:12px!important;box-shadow:18px 0 45px rgba(0,0,0,.45)!important;transition:transform .22s ease!important}
body.history-open #sidebar{transform:translateX(0)!important}
#historyOverlay{position:fixed!important;inset:0!important;z-index:190!important;background:rgba(0,0,0,.58)!important;display:none!important}
body.history-open #historyOverlay{display:block!important}
#main{position:relative!important;width:100%!important;height:100dvh!important;min-width:0!important;display:flex!important;flex-direction:column!important;background:#0b0d0f!important}
header{position:relative!important;top:auto!important;left:auto!important;right:auto!important;width:100%!important;height:56px!important;min-height:56px!important;flex:0 0 56px!important;padding:6px 8px!important;background:#0b0d0f!important;border:0!important;box-shadow:none!important;z-index:80!important}
#historyToggle{display:flex!important;align-items:center!important;justify-content:center!important;width:42px!important;height:42px!important;flex:0 0 42px!important;margin:0!important;padding:0!important;background:transparent!important;color:#f3f4f6!important;border-radius:12px!important}
#topbar-title{font-size:17px!important;font-weight:650!important;justify-content:center!important;flex:1!important;min-width:0!important;white-space:nowrap!important}
#topbar-actions{margin-left:0!important;display:flex!important;gap:0!important}
.topbar-btn{width:42px!important;height:42px!important;flex:0 0 42px!important;border-radius:12px!important;background:transparent!important}
#chat{position:relative!important;flex:1 1 auto!important;width:100%!important;min-height:0!important;overflow-y:auto!important;overflow-x:hidden!important;padding:8px 16px 145px!important;display:flex!important;flex-direction:column!important;gap:0!important;-webkit-overflow-scrolling:touch!important;overscroll-behavior-y:contain!important}
.message{width:100%!important;max-width:100%!important;margin:0!important;padding:13px 0!important;font-size:16px!important;line-height:1.6!important}
.user{width:100%!important;justify-content:flex-end!important;align-self:stretch!important}
.user .message-body{max-width:82%!important;background:#2f3033!important;border-radius:19px 19px 5px 19px!important;padding:10px 14px!important}
.alpha .message-body{max-width:100%!important;padding:3px 0!important;background:transparent!important}
.dax-welcome{width:100%!important;max-width:520px!important;margin:auto!important;padding:20px 4px 18px!important;text-align:center!important}
.dax-welcome h1{font-size:26px!important;line-height:1.2!important;margin-bottom:8px!important}
.dax-welcome p{font-size:14px!important;margin-bottom:22px!important;color:#9b9da3!important}
.dax-suggestions{width:100%!important;grid-template-columns:1fr 1fr!important;gap:9px!important}
.dax-suggestion{min-height:70px!important;padding:12px!important;border-radius:15px!important;background:#15171a!important;border:1px solid #292c30!important}
.composer-wrap{position:fixed!important;left:0!important;right:0!important;bottom:0!important;width:100%!important;z-index:100!important;padding:8px 12px max(9px,env(safe-area-inset-bottom))!important;background:linear-gradient(to top,#0b0d0f 72%,rgba(11,13,15,0))!important;border:0!important}
#status{width:100%!important;max-width:none!important;text-align:center!important;font-size:11px!important;color:#8b8e94!important;padding:0 0 5px!important}
.composer{position:relative!important;width:100%!important;max-width:none!important;min-height:52px!important;height:auto!important;margin:0!important;padding:5px 6px!important;display:flex!important;align-items:flex-end!important;gap:5px!important;background:#202123!important;border:1px solid #3a3b3e!important;border-radius:27px!important;box-shadow:0 2px 18px rgba(0,0,0,.3)!important}
#message{flex:1 1 auto!important;width:auto!important;min-width:0!important;min-height:42px!important;max-height:126px!important;margin:0!important;padding:10px 8px!important;border:0!important;background:transparent!important;color:#f5f5f5!important;border-radius:20px!important;font-size:16px!important;line-height:1.35!important;box-shadow:none!important;outline:none!important}
#message:focus{border:0!important;box-shadow:none!important}
#imageButton,#micButton,#sendButton{width:42px!important;height:42px!important;min-width:42px!important;flex:0 0 42px!important;margin:0!important;border-radius:50%!important;align-self:flex-end!important}
#imageButton,#micButton{background:#2b2c30!important;color:#f1f2f3!important}
#sendButton{background:#f4f4f4!important;color:#111214!important}
#attachmentMenu{position:absolute!important;left:6px!important;bottom:60px!important;width:220px!important;border-radius:17px!important}
#attachmentPreview{width:100%!important;max-width:none!important;padding:4px 2px 6px!important}
#imagePanel{width:100%!important;max-width:none!important;margin:0!important;border-radius:16px!important}
.install-banner{left:10px!important;right:10px!important;bottom:78px!important;width:auto!important}
.message-actions{display:flex!important;flex-wrap:wrap!important;gap:5px!important}
@media(max-width:430px){
  header{height:54px!important;min-height:54px!important;flex-basis:54px!important}
  #chat{padding-left:14px!important;padding-right:14px!important;padding-bottom:142px!important}
  .message{font-size:15.5px!important}
  .user .message-body{max-width:86%!important}
  .dax-suggestions{grid-template-columns:1fr!important}
  .dax-welcome{padding-top:18px!important}
}
@media(max-width:360px){
  #topbar-actions .topbar-btn:nth-child(n+2){display:none!important}
  .dax-welcome h1{font-size:23px!important}
  #chat{padding-left:12px!important;padding-right:12px!important}
  #imageButton,#micButton,#sendButton{width:40px!important;height:40px!important;min-width:40px!important;flex-basis:40px!important}
}
@media(min-width:701px){
  /* Keep the same phone-first experience on wider screens; only center the phone-like content. */
  #main{max-width:760px!important;margin:0 auto!important;border-left:1px solid #202226!important;border-right:1px solid #202226!important}
  #sidebar{display:none!important}
  #historyToggle{display:flex!important}
  #chat{padding-left:18px!important;padding-right:18px!important}
  .composer-wrap{left:calc(50% - 380px)!important;right:calc(50% - 380px)!important;width:760px!important}
}


/* ===== DAXX GPT-STYLE FINAL LAYOUT ===== */
@media(min-width:801px){
  body{display:flex!important;background:#0b0d0f!important}
  #sidebar{display:flex!important;position:fixed!important;left:0!important;top:0!important;bottom:0!important;width:260px!important;max-width:none!important;transform:none!important;background:#17181b!important}
  body.history-open #sidebar{transform:none!important}
  #historyToggle{display:none!important}
  #main{width:calc(100% - 260px)!important;max-width:none!important;margin-left:260px!important;margin-right:0!important;height:100dvh!important;border:0!important}
  #chat{padding-left:24px!important;padding-right:24px!important}
  .composer-wrap{left:260px!important;right:0!important;width:auto!important}
}
@media(max-width:800px){
  #sidebar{display:flex!important;transform:translateX(-105%)!important}
  body.history-open #sidebar{transform:translateX(0)!important}
  #main{margin:0!important;width:100%!important;max-width:none!important;border:0!important}
}
#sidebar .side-tool,#newChat{font-weight:500}#sidebar .section-label{letter-spacing:.08em}.history-item{font-size:13px!important}.dax-welcome{max-width:760px!important}.dax-welcome h1{font-size:30px!important}.message{max-width:760px!important}.alpha .message-body{font-size:16px}.user .message-body{background:#2f3033!important}.composer-wrap{backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}

/* DAXX MOBILE-FIRST ONLY: phone is the canonical layout. */
@media (min-width: 601px){
  html,body{width:100%!important;max-width:600px!important;min-width:0!important;margin:0 auto!important;overflow:hidden!important;background:#0b0d0f!important;}
  body{display:flex!important;position:relative!important;}
  #sidebar{width:292px!important;max-width:292px!important;transform:translateX(-105%)!important;transition:transform .2s ease!important;box-shadow:18px 0 40px rgba(0,0,0,.45)!important;}
  body.history-open #sidebar{transform:translateX(0)!important;}
  #historyToggle{display:flex!important;}
  #main{margin-left:0!important;width:100%!important;max-width:600px!important;height:100dvh!important;}
  header{height:56px!important;min-height:56px!important;padding:7px 10px!important;}
  #chat{padding:20px 12px 16px!important;}
  #chat>*{width:100%!important;max-width:100%!important;}
  .message{width:100%!important;max-width:100%!important;padding:12px 4px!important;font-size:15px!important;}
  .user .message-body{max-width:88%!important;}
  .composer-wrap{width:100%!important;padding:5px 8px max(9px,env(safe-area-inset-bottom))!important;}
  .composer{width:100%!important;}
  #attachmentMenu{left:8px!important;right:8px!important;width:auto!important;}
  #imagePanel,#attachmentPreview,#status{width:100%!important;max-width:100%!important;}
  .dax-welcome{width:100%!important;padding:22px 4px!important;}
  .dax-welcome h1{font-size:24px!important;}
}
@media (max-width:380px){
  .dax-suggestions{grid-template-columns:1fr!important;}
  #imageButton,#micButton,#sendButton{width:38px!important;height:38px!important;min-width:38px!important;flex-basis:38px!important;}
  #message{font-size:15px!important;}
}

</style>
<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#111418"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="icon" type="image/png" sizes="512x512" href="/icon-512.png"><link rel="apple-touch-icon" sizes="192x192" href="/icon-192.png">
</head>

<body>

<aside id="sidebar">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 8px 14px">
        <strong style="font-size:22px">Daxx</strong><span style="opacity:.55;font-size:13px">AI assistant</span>
    </div>
    <button id="closeHistory" onclick="closeHistory()">✕ Close</button>
    <button class="side-tool" onclick="openPopout('search')">🔎 Search</button>
    <button class="side-tool" onclick="openPopout('images')">🖼️ Images</button>
    <button class="side-tool" onclick="openPopout('library')">📚 Library</button>
    <button class="side-tool" onclick="openPopout('projects')">📁 Projects</button>
    <button class="side-tool" onclick="openPopout('plugins')">◉ Plugins</button>
    <div class="section-label">RECENTS</div>
    <button id="newChat" onclick="newChat()">＋ New chat</button>
    <div id="history"></div>
    <div style="border-top:1px solid #2a303a;padding-top:10px;margin-top:10px">
        <button id="logoutButton" onclick="logout()" style="width:100%;padding:10px;border:1px solid #343b46;border-radius:10px;background:#222832;color:#ddd;cursor:pointer">Log out</button>
    </div>
</aside>

<div id="historyOverlay" onclick="closeHistory()"></div>

<div id="popoutPanel">
  <div id="popoutHeader"><div id="popoutTitle">Dax</div><button id="popoutClose" onclick="closePopout()">✕</button></div>
  <div id="popoutBody" class="popout-content"></div>
</div>
<div id="installBanner" class="install-banner"><div style="font-weight:700;margin-bottom:4px">Install Daxx</div><div style="opacity:.75;font-size:13px;margin-bottom:10px">Add Daxx to your Android home screen like an app.</div><div style="display:flex;gap:8px;justify-content:flex-end"><button onclick="dismissInstall()">Not now</button><button onclick="installDaxx()">Install</button></div></div>

<section id="main">

<header style="position:relative">
    <div id="topbar-title"><button id="historyToggle" onclick="toggleHistory()">☰</button><span>Daxx</span><span id="webModeNote">Web</span></div>
    <div id="topbar-actions">
        <button class="topbar-btn" id="topVoiceButton" onclick="toggleRecording()" aria-label="Voice" title="Voice">
            <svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/></svg>
        </button>
        <button class="topbar-btn" id="topSearchButton" onclick="toggleWebMode()" aria-label="Search the web" title="Search the web">
            <svg viewBox="0 0 24 24"><circle cx="10.8" cy="10.8" r="6.5"/><path d="m16 16 5 5"/></svg>
        </button>
        <button class="topbar-btn" onclick="toggleChatMore()" aria-label="More" title="More">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>
        </button>
    </div>
    <div id="chatMoreMenu">
        <button onclick="shareDaxxApp()">📤 Share Daxx app</button>
        <button onclick="shareCurrentChat()">↗️ Share chat</button>
        <button onclick="renameCurrentChat()">✏️ Rename chat</button>
        <button onclick="findInChat()">🔎 Find in chat</button>
        <button onclick="toggleVoiceReplies()">🔊 Voice replies: <span id="voiceReplyState">Off</span></button>
        <button onclick="readLastReply()">🔊 Read last reply</button>
        <button onclick="newChat();closeChatMore()">＋ New chat</button>
        <button onclick="deleteCurrentChat()" style="color:#ff8f8f">🗑️ Delete chat</button>
    </div>
</header>

<div id="chat"></div>

<div id="status" class="status"></div>

<div id="imagePanel">

    <input
        id="imageFile"
        type="file"
        accept="image/*"
    >

    <div id="imagePreview"></div>

    <div style="font-size:13px;opacity:.75;margin:6px 0">
        Add one photo when you want Dax to edit it. It is cleared automatically after a successful edit.
    </div>

    <div
        id="imageCount"
        style="font-size:13px;opacity:.8;margin-bottom:8px"
    >
        No photos selected
    </div>

    <input
        id="imagePrompt"
        placeholder="Tell Dax how to edit this photo..."
    >

    <button
        id="editButton"
        onclick="editImage()"
    >
        🎨 Edit Photo
    </button>

</div>

<div class="composer-wrap">
    <div id="attachmentMenu">
        <button class="attachment-option" type="button" onclick="chooseAnalyzeImage()">
            <span>🖼️</span><span>Analyze photo / screenshot</span>
        </button>
        <button class="attachment-option" type="button" onclick="chooseEditImage()">
            <span>✏️</span><span>Edit a photo</span>
        </button>
        <button class="attachment-option" type="button" onclick="chooseDocument()">
            <span>📄</span><span>Add document</span>
        </button>
        <button class="attachment-option" type="button" onclick="takePhoto()">
            <span>📷</span><span>Take a photo</span>
        </button>
    </div>
    <input id="chatImageInput" type="file" accept="image/*" multiple hidden>
    <input id="chatDocumentInput" type="file" accept=".txt,.md,.csv,.json,.pdf,.docx,text/plain,text/markdown,text/csv,application/json,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" multiple hidden>
    <div id="attachmentPreview"></div>
    <div class="composer">
        <button id="imageButton" class="icon-button" type="button" onclick="toggleAttachmentMenu()" aria-label="Add photos and files" title="Add photos and files">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
        </button>
        <textarea id="message" rows="1" placeholder="Message Daxx..." autocomplete="off" enterkeyhint="enter"></textarea>
        <button id="micButton" class="icon-button" type="button" onclick="toggleRecording()" aria-label="Voice conversation" title="Voice">
            <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/></svg>
        </button>
        <button id="sendButton" type="button" onclick="sendMessage()" aria-label="Send message" title="Send">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>
        </button>
    </div>
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
const chatImageInput=document.getElementById("chatImageInput");
const chatDocumentInput=document.getElementById("chatDocumentInput");
const attachmentMenu=document.getElementById("attachmentMenu");
let chatDocumentFiles=[];
const attachmentPreview=document.getElementById("attachmentPreview");
let chatImageFiles=[];
let chatImageMode="analyze";
let forceWebSearch=false;

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
    let normalized=String(text ?? "");

    // Remove provider-specific web-search artifacts before displaying.
    // Groq/search providers can return literal HTML <br> tags and
    // citation markers such as [1†L6-L13].
    normalized=normalized.replace(/<br\s*\/?>(?=\s*)/gi,"\n");
    normalized=normalized.replace(/\[\s*\d+†L\d+(?:-L\d+)?\s*\]/g,"");
    normalized=normalized.replace(/\[\s*\d+†L\d+(?:-\d+)?\s*\]/g,"");

    let safe=escapeHtml(normalized);

    // Markdown links are generated by Dax for verified web references.
    safe=safe.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer" style="color:#8ab4ff;text-decoration:none;">$1</a>'
    );

    safe=safe.replace(/`([^`]+)`/g,"<code>$1</code>");
    safe=safe.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
    safe=safe.replace(/(^|\n)#{1,3}\s+(.+)/g,"$1<strong>$2</strong>");
    safe=safe.replace(/\n/g,"<br>");

    return safe;
}

async function copyText(text){try{await navigator.clipboard.writeText(text);setStatus("Copied.");setTimeout(()=>setStatus(""),1200);}catch(e){window.prompt("Copy this text:",text);}}
async function shareText(text){try{if(navigator.share){await navigator.share({title:"Daxx",text});}else{await copyText(text);}}catch(e){}}
async function regenerateLast(){if(!currentChatId)return;const ms=[...document.querySelectorAll("#chat .message.user")];if(!ms.length)return;const last=ms[ms.length-1].innerText;messageInput.value=last;autoResize();await sendMessage();}
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
        const actions=document.createElement("div"); actions.className="message-actions";
        const edit=document.createElement("button"); edit.className="message-action"; edit.textContent="Edit"; edit.onclick=()=>{messageInput.value=text;autoResize();messageInput.focus();}; actions.appendChild(edit);
        const copy=document.createElement("button"); copy.className="message-action"; copy.textContent="Copy"; copy.onclick=()=>copyText(text); actions.appendChild(copy);
        div.appendChild(actions);
    }else{
        const body=document.createElement("div");
        body.className="message-body";
        body.innerHTML=renderDaxMarkdown(text);
        div.appendChild(body);

        const actions=document.createElement("div"); actions.className="message-actions";
        const makeAction=(label,fn)=>{const b=document.createElement("button");b.type="button";b.className="message-action";b.textContent=label;b.onclick=fn;return b;};
        actions.appendChild(makeAction("Copy",()=>copyText(text)));
        actions.appendChild(makeAction("Read aloud",()=>speak(text)));
        actions.appendChild(makeAction("Share",()=>shareText(text)));
        actions.appendChild(makeAction("Regenerate",()=>regenerateLast()));
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

function toggleVoiceReplies(){
    const on=localStorage.getItem("daxxVoiceReplies")==="1";
    localStorage.setItem("daxxVoiceReplies",on?"0":"1");
    updateVoiceReplyState();
    if(!on) setStatus("Voice replies enabled.");
}
function updateVoiceReplyState(){
    const el=document.getElementById("voiceReplyState");
    if(el) el.textContent=localStorage.getItem("daxxVoiceReplies")==="1"?"On":"Off";
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

function openPopout(kind){
    const panel=document.getElementById("popoutPanel");
    const body=document.getElementById("popoutBody");
    const title=document.getElementById("popoutTitle");
    panel.style.display="block";
    closeHistory();
    body.innerHTML="";

    if(kind==="search") return renderSearchPanel(title,body);
    if(kind==="images") return renderImagesPanel(title,body);
    if(kind==="library") return renderLibraryPanel(title,body);
    if(kind==="projects") return renderProjectsPanel(title,body);
    if(kind==="plugins") return renderPluginsPanel(title,body);
}

function closePopout(){ document.getElementById("popoutPanel").style.display="none"; }

async function renderSearchPanel(title,body){
    title.textContent="Search";
    body.innerHTML='<input id="daxSearch" class="search-box" placeholder="Search your chats, images and projects..." autofocus><div id="searchResults"></div>';
    const input=document.getElementById("daxSearch");
    input.oninput=async()=>{
        const q=input.value.trim(); const box=document.getElementById("searchResults");
        if(!q){box.innerHTML='<div style="opacity:.6">Search your Dax history.</div>';return;}
        const r=await fetch('/search?q='+encodeURIComponent(q)); const d=await r.json();
        box.innerHTML='';
        (d.chats||[]).forEach(c=>{const b=document.createElement('button');b.className='popout-card';b.textContent='💬 '+c.title;b.onclick=()=>{closePopout();openChat(c.id)};box.appendChild(b)});
        (d.images||[]).forEach(i=>{const b=document.createElement('button');b.className='popout-card';b.textContent='🖼️ '+i.title;box.appendChild(b)});
        if(!box.children.length) box.innerHTML='<div style="opacity:.6">No results.</div>';
    };
    input.focus();
}

async function renderImagesPanel(title,body){
    title.textContent="Images";
    body.innerHTML='<div style="opacity:.7;margin-bottom:16px">Images created with Dax appear here.</div><div id="imageGrid" class="popout-grid"></div>';
    const r=await fetch('/images'); const d=await r.json(); const grid=document.getElementById('imageGrid');
    (d.images||[]).forEach(i=>{const card=document.createElement('div');card.className='popout-card';card.innerHTML='<img class="library-img" src="'+i.image_url+'"><div>'+escapeHtml(i.title)+'</div><a href="'+i.image_url+'" download="dax-image.jpg" style="display:block;margin-top:8px;color:#8ab4ff">Save image</a>';grid.appendChild(card)});
    if(!grid.children.length) grid.innerHTML='<div style="opacity:.6">No generated images yet.</div>';
}

async function renderLibraryPanel(title,body){
    title.textContent="Library";
    body.innerHTML='<div style="opacity:.7;margin-bottom:16px">Your saved Dax images and files.</div><div id="libraryGrid" class="popout-grid"></div>';
    const r=await fetch('/images'); const d=await r.json(); const grid=document.getElementById('libraryGrid');
    (d.images||[]).forEach(i=>{const card=document.createElement('div');card.className='popout-card';card.innerHTML='<img class="library-img" src="'+i.image_url+'"><div>'+escapeHtml(i.title)+'</div>';grid.appendChild(card)});
    if(!grid.children.length) grid.innerHTML='<div style="opacity:.6">Library is empty. Generated images will be saved here.</div>';
}

async function renderProjectsPanel(title,body){
    title.textContent="Projects";
    body.innerHTML='<button style="width:100%;height:46px;border-radius:12px;background:#2a6df4;margin-bottom:16px" onclick="createDaxProject()">＋ New project</button><div id="projectGrid" class="popout-grid"></div>';
    const r=await fetch('/projects'); const d=await r.json(); const grid=document.getElementById('projectGrid');
    (d.projects||[]).forEach(p=>{const card=document.createElement('div');card.className='popout-card';card.innerHTML='<div style="font-size:28px">'+p.icon+'</div><strong>'+escapeHtml(p.name)+'</strong><div style="opacity:.6;font-size:12px;margin-top:5px">Project space</div>';grid.appendChild(card)});
    if(!grid.children.length) grid.innerHTML='<div style="opacity:.6">Create a project to keep related chats together.</div>';
}

async function createDaxProject(){
    const name=prompt('Project name'); if(!name) return;
    const r=await fetch('/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    if(!r.ok){alert('Could not create project.');return;}
    renderProjectsPanel(document.getElementById('popoutTitle'),document.getElementById('popoutBody'));
}

async function renderPluginsPanel(title,body){
    title.textContent="Plugins";
    body.innerHTML='<div style="opacity:.7;margin-bottom:16px">Tools available to Dax.</div><div id="pluginGrid" class="popout-grid"></div>';
    const r=await fetch('/plugins'); const d=await r.json(); const grid=document.getElementById('pluginGrid');
    (d.plugins||[]).forEach(p=>{const card=document.createElement('div');card.className='popout-card';card.innerHTML='<div style="font-size:28px">'+p.icon+'</div><strong>'+escapeHtml(p.name)+'</strong><div style="opacity:.65;font-size:13px;margin-top:6px">'+escapeHtml(p.description)+'</div>';grid.appendChild(card)});
}

function toggleWebMode(){
    forceWebSearch=!forceWebSearch;
    document.getElementById("topSearchButton")?.classList.toggle("active",forceWebSearch);
    const note=document.getElementById("webModeNote");
    if(note) note.style.display=forceWebSearch?"inline":"none";
    setStatus(forceWebSearch?"🌐 Web search is on for the next message.":"");
}

function toggleChatMore(){
    const m=document.getElementById("chatMoreMenu");
    m.style.display=m.style.display==="block"?"none":"block";
}
function closeChatMore(){document.getElementById("chatMoreMenu").style.display="none";}

async function shareDaxxApp(){
    closeChatMore();
    const appUrl=window.location.origin + "/";
    const shareText="Try Daxx — your personal AI assistant.";
    try{
        if(navigator.share){
            await navigator.share({title:"Daxx",text:shareText,url:appUrl});
            return;
        }
        if(navigator.clipboard){
            await navigator.clipboard.writeText(appUrl);
            alert("Daxx app link copied. You can paste it into WhatsApp or anywhere you want to share it.");
            return;
        }
        window.prompt("Copy your Daxx app link:",appUrl);
    }catch(e){
        if(e && e.name==="AbortError") return;
        try{
            await navigator.clipboard.writeText(appUrl);
            alert("Daxx app link copied.");
        }catch(err){
            window.prompt("Copy your Daxx app link:",appUrl);
        }
    }
}

async function shareCurrentChat(){
    closeChatMore();
    const title="Dax chat";
    const text=[...document.querySelectorAll("#chat .message")].map(x=>x.innerText).join("\n\n");
    if(navigator.share){ try{ await navigator.share({title,text}); }catch(e){} }
    else { try{ await navigator.clipboard.writeText(text); alert("Chat copied to clipboard."); }catch(e){ alert("Could not share this chat."); } }
}

async function renameCurrentChat(){
    closeChatMore();
    if(!currentChatId) return;
    const title=prompt("Rename chat");
    if(!title || !title.trim()) return;
    const r=await fetch("/conversation/"+currentChatId+"/rename",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:title.trim()})});
    if(!r.ok){alert("Could not rename this chat.");return;}
    await loadHistory();
}

function findInChat(){
    closeChatMore();
    const q=prompt("Find in this chat");
    if(!q) return;
    const nodes=[...document.querySelectorAll("#chat .message")];
    const hit=nodes.find(n=>n.innerText.toLowerCase().includes(q.toLowerCase()));
    if(hit){ hit.scrollIntoView({behavior:"smooth",block:"center"}); hit.style.outline="2px solid #6ea8fe"; setTimeout(()=>hit.style.outline="",1800); }
    else alert("No match found in this chat.");
}

function readLastReply(){
    closeChatMore();
    const replies=[...document.querySelectorAll("#chat .message.alpha")].filter(x=>x.id!=="typingIndicator");
    if(replies.length) speak(replies[replies.length-1].querySelector(".message-body")?.innerText||replies[replies.length-1].innerText);
}

async function deleteCurrentChat(){
    closeChatMore();
    if(!currentChatId || !confirm("Delete this chat? This cannot be undone.")) return;
    const r=await fetch("/conversation/"+currentChatId,{method:"DELETE"});
    if(!r.ok){alert("Could not delete this chat.");return;}
    await newChat();
}

function toggleHistory(){
    document.body.classList.toggle("history-open");
}

function closeHistory(){
    document.body.classList.remove("history-open");
}

let deferredInstallPrompt=null;
window.addEventListener("beforeinstallprompt",e=>{e.preventDefault();deferredInstallPrompt=e;if(!localStorage.getItem("daxxInstallDismissed"))document.getElementById("installBanner").style.display="block";});
async function installDaxx(){const b=document.getElementById("installBanner");if(!deferredInstallPrompt){b.style.display="none";alert("On Android Chrome, open the browser menu and choose Add to Home screen or Install app.");return;}deferredInstallPrompt.prompt();try{await deferredInstallPrompt.userChoice;}catch(e){}deferredInstallPrompt=null;b.style.display="none";}
function dismissInstall(){localStorage.setItem("daxxInstallDismissed","1");document.getElementById("installBanner").style.display="none";}
if("serviceWorker" in navigator)window.addEventListener("load",()=>navigator.serviceWorker.register("/service-worker.js").catch(()=>{}));

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

    const filesForThisTurn=[...chatImageFiles];
    const imageModeForThisTurn=chatImageMode;
    const documentsForThisTurn=[...chatDocumentFiles];
    addMessage(text,"user");

    messageInput.value="";
    chatImageFiles=[];
    chatImageInput.value="";
    chatDocumentFiles=[];
    chatDocumentInput.value="";
    attachmentPreview.innerHTML="";
    attachmentPreview.style.display="none";

    setStatus("");
    sendButton.disabled=true;
    messageInput.disabled=true;
    showTyping();

    try{

        const form=new FormData();
        form.append("message",text);
        form.append("conversation_id",currentChatId||"");
        form.append("force_web",forceWebSearch?"1":"0");
        if(filesForThisTurn.length){
            filesForThisTurn.forEach(f=>form.append("images",f));
            form.append("image_mode",imageModeForThisTurn);
        }
        if(documentsForThisTurn.length){
            documentsForThisTurn.forEach(f=>form.append("documents",f));
        }
        const response=await fetch("/chat",{method:"POST",body:form});

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
        if(localStorage.getItem("daxxVoiceReplies")==="1") speak(data.reply);

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


function toggleAttachmentMenu(){
    attachmentMenu.style.display=attachmentMenu.style.display==="block"?"none":"block";
}

function chooseAnalyzeImage(){
    chatImageMode="analyze";
    attachmentMenu.style.display="none";
    chatImageInput.setAttribute("capture","environment");
    chatImageInput.click();
}

function chooseEditImage(){
    attachmentMenu.style.display="none";
    toggleImagePanel();
}

function takePhoto(){
    chatImageMode="analyze";
    attachmentMenu.style.display="none";
    chatImageInput.setAttribute("capture","environment");
    chatImageInput.click();
}

function chooseDocument(){
    attachmentMenu.style.display="none";
    chatDocumentInput.click();
}
function updateDocumentPreview(){
    chatDocumentFiles=Array.from(chatDocumentInput.files||[]).slice(0,3);
    const names=chatDocumentFiles.map(f=>f.name).join(", ");
    if(chatDocumentFiles.length){
        attachmentPreview.style.display="block";
        attachmentPreview.innerHTML=`<span>${chatDocumentFiles.length} document${chatDocumentFiles.length===1?"":"s"} attached: ${escapeHtml(names)}</span>`;
    }
}
chatDocumentInput.addEventListener("change",updateDocumentPreview);

function updateChatImagePreview(){
    chatImageFiles=Array.from(chatImageInput.files||[]).slice(0,3);
    attachmentPreview.innerHTML="";
    if(!chatImageFiles.length){ attachmentPreview.style.display="none"; return; }
    attachmentPreview.style.display="block";
    const label=document.createElement("span");
    label.textContent=`${chatImageFiles.length} image${chatImageFiles.length===1?"":"s"} attached`;
    attachmentPreview.appendChild(label);
    chatImageFiles.forEach(f=>{
        const img=document.createElement("img"); img.src=URL.createObjectURL(f); attachmentPreview.appendChild(img);
    });
}

chatImageInput.addEventListener("change",updateChatImagePreview);
updateVoiceReplyState();
document.addEventListener("click",e=>{
    if(!attachmentMenu.contains(e.target) && e.target!==document.getElementById("imageButton")){ attachmentMenu.style.display="none"; }
});

function toggleImagePanel(){

    const panel=document.getElementById("imagePanel");

    panel.style.display=
        panel.style.display==="none"
        ?"block"
        :"none";
}


async function editImage(){

    const selectedFiles=Array.from(
        document.getElementById("imageFile").files||[]
    );
    const files=selectedFiles.slice(0,1);

    const prompt=document
        .getElementById("imagePrompt")
        .value
        .trim();

    if(!files.length){
        alert("Select one photo first.");
        return;
    }


    if(!prompt){
        alert(
            "Tell Dax what you want changed in the photo."
        );
        return;
    }

    if(files.length!==1){
        alert("Please select one photo at a time.");
        return;
    }

    if(files[0].size>8*1024*1024){
        alert("Please keep the photo below 8 MB for free editing.");
        return;
    }

    setStatus(
        "🎨 Dax is editing your photo with the free AI image editor..."
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
    download.download="daxx-edited-image.png";
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


messageInput.addEventListener("keydown",e=>{
    // Enter/Return always creates a new line, matching a multiline composer.
    // Ctrl+Enter (or Cmd+Enter) is the optional keyboard shortcut to send.
    if(e.key==="Enter" && (e.ctrlKey || e.metaKey)){
        e.preventDefault();
        if(!sendButton.disabled) sendMessage();
    }
});

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

    try:
        chat_id_int = int(chat_id) if chat_id else None
    except (TypeError, ValueError):
        chat_id_int = None
    if not chat_id_int or not conversation_belongs_to_user(chat_id_int, user_id):
        chat_id_int = create_conversation(user_id)
    chat_id = str(chat_id_int)

    response = make_response(render_template_string(HTML))
    response.set_cookie("alpha_chat_id", chat_id, max_age=60*60*24*365, httponly=True, samesite="Lax")
    return response



@app.route("/auth/google")
def google_login():
    if get_current_user_id():
        return redirect("/")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return redirect("/login?google_error=Google+login+is+not+configured+yet.")
    state = uuid.uuid4().hex
    session["google_oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    from urllib.parse import urlencode
    return redirect(GOOGLE_AUTH_URL + "?" + urlencode(params))


@app.route("/auth/google/callback")
def google_callback():
    if request.args.get("error"):
        detail = request.args.get("error_description") or request.args.get("error")
        from urllib.parse import quote_plus
        return redirect("/login?google_error=" + quote_plus("Google sign-in was not completed: " + detail))
    state = request.args.get("state", "")
    expected = session.pop("google_oauth_state", "")
    if not state or not expected or state != expected:
        return redirect("/login?google_error=Google+sign-in+expired.+Please+try+again.")
    code = request.args.get("code", "")
    if not code or not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return redirect("/login?google_error=Google+login+is+not+configured+correctly.")
    try:
        token_r = requests.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": _google_redirect_uri(),
            "grant_type": "authorization_code",
        }, timeout=20)

        if not token_r.ok:
            try:
                token_error = token_r.json()
                token_error_code = token_error.get("error", "unknown_error")
                token_error_description = token_error.get("error_description", "")
            except Exception:
                token_error_code = f"HTTP {token_r.status_code}"
                token_error_description = token_r.text[:300]

            if token_r.status_code == 401 or token_error_code == "invalid_client":
                raise ValueError(
                    "Google rejected the OAuth Client ID/Client Secret. "
                    "Check GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Render and make sure they belong to the same Google Web application client."
                )
            if token_error_code in ("invalid_grant", "redirect_uri_mismatch"):
                raise ValueError(
                    "Google rejected the OAuth callback. Make sure GOOGLE_REDIRECT_URI exactly matches the Authorized redirect URI in Google Cloud Console: "
                    + _google_redirect_uri()
                )
            raise ValueError(
                f"Google OAuth token exchange failed ({token_error_code}): {token_error_description or token_r.text[:300]}"
            )

        access_token = token_r.json().get("access_token")
        if not access_token:
            raise ValueError("Google did not return an access token.")
        user_r = requests.get(GOOGLE_USERINFO_URL, headers={"Authorization": "Bearer " + access_token}, timeout=20)
        user_r.raise_for_status()
        profile = user_r.json()
        google_sub = str(profile.get("sub", "")).strip()
        email = str(profile.get("email", "")).strip().lower()
        verified = profile.get("email_verified") is True
        if not google_sub or not email or not verified:
            raise ValueError("Google did not provide a verified email address.")

        conn = db_connect()
        row = conn.execute("SELECT id FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
        if row:
            user_id = row[0]
        else:
            row = conn.execute("SELECT id, google_sub FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                user_id = row[0]
                if not row[1]:
                    conn.execute("UPDATE users SET google_sub = ? WHERE id = ?", (google_sub, user_id))
            else:
                cur = conn.execute("INSERT INTO users (email, password_hash, google_sub) VALUES (?, ?, ?)", (email, generate_password_hash(uuid.uuid4().hex), google_sub))
                user_id = cur.lastrowid
        conn.commit()
        conn.close()
        session.clear()
        session["user_id"] = user_id
        return redirect("/")
    except Exception as exc:
        app.logger.exception("Google OAuth callback failed")
        from urllib.parse import quote_plus
        detail = str(exc).strip() or "Unknown Google OAuth error"
        return redirect("/login?google_error=" + quote_plus("Google sign-in failed: " + detail[:240]))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if get_current_user_id():
            return redirect("/")
        return render_template_string(LOGIN_HTML)

    data = request.get_json(silent=True) or request.form.to_dict()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not email or "@" not in email or len(email) > 320:
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
    data = request.get_json(silent=True) or request.form.to_dict()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not email or "@" not in email or len(email) > 320:
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if len(password) > 256:
        return jsonify({"error": "Password must be 256 characters or fewer."}), 400

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

    try:
        chat_id_int = int(chat_id) if chat_id else None
    except (TypeError, ValueError):
        chat_id_int = None

    if not chat_id_int or not conversation_belongs_to_user(chat_id_int, user_id):
        chat_id_int = create_conversation(user_id)

    response = jsonify({
        "id": chat_id_int
    })
    response.set_cookie(
        "alpha_chat_id",
        str(chat_id_int),
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


def attachments_api_payload(rows):
    return [{"message_id":r[0],"kind":r[1],"filename":r[2],"mime_type":r[3],"data_url":r[4],"text_content":r[5]} for r in rows]


@app.route("/history/<int:conversation_id>")
@login_required_api
def history_chat(conversation_id):

    user_id = get_current_user_id()

    rows=get_messages(conversation_id, user_id)
    attachments = get_message_attachments(conversation_id, user_id)
    by_message = {}
    for mid, kind, filename, mime, data_url, text_content in attachments:
        by_message.setdefault(mid, []).append({"kind":kind,"filename":filename,"mime_type":mime,"data_url":data_url,"text_content":text_content})
    # get_messages intentionally stays compatible with the older 3-column shape;
    # attachment display is also exposed through a lightweight separate endpoint.
    return jsonify({
        "messages":[{"role":r[0],"content":r[1],"created_at":r[2]} for r in rows],
        "attachments":attachments_api_payload(attachments)
    })


@app.route("/conversation/<int:conversation_id>/rename", methods=["POST"])
@login_required_api
def rename_conversation_api(conversation_id):
    user_id=get_current_user_id()
    data=request.get_json(silent=True) or {}
    title=str(data.get("title","")).strip()[:120]
    if not title: return jsonify({"error":"Enter a chat name."}),400
    conn=db_connect()
    row=conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?",(conversation_id,user_id)).fetchone()
    if not row:
        conn.close(); return jsonify({"error":"Chat not found."}),404
    conn.execute("UPDATE conversations SET title=? WHERE id=? AND user_id=?",(title,conversation_id,user_id)); conn.commit(); conn.close()
    return jsonify({"success":True,"title":title})

@app.route("/conversation/<int:conversation_id>", methods=["DELETE"])
@login_required_api
def delete_conversation_api(conversation_id):
    user_id=get_current_user_id()
    conn=db_connect()
    row=conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?",(conversation_id,user_id)).fetchone()
    if not row:
        conn.close(); return jsonify({"error":"Chat not found."}),404
    conn.execute("DELETE FROM dax_attachments WHERE conversation_id=? AND user_id=?", (conversation_id, user_id))
    conn.execute("DELETE FROM dax_project_chats WHERE conversation_id=?", (conversation_id,))
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    conn.execute("DELETE FROM conversations WHERE id=? AND user_id=?",(conversation_id,user_id))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/search")
@login_required_api
def search_content():
    q = str(request.args.get("q", "")).strip()
    if not q:
        return jsonify({"chats": [], "images": []})
    chats, images = search_user_content(get_current_user_id(), q)
    return jsonify({
        "chats": [{"id": r[0], "title": r[1], "created_at": r[2]} for r in chats],
        "images": [{"id": r[0], "title": r[1], "created_at": r[2]} for r in images]
    })


@app.route("/images")
@login_required_api
def images_library():
    rows = get_dax_images(get_current_user_id())
    return jsonify({"images": [{"id":r[0], "conversation_id":r[1], "image_url":r[2], "title":r[3], "created_at":r[4]} for r in rows]})


@app.route("/projects", methods=["GET", "POST"])
@login_required_api
def projects_api():
    user_id = get_current_user_id()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return jsonify({"error":"Enter a project name."}), 400
        pid = create_project(user_id, name, str(data.get("icon", "📁")))
        return jsonify({"id":pid, "name":name, "icon":str(data.get("icon", "📁"))})
    rows = get_projects(user_id)
    return jsonify({"projects":[{"id":r[0],"name":r[1],"icon":r[2],"created_at":r[3]} for r in rows]})


@app.route("/projects/<int:project_id>/chats", methods=["GET", "POST"])
@login_required_api
def project_chats_api(project_id):
    user_id=get_current_user_id()
    conn=db_connect()
    project=conn.execute("SELECT id FROM dax_projects WHERE id=? AND user_id=?",(project_id,str(user_id))).fetchone()
    if not project:
        conn.close(); return jsonify({"error":"Project not found."}),404
    if request.method=="POST":
        data=request.get_json(silent=True) or {}
        cid=data.get("conversation_id")
        try: cid=int(cid)
        except Exception: conn.close(); return jsonify({"error":"Invalid chat id."}),400
        ok=conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?",(cid,user_id)).fetchone()
        if not ok: conn.close(); return jsonify({"error":"Chat not found."}),404
        conn.execute("INSERT OR IGNORE INTO dax_project_chats(project_id,conversation_id) VALUES(?,?)",(project_id,cid)); conn.commit()
    rows=conn.execute("""SELECT c.id,c.title,c.created_at FROM conversations c JOIN dax_project_chats pc ON pc.conversation_id=c.id WHERE pc.project_id=? AND c.user_id=? ORDER BY c.id DESC""",(project_id,user_id)).fetchall()
    conn.close()
    return jsonify({"chats":[{"id":r[0],"title":r[1],"created_at":r[2]} for r in rows]})


@app.route("/plugins")
@login_required_api
def plugins_api():
    return jsonify({"plugins":[
        {"id":"web-search","name":"Web Search","icon":"🌐","description":"Search the web and return source links."},
        {"id":"image-editor","name":"Image Editor","icon":"🖼️","description":"Edit and transform uploaded photos."},
        {"id":"voice","name":"Voice","icon":"🎙️","description":"Record a message and transcribe it."}
    ]})


@app.route("/manifest.webmanifest")
def pwa_manifest():
    return jsonify({
        "name": "Daxx", "short_name": "Daxx", "description": "Daxx personal AI assistant",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#111418", "theme_color": "#111418", "orientation": "portrait-primary",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    })

@app.route("/icon-192.png")
def pwa_icon_192():
    return send_file(os.path.join(os.path.dirname(__file__), "icon-192.png"), mimetype="image/png", max_age=31536000)

@app.route("/icon-512.png")
def pwa_icon_512():
    return send_file(os.path.join(os.path.dirname(__file__), "icon-512.png"), mimetype="image/png", max_age=31536000)

@app.route("/app-icon.svg")
def pwa_icon():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#111418"/><circle cx="256" cy="256" r="170" fill="#2f6fed"/><path d="M155 181h170c35 0 63 28 63 63v25c0 35-28 63-63 63h-42l-46 48v-48h-82c-35 0-63-28-63-63v-25c0-35 28-63 63-63z" fill="white"/><circle cx="205" cy="256" r="15" fill="#2f6fed"/><circle cx="256" cy="256" r="15" fill="#2f6fed"/><circle cx="307" cy="256" r="15" fill="#2f6fed"/></svg>'''
    return Response(svg, mimetype="image/svg+xml")

@app.route("/service-worker.js")
def service_worker():
    js = '''const CACHE="daxx-shell-v1";self.addEventListener("install",e=>self.skipWaiting());self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));self.addEventListener("fetch",e=>{if(e.request.method!=="GET")return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));});'''
    return Response(js, mimetype="application/javascript")

@app.route("/live_time")
@login_required_api
def live_time():
    now = datetime.now(ZoneInfo("Africa/Lagos"))
    return jsonify({"iso": now.isoformat(), "time": now.strftime("%I:%M:%S %p"), "date": now.strftime("%A, %d %B %Y"), "timezone":"Africa/Lagos"})


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

    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict()
    user_message = str(data.get("message", "")).strip()
    chat_images = [f for f in request.files.getlist("images") if f and f.filename]
    chat_documents = [f for f in request.files.getlist("documents") if f and f.filename]
    image_mode = str(data.get("image_mode", "analyze"))
    if len(chat_images) > 3:
        return jsonify({"error":"You can attach up to 3 images for analysis."}), 400

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

    user_message_id = save_message(conversation_id, "user", user_message)

    # Persist uploaded images/documents so they remain visible in the conversation.
    persisted_image_parts = []
    for f in chat_images[:3]:
        raw = f.read()
        if not raw: continue
        mime = f.mimetype or "image/jpeg"
        if mime not in ("image/jpeg","image/png","image/gif","image/webp"): continue
        encoded = base64.b64encode(raw).decode("utf-8")
        data_url = f"data:{mime};base64,{encoded}"
        save_attachment(user_id, conversation_id, user_message_id, "image", f.filename or "image", mime, data_url=data_url)
        persisted_image_parts.append({"type":"image_url","image_url":{"url":data_url}})
    document_context = []
    for f in chat_documents[:3]:
        try:
            extracted = extract_document_text(f)
            save_attachment(user_id, conversation_id, user_message_id, "document", f.filename or "document", f.mimetype or "application/octet-stream", text_content=extracted)
            document_context.append(f"DOCUMENT: {f.filename}\n{extracted}")
        except ValueError as exc:
            return jsonify({"error":str(exc)}),400

    rows = get_messages(conversation_id, user_id)
    memories = get_memories(user_id)

    system_prompt = """
You are Dax, a helpful personal AI assistant.

Be friendly, clear, practical and honest.
Help the user with normal questions, coding, writing, ideas, planning,
learning and creative tasks.

Do not claim that you completed an action that you did not actually complete.
Keep answers reasonably concise unless the user asks for detail.

When browser search is available, use it for live Internet information such as
latest news, current events, current prices, sports, weather, exchange rates,
product availability, public web pages, and other information that can change.
For exact current time in Nigeria, the application can provide a live Africa/Lagos
clock. Never claim a live result without actually using the available live source.
Include useful source references when the provider supplies them. Never invent sources or URLs.

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

    # Current-turn image/document understanding. Persisted image data also lets
    # later turns in the same conversation keep access to earlier images.
    current_user_content = user_message
    if persisted_image_parts or document_context:
        if persisted_image_parts and image_mode == "analyze":
            current_user_content = (
                "Analyze the attached image(s) carefully. Read visible text/OCR, "
                "inspect the UI or scene, identify errors and explain concrete "
                "fixes when the user asks for troubleshooting. Do not pretend "
                "you can see anything that is not visible.\n\n" + user_message
            )
        if document_context:
            current_user_content += "\n\n" + "\n\n".join(document_context)
        parts = [{"type":"text","text":current_user_content}] + persisted_image_parts
        messages[-1] = {"role":"user","content":parts}

    force_web = str(request.form.get("force_web", "0")).lower() in ("1","true","yes","on") if request.form else False

    web_keywords = (
        "search the web", "search online", "look this up", "look it up",
        "find online", "latest", "current", "today", "news", "recent",
        "source", "sources", "reference", "references", "according to",
        "what happened", "right now", "this week", "this month",
        "weather", "temperature", "forecast", "rain", "exchange rate", "dollar",
        "naira", "price", "cost", "stock", "shares", "bitcoin", "crypto",
        "sports", "score", "match", "game", "traffic", "opening hours",
        "available now", "live", "internet", "online", "website", "who is"
    )
    use_web = force_web or any(k in lower for k in web_keywords)
    time_words = ("what time is it", "current time", "live clock", "time now", "what's the time", "whats the time")
    if chat_images:
        use_web = False
    if any(k in lower for k in time_words):
        now = datetime.now(ZoneInfo("Africa/Lagos"))
        messages[0]["content"] += f"\n\nLIVE CLOCK (Africa/Lagos): {now.strftime('%A, %d %B %Y, %I:%M:%S %p')} WAT. Use this exact current time when answering."
        use_web = False

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
            "model": GROQ_VISION_MODEL if chat_images else GROQ_CHAT_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_completion_tokens": 4096
        }

        if use_web and not chat_images:
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
                        "model": GROQ_VISION_MODEL if chat_images else GROQ_CHAT_MODEL,
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

    # Normalize provider-specific search artifacts before saving. This also
    # prevents them from returning when the conversation is loaded later.
    import re
    reply = re.sub(r"<br\s*/?>", "\n", reply, flags=re.I)
    reply = re.sub(r"\[\s*\d+†L\d+(?:-L\d+)?\s*\]", "", reply)
    reply = re.sub(r"\[\s*\d+†L\d+(?:-\d+)?\s*\]", "", reply)

    if not reply:
        return jsonify({
            "error": "Dax received an empty response."
        }), 502

    # Groq browser_search returns executed_tools on the assistant message.
    # Keep the verified URLs so Dax can show a real Sources section.
    if use_web and result_provider == "groq":
        sources = []
        executed_tools = (message_obj.get("executed_tools") or
                          result_data.get("executed_tools") or [])

        def collect_sources(value):
            if isinstance(value, dict):
                # Search result objects are normally {title, url, ...}.
                url = value.get("url") or value.get("link")
                title = value.get("title") or value.get("name") or url
                if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                    pair = (str(title), url)
                    if pair not in sources:
                        sources.append(pair)
                for child in value.values():
                    collect_sources(child)
            elif isinstance(value, list):
                for child in value:
                    collect_sources(child)

        collect_sources(executed_tools)

        # Some Groq responses include citation URLs directly in the final
        # message. Preserve those as sources as well.
        import re
        for url in re.findall(r'https?://[^\s)\]<>]+', reply):
            clean = url.rstrip('.,;')
            if clean and not any(u == clean for _, u in sources):
                sources.append((clean, clean))

        if sources:
            reply += "\n\n**Sources**\n" + "\n".join(
                f"- [{title}]({url})"
                for title, url in sources[:8]
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


def _resize_image_for_flux(raw: bytes, max_side: int = 511, quality: int = 95) -> bytes:
    """Convert an uploaded image to JPEG and keep both dimensions < 512px."""
    try:
        source = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"Invalid image: {exc}") from exc

    source.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    source.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def _make_identity_reference(raw: bytes) -> bytes:
    """Create an optional square portrait-focused reference from one upload."""
    try:
        source = Image.open(io.BytesIO(raw)).convert("RGB")
        width, height = source.size
        crop_size = max(1, int(min(width, height) * 0.78))
        center_x = width / 2.0
        center_y = height * 0.42
        left = int(max(0, min(width - crop_size, center_x - crop_size / 2)))
        top = int(max(0, min(height - crop_size, center_y - crop_size / 2)))
        crop = source.crop((left, top, left + crop_size, top + crop_size))
        crop.thumbnail((511, 511), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        crop.save(output, format="JPEG", quality=97, optimize=True)
        return output.getvalue()
    except Exception as exc:
        raise ValueError(f"Could not create identity reference: {exc}") from exc



def _build_image_edit_prompt(user_prompt, single_photo):
    base = user_prompt.strip()
    if single_photo:
        return ("Edit the supplied photograph according to the user's request. "
                "Preserve the person's identity, facial structure, natural skin texture, pose, clothing, "
                "composition and background unless the user explicitly asks to change them. "
                "Make only the requested changes. Produce one coherent final photograph.\n\nUSER REQUEST: " + base)
    return ("Edit the supplied reference photos according to the user's request. "
            "Use the images as references and follow the requested composition carefully. "
            "Do not duplicate subjects or create a collage unless explicitly requested. "
            "Produce one coherent final image.\n\nUSER REQUEST: " + base)


def _image_data_url(raw: bytes) -> str:
    """Normalize an uploaded image for OpenAI-compatible image-to-image APIs."""
    source = Image.open(io.BytesIO(raw)).convert("RGB")
    source.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    source.save(buf, format="JPEG", quality=94, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _save_edited_image(user_id, conversation_id, image_bytes, provider):
    out = io.BytesIO()
    Image.open(io.BytesIO(image_bytes)).convert("RGB").save(out, format="JPEG", quality=92, optimize=True)
    final_data_url = "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    image_id = save_dax_image(user_id, conversation_id, final_data_url, "Dax edited image")
    return final_data_url, image_id, provider


def _pollinations_edit(raw: bytes, prompt: str):
    if not POLLINATIONS_API_KEY:
        raise RuntimeError("POLLINATIONS_API_KEY is not configured")
    last_error = None
    for model in POLLINATIONS_EDIT_MODELS:
        try:
            image_file = io.BytesIO(raw)
            image_file.name = "dax_photo.jpg"
            files = {"image": ("dax_photo.jpg", image_file, "image/jpeg")}
            data = {
                "prompt": _build_image_edit_prompt(prompt, True),
                "model": model,
                "size": "1024x1024",
            }
            r = requests.post(
                "https://gen.pollinations.ai/v1/images/edits",
                headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"},
                files=files,
                data=data,
                timeout=180,
            )
                        if not r.ok:
                raise RuntimeError(f"Pollinations {model}: HTTP {r.status_code}: {r.text[:500]}")
