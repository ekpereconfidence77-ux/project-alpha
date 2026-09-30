import os
import uuid
import sqlite3
import requests
from flask import Flask, request, jsonify, make_response, render_template_string

app = Flask(__name__)

DB_FILE = "alpha_memory.db"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            memory TEXT NOT NULL
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


init_db()


HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Project Alpha</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #0f1115;
            color: white;
        }
        .app {
            max-width: 700px;
            margin: 0 auto;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        header {
            padding: 18px;
            text-align: center;
            font-size: 24px;
            font-weight: bold;
            border-bottom: 1px solid #292d36;
        }
        #chat {
            flex: 1;
            padding: 16px;
            overflow-y: auto;
        }
        .message {
            padding: 12px 14px;
            margin: 10px 0;
            border-radius: 14px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .user {
            background: #2b6cff;
            margin-left: 20%;
        }
        .alpha {
            background: #20242c;
            margin-right: 10%;
        }
        .composer {
            display: flex;
            gap: 8px;
            padding: 12px;
            border-top: 1px solid #292d36;
            background: #15181e;
            position: sticky;
            bottom: 0;
        }
        input {
            flex: 1;
            min-width: 0;
            padding: 14px;
            border: 1px solid #3a404c;
            border-radius: 12px;
            background: #0f1115;
            color: white;
            font-size: 16px;
            outline: none;
        }
        button {
            border: 0;
            border-radius: 12px;
            padding: 0 15px;
            font-size: 18px;
            cursor: pointer;
        }
        #micButton { background: #303641; color: white; }
        #sendButton { background: #2b6cff; color: white; }
        #micButton.listening { background: #d22; }
    </style>
</head>
<body>
<div class="app">
    <header>🤖 Project Alpha</header>

    <div id="chat">
        <div class="message alpha">Hello 👋 I'm Alpha. How can I help you?</div>
    </div>

    <div class="composer">
        <button id="micButton" onclick="startVoice()">🎤</button>
        <input id="message" placeholder="Talk to Alpha..." autocomplete="off">
        <button id="sendButton" onclick="sendMessage()">➤</button>
    </div>
</div>

<script>
let recognition = null;

function speak(text) {
    if (!('speechSynthesis' in window)) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'en-US';
    utterance.rate = 1;
    window.speechSynthesis.speak(utterance);
}

function startVoice() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const micButton = document.getElementById('micButton');
    const input = document.getElementById('message');

    if (!SpeechRecognition) {
        alert('Speech recognition is not supported by this browser. Please use the latest Google Chrome.');
        return;
    }

    if (recognition) {
        recognition.stop();
        recognition = null;
        return;
    }

    recognition = new SpeechRecognition();
    recognition.lang = 'en-NG';
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.maxAlternatives = 1;

    recognition.onstart = function() {
        micButton.classList.add('listening');
        micButton.textContent = '🔴';
        input.placeholder = 'Listening... speak now';
        input.value = '';
    };

    recognition.onresult = function(event) {
        let transcript = '';
        for (let i = event.resultIndex; i < event.results.length; i++) {
            transcript += event.results[i][0].transcript;
        }
        transcript = transcript.trim();
        if (transcript) input.value = transcript;

        const lastResult = event.results[event.results.length - 1];
        if (lastResult && lastResult.isFinal && transcript) {
            setTimeout(function() { sendMessage(); }, 300);
        }
    };

    recognition.onerror = function(event) {
        console.log('Speech recognition error:', event.error);
        micButton.classList.remove('listening');
        micButton.textContent = '🎤';
        input.placeholder = 'Talk to Alpha...';

        let message = 'I could not hear you.';
        if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
            message = 'Microphone permission was blocked. Allow microphone access for Chrome.';
        } else if (event.error === 'no-speech') {
            message = 'I did not detect speech. Tap 🎤 and speak clearly.';
        } else if (event.error === 'audio-capture') {
            message = 'I cannot access the microphone. Check that another app is not using it.';
        } else if (event.error === 'network') {
            message = 'Speech recognition needs an internet connection. Check your internet.';
        }
        alert(message);
        recognition = null;
    };

    recognition.onend = function() {
        micButton.classList.remove('listening');
        micButton.textContent = '🎤';
        input.placeholder = 'Talk to Alpha...';
        recognition = null;
    };

    try {
        recognition.start();
    } catch (error) {
        console.log('Could not start speech recognition:', error);
        micButton.classList.remove('listening');
        micButton.textContent = '🎤';
        input.placeholder = 'Talk to Alpha...';
        recognition = null;
    }
}

async function sendMessage() {
    const input = document.getElementById('message');
    const chat = document.getElementById('chat');
    const message = input.value.trim();
    if (!message) return;

    chat.innerHTML += '<div class="message user">' + escapeHtml(message) + '</div>';
    input.value = '';

    const thinking = document.createElement('div');
    thinking.className = 'message alpha';
    thinking.textContent = 'Thinking...';
    chat.appendChild(thinking);

    try {
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: message})
        });
        const data = await response.json();
        if (data.reply) {
            thinking.textContent = data.reply;
            speak(data.reply);
        } else {
            thinking.textContent = data.error || 'Something went wrong.';
        }
    } catch (error) {
        thinking.textContent = 'Connection error. Please try again.';
        console.error(error);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

document.getElementById('message').addEventListener('keydown', function(event) {
    if (event.key === 'Enter') sendMessage();
});
</script>
</body>
</html>
"""


@app.route("/")
def home():
    user_id = request.cookies.get("alpha_user_id")

    if not user_id:
        user_id = str(uuid.uuid4())

    response = make_response(render_template_string(HTML))
    response.set_cookie(
        "alpha_user_id",
        user_id,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax"
    )
    return response


@app.route("/chat", methods=["POST"])
def chat():
    api_key = os.environ.get("OPENROUTER_API_KEY")

    if not api_key:
        return jsonify({"error": "OPENROUTER_API_KEY is not configured on the server."}), 500

    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()

    if not message:
        return jsonify({"error": "Please enter a message."}), 400

    user_id = request.cookies.get("alpha_user_id")
    if not user_id:
        user_id = str(uuid.uuid4())

    lower_message = message.lower()

    if lower_message.startswith("remember that "):
        memory = message[len("remember that "):].strip()
        if memory:
            save_memory(user_id, memory)
            return jsonify({"reply": "Got it. I'll remember that for this browser."})

    memories = get_memories(user_id)
    memory_text = "\n".join("- " + item for item in memories)

    system_prompt = """You are Alpha, a friendly personal AI assistant.
You are used by multiple people.
Each person has separate private memories.
Never reveal one person's memories to another person.
Be helpful, natural, and concise.
"""

    if memory_text:
        system_prompt += "\nMemories for this user:\n" + memory_text

    payload = {
        "model": "openrouter/free",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message}
        ]
    }

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=60
        )

        if response.status_code != 200:
            return jsonify({
                "error": f"AI service error ({response.status_code})."
            }), 502

        result = response.json()
        reply = result["choices"][0]["message"]["content"]

        return jsonify({"reply": reply})

    except Exception as error:
        print("Chat error:", error)
        return jsonify({"error": "Alpha could not connect to the AI service."}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
